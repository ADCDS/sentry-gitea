from django.urls import re_path

from .install import GiteaInstallView, GiteaSetupView
from .search import GiteaSearchEndpoint
from .webhooks import GiteaWebhookEndpoint

urlpatterns = [
    # Starts the installation pipeline (see install.py); Sentry 26.7+ has no page that does.
    re_path(
        r"^install/(?P<organization_slug>[^/]+)/$",
        GiteaInstallView.as_view(),
        name="sentry-gitea-install",
    ),
    # Continues it. Same address as Sentry's own advancer for this provider, which our view
    # falls back to; ours is matched first.
    re_path(r"^setup/$", GiteaSetupView.as_view(), name="sentry-gitea-setup"),
    re_path(
        r"^webhook/(?P<integration_id>\d+)/(?P<repo_id>\d+)/$",
        GiteaWebhookEndpoint.as_view(),
        name="sentry-gitea-webhook",
    ),
    # Hooks from before per-repository secrets: answered 404 (logged), so Gitea shows the
    # failure until the repository is added again in Sentry.
    re_path(
        r"^webhook/(?P<integration_id>\d+)/$",
        GiteaWebhookEndpoint.as_view(),
        name="sentry-gitea-webhook-legacy",
    ),
    re_path(
        r"^search/(?P<organization_id_or_slug>[^/]+)/(?P<integration_id>\d+)/$",
        GiteaSearchEndpoint.as_view(),
        name="sentry-gitea-search",
    ),
]
