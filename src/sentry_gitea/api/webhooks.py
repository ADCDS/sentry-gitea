"""Gitea webhook authentication and payload normalization, independent of Sentry.

Shapes follow Gitea 28 `modules/structs/hook.go`: `X-Gitea-Signature` is the unprefixed hex
HMAC-SHA256 of the exact request body; `X-Gitea-Event` carries the top-level event, so issue
assignment arrives as `issues` with action `assigned`/`unassigned`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .client import is_sha, issue_number, normalize_repo

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
SIGNATURE = re.compile(r"[0-9a-fA-F]{64}")
SKIP_MARKER = "#skipsentry"
STATUS_ACTIONS = frozenset({"closed", "reopened"})
ASSIGN_ACTIONS = frozenset({"assigned", "unassigned"})
# `issue_assign` is Gitea's X-Gitea-Event-Type; accept it as the event name too.
ISSUE_EVENTS = frozenset({"issues", "issue_assign"})
EVENTS = frozenset({"push", "pull_request"} | ISSUE_EVENTS)
FILE_STATUS = {
    "added": "A",
    "copied": "A",
    "removed": "D",
    "deleted": "D",
    "modified": "M",
    "changed": "M",
}


class PayloadError(ValueError):
    """Invalid wire data. Messages are fixed strings and never include payload content."""


def verify_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    """Constant-time check of Gitea's signature over the raw body; an empty secret never passes."""
    if not isinstance(secret, str) or not secret or not isinstance(body, bytes):
        return False
    if not isinstance(signature, str) or not SIGNATURE.fullmatch(signature.strip()):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.strip().lower().encode())


def is_zero_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) in {40, 64} and set(value) == {"0"}


def parse_json(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_PAYLOAD_BYTES:
        raise PayloadError("Payload exceeds the size limit.")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise PayloadError("Payload is not valid JSON.") from None
    if not isinstance(data, dict):
        raise PayloadError("Payload must be a JSON object.")
    return data


def timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        raise PayloadError("Invalid timestamp.") from None
    if result.tzinfo is None:
        raise PayloadError("Invalid timestamp.")
    return result


def _positive(value: Any) -> int:
    if type(value) is not int:
        raise PayloadError("Invalid identifier.")
    try:
        return int(issue_number(value))
    except ValueError:
        raise PayloadError("Invalid identifier.") from None


def _text(value: Any, *, limit: int | None = None) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PayloadError("Invalid text field.")
    return value if limit is None else value[:limit]


@dataclass(frozen=True)
class RepositoryRef:
    id: int
    owner_id: int
    full_name: str
    default_branch: str | None = None


def repository_ref(payload: dict[str, Any]) -> RepositoryRef:
    repo = payload.get("repository")
    if not isinstance(repo, dict) or not isinstance(repo.get("owner"), dict):
        raise PayloadError("Missing repository.")
    try:
        full_name = normalize_repo(repo.get("full_name"))
    except ValueError:
        raise PayloadError("Invalid repository name.") from None
    branch = repo.get("default_branch")
    return RepositoryRef(
        id=_positive(repo.get("id")),
        owner_id=_positive(repo["owner"].get("id")),
        full_name=full_name,
        default_branch=branch if isinstance(branch, str) and branch else None,
    )


def _patch_set(changes: dict[str, str]) -> list[dict[str, str]]:
    return [{"path": path, "type": kind} for path, kind in changes.items()]


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise PayloadError("Invalid file path.")
    return value


def normalize_push_commit(data: Any, repository: str) -> dict[str, Any] | None:
    """A push payload commit (`PayloadCommit`); None for commits marked #skipsentry."""
    if not isinstance(data, dict):
        raise PayloadError("Invalid commit.")
    message = _text(data.get("message"))
    sha = data.get("id")
    if not is_sha(sha):
        raise PayloadError("Invalid commit SHA.")
    author = data.get("author") or {}
    if not isinstance(author, dict):
        raise PayloadError("Invalid commit author.")
    changes: dict[str, str] = {}
    for key, kind in (("added", "A"), ("removed", "D"), ("modified", "M")):
        paths = data.get(key) or []
        if not isinstance(paths, list):
            raise PayloadError("Invalid commit file list.")
        for path in paths:
            changes[_path(path)] = kind
    commit = {
        "id": sha,
        "repository": repository,
        "message": message,
        "author_name": _text(author.get("name"), limit=128) or None,
        "author_email": _text(author.get("email")) or None,
        "timestamp": timestamp(data.get("timestamp")),
        "patch_set": _patch_set(changes),
    }
    return None if SKIP_MARKER in message else commit


def normalize_api_commit(data: Any, repository: str) -> dict[str, Any] | None:
    """A commits-API row (`api.Commit` with `files`); None for commits marked #skipsentry."""
    if not isinstance(data, dict) or not isinstance(data.get("commit"), dict):
        raise PayloadError("Invalid commit.")
    sha = data.get("sha")
    if not is_sha(sha):
        raise PayloadError("Invalid commit SHA.")
    inner = data["commit"]
    message = _text(inner.get("message"))
    author = inner.get("author") or {}
    if not isinstance(author, dict):
        raise PayloadError("Invalid commit author.")
    files = data.get("files")
    if not isinstance(files, list):
        raise PayloadError("Commit file changes are missing.")
    changes: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict):
            raise PayloadError("Invalid commit file change.")
        path, status = _path(item.get("filename")), item.get("status")
        previous = item.get("previous_filename")
        if status == "renamed" and previous:
            changes[_path(previous)] = "D"
            changes[path] = "A"
        else:
            # Unknown statuses still prove the path was touched.
            changes[path] = FILE_STATUS.get(status, "M")
    commit = {
        "id": sha,
        "repository": repository,
        "message": message,
        "author_name": _text(author.get("name"), limit=128) or None,
        "author_email": _text(author.get("email")) or None,
        "timestamp": timestamp(author.get("date")),
        "patch_set": _patch_set(changes),
    }
    return None if SKIP_MARKER in message else commit


