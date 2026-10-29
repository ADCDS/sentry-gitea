from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from sentry_gitea import __version__


@require_GET
def ping(request: HttpRequest) -> JsonResponse:
    """Public probe for the extension's installation and URL configuration."""
    return JsonResponse({"ok": True, "version": __version__})
