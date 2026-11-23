"""Settings, links and the Gitea client for alert filing, without the legacy plugin classes.

Everything here used to be a method of `GiteaIssuePlugin`. It takes the project or the group
instead, and reads the same storage the plugin wrote (see the package docstring), so it runs on
a Sentry with or without `sentry.plugins.bases`.
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
from sentry import options
from sentry.exceptions import PluginError, RestrictedIPAddress
from sentry.http import build_session
from sentry.locks import locks
from sentry.models.groupmeta import GroupMeta
from sentry.models.options.project_option import ProjectOption
from sentry.utils.http import absolute_uri
from sentry.utils.locking import UnableToAcquireLock

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
from sentry_gitea.render import (
    EventData,
    clean_text,
    culprit_to_paths,
    ensure_metadata,
    event_is_handled,
    normalize_release,
    parse_label_names,
    render_fix_hint,
    resolve_release,
)

if TYPE_CHECKING:
    from sentry.models.group import Group
    from sentry.models.project import Project

logger = logging.getLogger("sentry_gitea.plugins.issue")
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

# The legacy plugins' slugs are the prefixes of everything they stored.
ISSUE_SLUG = "gitea"
ALERTS_SLUG = "gitea_alerts"
# Link fields kept per Sentry issue, as GroupMeta `gitea:issue_<field>`.
ISSUE_FIELDS = ("id", "title", "url", "repo", "repo_id", "base_url")
LINK_KEYS = {field: f"{ISSUE_SLUG}:issue_{field}" for field in ISSUE_FIELDS}

ALERT_DEFAULTS: dict[str, Any] = {
    "labels": "source:sentry",
    "severity_labels": True,
    "comment_on_repeat": True,
    "fix_hint": False,
    "refile_on_regression": True,
    "refile_after_close": False,
}
ALERT_BOOL_OPTIONS = (
    "severity_labels",
    "comment_on_repeat",
    "fix_hint",
    "refile_on_regression",
    "refile_after_close",
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
    """Drop this thread's cached project options before reading settings.

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


def option(project: Project, slug: str, name: str) -> Any:
    """One stored setting of a legacy plugin, or None."""
    return ProjectOption.objects.get_value(project, f"{slug}:{name}", None)


def enabled(project: Project | None, slug: str) -> bool:
    """Whether the operator enabled that plugin for the project (off unless set)."""
    return project is not None and bool(option(project, slug, "enabled"))


def configuration(project: Project) -> dict[str, str]:
    base = normalize_base_url(option(project, ISSUE_SLUG, "base_url"))
    public = option(project, ISSUE_SLUG, "public_url")
    public = "" if public in (None, "") else normalize_base_url(public)
    repo = normalize_repo(option(project, ISSUE_SLUG, "repo"))
    token = option(project, ISSUE_SLUG, "token")
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
        raise ValueError("Save a Gitea personal access token first.")
    return {"base_url": base, "public_url": public, "repo": repo, "token": token}


def is_configured(project: Project | None) -> bool:
    if project is None:
        return False
    try:
        configuration(project)
    except ValueError:
        return False
    return True


def alert_options(project: Project) -> dict[str, Any]:
    return {
        key: default if (value := option(project, ALERTS_SLUG, key)) is None else value
        for key, default in ALERT_DEFAULTS.items()
    }


def validate_alert_options(values: dict[str, Any]) -> dict[str, Any]:
    values = dict(values)
    values["labels"] = ", ".join(parse_label_names(values["labels"]))
    for key in ALERT_BOOL_OPTIONS:
        if type(values[key]) is not bool:
            raise ValueError(f"{key} must be a boolean.")
    return values


def alerts_ready(project: Project | None) -> bool:
    """Both settings pages are enabled and valid: an alert for this project can be delivered."""
    if not (enabled(project, ALERTS_SLUG) and enabled(project, ISSUE_SLUG)):
        return False
    if not is_configured(project):
        return False
    try:
        validate_alert_options(alert_options(project))
    except ValueError:
        return False
    return True


@contextmanager
def api_errors(project: Project, group: Group | None = None) -> Iterator[None]:
    """Turn a failed Gitea call into a PluginError a user may read; log it with IDs only."""
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


