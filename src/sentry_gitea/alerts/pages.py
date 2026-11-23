"""Project settings pages for the Gitea issue and alert settings.

Sentry 26.7 removed legacy plugins and, with them, the two settings pages under
Project Settings > Legacy Integrations. The settings themselves are still read on every alert,
so without a replacement nobody could point a project at another repository, rotate its token
or turn alerts off. These pages edit the same project options the plugin pages wrote, with the
same checks and the same permission (`project:write` or `project:admin` on the project, team
roles included), and they work on every supported Sentry.

- `/extensions/gitea/settings/<organization>/`            the projects you may configure
- `/extensions/gitea/settings/<organization>/<project>/`  one project's settings

They are plain server-rendered forms: no script, nothing from Sentry's frontend bundle. The
personal access token is write-only. It is never sent back to the browser, and a blank field
keeps the saved one, but only while the base URL stays the same.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import router, transaction
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.template import engines
from django.urls import reverse
from sentry import audit_log
from sentry.constants import ObjectStatus
from sentry.exceptions import PluginError
from sentry.models.options.project_option import ProjectOption
from sentry.models.project import Project
from sentry.utils.audit import create_audit_entry
from sentry.web.frontend import base as sentry_views
from sentry.web.frontend.base import OrganizationView, ProjectView

from sentry_gitea import __version__
from sentry_gitea.meta import PROJECT_URL

from . import core
from .page_templates import INDEX_TEMPLATE, PROJECT_TEMPLATE, STYLE

logger = logging.getLogger("sentry_gitea.plugins.issue")
# What Sentry's project settings API asks for, and so what the legacy plugin pages asked for.
SCOPES = ("project:write", "project:admin")


def may_configure(request: Any, project: Project) -> bool:
    """Whether the user may change this project's settings.

    `has_any_project_scope` honours organization roles and team roles alike. Sentry's own
    `ProjectView` check would not do: it wants the scope from the organization role as well,
    which shuts out a team admin, and it fails for a project that has no team.
    """
    return bool(request.access.has_any_project_scope(project, SCOPES))


ISSUE_TEXT_FIELDS = ("base_url", "public_url", "repo", "default_labels")
# Sentry names the decorator for its cell (region) silo differently across releases; in a
# self-hosted monolith it does nothing either way.
_silo = getattr(sentry_views, "cell_silo_view", None) or getattr(
    sentry_views, "region_silo_view", lambda view: view
)


def render(request: Any, template: str, context: dict[str, Any], status: int = 200) -> HttpResponse:
    body = (
        engines["django"]
        .from_string(template)
        .render(
            {**context, "style": STYLE, "version": __version__, "source_url": PROJECT_URL},
            request,
        )
    )
    response = HttpResponse(body, status=status)
    # The form holds a project's connection settings; no cache or history copy of it.
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    return response


def stored(project: Project) -> dict[str, Any]:
    """The saved settings as form values. The token is reduced to whether one exists."""
    core.refresh_project_options(project)
    form: dict[str, Any] = {
        name: core.option(project, core.ISSUE_SLUG, name) or "" for name in ISSUE_TEXT_FIELDS
    }
    form["token_saved"] = bool(core.option(project, core.ISSUE_SLUG, "token"))
    form["issue_enabled"] = core.enabled(project, core.ISSUE_SLUG)
    form["alerts_enabled"] = core.enabled(project, core.ALERTS_SLUG)
    form.update(core.alert_options(project))
    return form


def submitted(project: Project, post: Any) -> dict[str, Any]:
    form: dict[str, Any] = {name: (post.get(name) or "").strip() for name in ISSUE_TEXT_FIELDS}
    form["token_saved"] = bool(core.option(project, core.ISSUE_SLUG, "token"))
    form["issue_enabled"] = "issue_enabled" in post
    form["alerts_enabled"] = "alerts_enabled" in post
    form["labels"] = (post.get("labels") or "").strip()
    for name in core.ALERT_BOOL_OPTIONS:
        form[name] = name in post
    return form


def save(project: Project, form: dict[str, Any], token: str) -> None:
    """Validate the form and store it. Raises PluginError with a message safe to show."""
    if form["alerts_enabled"] and not form["issue_enabled"]:
        raise PluginError("Enable the issue settings first: alerts are filed through them.")
    try:
        alert_values = core.validate_alert_options(
            {name: form[name] for name in core.ALERT_DEFAULTS}
        )
    except ValueError as error:
        raise PluginError(str(error)) from None
    issue_values = None
    if form["issue_enabled"]:
        candidate = {name: form[name] for name in ISSUE_TEXT_FIELDS}
        candidate["token"] = token
        # Includes a live check: the token's user and the repository must answer.
        issue_values = core.validate_issue_settings(project, candidate)
    with transaction.atomic(using=router.db_for_write(ProjectOption)):
        if issue_values is not None:
            core.save_options(project, core.ISSUE_SLUG, issue_values)
        core.save_options(project, core.ISSUE_SLUG, {"enabled": form["issue_enabled"]})
        core.save_options(
            project, core.ALERTS_SLUG, {**alert_values, "enabled": form["alerts_enabled"]}
        )


@_silo
class ProjectSettingsView(ProjectView):
    required_scope = None  # decided in has_permission

    def has_permission(
        self, request: Any, organization: Any, project: Any, *args: Any, **kwargs: Any
    ) -> bool:  # type: ignore[override]
        if project is None:
            return False
        # Membership of the organization and its single sign-on rule, from Sentry's view.
        if not super(ProjectView, self).has_permission(request, organization):
            return False
        return may_configure(request, project)

    @staticmethod
    def urls(organization: Any, project: Project) -> dict[str, str]:
        return {
            "page_url": reverse(
                "sentry-gitea-project-settings",
                kwargs={"organization_slug": organization.slug, "project_id_or_slug": project.slug},
            ),
            "index_url": reverse(
                "sentry-gitea-settings", kwargs={"organization_slug": organization.slug}
            ),
        }

    def page(self, request: Any, organization: Any, project: Project, **context: Any) -> Any:
        status = context.pop("status", 200)
        context.setdefault("form", stored(project))
        return render(
            request,
            PROJECT_TEMPLATE,
            {"organization": organization, "project": project, **self.urls(organization, project)}
            | context,
            status,
        )

    def handle(self, request: Any, organization: Any, project: Project, **kwargs: Any) -> Any:
        if request.method == "GET":
            return self.page(
                request,
                organization,
                project,
                saved=request.GET.get("saved") == "1",
                tested=request.GET.get("tested") == "1",
                deleted=request.GET.get("deleted") == "1",
                unlinked=request.GET.get("unlinked")
                if request.GET.get("unlinked", "").isdigit()
                else None,
            )
        if request.method != "POST":
            return HttpResponseNotAllowed(["GET", "POST"])
        page_url = self.urls(organization, project)["page_url"]
        if request.POST.get("action") == "test":
            core.refresh_project_options(project)
            try:
                if not core.is_configured(project):
                    raise PluginError("Save a base URL, a repository and a token first.")
                core.check_connection(project)
            except PluginError as error:
                return self.page(request, organization, project, error=str(error), status=400)
            return HttpResponseRedirect(page_url + "?tested=1")
        if request.POST.get("action") == "unlink":
            if "confirm" not in request.POST:
                return self.page(
                    request,
                    organization,
                    project,
                    error="Tick the box to confirm forgetting the issue links.",
                    status=400,
                )
            cleared = core.clear_links(project)
            logger.info(
                "sentry-gitea: project issue links cleared",
                extra={"project_id": project.id, "user_id": request.user.id, "issues": cleared},
            )
            self.audit(request, organization, project, links_cleared=cleared)
            return HttpResponseRedirect(f"{page_url}?unlinked={cleared}")
        if request.POST.get("action") == "delete":
            if "confirm" not in request.POST:
                return self.page(
                    request,
                    organization,
                    project,
                    error="Tick the box to confirm deleting the saved settings.",
                    status=400,
                )
            removed = core.delete_options(project)
            logger.info(
                "sentry-gitea: project settings deleted",
                extra={"project_id": project.id, "user_id": request.user.id, "options": removed},
            )
            self.audit(request, organization, project, deleted=True)
            return HttpResponseRedirect(page_url + "?deleted=1")
        form = submitted(project, request.POST)
        try:
            save(project, form, request.POST.get("token") or "")
        except PluginError as error:
            # Shown with what was typed, except the token, which is never echoed.
            return self.page(
                request, organization, project, form=form, error=str(error), status=400
            )
        logger.info(
            "sentry-gitea: project settings saved",
            extra={
                "project_id": project.id,
                "user_id": request.user.id,
                "issues_enabled": form["issue_enabled"],
                "alerts_enabled": form["alerts_enabled"],
            },
        )
        self.audit(request, organization, project)
        return HttpResponseRedirect(page_url + "?saved=1")

    @staticmethod
    def audit(request: Any, organization: Any, project: Project, **data: Any) -> None:
        try:
            # The entry Sentry wrote when a legacy plugin's settings were saved.
            create_audit_entry(
                request=request,
                organization=organization,
                target_object=project.id,
                event=audit_log.get_event_id("INTEGRATION_EDIT"),
                data={"integration": core.ISSUE_SLUG, "project": project.slug, **data},
            )
        except Exception as error:
            logger.error(
                "sentry-gitea: settings changed but the audit entry failed",
                extra={"project_id": project.id, "error_type": type(error).__name__},
            )


@_silo
class SettingsIndexView(OrganizationView):
    required_scope = "org:read"

    def handle(self, request: Any, organization: Any, **kwargs: Any) -> Any:
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        rows = []
        projects = Project.objects.filter(
            organization_id=organization.id, status=ObjectStatus.ACTIVE
        ).order_by("slug")
        for project in projects:
            # The same test as the page the row links to.
            if not may_configure(request, project):
                continue
            rows.append(
                {
                    "slug": project.slug,
                    "url": ProjectSettingsView.urls(organization, project)["page_url"],
                    "repo": core.option(project, core.ISSUE_SLUG, "repo") or "",
                    "issues": "on" if core.enabled(project, core.ISSUE_SLUG) else "off",
                    "alerts": "on" if core.enabled(project, core.ALERTS_SLUG) else "off",
                }
            )
        return render(request, INDEX_TEMPLATE, {"organization": organization, "rows": rows})