@dataclass(frozen=True)
class PushEvent:
    repository: RepositoryRef
    before: str
    after: str
    total_commits: int
    commits: list[dict[str, Any]] = field(default_factory=list)
    # Rows Gitea delivered, including #skipsentry commits that normalize to nothing.
    delivered: int = 0

    @property
    def deleted(self) -> bool:
        return is_zero_sha(self.after)

    @property
    def created(self) -> bool:
        return is_zero_sha(self.before)

    @property
    def truncated(self) -> bool:
        # Gitea cuts commits[] at FEED_MAX_COMMIT_NUM but keeps the real total_commits.
        return self.total_commits > self.delivered


def parse_push(payload: dict[str, Any]) -> PushEvent:
    repo = repository_ref(payload)
    before, after = payload.get("before"), payload.get("after")
    for sha in (before, after):
        if not (is_sha(sha) or is_zero_sha(sha)):
            raise PayloadError("Invalid push range.")
    rows = payload.get("commits") or []
    total = payload.get("total_commits", len(rows))
    if not isinstance(rows, list) or type(total) is not int or total < len(rows):
        raise PayloadError("Invalid push commits.")
    if is_zero_sha(after):
        rows = []
    commits = [c for row in rows if (c := normalize_push_commit(row, repo.full_name)) is not None]
    return PushEvent(repo, before, after, total, commits, delivered=len(rows))


@dataclass(frozen=True)
class IssueEvent:
    repository: RepositoryRef
    action: str
    number: int
    is_pull_request: bool


def parse_issue(payload: dict[str, Any]) -> IssueEvent:
    repo = repository_ref(payload)
    action = payload.get("action")
    issue = payload.get("issue")
    if not isinstance(action, str) or not isinstance(issue, dict):
        raise PayloadError("Invalid issue event.")
    # Plain issues serialize `"pull_request": null`.
    return IssueEvent(
        repo, action, _positive(issue.get("number")), issue.get("pull_request") is not None
    )


@dataclass(frozen=True)
class PullRequestEvent:
    repository: RepositoryRef
    action: str
    number: int
    title: str
    body: str
    author_id: int
    author_login: str
    author_name: str
    author_email: str | None
    merge_commit_sha: str | None
    created_at: datetime | None


def placeholder_email(email: str, user_id: int, login: str) -> bool:
    """Gitea hides private addresses as `<id>+<lowercase login>@<NO_REPLY_ADDRESS>`."""
    return email.casefold().startswith(f"{user_id}+{login.casefold()}@")


def parse_pull_request(payload: dict[str, Any]) -> PullRequestEvent:
    repo = repository_ref(payload)
    action = payload.get("action")
    pull = payload.get("pull_request")
    if not isinstance(action, str) or not isinstance(pull, dict):
        raise PayloadError("Invalid pull request event.")
    user = pull.get("user")
    if not isinstance(user, dict):
        raise PayloadError("Invalid pull request author.")
    user_id = _positive(user.get("id"))
    login = _text(user.get("login"))
    email = _text(user.get("email"))
    if not email or "@" not in email or placeholder_email(email, user_id, login):
        email = ""
    merge_sha = pull.get("merge_commit_sha") if pull.get("merged") is True else None
    if merge_sha is not None and not is_sha(merge_sha):
        raise PayloadError("Invalid merge commit.")
    created = pull.get("created_at")
    return PullRequestEvent(
        repository=repo,
        action=action,
        number=_positive(pull.get("number")),
        title=_text(pull.get("title")),
        body=_text(pull.get("body")),
        author_id=user_id,
        author_login=login,
        author_name=_text(user.get("full_name"), limit=128) or login[:128],
        author_email=email or None,
        merge_commit_sha=merge_sha,
        created_at=timestamp(created) if created else None,
    )
