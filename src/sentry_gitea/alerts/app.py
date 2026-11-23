"""The internal "Gitea Alerts" Sentry App.

A Sentry without legacy plugins no longer offers "Gitea Alerts" in the alert-rule editor. An
internal Sentry App that is alertable does: it appears there as "Send a notification via
Gitea Alerts", so rules can be created and edited again. Its webhook URL is this package's
signed endpoint (`webhook.py`).

List the app's slug in `GITEA_ALERTS_WEBHOOK_TARGETS` and the bridge takes its alerts itself,
without the webhook ever being sent; without that setting Sentry posts each alert to the
endpoint. Either way the same task delivers it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from sentry import options
from sentry.sentry_apps.logic import SentryAppCreator
from sentry.sentry_apps.models.sentry_app import SentryApp

from sentry_gitea.meta import AUTHOR

from .webhook import PATH, posts_here

APP_NAME = "Gitea Alerts"


def webhook_url() -> str:
    """Where Sentry posts the app's alerts: this Sentry, as its own workers reach it.

    `GITEA_ALERTS_WEBHOOK_URL` overrides the default, Sentry's public URL plus the endpoint
    path, for installations whose workers cannot reach that URL.
    """
    explicit = getattr(settings, "GITEA_ALERTS_WEBHOOK_URL", None)
    url = str(explicit or str(options.get("system.url-prefix") or "").rstrip("/") + PATH)
    try:
        parts = urlsplit(url)
        usable = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        parts, usable = None, False
    if not usable or parts is None:
        # Sentry would store a relative URL as given and never deliver to it.
        raise ValueError(
            "No absolute URL for the app's webhook: set Sentry's system.url-prefix, or "
            f"GITEA_ALERTS_WEBHOOK_URL to <how workers reach Sentry>{PATH}"
        )
    if parts.path.rstrip("/") != PATH.rstrip("/") or parts.query or parts.fragment:
        raise ValueError(f"GITEA_ALERTS_WEBHOOK_URL must end in {PATH}")
    return url


def find_app(organization_id: int) -> SentryApp | None:
    """This organization's internal app that posts to our endpoint, if one exists."""
    for app in SentryApp.objects.filter(owner_id=organization_id, is_alertable=True).order_by("id"):
        if posts_here(app):
            return app
    return None


def ensure_app(organization_id: int, user: Any) -> tuple[SentryApp, bool]:
    """Return the organization's Gitea Alerts app, creating it when missing.

    Raises ValueError when no usable webhook URL can be derived (see `webhook_url`).
    """
    existing = find_app(organization_id)
    if existing is not None:
        return existing, False
    app = SentryAppCreator(
        name=APP_NAME,
        author=AUTHOR,
        organization_id=organization_id,
        is_internal=True,
        verify_install=False,
        is_alertable=True,
        webhook_url=webhook_url(),
        # It only receives alerts; it reads nothing through Sentry's API.
        scopes=[],
        events=[],
        overview="Files and updates Gitea issues from alert rules (sentry-gitea).",
    ).run(user=user, request=None, skip_default_auth_token=True)
    return app, True