def validate_issue_settings(project: Project, submitted: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise submitted issue settings and prove them against Gitea before they are saved.

    A field that was not submitted keeps its saved value. A blank token keeps the saved one,
    but only for the same server: a saved token is never sent to a new base URL. Raises
    PluginError with a message that is safe to show.
    """
    values = {
        name: submitted.get(name, option(project, ISSUE_SLUG, name))
        for name in ("base_url", "public_url", "repo", "default_labels")
    }
    values["token"] = submitted.get("token")
    keep_saved_token = values["token"] is None or (
        isinstance(values["token"], str) and not values["token"].strip()
    )
    with api_errors(project):
        values["base_url"] = normalize_base_url(values["base_url"])
        if keep_saved_token:
            try:
                saved_base = normalize_base_url(option(project, ISSUE_SLUG, "base_url"))
            except ValueError:
                saved_base = None
            if saved_base != values["base_url"]:
                raise PluginError(TOKEN_REENTRY)
            values["token"] = option(project, ISSUE_SLUG, "token")
        values["public_url"] = (
            "" if values["public_url"] in (None, "") else normalize_base_url(values["public_url"])
        )
        values["repo"] = normalize_repo(values["repo"])
        values["default_labels"] = ", ".join(parse_label_names(values["default_labels"]))
        with new_client(values["base_url"], values["token"], values["repo"]) as client:
            client.current_user_id()
            check_repository(client.get_repo())
    return values


def save_options(project: Project, slug: str, values: Mapping[str, Any]) -> None:
    for name, value in values.items():
        ProjectOption.objects.set_value(project, f"{slug}:{name}", value)


def delete_options(project: Project) -> int:
    """Remove every stored setting of the project, the token included; returns how many.

    What the legacy pages' "Reset Configuration" did. Issue links are not settings and stay.
    """
    keys = [
        key
        for key in ProjectOption.objects.filter(project=project).values_list("key", flat=True)
        if key.startswith((f"{ISSUE_SLUG}:", f"{ALERTS_SLUG}:"))
    ]
    for key in keys:
        ProjectOption.objects.unset_value(project, key)
    refresh_project_options(project)
    return len(keys)


def clear_links(project: Project) -> int:
    """Forget the Gitea issue linked to every issue of the project; returns how many had one.

    The way out when the stored links no longer match the settings (another base URL or
    repository): alerts refuse to write through such a link. With the link gone, the next
    alert for an issue looks for the Gitea issue that carries its marker in the repository
    configured now and links that again, or files a new one. Nothing changes in Gitea.
    """
    rows = GroupMeta.objects.filter(group__project_id=project.id, key__in=list(LINK_KEYS.values()))
    linked = rows.filter(key=LINK_KEYS["id"]).count()
    rows.delete()
    GroupMeta.objects.clear_local_cache()
    return linked


def check_connection(project: Project) -> None:
    """Read-only proof that the saved settings work: the token's user and the repository."""
    with api_errors(project), get_client(project) as client:
        client.get_current_user()
        check_repository(client.get_repo())


def plugin_allowed_origins() -> frozenset[str]:
    """Origins whose base URL may resolve to private addresses.

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


def new_client(base_url: str, token: str, repo: str, **kwargs: Any) -> GiteaClient:
    """A client whose session applies the SSRF guard (see plugin_session)."""
    session = plugin_session(base_url)
    try:
        return GiteaClient(base_url, token, repo, session=session, owns_session=True, **kwargs)
    except BaseException:
        session.close()
        raise


def get_client(
    project: Project, *, on_recovered_error: Callable[[int], None] | None = None
) -> GiteaClient:
    if on_recovered_error is None:

        def on_recovered_error(status: int) -> None:
            logger.error(
                "sentry-gitea: issue operation failed; recovered label conflict",
                extra={"project_id": project.id, "status": status},
            )

    config = configuration(project)
    return new_client(
        config["base_url"],
        config["token"],
        config["repo"],
        deadline=_deadline.get(),
        on_recovered_error=on_recovered_error,
    )


def check_repository(repo: dict[str, Any]) -> None:
    if type(repo.get("id")) is not int or repo["id"] <= 0:
        raise GiteaApiError(200, "Gitea returned an invalid repository ID.")
    if repo.get("has_issues") is False:
        raise PluginError("Enable Gitea's issue tracker for the configured repository.")


def event_data(group: Group, event: Any) -> EventData:
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


def read_link(group: Group) -> dict[str, str] | None:
    """The Gitea issue this Sentry issue is linked to, read past the per-thread cache."""
    GroupMeta.objects.clear_local_cache()
    GroupMeta.objects.populate_cache([group])
    link = {
        field: GroupMeta.objects.get_value(group, key, None) for field, key in LINK_KEYS.items()
    }
    return link if any(link.values()) else None


def write_link(group: Group, link: Mapping[str, str]) -> None:
    with link_transaction():
        for field, key in LINK_KEYS.items():
            if field in link:
                GroupMeta.objects.set_value(group, key, str(link[field]))
            else:
                GroupMeta.objects.unset_value(group, key)


def assert_binding(
    project: Project, link: Mapping[str, str], repo: dict[str, Any] | None = None
) -> None:
    config = configuration(project)
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
    project: Project, client: GiteaClient, repo: dict[str, Any], issue: dict[str, Any]
) -> dict[str, str]:
    check_repository(repo)
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
            public_url=option(project, ISSUE_SLUG, "public_url"),
        ),
    }


def advisory_hint(
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
    existing = read_link(group)
    if existing and not same_issue(existing, replacing):
        assert_binding(group.project, existing, repo)
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
        return remote_link(group.project, client, repo, recovered), False
    label_ids = client.resolve_label_ids(labels)
    content = ensure_metadata(body, data)
    if fix_hint:
        hint = advisory_hint(client, repo, data, on_hint_error)
        if hint:
            content += "\n\n" + hint
    remote = client.create_issue(title, content, label_ids=label_ids, assignees=assignees or [])
    return remote_link(group.project, client, repo, remote), True
