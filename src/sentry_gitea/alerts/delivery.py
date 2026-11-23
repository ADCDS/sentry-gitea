"""What one alert does in Gitea: file an issue, comment on the open one, or file again.

This is the delivery the `gitea_alerts` legacy plugin performed in `notify_users`, moved out of
the plugin class so that the bridge and the webhook fallback run the same code on a Sentry
without `sentry.plugins.bases`. Failures are raised as `PluginError` after being logged with
IDs and a status only; callers decide whether to swallow them.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from django.utils import timezone
from sentry import ratelimits
from sentry.exceptions import PluginError, RestrictedIPAddress
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.models.activity import Activity
from sentry.models.grouplink import GroupLink
from sentry.types.activity import ActivityType
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea.api.client import parse_gitea_time
from sentry_gitea.api.errors import GiteaApiError
from sentry_gitea.render import (
    map_class,
    map_severity,
    parse_label_names,
    render_body,
    render_comment,
    render_title,
)

from . import core

if TYPE_CHECKING:
    from sentry.models.group import Group

logger = logging.getLogger("sentry_gitea.plugins.alerts")

# Alerts wait briefly for a concurrent link operation on the same group, then skip.
LOCK_WAIT = 10.0
# refile_after_close ignores events this soon after the close: a fix closed by a merge is
# usually still deploying, and its last pre-deploy events should not reopen the conversation.
REFILE_GRACE = timedelta(hours=1)
# Alerts per project and minute, the limit Sentry applied to a legacy notification plugin.
RATE_LIMIT = 10


class Recurrence(NamedTuple):
    """Whether an event for a group linked to a closed issue files a new one, and why not."""

    reason: str | None  # "regressed" or "after_close" when a new issue is filed
    skipped: str | None  # why not, for the skip log
    closed_at: datetime | None = None


def integration_linked(group: Group, link: dict[str, str]) -> bool:
    """The integration links the same Gitea issue and syncs its status itself."""
    key = f"{link.get('repo_id')}#{link.get('id')}"
    external_ids = list(
        ExternalIssue.objects.filter(
            organization_id=group.project.organization_id, key=key
        ).values_list("id", flat=True)
    )
    return (
        bool(external_ids)
        and GroupLink.objects.filter(
            group_id=group.id, linked_type=GroupLink.LinkedType.issue, linked_id__in=external_ids
        ).exists()
    )


def recurrence(
    group: Group, remote: dict[str, Any], link: dict[str, str], config: dict[str, Any]
) -> Recurrence:
    if not (config["refile_on_regression"] or config["refile_after_close"]):
        return Recurrence(None, "disabled")
    closed_at = parse_gitea_time(remote.get("closed_at"))
    if closed_at is None:
        return Recurrence(None, "no closed_at")
    if integration_linked(group, link):
        return Recurrence(None, "integration linked", closed_at)
    # Sentry records every regression (release- and commit-aware resolution included) as an
    # activity row; one dated after the close means the problem came back after the fix.
    if (
        config["refile_on_regression"]
        and Activity.objects.filter(
            group_id=group.id,
            type=ActivityType.SET_REGRESSION.value,
            datetime__gt=closed_at,
        ).exists()
    ):
        return Recurrence("regressed", None, closed_at)
    # Plugin links have no status sync, so a Gitea close leaves the Sentry issue unresolved
    # and it can never regress. Server time, not the client-supplied event timestamp.
    if config["refile_after_close"] and timezone.now() > closed_at + REFILE_GRACE:
        return Recurrence("after_close", None, closed_at)
    return Recurrence(None, "no recurrence signal", closed_at)


def log_failure(group: Group, status: int, error: BaseException | None = None) -> None:
    # Never pass exception text or traceback: requests/DB errors may embed credentials.
    # The exception type alone is safe and makes unexpected failures diagnosable.
    logger.error(
        "sentry-gitea: alert delivery failed",
        extra={
            "project_id": group.project_id,
            "group_id": group.id,
            "status": status,
            "error_type": type(error).__name__ if error else None,
            # GiteaApiError text is built from fixed strings + redact(); safe to log.
            "error_message": error.message if isinstance(error, GiteaApiError) else None,
        },
    )


def rate_limited(group: Group) -> bool:
    """More than RATE_LIMIT alerts for this project in the current minute."""
    return ratelimits.backend.is_limited(
        project=group.project, key=core.ALERTS_SLUG, limit=RATE_LIMIT
    )


def deliver(group: Group, event: Any) -> None:
    """File, comment or re-file for one alert. Raises PluginError on failure (already logged)."""
    core.refresh_project_options()
    try:
        if not core.alerts_ready(group.project):
            raise PluginError("Enable and configure both Gitea plugins before delivering alerts.")
        config = core.alert_options(group.project)
        with core.group_lock(group, wait=LOCK_WAIT):
            link = core.read_link(group)
            if link:
                core.assert_binding(group.project, link)
            with core.get_client(
                group.project, on_recovered_error=lambda status: log_failure(group, status)
            ) as client:
                repo = client.get_repo()
                core.check_repository(repo)
                replacing = None
                found = Recurrence(None, None)
                if link:
                    core.assert_binding(group.project, link, repo)
                    remote = client.get_issue(link["id"])
                    if remote.get("state") == "closed":
                        found = recurrence(group, remote, link, config)
                        if found.reason:
                            replacing = link
                    if not replacing and (
                        remote.get("state") == "closed" or not config["comment_on_repeat"]
                    ):
                        # Visible no-op: operators (and E2E) can tell "evaluated and skipped"
                        # apart from "never ran".
                        logger.info(
                            "sentry-gitea: skipped repeat event",
                            extra={
                                "project_id": group.project_id,
                                "group_id": group.id,
                                "issue_id": link["id"],
                                "reason": "closed"
                                if remote.get("state") == "closed"
                                else "comment_on_repeat disabled",
                                "refile_skipped": found.skipped,
                            },
                        )
                        return
                    if not replacing:
                        if remote.get("state") != "open":
                            raise GiteaApiError(200, "Gitea returned an unknown issue state.")
                        client.create_comment(
                            link["id"], render_comment(core.event_data(group, event))
                        )
                        logger.info(
                            "sentry-gitea: commented on issue",
                            extra={
                                "project_id": group.project_id,
                                "group_id": group.id,
                                "issue_id": link["id"],
                            },
                        )
                        return
                data = core.event_data(group, event)
                if replacing and found.closed_at:
                    data = dataclasses.replace(
                        data,
                        recurrence_of=replacing["id"],
                        recurrence_closed=found.closed_at.date().isoformat(),
                    )
                labels = parse_label_names(
                    core.option(group.project, core.ISSUE_SLUG, "default_labels")
                )
                labels += parse_label_names(config["labels"])
                if config["severity_labels"]:
                    labels += [
                        f"severity:{map_severity(data.level, data.handled)}",
                        f"class:{map_class(data.handled)}",
                    ]
                link, created = core.file_issue(
                    group,
                    data,
                    client,
                    repo,
                    title=render_title(data),
                    body=render_body(data),
                    labels=parse_label_names(labels),
                    fix_hint=config["fix_hint"],
                    on_hint_error=lambda status: log_failure(group, status),
                    replacing=replacing,
                )
                core.write_link(group, link)
                logger.info(
                    "sentry-gitea: filed issue",
                    extra={
                        "project_id": group.project_id,
                        "group_id": group.id,
                        "issue_id": link["id"],
                        "recovered": not created,
                        "recurrence_of": replacing["id"] if replacing else None,
                        "refile_reason": found.reason,
                    },
                )
    except UnableToAcquireLock:
        # Not retried later: the next alert for this group delivers normally.
        logger.warning(
            "sentry-gitea: alert skipped; group lock busy",
            extra={"project_id": group.project_id, "group_id": group.id, "waited": LOCK_WAIT},
        )
    except RestrictedIPAddress as error:
        log_failure(group, 0, error)
        raise core.blocked_address_error() from None
    except GiteaApiError as error:
        log_failure(group, error.status, error)
        raise core.plugin_error(error) from None
    except PluginError as error:
        log_failure(group, getattr(error, "status", 0), error)
        raise
    except Exception as error:
        log_failure(group, 0, error)
        raise PluginError(
            "Unable to deliver the Gitea alert or save its link. Retry to recover "
            "a created issue using its metadata marker."
        ) from None
