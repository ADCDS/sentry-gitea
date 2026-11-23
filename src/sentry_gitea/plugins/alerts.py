"""Legacy alerts plugin: the settings page and the entry point Sentry's rule action calls.

The delivery itself is `sentry_gitea.alerts.delivery.deliver`, shared with the bridge that
replaces this plugin on Sentry 26.7 and newer, where this module cannot be imported.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sentry.exceptions import PluginError
from sentry.plugins.base import plugins
from sentry.plugins.bases.notify import NotificationPlugin

from sentry_gitea import __version__
from sentry_gitea.alerts import core, delivery
from sentry_gitea.alerts.delivery import (  # noqa: F401  (re-exported: the names this module had)
    LOCK_WAIT,
    REFILE_GRACE,
    Recurrence,
    integration_linked,
    recurrence,
)
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.plugins.issue import (
    FreshOptionsMixin,
    GiteaIssuePlugin,
    refresh_project_options,
)

if TYPE_CHECKING:
    from sentry.models.group import Group
    from sentry.models.project import Project

logger = logging.getLogger(__name__)

DEFAULTS = core.ALERT_DEFAULTS
BOOL_OPTIONS = core.ALERT_BOOL_OPTIONS


class GiteaAlertsPlugin(FreshOptionsMixin, NotificationPlugin):
    slug = conf_key = "gitea_alerts"
    title = "Gitea Alerts"
    description = "File Gitea issues and comment on repeats using this project's Gitea plugin."
    version = __version__
    author = AUTHOR
    author_url = PROJECT_URL
    resource_links = [
        ("Source", PROJECT_URL),
        ("Bug Tracker", ISSUES_URL),
        ("Gitea", "https://about.gitea.com/"),
    ]

    @property
    def issue_plugin(self) -> GiteaIssuePlugin:
        return plugins.get("gitea")

    def alert_options(self, project: Project) -> dict[str, Any]:
        return {
            key: default if (value := self.get_option(key, project)) is None else value
            for key, default in DEFAULTS.items()
        }

    def is_configured(self, project: Project | None) -> bool:
        if (
            project is None
            or not self.is_enabled(project)
            or not self.issue_plugin.is_enabled(project)
        ):
            return False
        if not self.issue_plugin.is_configured(project):
            return False
        try:
            self.validate_options(self.alert_options(project))
        except ValueError:
            return False
        return True

    def get_config(
        self,
        project: Project,
        user: Any = None,
        initial: Any = None,
        add_additional_fields: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": "labels",
                "label": "Alert labels",
                "type": "text",
                "required": False,
                "default": DEFAULTS["labels"],
                "help": "Comma-separated labels for new alert issues.",
            },
            {
                "name": "severity_labels",
                "label": "Severity and class labels",
                "type": "bool",
                "required": False,
                "default": True,
            },
            {
                "name": "comment_on_repeat",
                "label": "Comment on repeat events",
                "type": "bool",
                "required": False,
                "default": True,
            },
            {
                "name": "fix_hint",
                "label": "Advisory where-to-look hint",
                "type": "bool",
                "required": False,
                "default": False,
                "help": "Optional bounded comparison of the release with the default branch.",
            },
            {
                "name": "refile_on_regression",
                "label": "New issue when a closed issue regresses",
                "type": "bool",
                "required": False,
                "default": True,
                "help": "If the linked Gitea issue is closed and Sentry marked the issue as "
                "regressed after that, file a new issue that references the closed one.",
            },
            {
                "name": "refile_after_close",
                "label": "New issue for any event an hour after closing",
                "type": "bool",
                "required": False,
                "default": False,
                "help": "Also file a new issue when the Sentry issue was never resolved: any "
                "alert more than an hour after the linked Gitea issue was closed.",
            },
        ]

    @staticmethod
    def validate_options(values: dict[str, Any]) -> dict[str, Any]:
        return core.validate_alert_options(values)

    def validate_config(
        self, project: Project, config: dict[str, Any], actor: Any = None
    ) -> dict[str, Any]:
        if not self.issue_plugin.is_enabled(project) or not self.issue_plugin.is_configured(
            project
        ):
            raise PluginError("Enable and configure the Gitea issue plugin first.")
        try:
            values = self.alert_options(project)
            values.update(
                {
                    key: value
                    for key, value in config.items()
                    if key in DEFAULTS and value is not None
                }
            )
            return self.validate_options(values)
        except ValueError as error:
            raise PluginError(str(error)) from None

    @staticmethod
    def log_failure(group: Group, status: int, error: BaseException | None = None) -> None:
        delivery.log_failure(group, status, error)

    def should_notify(self, group: Group, event: Any) -> bool:
        # Called by the rule action before rule_notify; reads options via is_configured.
        refresh_project_options()
        return super().should_notify(group, event)

    def notify_users(self, group: Group, event: Any, triggering_rules: list[str]) -> None:
        delivery.deliver(group, event)

    def test_configuration(self, project: Project) -> None:
        refresh_project_options()
        if not self.is_configured(project):
            raise PluginError("Enable and configure both Gitea plugins first.")
        core.check_connection(project)
