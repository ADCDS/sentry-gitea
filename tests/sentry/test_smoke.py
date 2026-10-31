from types import SimpleNamespace

import pytest
from django.test import Client, RequestFactory, override_settings
from django.urls import resolve
from sentry.api.serializers import serialize
from sentry.core.endpoints.organization_details import OrganizationDetailsEndpoint
from sentry.integrations.api.serializers.models.integration import IntegrationProviderSerializer
from sentry.integrations.manager import default_manager as integrations
from sentry.plugins.base import plugins

from sentry_gitea import __version__
from sentry_gitea.integration.provider import GiteaIntegrationProvider
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.plugins.alerts import GiteaAlertsPlugin
from sentry_gitea.plugins.issue import GiteaIssuePlugin


@pytest.mark.parametrize(
    ("slug", "plugin_class"), [("gitea", GiteaIssuePlugin), ("gitea_alerts", GiteaAlertsPlugin)]
)
def test_plugins_discovered_from_installed_entry_points(slug, plugin_class) -> None:
    plugin = plugins.get(slug)
    assert isinstance(plugin, plugin_class)
    assert plugin.version == __version__ == "0.2.0"
    assert (plugin.author, plugin.author_url) == (AUTHOR, PROJECT_URL)
    assert ("Source", PROJECT_URL) in plugin.resource_links
    assert ("Bug Tracker", ISSUES_URL) in plugin.resource_links
    assert plugin.is_configured(None) is False
    assert plugin.get_conf_key() == slug


@pytest.mark.django_db(databases="__all__")
def test_provider_registered_from_settings_and_serializable() -> None:
    provider = integrations.get("gitea")
    assert isinstance(provider, GiteaIntegrationProvider)
    assert "gitea" in {item.key for item in integrations.all()}
    data = serialize(
        provider,
        serializer=IntegrationProviderSerializer(),
        organization=SimpleNamespace(slug="e2e"),
    )
    assert data["key"] == "gitea"
    assert data["name"] == "Gitea"
    assert data["canAdd"] is True
    assert data["metadata"]["description"]
    assert data["metadata"]["source_url"] == PROJECT_URL
    assert data["metadata"]["issue_url"] == ISSUES_URL
    assert data["metadata"]["author"] == AUTHOR


@pytest.mark.django_db(databases="__all__")
@override_settings(ROOT_URLCONF="sentry_gitea.urls")
def test_ping_without_authentication() -> None:
    response = Client().get("/extensions/gitea/ping/")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": __version__}


@override_settings(ROOT_URLCONF="sentry_gitea.urls")
def test_ping_rejects_post() -> None:
    request = RequestFactory().post("/extensions/gitea/ping/")
    assert resolve(request.path).func(request).status_code == 405


@override_settings(ROOT_URLCONF="sentry_gitea.urls")
def test_core_sentry_route_is_preserved() -> None:
    match = resolve("/api/0/organizations/e2e/")
    assert match.url_name == "sentry-api-0-organization-details"
    assert match.func.view_class is OrganizationDetailsEndpoint
