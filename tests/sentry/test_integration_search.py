import responses
from django.urls import resolve, reverse
from sentry.testutils.silo import no_silo_test
from test_integration_provider import API, REPO, IntegrationFixture

from sentry_gitea.integration.search import GiteaSearchEndpoint


@no_silo_test
class TestIntegrationSearch(IntegrationFixture):
    def endpoint(self, organization=None, integration=None):
        return reverse(
            "sentry-gitea-search",
            args=[organization or self.organization.slug, integration or self.integration.id],
        )

    def test_route_and_generic_setup_not_shadowed(self):
        assert resolve(self.endpoint()).func.view_class is GiteaSearchEndpoint
        assert resolve("/extensions/gitea/setup/").url_name != "sentry-gitea-search"

    @responses.activate
    def test_repo_numeric_and_text_choices(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        result = self.client.get(self.endpoint(), {"field": "repo", "query": "repo"})
        assert result.status_code == 200, result.data
        assert result.data == [{"label": "team/repo", "value": "team/repo"}]
        responses.get(API + "/repos/team/repo/issues/7", json=self.remote_issue())
        responses.get(
            API + "/repos/team/repo/issues",
            json=[self.remote_issue(), self.remote_issue(number=8, pull_request={})],
            headers={"X-Total-Count": "2"},
        )
        for query in ["7", "Remote"]:
            result = self.client.get(
                self.endpoint(str(self.organization.id)),
                {"field": "externalIssue", "repo": "team/repo", "query": query},
            )
            assert result.status_code == 200, result.data
            assert result.data == [{"label": "#7 Remote problem", "value": "41#7"}]

    def test_invalid_parameters(self):
        for params in [
            {},
            {"field": "repo"},
            {"field": "repo", "query": ""},
            {"field": "unknown", "query": "x"},
            {"field": "externalIssue", "query": "7"},
        ]:
            result = self.client.get(self.endpoint(), params)
            assert result.status_code == 400

    @responses.activate
    def test_wrong_owner_is_rejected(self):
        responses.get(
            API + "/repos/other/repo",
            json={**REPO, "full_name": "other/repo", "owner": {"id": 999, "login": "other"}},
        )
        result = self.client.get(
            self.endpoint(), {"field": "externalIssue", "repo": "other/repo", "query": "7"}
        )
        assert result.status_code == 400

    def test_unassociated_org_wrong_provider_nonmember_and_anonymous(self):
        other_org = self.create_organization(owner=self.user)
        params = {"field": "repo", "query": "repo"}
        assert self.client.get(self.endpoint(other_org.slug), params).status_code == 404
        other_integration = self.create_integration(
            organization=self.organization, provider="github", external_id="other-provider"
        )
        assert (
            self.client.get(self.endpoint(integration=other_integration.id), params).status_code
            == 404
        )
        self.login_as(self.create_user())
        assert self.client.get(self.endpoint(), params).status_code in {403, 404}
        self.client.logout()
        assert self.client.get(self.endpoint(), params).status_code in {401, 403}

    @responses.activate
    def test_repo_choices_exclude_code_only_repositories_and_map_upstream_errors(self):
        responses.get(
            API + "/repos/search",
            json={"data": [{**REPO, "has_issues": False}]},
            headers={"X-Total-Count": "1"},
        )
        result = self.client.get(self.endpoint(), {"field": "repo", "query": "repo"})
        assert result.status_code == 200 and result.data == []
        responses.replace(responses.GET, API + "/repos/search", status=502, body="bad gateway")
        result = self.client.get(self.endpoint(), {"field": "repo", "query": "repo"})
        assert result.status_code == 400
        assert "bad gateway" not in str(result.data)
