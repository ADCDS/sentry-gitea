"""Deliver alert-rule actions that used to reach the `gitea_alerts` plugin, without the plugin.

Sentry's workflow engine stores the two ways a rule could reach the plugin as two action types:

- "Send a notification via Gitea Alerts" is a **webhook** action whose `target_identifier` is
  the plugin's slug. Up to Sentry 26.6 the webhook handler hands it to the legacy plugin. From
  26.7 legacy plugins are gone and the handler reads the slug as a Sentry App's; no app is
  called `gitea_alerts`, so the rule still fires and nothing happens, without an error anywhere.
- "Send a notification (for all legacy integrations)" is a **plugin** action. Up to 26.6 it
  notifies every legacy notification plugin enabled on the project, this one included. From
  26.7 it only sends the legacy webhooks.

`install()` puts a subclass of each registered handler in its place.

- Webhook actions that target one of our slugs are taken here; every other webhook action
  reaches the original handler untouched. On a Sentry that still has legacy plugins this takes
  those actions away from the `gitea_alerts` plugin, so an alert is delivered once, by the code
  that also runs after the upgrade.
- Plugin actions always reach the original handler. On a Sentry without legacy plugins the
  bridge then also delivers to Gitea, for projects where Gitea alerts are enabled, which is
  what the plugin did as one of "all legacy integrations". Where the plugin still exists it
  does that itself and the bridge adds nothing.

The bridge does not talk to Gitea itself. Sentry runs every rule action in a task with a
30-second deadline and retries it when the deadline passes; a delivery (waiting for the issue's
lock, then several Gitea calls) may take longer, and a retry would post a comment twice and
re-run the original handler. So the bridge only queues `tasks.deliver_alert`, which has its own
deadline and is never retried, and returns.

Set `GITEA_ALERTS_BRIDGE = False` to leave Sentry's handlers alone.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from . import core

logger = logging.getLogger("sentry_gitea.plugins.alerts")
MARKER = "sentry_gitea_alert_bridge"
_installed = False


def switched_on() -> bool:
    value = getattr(settings, "GITEA_ALERTS_BRIDGE", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def targets() -> frozenset[str]:
    """Target identifiers this bridge answers for.

    Always the legacy plugin's slug. `GITEA_ALERTS_WEBHOOK_TARGETS` (a slug or a list of
    slugs) adds more, for rules that were repointed to an internal Sentry App (see
    `sentry_gitea.alerts.app`): with the bridge active those are delivered here too, and the
    app's webhook is never needed. Anything else in that setting is ignored.
    """
    extra = getattr(settings, "GITEA_ALERTS_WEBHOOK_TARGETS", None) or ()
    if isinstance(extra, str):
        extra = (extra,)
    if not isinstance(extra, (list, tuple, set, frozenset)):
        extra = ()
    return frozenset({core.ALERTS_SLUG, *(v for v in extra if isinstance(v, str) and v)})


def active() -> bool:
    """Whether this process routes our alert actions through the bridge."""
    return _installed


def wants(invocation: Any) -> bool:
    config = getattr(getattr(invocation, "action", None), "config", None)
    return isinstance(config, dict) and config.get("target_identifier") in targets()


def legacy_plugin_registered() -> bool:
    """Whether this Sentry still has the `gitea_alerts` legacy plugin to notify by itself."""
    try:
        from sentry.plugins.base import plugins

        plugins.get(core.ALERTS_SLUG)
    except Exception:
        return False
    return True


def queue_invocation(invocation: Any, *, quiet: bool = False) -> None:
    """Queue the delivery of one workflow action. Never raises: the rule must not fail, or be
    retried, because of Gitea.

    `quiet` drops the "not enabled" log line, for the plugin route: that action fires in
    every project that uses it, most of which may have nothing to do with Gitea.
    """
    try:
        from sentry.services.eventstore.models import GroupEvent

        from .tasks import deliver_alert

        event = invocation.event_data.event
        if not isinstance(event, GroupEvent):
            # Activity-triggered workflows (an issue was resolved, assigned...) never reached
            # the legacy notification plugin either.
            return
        group = event.group
        project = group.project
        core.refresh_project_options(project)
        if not core.alerts_ready(project):
            if not quiet:
                logger.info(
                    "sentry-gitea: alert skipped; "
                    "Gitea is not enabled and configured for the project",
                    extra={"project_id": project.id, "group_id": group.id},
                )
            return
        deliver_alert.delay(project.organization_id, project.id, group.id, str(event.event_id))
    except Exception as error:
        logger.error(
            "sentry-gitea: alert could not be queued",
            extra={"error_type": type(error).__name__},
        )


def _wrap(registry: Any, key: Any, name: str, make_execute: Any) -> bool:
    """Replace the handler registered under `key` with a marked subclass. False when absent."""
    stock = registry.registrations.get(key)
    if stock is None:
        return False
    if not getattr(stock, MARKER, False):
        registry.registrations[key] = type(
            name,
            (stock,),
            {MARKER: True, "stock_handler": stock, "execute": staticmethod(make_execute(stock))},
        )
    return True


def _webhook_execute(stock: Any) -> Any:
    def execute(invocation: Any) -> None:
        if wants(invocation):
            queue_invocation(invocation)
            return None
        return stock.execute(invocation)

    return execute


def _plugin_execute(stock: Any) -> Any:
    def execute(invocation: Any) -> None:
        try:
            return stock.execute(invocation)
        finally:
            if not legacy_plugin_registered():
                queue_invocation(invocation, quiet=True)

    return execute


def install() -> bool:
    """Route our alert actions through this module. Idempotent; True when active.

    Never raises. Sentry calls this through the provider's `setup()` in every process, where
    an exception other than AttributeError stops the process and an AttributeError is
    swallowed without a word.
    """
    global _installed
    try:
        if not switched_on():
            return False
        configured = getattr(settings, "GITEA_ALERTS_WEBHOOK_TARGETS", None)
        if configured and not isinstance(configured, (str, list, tuple, set, frozenset)):
            logger.warning(
                "sentry-gitea: GITEA_ALERTS_WEBHOOK_TARGETS ignored; "
                "it must be a slug or a list of slugs"
            )
        # Importing the modules is what registers Sentry's own handlers.
        from sentry.notifications.notification_action.action_handler_registry import (
            plugin_handler,  # noqa: F401
            webhook_handler,  # noqa: F401
        )
        from sentry.workflow_engine.models import Action
        from sentry.workflow_engine.registry import action_handler_registry

        # Registers the task in this process: a worker can run it without a settings line.
        from . import tasks  # noqa: F401

        if not _wrap(
            action_handler_registry,
            Action.Type.WEBHOOK,
            "GiteaAlertsWebhookActionHandler",
            _webhook_execute,
        ):
            logger.warning(
                "sentry-gitea: alert bridge not installed; no webhook handler registered"
            )
            return False
        if not _wrap(
            action_handler_registry,
            Action.Type.PLUGIN,
            "GiteaAlertsPluginActionHandler",
            _plugin_execute,
        ):
            logger.warning(
                "sentry-gitea: no plugin action handler registered; "
                "'all legacy integrations' rules are not bridged"
            )
    except Exception as error:
        logger.error(
            "sentry-gitea: alert bridge not installed",
            extra={"error_type": type(error).__name__, "error": str(error)[:200]},
        )
        return False
    if not _installed:
        logger.info("sentry-gitea: alert bridge installed", extra={"targets": sorted(targets())})
    _installed = True
    return True


def uninstall() -> None:
    """Put Sentry's handlers back (tests)."""
    global _installed
    from sentry.workflow_engine.models import Action
    from sentry.workflow_engine.registry import action_handler_registry

    for key in (Action.Type.WEBHOOK, Action.Type.PLUGIN):
        current = action_handler_registry.registrations.get(key)
        if getattr(current, MARKER, False):
            action_handler_registry.registrations[key] = current.stock_handler
    _installed = False
