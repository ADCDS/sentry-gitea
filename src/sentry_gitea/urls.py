from django.urls import include, path, re_path

from sentry_gitea.views import ping

urlpatterns = [
    re_path(r"^extensions/gitea/", include([path("ping/", ping, name="sentry-gitea-ping")])),
    re_path(r"^extensions/gitea/", include("sentry_gitea.integration.urls")),
    re_path(r"", include("sentry.conf.urls")),
]
