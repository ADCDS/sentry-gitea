"""Alert filing using the issue plugin's credential binding and shared link."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sentry.exceptions import PluginError, RestrictedIPAddress
from sentry.plugins.base import plugins
from sentry.plugins.bases.notify import NotificationPlugin
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea import __version__
from sentry_gitea.api.errors import GiteaApiError
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.plugins.issue import (
    FreshOptionsMixin,
    GiteaIssuePlugin,
    blocked_address_error,
    group_lock,
    plugin_error,
    refresh_project_options,
)
from sentry_gitea.render import (
    map_class,
    map_severity,
    parse_label_names,
    render_body,
    render_comment,
    render_title,
)

if TYPE_CHECKING:
    from sentry.models.group import Group
    from sentry.models.project import Project

logger = logging.getLogger(__name__)

# Alerts wait briefly for a concurrent link operation on the same group, then skip.
LOCK_WAIT = 10.0

DEFAULTS = {
    "labels": "source:sentry",
    "severity_labels": True,
    "comment_on_repeat": True,
    "fix_hint": False,
}


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
        ]

    @staticmethod
    def validate_options(values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        values["labels"] = ", ".join(parse_label_names(values["labels"]))
        for key in ("severity_labels", "comment_on_repeat", "fix_hint"):
            if type(values[key]) is not bool:
                raise ValueError(f"{key} must be a boolean.")
        return values

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

    def should_notify(self, group: Group, event: Any) -> bool:
        # Called by the rule action before rule_notify; reads options via is_configured.
        refresh_project_options()
        return super().should_notify(group, event)

    def notify_users(self, group: Group, event: Any, triggering_rules: list[str]) -> None:
        refresh_project_options()
        issue = self.issue_plugin
        try:
            if not self.is_configured(group.project):
                raise PluginError(
                    "Enable and configure both Gitea plugins before delivering alerts."
                )
            config = self.alert_options(group.project)
            with group_lock(group, wait=LOCK_WAIT):
                link = issue.read_link(group)
                if link:
                    issue.assert_binding(group.project, link)
                with issue.get_client(
                    group.project, on_recovered_error=lambda status: self.log_failure(group, status)
                ) as client:
                    repo = client.get_repo()
                    issue.check_repository(repo)
                    if link:
                        issue.assert_binding(group.project, link, repo)
                        remote = client.get_issue(link["id"])
                        if remote.get("state") == "closed" or not config["comment_on_repeat"]:
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
                                },
                            )
                            return
                        if remote.get("state") != "open":
                            raise GiteaApiError(200, "Gitea returned an unknown issue state.")
                        client.create_comment(
                            link["id"], render_comment(issue.event_data(group, event))
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
                    data = issue.event_data(group, event)
                    labels = parse_label_names(issue.get_option("default_labels", group.project))
                    labels += parse_label_names(config["labels"])
                    if config["severity_labels"]:
                        labels += [
                            f"severity:{map_severity(data.level, data.handled)}",
                            f"class:{map_class(data.handled)}",
                        ]
                    link, created = issue.file_issue(
                        group,
                        data,
                        client,
                        repo,
                        title=render_title(data),
                        body=render_body(data),
                        labels=parse_label_names(labels),
                        fix_hint=config["fix_hint"],
                        on_hint_error=lambda status: self.log_failure(group, status),
                    )
                    issue.write_link(group, link)
                    logger.info(
                        "sentry-gitea: filed issue",
                        extra={
                            "project_id": group.project_id,
                            "group_id": group.id,
                            "issue_id": link["id"],
                            "recovered": not created,
                        },
                    )
        except UnableToAcquireLock:
            # Not retried later: the next alert for this group delivers normally.
            logger.warning(
                "sentry-gitea: alert skipped; group lock busy",
                extra={"project_id": group.project_id, "group_id": group.id, "waited": LOCK_WAIT},
            )
        except RestrictedIPAddress as error:
            self.log_failure(group, 0, error)
            raise blocked_address_error() from None
        except GiteaApiError as error:
            self.log_failure(group, error.status, error)
            raise plugin_error(error) from None
        except PluginError as error:
            self.log_failure(group, getattr(error, "status", 0), error)
            raise
        except Exception as error:
            self.log_failure(group, 0, error)
            raise PluginError(
                "Unable to deliver the Gitea alert or save its link. Retry to recover "
                "a created issue using its metadata marker."
            ) from None

    def test_configuration(self, project: Project) -> None:
        refresh_project_options()
        if not self.is_configured(project):
            raise PluginError("Enable and configure both Gitea plugins first.")
        with self.issue_plugin.api_errors(project), self.issue_plugin.get_client(project) as client:
            client.get_current_user()
            self.issue_plugin.check_repository(client.get_repo())
