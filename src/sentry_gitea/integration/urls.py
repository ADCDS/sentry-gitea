from django.urls import re_path

from .search import GiteaSearchEndpoint
from .webhooks import GiteaWebhookEndpoint

urlpatterns = [
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
