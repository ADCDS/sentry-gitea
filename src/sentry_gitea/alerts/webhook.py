"""Fallback entry point: the same delivery behind a signed Sentry App webhook.

The bridge (`bridge.py`) replaces a handler inside Sentry's workflow engine. If a future Sentry
moves that seam, or an operator turns the bridge off, alert rules can instead name an internal
Sentry App whose webhook URL is this endpoint (`app.py` creates it). Sentry then signs each
alert with the app's client secret and posts it here; a valid delivery is queued as
`tasks.deliver_alert`.

The endpoint is unauthenticated by design, like every webhook receiver. A request is accepted
only when the installation it names belongs to an internal app that is configured to post here,
its signature verifies against that app's secret, and the project it names belongs to the
installation's organization. Nothing in the body is acted on before the signature has been
checked. Sentry signs the body only, so a captured request can be replayed; all a replay can do
is deliver the same alert again (one more comment on the linked issue, within the per-project
limit).
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from sentry.models.project import Project
from sentry.sentry_apps.models.sentry_app_installation import SentryAppInstallation

from .tasks import deliver_alert

logger = logging.getLogger("sentry_gitea.plugins.alerts")
PATH = "/extensions/gitea/alerts/"
# An alert payload carries one event. Sentry caps events far below this.
MAX_BODY = 4 * 1024 * 1024
UUID = re.compile(r"[0-9a-fA-F-]{8,64}")


def refuse(status: int, reason: str) -> HttpResponse:
    logger.warning("sentry-gitea: alert webhook refused", extra={"reason": reason})
    return JsonResponse({"detail": "Not accepted."}, status=status)


def posts_here(app: Any) -> bool:
    """Whether a Sentry App is one of ours: internal, with this endpoint as its webhook URL.

    Any other app installed in the organization also has a secret that would produce a valid
    signature; its holder must not be able to file issues through this endpoint.
    """
    try:
        path = urlsplit(app.webhook_url or "").path
    except ValueError:
        return False
    return bool(app.is_internal) and path.rstrip("/") == PATH.rstrip("/")


def _ids(event: Any) -> tuple[int, int, str | None]:
    project_id = int(event["project"])
    group_id = int(event["issue_id"])
    event_id = event.get("event_id")
    return project_id, group_id, str(event_id) if event_id else None


@csrf_exempt
@require_POST
def alerts_webhook(request: HttpRequest) -> HttpResponse:
    length = request.META.get("CONTENT_LENGTH") or "0"
    if not length.isdigit() or int(length) > MAX_BODY:
        return refuse(413, "body too large")
    raw = request.body
    signature = request.headers.get("Sentry-Hook-Signature") or ""
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
        uuid = payload["installation"]["uuid"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, RecursionError):
        return refuse(400, "malformed body")
    # Nothing unvalidated reaches the database before the signature is checked.
    if not isinstance(uuid, str) or not UUID.fullmatch(uuid) or not signature:
        return refuse(401, "missing signature or installation")
    install = SentryAppInstallation.objects.filter(uuid=uuid).select_related("sentry_app").first()
    if install is None or not posts_here(install.sentry_app):
        return refuse(401, "unknown installation")
    expected = install.sentry_app.build_signature(text)
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return refuse(401, "bad signature")
    # Authenticated from here on.
    if request.headers.get("Sentry-Hook-Resource") != "event_alert":
        return JsonResponse({"detail": "Ignored: not an alert."}, status=202)
    try:
        project_id, group_id, event_id = _ids(payload["data"]["event"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return refuse(400, "no event in the alert")
    if not Project.objects.filter(id=project_id, organization_id=install.organization_id).exists():
        return refuse(404, "project outside the installation's organization")
    deliver_alert.delay(install.organization_id, project_id, group_id, event_id)
    return JsonResponse({"detail": "Queued."}, status=202)
