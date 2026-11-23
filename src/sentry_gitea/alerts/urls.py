from django.urls import re_path

from .pages import ProjectSettingsView, SettingsIndexView
from .webhook import alerts_webhook

urlpatterns = [
    # Signed Sentry App webhook (see webhook.py): the fallback when the bridge is not used.
    # Both spellings: Sentry does not follow the redirect Django would answer to the other.
    re_path(r"^alerts/?$", alerts_webhook, name="sentry-gitea-alerts-webhook"),
    re_path(
        r"^settings/(?P<organization_slug>[^/]+)/$",
        SettingsIndexView.as_view(),
        name="sentry-gitea-settings",
    ),
    re_path(
        r"^settings/(?P<organization_slug>[^/]+)/(?P<project_id_or_slug>[^/]+)/$",
        ProjectSettingsView.as_view(),
        name="sentry-gitea-project-settings",
    ),
]
