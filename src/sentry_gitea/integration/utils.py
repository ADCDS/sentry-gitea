"""Sentry-free integration identifiers, webhook secrets and OAuth token bookkeeping."""

import hashlib
import hmac
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from sentry_gitea.api.client import issue_number, normalize_base_url

OAUTH_SCOPES = ("read:user", "read:organization", "write:issue", "write:repository")
# Refresh an access token proactively when it expires within this many seconds.
REFRESH_MARGIN = 300


def validate_scopes(scopes: Sequence[str]) -> list[str]:
    if isinstance(scopes, str) or not isinstance(scopes, (list, tuple)):
        raise ValueError("Invalid Gitea OAuth scopes.")
    if any(not isinstance(scope, str) for scope in scopes) or set(scopes) != set(OAUTH_SCOPES):
        raise ValueError("Invalid Gitea OAuth scopes.")
    return list(OAUTH_SCOPES)


def external_id(base_url: str, owner_kind: str, owner_id: int | str) -> str:
    if owner_kind not in {"user", "org"}:
        raise ValueError("Invalid Gitea owner kind.")
    source = f"v1|{normalize_base_url(base_url)}|{owner_kind}|{issue_number(owner_id)}"
    return hashlib.sha256(source.encode()).hexdigest()


def issue_key(repo_id: int | str, number: int | str) -> str:
    return f"{issue_number(repo_id)}#{issue_number(number)}"


def repo_webhook_secret(secret: str, repo_id: int | str) -> str:
    """Per-repository hook secret: hex HMAC-SHA256(key=integration secret, msg="repo:<id>").

    Gitea shows a hook's secret to that repository's admins, so each repository gets its own
    derived secret; a leaked one can only sign deliveries for its own repository.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("A webhook secret is required.")
    message = f"repo:{issue_number(repo_id)}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def identity_user_id(external_id: str) -> int | None:
    """The Gitea user ID recorded in an identity's `<base url>|<user id>` external ID."""
    if not isinstance(external_id, str) or "|" not in external_id:
        return None
    try:
        return int(issue_number(external_id.rsplit("|", 1)[1]))
    except ValueError:
        return None


def token_expiring(data: Mapping[str, Any], *, now: float, margin: int = REFRESH_MARGIN) -> bool:
    """True when the stored access token expires within `margin` seconds (unknown: False)."""
    expires = data.get("expires")
    return type(expires) is int and expires - now < margin


def parse_issue_key(key: str) -> tuple[int, int]:
    if not isinstance(key, str) or not re.fullmatch(r"[1-9][0-9]*#[1-9][0-9]*", key):
        raise ValueError("Select a Gitea issue using its repository and issue number.")
    repo_id, number = key.split("#")
    return int(repo_id), int(number)


def normalize_tokens(
    response: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    previous = previous or {}
    access = response.get("access_token")
    refresh = response.get("refresh_token", previous.get("refresh_token"))
    expires_in = response.get("expires_in")
    if (
        any(
            not isinstance(token, str) or not token or any(c.isspace() for c in token)
            for token in (access, refresh)
        )
        or type(expires_in) is not int
        or expires_in <= 0
    ):
        raise ValueError("Gitea returned invalid OAuth credentials.")
    return {
        **previous,
        "access_token": access,
        "refresh_token": refresh,
        "expires": int(time.time() if now is None else now) + expires_in,
    }


def stamp_issued(tokens: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Record when Gitea issued these tokens; the keep-alive task refreshes old grants."""
    return {**tokens, "issued_at": int(time.time() if now is None else now)}
