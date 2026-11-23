"""The taskworker task that delivers one alert to Gitea.

Both entry points queue it: the bridge (`bridge.py`) and the signed-webhook fallback
(`webhook.py`). Neither delivers by itself, because both run where a slow Gitea must not be
waited for: a rule action with a 30-second deadline, and a web request.

The task is registered by importing this module. The bridge imports it in every Sentry
process, and `sentry_gitea.integration.tasks`, which installations list in
`TASKWORKER_IMPORTS`, imports it too.
"""

from __future__ import annotations

import logging
from typing import Any

from sentry.exceptions import PluginError
from sentry.models.group import Group
from sentry.services import eventstore
from sentry.silo.base import SiloMode
from sentry.tasks.base import instrumented_task
from sentry.taskworker.namespaces import integrations_tasks

from sentry_gitea.api.client import OPERATION_BUDGET

from . import core, delivery

logger = logging.getLogger("sentry_gitea.plugins.alerts")


def load_event(group: Group, event_id: str | None) -> Any:
    """The event the alert is about, as the rule saw it; else the issue's latest; else None.

    Reading by ID with the issue given needs only the node store. The fallbacks cover an event
    that is not readable (yet): the latest event describes the same problem, and the issue
    alone is enough to file. Nothing here may lose the alert.
    """
    try:
        event = None
        if event_id:
            event = eventstore.backend.get_event_by_id(
                group.project_id, event_id, group_id=group.id
            )
        if event is None:
            event = group.get_latest_event()
        if event is not None and getattr(event, "group_id", None) is None:
            event.group_id = group.id
        return event
    except Exception as error:
        logger.warning(
            "sentry-gitea: could not load an event for the alert; using the issue only",
            extra={"group_id": group.id, "error_type": type(error).__name__},
        )
        return None


@instrumented_task(
    name="sentry_gitea.alerts.tasks.deliver_alert",
    namespace=integrations_tasks,
    processing_deadline_duration=int(OPERATION_BUDGET + delivery.LOCK_WAIT + 30),
    silo_mode=SiloMode.CELL,
)
def deliver_alert(
    organization_id: int, project_id: int, group_id: int, event_id: str | None = None
) -> None:
    """Deliver one alert by IDs. Never retried: the next alert for the issue delivers again."""
    group = (
        Group.objects.filter(
            id=group_id, project_id=project_id, project__organization_id=organization_id
        )
        .select_related("project")
        .first()
    )
    if group is None:
        logger.info(
            "sentry-gitea: alert skipped; the issue is gone or belongs to another organization",
            extra={"project_id": project_id, "group_id": group_id},
        )
        return
    core.refresh_project_options(group.project)
    if not core.alerts_ready(group.project):
        logger.info(
            "sentry-gitea: alert skipped; Gitea is not enabled and configured for the project",
            extra={"project_id": group.project_id, "group_id": group.id},
        )
        return
    if delivery.rate_limited(group):
        logger.info(
            "sentry-gitea: alert skipped; rate limited",
            extra={"project_id": group.project_id, "group_id": group.id},
        )
        return
    event = load_event(group, event_id)
    try:
        delivery.deliver(group, event)
    except PluginError:
        pass  # logged by deliver() with the project, the issue and a status
    except Exception as error:
        delivery.log_failure(group, 0, error)
