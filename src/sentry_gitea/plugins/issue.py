"""Legacy issue plugin: the settings page and the manual create/link/unlink views.

Sentry's base views own manual link persistence. Their entire mutation is wrapped
below; lower-level hooks deliberately do not acquire the non-reentrant group lock.

Settings, links, locking and the Gitea client live in `sentry_gitea.alerts.core`, which does
not need the legacy plugin classes; this module only adapts them to `IssuePlugin2`. It cannot
be imported on Sentry 26.7 or newer, where that base class no longer exists.
"""

from __future__ import annotations

import logging
import time  # noqa: F401  (kept importable from here)
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from rest_framework.response import Response
from sentry.exceptions import PluginError
from sentry.locks import locks  # noqa: F401  (tests reach the shared lock manager through here)
from sentry.plugins.bases.issue2 import IssuePlugin2
from sentry.utils.http import absolute_uri
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea import __version__
from sentry_gitea.alerts import core
from sentry_gitea.alerts.core import (  # noqa: F401  (re-exported: the names this module had)
    BLOCKED_ADDRESS,
    LOCK_RETRY_DELAY,
    TOKEN_REENTRY,
    blocked_address_error,
    group_lock,
    link_transaction,
    plugin_allowed_origins,
    plugin_error,
    plugin_session,
    refresh_project_options,
    same_issue,
)
from sentry_gitea.api.client import GiteaClient, issue_number, normalize_base_url, normalize_repo
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.render import EventData, clean_text, parse_label_names, render_body, render_title

if TYPE_CHECKING:
    from rest_framework.request import Request
    from sentry.models.group import Group
    from sentry.models.project import Project

logger = logging.getLogger(__name__)


class FreshOptionsMixin:
    """Read the enabled flag from current project options (see refresh_project_options).

    The alert rule action checks ``is_enabled`` before ``should_notify``, so a refresh there
    alone would leave an operator's "enable" invisible to a long-lived task worker.
    """

    def is_enabled(self, project: Any = None) -> bool:
        if project is not None:
            refresh_project_options(project)
        return super().is_enabled(project)  # type: ignore[misc]


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
    issue_fields = frozenset(core.ISSUE_FIELDS)

    def configuration(self, project: Project) -> dict[str, str]:
        return core.configuration(project)

    def is_configured(self, project: Project | None) -> bool:
        return core.is_configured(project)

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
        with core.api_errors(project, group):
            yield

    def validate_config(
        self, project: Project, config: dict[str, Any], actor: Any = None
    ) -> dict[str, Any]:
        return core.validate_issue_settings(project, config)

    @staticmethod
    def new_client(base_url: str, token: str, repo: str, **kwargs: Any) -> GiteaClient:
        """A client whose session applies the plugin SSRF guard (see plugin_session)."""
        return core.new_client(base_url, token, repo, **kwargs)

    def get_client(
        self, project: Project, *, on_recovered_error: Callable[[int], None] | None = None
    ) -> GiteaClient:
        return core.get_client(project, on_recovered_error=on_recovered_error)

    @staticmethod
    def check_repository(repo: dict[str, Any]) -> None:
        core.check_repository(repo)

    def event_data(self, group: Group, event: Any) -> EventData:
        return core.event_data(group, event)

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
        return core.read_link(group)

    def write_link(self, group: Group, link: dict[str, str]) -> None:
        core.write_link(group, link)

    def assert_binding(
        self, project: Project, link: dict[str, str], repo: dict[str, Any] | None = None
    ) -> None:
        core.assert_binding(project, link, repo)

    def remote_link(
        self, project: Project, client: GiteaClient, repo: dict[str, Any], issue: dict[str, Any]
    ) -> dict[str, str]:
        return core.remote_link(project, client, repo, issue)

    def advisory_hint(
        self,
        client: GiteaClient,
        repo: dict[str, Any],
        data: EventData,
        on_error: Callable[[int], None],
    ) -> str:
        return core.advisory_hint(client, repo, data, on_error)

    def file_issue(
        self,
        group: Group,
        data: EventData,
        client: GiteaClient,
        repo: dict[str, Any],
        **kwargs: Any,
    ) -> tuple[dict[str, str], bool]:
        """Caller holds the group lock through its subsequent local link persistence."""
        return core.file_issue(group, data, client, repo, **kwargs)

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
