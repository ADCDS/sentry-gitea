"""The installation pipeline's own entry points.

Sentry 26.7 moved its integrations to pipelines made of API steps, rendered by provider-specific
frontend code that a third-party provider cannot supply. Two things went with that:

- the page that started a view-based pipeline
  (`/organizations/<slug>/integrations/<provider>/setup/`) was removed;
- `IntegrationPipeline.get_pipeline_views()` returns an empty list, so even a started pipeline
  ignores this provider's views and finishes at once with no state.

The engine for view-based pipelines is still there. `GiteaIntegrationPipeline` asks the
provider for its views again, under its own session key, and two views drive it:

- `/extensions/gitea/install/<organization slug>/` starts it, with the requirement the removed
  page had (`org:integrations` in the organization);
- `/extensions/gitea/setup/` continues it. That is the OAuth redirect URI registered in Gitea,
  and this package's URL configuration is consulted before Sentry's, so the address does not
  change. A request that is not ours (a pipeline started by Sentry's own page, on a Sentry that
  still has one) is handed to Sentry's view unchanged.

Both are served on every supported Sentry, so one install path is tested everywhere.
"""

from __future__ import annotations

from typing import Any

from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from sentry import features
from sentry.features.exceptions import FeatureNotRegistered
from sentry.integrations.pipeline import IntegrationPipeline
from sentry.web.frontend.base import BaseView, ControlSiloOrganizationView, control_silo_view

PROVIDER = "gitea"


class GiteaIntegrationPipeline(IntegrationPipeline):
    pipeline_name = "gitea_integration_pipeline"

    def get_pipeline_views(self) -> Any:
        return self.provider.get_pipeline_views()


# Adapted from sentry 24.10.0: src/sentry/integrations/web/organization_integration_setup.py
# (the checks a pipeline start makes: the organization scope, the provider's feature flags,
# `can_add`). Listed in NOTICE.
def _feature_enabled(feature: Any, organization: Any) -> bool:
    """An integration feature counts as enabled unless its flag exists and is off."""
    name = f"organizations:integrations-{feature.value}"
    try:
        features.get(name, None)
    except FeatureNotRegistered:
        return True
    return features.has(name, organization)


@control_silo_view
class GiteaInstallView(ControlSiloOrganizationView):
    required_scope = "org:integrations"

    def handle(self, request: Any, organization: Any, **kwargs: Any) -> Any:
        if request.method != "GET":
            return HttpResponseNotAllowed(["GET"])
        pipeline = GiteaIntegrationPipeline(
            request=request, organization=organization, provider_key=PROVIDER
        )
        provider = pipeline.provider
        if provider is None or not provider.can_add:
            raise Http404
        if not any(_feature_enabled(feature, organization) for feature in provider.features):
            return HttpResponse(
                "No feature of the Gitea integration is enabled for this organization.",
                status=400,
                content_type="text/plain",
            )
        pipeline.initialize()
        return pipeline.current_step()


@control_silo_view
class GiteaSetupView(BaseView):
    # An OAuth redirect: the browser arrives from Gitea, and the steps check the session's
    # user, state and (for the owner form) the CSRF token themselves.
    auth_required = False
    csrf_protect = False

    def handle(self, request: Any, **kwargs: Any) -> Any:
        pipeline = GiteaIntegrationPipeline.get_for_request(request=request)
        if pipeline is not None and pipeline.is_valid():
            return pipeline.current_step()
        from sentry.web.frontend.pipeline_advancer import PipelineAdvancerView

        return PipelineAdvancerView.as_view()(request, provider_id=PROVIDER)
