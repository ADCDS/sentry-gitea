"""Stack-trace links, source URL parsing and email-only CODEOWNERS for Gitea repositories."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from rest_framework.exceptions import ValidationError
from sentry.integrations.source_code_management.repository import RepositoryIntegration
from sentry.issues.ownership.grammar import get_codeowners_path_and_owners
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import ApiError, IntegrationError

from sentry_gitea.api.client import is_sha, normalize_repo

from .commits import CODEOWNERS_LOCATIONS
from .identity import OAuthSettings

SOURCE_KINDS = ("branch", "commit", "tag")
# Branch and tag names may contain slashes; at most this many leading segments are tried.
MAX_REF_SEGMENTS = 10
HANDLE_ERROR = "Gitea CODEOWNERS supports email owners only; found @handles: "


def _segments(value: str) -> list[str]:
    """Path segments with '.', '..' and empty parts resolved, never rising above the root."""
    parts: list[str] = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return parts


def _encode(parts: list[str]) -> str:
    return "/".join(quote(part, safe="") for part in parts)


def repository_url(path: str) -> str:
    """Browser URL of a repository on the configured public Gitea URL (never `html_url`)."""
    return OAuthSettings.load().public_url + "/" + _encode(normalize_repo(path).split("/"))


def repository_link_path(repo: Any) -> str:
    """The stored owner/name; webhooks and Add Repository keep it current without HTTP."""
    return normalize_repo((repo.config or {}).get("path") or repo.name)


class GiteaSource(RepositoryIntegration):
    integration_name = "gitea"
    codeowners_locations = list(CODEOWNERS_LOCATIONS)

    # URL formatting and parsing.

    def format_source_url(self, repo: Any, filepath: str, branch: str | None) -> str:
        ref = branch or (repo.config or {}).get("default_branch") or "main"
        ref_parts = _segments(ref) or ["main"]
        kind = "commit" if len(ref_parts) == 1 and is_sha(ref_parts[0].lower()) else "branch"
        return (
            f"{repository_url(repository_link_path(repo))}/src/{kind}/"
            f"{_encode(ref_parts)}/{_encode(_segments(filepath))}"
        )

    def _public_parts(self, url: str) -> list[str] | None:
        """Decoded path segments after the public base path, or None for another location."""
        try:
            parsed = urlsplit(url)
            base = urlsplit(OAuthSettings.load().public_url)
        except ValueError:
            return None
        if parsed.scheme != base.scheme or parsed.netloc.lower() != base.netloc.lower():
            return None
        prefix = base.path.rstrip("/") + "/"
        if not parsed.path.startswith(prefix):
            return None
        parts = [unquote(part) for part in parsed.path[len(prefix) :].split("/")]
        if any(part in {".", ".."} or "/" in part for part in parts):
            return None
        return parts

    def source_url_matches(self, url: str) -> bool:
        parts = self._public_parts(url) if isinstance(url, str) else None
        if not parts or len(parts) < 6 or parts[2] != "src" or parts[3] not in SOURCE_KINDS:
            return False
        try:
            path = normalize_repo("/".join(parts[:2]))
        except ValueError:
            return False
        if parts[0].casefold() == str(self.model.metadata.get("owner_login", "")).casefold():
            return True
        # The owner may have been renamed since installation; trust recorded repositories.
        return any(
            name.casefold() == path.casefold()
            for name in Repository.objects.filter(
                organization_id=self.organization_id, integration_id=self.model.id
            ).values_list("name", flat=True)
        )

    def _split_source_url(self, repo: Any, url: str) -> tuple[str, str]:
        cache = self.__dict__.setdefault("_gitea_source_urls", {})
        if (repo.id, url) in cache:
            return cache[(repo.id, url)]
        parts = self._public_parts(url)
        path = repository_link_path(repo)
        if (
            not parts
            or len(parts) < 6
            or "/".join(parts[:2]).casefold() != path.casefold()
            or parts[2] != "src"
            or parts[3] not in SOURCE_KINDS
        ):
            raise ValidationError("The source URL does not belong to this Gitea repository.")
        kind, rest = parts[3], parts[4:]
        if kind == "commit":
            if not is_sha(rest[0].lower()):
                raise ValidationError("The source URL has an invalid commit.")
            result = (rest[0], "/".join(rest[1:]))
        else:
            result = self._resolve_ref(repo, kind, rest)
        if not result[1]:
            raise ValidationError("The source URL does not name a file.")
        cache[(repo.id, url)] = result
        return result

    def _resolve_ref(self, repo: Any, kind: str, rest: list[str]) -> tuple[str, str]:
        """Find the branch/tag prefix: Git forbids both `a` and `a/b`, so the first hit wins."""
        try:
            client = self.get_client()
            for size in range(1, min(len(rest), MAX_REF_SEGMENTS + 1)):
                name = "/".join(rest[:size])
                if client.get_ref(repo, kind, name) is not None:
                    return name, "/".join(rest[size:])
        except (ApiError, IntegrationError):
            pass
        # Unresolvable (deleted ref or Gitea unavailable): assume a slash-free name.
        return rest[0], "/".join(rest[1:])

    def extract_branch_from_source_url(self, repo: Any, url: str) -> str:
        return self._split_source_url(repo, url)[0]

    def extract_source_path_from_source_url(self, repo: Any, url: str) -> str:
        return self._split_source_url(repo, url)[1]

    # Repository metadata.

    def has_repo_access(self, repo: Any) -> bool:
        try:
            self.get_client().repository_path(repo)
        except (ApiError, IntegrationError):
            return False
        return True

    def get_repository_default_branch(self, repo: Any) -> str | None:
        client = self.get_client()
        stable = str((repo.config or {}).get("repo_id") or repo.external_id or "")
        try:
            data = (
                client.get_scm_repo(repo_id=int(stable))
                if stable.isdigit()
                else client.get_scm_repo(path=repository_link_path(repo))
            )
        except (ApiError, IntegrationError, ValueError):
            return (repo.config or {}).get("default_branch")
        branch = data.get("default_branch")
        return branch if isinstance(branch, str) and branch else None

    # CODEOWNERS.

    def get_codeowner_file(self, repo: Any, ref: str | None = None) -> Mapping[str, str] | None:
        result = super().get_codeowner_file(repo, ref)
        if result:
            handles = codeowners_handles(result["raw"])
            if handles:
                # Core resolves @names organization-wide against other providers' external
                # actors; refuse them instead of risking a match with an unrelated account.
                raise ApiError(HANDLE_ERROR + ", ".join(handles[:5]), code=400)
        return result


def codeowners_handles(raw: str) -> list[str]:
    """Distinct `@user`/`@org/team` owners, in file order; comments are ignored."""
    handles: dict[str, None] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        _, owners = get_codeowners_path_and_owners(line)
        handles.update((owner, None) for owner in owners if owner.startswith("@"))
    return list(handles)
