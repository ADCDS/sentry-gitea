"""Legacy issue adapter and the link/locking helpers shared with alert delivery.

Sentry's base views own manual link persistence. Their entire mutation is wrapped
below; lower-level hooks deliberately do not acquire the non-reentrant group lock.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import requests
from django.conf import settings
from django.db import router, transaction
from rest_framework.response import Response
from sentry import options
from sentry.exceptions import PluginError, RestrictedIPAddress
from sentry.http import build_session
from sentry.locks import locks
from sentry.models.groupmeta import GroupMeta
from sentry.models.options.project_option import ProjectOption
from sentry.plugins.bases.issue2 import IssuePlugin2
from sentry.utils.http import absolute_uri
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea import __version__
from sentry_gitea.api.client import (
    OPERATION_BUDGET,
    REQUEST_ALLOWANCE,
    TIMEOUT,
    GiteaClient,
    browser_issue_url,
    issue_number,
    normalize_base_url,
    normalize_repo,
    url_origin,
)
from sentry_gitea.api.errors import GiteaApiError
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.render import (
    EventData,
    clean_text,
    culprit_to_paths,
    ensure_metadata,
    event_is_handled,
    normalize_release,
    parse_label_names,
    render_body,
    render_fix_hint,
    render_title,
    resolve_release,
)

if TYPE_CHECKING:
    from rest_framework.request import Request
    from sentry.models.group import Group
    from sentry.models.project import Project

logger = logging.getLogger(__name__)
_deadline: ContextVar[float | None] = ContextVar("sentry_gitea_deadline", default=None)
LOCK_RETRY_DELAY = 0.1
BLOCKED_ADDRESS = (
    "Sentry refused to connect: the Gitea URL resolves to a private or reserved address. "
    "A Sentry administrator can allow its origin with GITEA_PLUGIN_ALLOWED_BASE_URLS."
)
TOKEN_REENTRY = (
    "Re-enter the personal access token when changing the Gitea base URL; "
    "a saved token is never sent to a different server."
)


def plugin_error(error: GiteaApiError) -> PluginError:
    advice = {
        0: "Cannot reach Gitea in time. Check connectivity and retry.",
        401: "Gitea rejected the token. Save a valid personal access token.",
        403: "Gitea denied access. Check PAT scopes and repository write permissions.",
        404: "Gitea repository or issue was not found, or the token cannot access it.",
        409: "Gitea reported a conflict. Refresh and retry.",
        422: "Gitea rejected the submitted fields. Check their values.",
        429: "Gitea rate limit reached. Retry later.",
    }.get(error.status, "Gitea could not complete the request.")
    # A response body is remote-controlled text (for a misdirected base URL, possibly another
    # service's data): users see the status category only. Our own messages are kept.
    detail = f"(Gitea status {error.status})" if error.upstream else str(error)
    result = PluginError(f"{advice} {detail}")
    result.status = error.status
    return result


def same_issue(link: Mapping[str, str] | None, other: Mapping[str, str] | None) -> bool:
    return bool(
        link
        and other
        and str(link.get("id")) == str(other.get("id"))
        and str(link.get("repo_id")) == str(other.get("repo_id"))
    )


def blocked_address_error() -> PluginError:
    result = PluginError(BLOCKED_ADDRESS)
    result.status = 0
    return result


def refresh_project_options(project: Any = None) -> None:
    """Drop this thread's cached project options before reading plugin settings.

    Sentry clears that cache only on Django's ``request_finished`` signal. Task workers never
    finish a request, so without this a worker keeps the settings it first saw (enabled state,
    token, repo or labels) until it restarts. The shared cache is updated on save, so the next
    read is current. With a project, only that project's entry is dropped.
    """
    manager = ProjectOption.objects
    if project is not None:
        try:
            manager._option_cache.pop(manager._make_key(project.id), None)
            return
        except (AttributeError, AssertionError, TypeError):
            pass
    manager.clear_local_cache()


class FreshOptionsMixin:
    """Read the enabled flag from current project options (see refresh_project_options).

    The alert rule action checks ``is_enabled`` before ``should_notify``, so a refresh there
    alone would leave an operator's "enable" invisible to a long-lived task worker.
    """

    def is_enabled(self, project: Any = None) -> bool:
        if project is not None:
            refresh_project_options(project)
        return super().is_enabled(project)  # type: ignore[misc]


def plugin_allowed_origins() -> frozenset[str]:
    """Origins whose plugin base URL may resolve to private addresses.

    ``GITEA_PLUGIN_ALLOWED_BASE_URLS`` replaces the default: the origins of ``GITEA_BASE_URL``
    and ``GITEA_PUBLIC_URL`` (the operator-configured instance), when those are set.
    """
    configured = getattr(settings, "GITEA_PLUGIN_ALLOWED_BASE_URLS", None)
    if configured is None:
        values = [getattr(settings, name, None) for name in ("GITEA_BASE_URL", "GITEA_PUBLIC_URL")]
        explicit = False
    else:
        explicit = True
        try:
            values = [configured] if isinstance(configured, str) else list(configured)
        except TypeError:
            values = [configured]
    origins = set()
    for value in values:
        if value in (None, ""):
            continue
        try:
            origins.add(url_origin(value))
        except ValueError:
            if explicit:
                logger.warning(
                    "sentry-gitea: ignoring an invalid GITEA_PLUGIN_ALLOWED_BASE_URLS entry"
                )
    return frozenset(origins)


def plugin_session(base_url: str) -> requests.Session:
    """A plain session for allowlisted origins, else Sentry's SSRF-guarded session.

    The guarded session refuses ``SENTRY_DISALLOWED_IPS`` (private, loopback, link-local...)
    when it connects, after DNS resolution, so a rebinding name cannot slip through.
    """
    if url_origin(base_url) in plugin_allowed_origins():
        return requests.Session()
    return build_session()


@contextmanager
def group_lock(group: Group, *, wait: float = 0.0) -> Iterator[None]:
    """Hold the per-group link lock, retrying for at most `wait` seconds while it is busy."""
    lock = locks.get(f"sentry-gitea:group:{group.id}", duration=60, name="sentry_gitea_link")
    stop = time.monotonic() + wait
    delay = LOCK_RETRY_DELAY
    while True:
        try:
            held = lock.acquire()
            break
        except UnableToAcquireLock:
            remaining = stop - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 1.0)
    with held:
        token = _deadline.set(time.monotonic() + OPERATION_BUDGET)
        try:
            yield
        finally:
            _deadline.reset(token)


@contextmanager
def link_transaction() -> Iterator[None]:
    try:
        with transaction.atomic(using=router.db_for_write(GroupMeta)):
            yield
    except Exception:
        # DB rollback does not roll back GroupMeta's per-thread cache.
        GroupMeta.objects.clear_local_cache()
        raise


class GiteaIssuePlugin(FreshOptionsMixin, IssuePlugin2):
    slug = conf_key = "gitea"
    title = "Gitea"
    description = "Create and link Gitea issues using a project-scoped personal access token."
    version = __version__
    author = AUTHOR
    author_url = PROJECT_URL
    resource_links = [
        ("Source", PROJECT_URL),
        ("Bug Tracker", ISSUES_URL),
        ("Gitea", "https://about.gitea.com/"),
    ]
    auth_provider = None
    issue_fields = frozenset({"id", "title", "url", "repo", "repo_id", "base_url"})

    def configuration(self, project: Project) -> dict[str, str]:
        base = normalize_base_url(self.get_option("base_url", project))
        public = self.get_option("public_url", project)
        public = "" if public in (None, "") else normalize_base_url(public)
        repo = normalize_repo(self.get_option("repo", project))
        token = self.get_option("token", project)
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise ValueError("Save a Gitea personal access token first.")
        return {"base_url": base, "public_url": public, "repo": repo, "token": token}

    def is_configured(self, project: Project | None) -> bool:
        if project is None:
            return False
        try:
            self.configuration(project)
        except ValueError:
            return False
        return True

    def get_configure_plugin_fields(self, project: Project, **kwargs: Any) -> list[dict[str, Any]]:
        saved = bool(self.get_option("token", project))
        return [
            {
                "name": "base_url",
                "label": "Gitea base URL",
                "type": "text",
                "required": True,
                "help": "URL reachable from Sentry containers; a hosting subpath is allowed.",
                "placeholder": "https://gitea.example",
            },
            {
                "name": "public_url",
                "label": "Gitea URL for links",
                "type": "text",
                "required": False,
                "help": "Browser-facing URL; leave empty when base_url is already "
                "reachable by users.",
                "placeholder": "https://gitea.example",
            },
            {
                "name": "token",
                "label": "Personal access token",
                "type": "secret",
                "required": not saved,
                "has_saved_value": saved,
                "help": "Scopes: read:user, read:repository, write:issue. "
                "Leave blank to keep the saved token.",
            },
            {
                "name": "repo",
                "label": "Repository",
                "type": "text",
                "required": True,
                "placeholder": "owner/name",
            },
            {
                "name": "default_labels",
                "label": "Default labels",
                "type": "text",
                "required": False,
                "help": "Comma-separated label names added to every new issue.",
            },
        ]

    def validate_config_field(
        self, project: Project, name: str, value: Any, actor: Any = None
    ) -> Any:
        if name == "token" and (value is None or (isinstance(value, str) and not value.strip())):
            # Keep "blank": validate_config decides whether the saved token may be reused.
            return ""
        value = super().validate_config_field(project, name, value, actor=actor)
        try:
            if name == "public_url" and value in (None, ""):
                return ""
            if name in {"base_url", "public_url"}:
                return normalize_base_url(value)
            if name == "repo":
                return normalize_repo(value)
            if name == "default_labels":
                return ", ".join(parse_label_names(value))
        except ValueError as error:
            raise PluginError(str(error)) from None
        return value

    @contextmanager
    def api_errors(self, project: Project, group: Group | None = None) -> Iterator[None]:
        try:
            yield
        except RestrictedIPAddress:
            logger.error(
                "sentry-gitea: issue operation failed",
                extra={
                    "project_id": project.id,
                    "group_id": group.id if group else None,
                    "status": 0,
                    "error_type": "RestrictedIPAddress",
                },
            )
            raise blocked_address_error() from None
        except GiteaApiError as error:
            logger.error(
                "sentry-gitea: issue operation failed",
                extra={
                    "project_id": project.id,
                    "group_id": group.id if group else None,
                    "status": error.status,
                    # GiteaApiError text is built from fixed strings + redact(); safe to log.
                    "error_message": error.message,
                },
            )
            raise plugin_error(error) from None
        except ValueError as error:
            raise PluginError(str(error)) from None

    def validate_config(
        self, project: Project, config: dict[str, Any], actor: Any = None
    ) -> dict[str, Any]:
        values = {
            name: config.get(name, self.get_option(name, project))
            for name in ("base_url", "public_url", "repo", "default_labels")
        }
        values["token"] = config.get("token")
        keep_saved_token = values["token"] is None or (
            isinstance(values["token"], str) and not values["token"].strip()
        )
        with self.api_errors(project):
            values["base_url"] = normalize_base_url(values["base_url"])
            if keep_saved_token:
                # A blank secret field keeps the saved token, but only for the same server.
                try:
                    saved_base = normalize_base_url(self.get_option("base_url", project))
                except ValueError:
                    saved_base = None
                if saved_base != values["base_url"]:
                    raise PluginError(TOKEN_REENTRY)
                values["token"] = self.get_option("token", project)
            values["public_url"] = (
                ""
                if values["public_url"] in (None, "")
                else normalize_base_url(values["public_url"])
            )
            values["repo"] = normalize_repo(values["repo"])
            values["default_labels"] = ", ".join(parse_label_names(values["default_labels"]))
            with self.new_client(values["base_url"], values["token"], values["repo"]) as client:
                client.current_user_id()
                self.check_repository(client.get_repo())
        return values

    @staticmethod
    def new_client(base_url: str, token: str, repo: str, **kwargs: Any) -> GiteaClient:
        """A client whose session applies the plugin SSRF guard (see plugin_session)."""
        session = plugin_session(base_url)
        try:
            return GiteaClient(base_url, token, repo, session=session, owns_session=True, **kwargs)
        except BaseException:
            session.close()
            raise

    def get_client(
        self, project: Project, *, on_recovered_error: Callable[[int], None] | None = None
    ) -> GiteaClient:
        if on_recovered_error is None:

            def on_recovered_error(status: int) -> None:
                logger.error(
                    "sentry-gitea: issue operation failed; recovered label conflict",
                    extra={"project_id": project.id, "status": status},
                )

        config = self.configuration(project)
        return self.new_client(
            config["base_url"],
            config["token"],
            config["repo"],
            deadline=_deadline.get(),
            on_recovered_error=on_recovered_error,
        )

    @staticmethod
    def check_repository(repo: dict[str, Any]) -> None:
        if type(repo.get("id")) is not int or repo["id"] <= 0:
            raise GiteaApiError(200, "Gitea returned an invalid repository ID.")
        if repo.get("has_issues") is False:
            raise PluginError("Enable Gitea's issue tracker for the configured repository.")

    def event_data(self, group: Group, event: Any) -> EventData:
        data = event.data if event is not None else {}
        tags = dict(data.get("tags") or [])
        release = data.get("release") or tags.get("sentry:release") or ""
        if isinstance(release, Mapping):
            release = release.get("version") or ""
        first_release = group.get_first_release() if not release else ""
        event_id = str(getattr(event, "event_id", "") or "")
        return EventData(
            short_id=group.qualified_short_id,
            title=str(getattr(event, "title", "") or group.title or "Sentry issue"),
            sentry_url=options.get("system.url-prefix") or absolute_uri("/"),
            project_id=group.project_id,
            group_id=group.id,
            issue_url=absolute_uri(group.get_absolute_url()),
            event_id=event_id,
            event_url=absolute_uri(group.get_absolute_url(event_id=event_id)) if event_id else "",
            level=str(data.get("level") or "error"),
            release=str(release),
            first_release=str(first_release or ""),
            environment=str(data.get("environment") or tags.get("environment") or ""),
            culprit=str(data.get("culprit") or group.culprit or ""),
            handled=event_is_handled(data),
            fingerprint=hashlib.sha256(f"{group.project_id}:{group.id}".encode()).hexdigest(),
        )

    def get_group_title(self, group: Group, event: Any) -> str:
        return render_title(self.event_data(group, event))

    def get_group_description(self, group: Group, event: Any) -> str:
        return render_body(self.event_data(group, event))

    def get_allowed_assignees(self, request: Request, group: Group) -> list[str]:
        with self.api_errors(group.project, group), self.get_client(group.project) as client:
            return sorted(
                {
                    user["login"]
                    for user in client.list_assignees()
                    if isinstance(user.get("login"), str) and user["login"]
                }
            )

    def get_new_issue_fields(
        self, request: Request, group: Group, event: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": "title",
                "label": "Title",
                "type": "text",
                "required": True,
                "default": self.get_group_title(group, event),
            },
            {
                "name": "description",
                "label": "Description",
                "type": "textarea",
                "required": True,
                "default": self.get_group_description(group, event),
            },
            {
                "name": "assignee",
                "label": "Assignee",
                "type": "select",
                "required": False,
                "choices": [(name, name) for name in self.get_allowed_assignees(request, group)],
            },
            {
                "name": "labels",
                "label": "Labels",
                "type": "text",
                "required": False,
                "help": "Comma-separated names, in addition to project defaults.",
            },
            {
                "name": "repo",
                "label": "Repository",
                "type": "text",
                "readonly": True,
                "default": self.get_option("repo", group.project),
            },
        ]

    def get_link_existing_issue_fields(
        self, request: Request, group: Group, event: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return [
            {"name": "issue_id", "label": "Issue number", "type": "text", "required": True},
            {
                "name": "comment",
                "label": "Backlink comment",
                "type": "textarea",
                "required": False,
                "default": f"Linked Sentry issue: {absolute_uri(group.get_absolute_url())}",
            },
        ]

    def read_link(self, group: Group) -> dict[str, str] | None:
        GroupMeta.objects.clear_local_cache()
        GroupMeta.objects.populate_cache([group])
        return self.build_issue(group)

    def write_link(self, group: Group, link: dict[str, str]) -> None:
        with link_transaction():
            for field, key in self.get_issue_field_map().items():
                if field in link:
                    GroupMeta.objects.set_value(group, key, str(link[field]))
                else:
                    GroupMeta.objects.unset_value(group, key)

    def assert_binding(
        self, project: Project, link: dict[str, str], repo: dict[str, Any] | None = None
    ) -> None:
        config = self.configuration(project)
        if (
            link.get("base_url") != config["base_url"]
            or link.get("repo") != config["repo"]
            or (repo is not None and link.get("repo_id") != str(repo["id"]))
        ):
            raise PluginError(
                "The linked Gitea repository no longer matches this project's configuration. "
                "Explicitly relink the issue before delivering further alerts."
            )

    def remote_link(
        self, project: Project, client: GiteaClient, repo: dict[str, Any], issue: dict[str, Any]
    ) -> dict[str, str]:
        self.check_repository(repo)
        number = issue_number(issue.get("number"))
        return {
            "id": number,
            "title": clean_text(issue.get("title")) or f"{client.repo}#{number}",
            "repo": client.repo,
            "repo_id": str(repo["id"]),
            "base_url": client.base_url,
            "url": browser_issue_url(
                client.base_url,
                client.repo,
                number,
                public_url=self.get_option("public_url", project),
            ),
        }

    def advisory_hint(
        self,
        client: GiteaClient,
        repo: dict[str, Any],
        data: EventData,
        on_error: Callable[[int], None],
    ) -> str:
        try:
            base = normalize_release(resolve_release(data.release, data.first_release))
            paths = culprit_to_paths(data.culprit)
            head = repo.get("default_branch")
            # Reserve a create request, a final read timeout, and two seconds of headroom.
            # The optional compare has its own cutoff within the shared operation budget.
            read_deadline = client.deadline - REQUEST_ALLOWANCE - TIMEOUT[1] - 2
            if not base or not paths or not head or time.monotonic() >= read_deadline:
                return ""
            comparison = client.compare_commits(base, head, read_deadline=read_deadline)
            commits = comparison.get("commits")
            return render_fix_hint(commits, paths, base, head) if isinstance(commits, list) else ""
        except Exception as error:
            on_error(error.status if isinstance(error, GiteaApiError) else 0)
            return ""

    def file_issue(
        self,
        group: Group,
        data: EventData,
        client: GiteaClient,
        repo: dict[str, Any],
        *,
        title: str,
        body: str,
        labels: list[str],
        assignees: list[str] | None = None,
        fix_hint: bool = False,
        on_hint_error: Callable[[int], None] = lambda status: None,
        replacing: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, str], bool]:
        """Caller holds the group lock through its subsequent local link persistence.

        With `replacing` (the group's current link, to a closed issue), that link does not count
        as existing, and recovery only adopts another open issue: the closed one carries the same
        marker and must never be re-adopted.
        """
        existing = self.read_link(group)
        if existing and not same_issue(existing, replacing):
            self.assert_binding(group.project, existing, repo)
            return existing, False
        # Only an issue the token's own user opened can be a lost result of our create.
        if replacing:
            recovered = client.search_issues_by_marker(
                data.dedup_id,
                author_id=client.current_user_id(),
                state="open",
                exclude=[replacing["id"]],
            )
        else:
            recovered = client.search_issues_by_marker(
                data.dedup_id, author_id=client.current_user_id()
            )
        if recovered:
            return self.remote_link(group.project, client, repo, recovered), False
        label_ids = client.resolve_label_ids(labels)
        content = ensure_metadata(body, data)
        if fix_hint:
            hint = self.advisory_hint(client, repo, data, on_hint_error)
            if hint:
                content += "\n\n" + hint
        remote = client.create_issue(title, content, label_ids=label_ids, assignees=assignees or [])
        return self.remote_link(group.project, client, repo, remote), True

    def create_issue(
        self, request: Request, group: Group, form_data: Mapping[str, Any]
    ) -> dict[str, str]:
        with self.api_errors(group.project, group):
            title = form_data.get("title")
            if not isinstance(title, str) or not clean_text(title):
                raise PluginError("Title must contain non-whitespace text.")
            body = form_data.get("description") or ""
            if not isinstance(body, str):
                raise PluginError("Description must be text.")
            labels = parse_label_names(self.get_option("default_labels", group.project))
            labels = parse_label_names(labels + parse_label_names(form_data.get("labels")))
            assignee = form_data.get("assignee") or ""
            if assignee and assignee not in self.get_allowed_assignees(request, group):
                raise PluginError("Choose an assignee username from this repository.")
            with self.get_client(group.project) as client:
                repo = client.get_repo()
                self.check_repository(repo)
                link, _ = self.file_issue(
                    group,
                    self.event_data(group, group.get_latest_event()),
                    client,
                    repo,
                    title=clean_text(title)[:255],
                    body=body,
                    labels=labels,
                    assignees=[assignee] if assignee else [],
                )
                return link

    def link_issue(
        self, request: Request, group: Group, form_data: Mapping[str, Any], **kwargs: Any
    ) -> dict[str, str]:
        with self.api_errors(group.project, group), self.get_client(group.project) as client:
            number = issue_number(form_data.get("issue_id"))
            comment = form_data.get("comment") or ""
            if not isinstance(comment, str):
                raise PluginError("Backlink comment must be text.")
            repo = client.get_repo()
            link = self.remote_link(group.project, client, repo, client.get_issue(number))
            if comment.strip():
                client.create_comment(number, comment)
            return link

    def get_issue_url(self, group: Group, issue_id: str) -> str:
        return (self.build_issue(group) or {}).get("url") or ""

    def get_issue_label(self, group: Group, issue_id: str) -> str:
        link = self.build_issue(group) or {}
        return f"{link.get('repo') or '(unknown repository)'}#{issue_id}"

    def _view(self, action: str, request: Request, group: Group, **kwargs: Any) -> Response:
        inherited = getattr(super(), f"view_{action}")
        if request.method == "GET" and action != "unlink":
            return inherited(request, group, **kwargs)
        try:
            with group_lock(group), link_transaction():
                self.read_link(group)
                return inherited(request, group, **kwargs)
        except UnableToAcquireLock:
            logger.warning(
                "sentry-gitea: issue operation deferred; group lock busy",
                extra={"project_id": group.project_id, "group_id": group.id},
            )
            return Response(
                {"message": "Another Gitea operation is in progress. Retry shortly."}, status=409
            )
        except Exception as error:
            # Type only: exception text may embed credentials (see alerts.log_failure).
            logger.error(
                "sentry-gitea: issue link persistence failed",
                extra={
                    "project_id": group.project_id,
                    "group_id": group.id,
                    "status": 0,
                    "error_type": type(error).__name__,
                },
            )
            return Response(
                {
                    "message": "Unable to save the Gitea link. "
                    "Retry to recover by its metadata marker."
                },
                status=500,
            )

    def view_create(self, request: Request, group: Group, **kwargs: Any) -> Response:
        return self._view("create", request, group, **kwargs)

    def view_link(self, request: Request, group: Group, **kwargs: Any) -> Response:
        return self._view("link", request, group, **kwargs)

    def view_unlink(self, request: Request, group: Group, **kwargs: Any) -> Response:
        return self._view("unlink", request, group, **kwargs)
