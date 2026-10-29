import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import responses
from django.test import RequestFactory, override_settings
from sentry.shared_integrations.exceptions import ApiError, IntegrationError
from sentry.testutils.silo import no_silo_test
from test_integration_provider import (
    API,
    BASE,
    PUBLIC,
    REFRESH,
    TOKEN,
    TOKENS,
    USER,
    FakePipeline,
    IntegrationFixture,
)

from sentry_gitea.api.client import TIMEOUT
from sentry_gitea.integration.identity import (
    GiteaIdentityProvider,
    OAuthAuthorizeView,
    OAuthCallbackView,
)
from sentry_gitea.integration.utils import OAUTH_SCOPES


@no_silo_test
class TestIntegrationIdentity(IntegrationFixture):
    def authorize(self):
        pipeline = FakePipeline()
        response = OAuthAuthorizeView().dispatch(RequestFactory().get("/"), pipeline)
        assert response.status_code == 302
        query = parse_qs(urlsplit(response["Location"]).query)
        assert response["Location"].startswith(PUBLIC + "/login/oauth/authorize?")
        assert query["scope"] == [" ".join(OAUTH_SCOPES)]
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["state"][0]) >= 32
        verifier = pipeline.fetch_state("code_verifier")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
        assert query["code_challenge"] == [expected.decode()]
        assert verifier not in response["Location"]
        return pipeline, query

    def test_authorization_generates_fresh_state_and_pkce(self):
        _, first = self.authorize()
        _, second = self.authorize()
        assert first["state"] != second["state"]

    def test_invalid_callbacks_fail_closed_with_sanitized_logs(self):
        for params in [
            {"code": "private-code", "state": "wrong-state"},
            {"error": "access_denied", "error_description": "private-code"},
            {},
            {"state": "wrong-state"},
        ]:
            pipeline, query = self.authorize()
            with self.assertLogs("sentry_gitea.integration.identity", level="WARNING") as logs:
                response = OAuthCallbackView().dispatch(
                    RequestFactory().get("/extensions/gitea/setup/", params), pipeline
                )
            assert pipeline.cleared and not pipeline.advanced
            assert response.status_code != 302
            for secret in ["private-code", "wrong-state", query["state"][0]]:
                assert secret not in str(logs.output) + response.content.decode()

    @responses.activate
    def test_code_exchange_normalizes_identity_without_scope_response(self):
        from unittest.mock import patch

        import requests

        pipeline, query = self.authorize()
        responses.post(
            BASE + "/login/oauth/access_token",
            json={"access_token": TOKEN, "refresh_token": REFRESH, "expires_in": 3600},
        )
        responses.get(API + "/user", json=USER)
        original = requests.Session.request
        calls = []

        def record(session, *args, **kwargs):
            calls.append(kwargs)
            return original(session, *args, **kwargs)

        with patch.object(requests.Session, "request", record):
            OAuthCallbackView().dispatch(
                RequestFactory().get(
                    "/extensions/gitea/setup/", {"code": "private-code", "state": query["state"][0]}
                ),
                pipeline,
            )
        assert pipeline.advanced
        assert all(c["timeout"] == TIMEOUT and c["allow_redirects"] is False for c in calls)
        result = GiteaIdentityProvider().build_identity(pipeline.fetch_state())
        assert result["id"] == f"{BASE}|2"
        assert result["data"]["access_token"] == TOKEN
        # The keep-alive task refreshes grants by the time Gitea issued their tokens.
        assert abs(result["data"]["issued_at"] - time.time()) < 60
        assert result["scopes"] == list(OAUTH_SCOPES)
        assert not pipeline.fetch_state("code_verifier")
        assert not pipeline.fetch_state("oauth_state")

    @responses.activate
    def test_read_refreshes_once_and_persists_rotated_credentials(self):
        responses.get(API + "/user", status=401, body=TOKEN)
        responses.post(
            BASE + "/login/oauth/access_token",
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 7200,
            },
        )
        responses.get(API + "/user", json=USER)
        assert self.installation.get_client().get_user() == USER
        self.identity.refresh_from_db()
        assert self.identity.data["access_token"] == "rotated-access"
        assert self.identity.data["refresh_token"] == "rotated-refresh"
        assert responses.calls[-1].request.headers["Authorization"] == "Bearer rotated-access"
        assert len(responses.calls) == 3

    @responses.activate
    def test_stale_identity_refresh_does_not_consume_rotated_token_twice(self):
        provider = GiteaIdentityProvider()
        responses.post(
            BASE + "/login/oauth/access_token",
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 7200,
            },
        )
        assert provider.refresh_identity(self.identity, failed_access_token=TOKEN) is None
        provider.refresh_identity(self.identity, failed_access_token=TOKEN)
        assert len(responses.calls) == 1
        self.identity.refresh_from_db()
        assert self.identity.data["refresh_token"] == "rotated-refresh"

    @responses.activate
    def test_second_401_does_not_refresh_again(self):
        responses.get(API + "/user", status=401)
        responses.post(
            BASE + "/login/oauth/access_token",
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 10,
            },
        )
        with self.assertRaises(ApiError):
            self.installation.get_client().get_user()
        assert len(responses.calls) == 3

    @responses.activate
    def test_write_401_is_never_replayed_and_forbidden_does_not_refresh(self):
        self.mock_repo()
        responses.post(API + "/repos/team/repo/issues", status=401)
        responses.post(
            BASE + "/login/oauth/access_token",
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )
        responses.get(API + "/user", status=403)
        client = self.installation.get_client()
        with self.assertRaises(ApiError):
            client.create_issue("Problem", "body", repo="team/repo")
        with self.assertRaises(ApiError):
            client.get_user()
        assert len(responses.calls) == 4
        assert (
            len(
                [
                    call
                    for call in responses.calls
                    if call.request.method == "POST" and call.request.url.endswith("/issues")
                ]
            )
            == 1
        )
        assert len([call for call in responses.calls if "access_token" in call.request.url]) == 1
        self.identity.refresh_from_db()
        assert self.identity.data["access_token"] == "rotated-access"

    @responses.activate
    def test_refresh_errors_are_sanitized_and_do_not_change_stored_identity(self):
        responses.get(API + "/user", status=401)
        responses.post(
            BASE + "/login/oauth/access_token",
            status=400,
            body=f"{TOKEN} {REFRESH} private-code synthetic-app-secret",
        )
        with self.assertLogs("sentry_gitea.integration.identity", level="WARNING") as logs:
            with self.assertRaises(ApiError):
                self.installation.get_client().get_user()
        self.identity.refresh_from_db()
        assert self.identity.data == TOKENS
        for secret in [TOKEN, REFRESH, "private-code", "synthetic-app-secret"]:
            assert secret not in str(logs.output)

    @responses.activate
    def test_direct_refresh_refuses_a_changed_instance(self):
        with override_settings(GITEA_BASE_URL="https://replacement.example"):
            with self.assertRaises(ApiError):
                GiteaIdentityProvider().refresh_identity(self.identity)
        assert not responses.calls

    @responses.activate
    def test_label_read_can_refresh_without_replaying_label_creation(self):
        self.mock_repo()
        responses.get(API + "/repos/team/repo/labels", status=401)
        responses.post(
            BASE + "/login/oauth/access_token",
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )
        responses.get(API + "/repos/team/repo/labels", json=[])
        responses.post(API + "/repos/team/repo/labels", json={"id": 5, "name": "bug"}, status=201)
        assert self.installation.get_client().resolve_label_ids(["bug"], repo="team/repo") == [5]
        assert (
            len(
                [
                    c
                    for c in responses.calls
                    if c.request.method == "POST" and c.request.url.endswith("/labels")
                ]
            )
            == 1
        )


ROTATED = {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600}
RENEW = "Gitea authorization could not be renewed. Reinstall the integration."


@no_silo_test
class TestProactiveRefresh(IntegrationFixture):
    def expire_in(self, seconds):
        self.identity.update(data={**TOKENS, "expires": int(time.time()) + seconds})

    @responses.activate
    def test_token_expiring_within_five_minutes_is_refreshed_before_use(self):
        self.expire_in(240)
        responses.post(BASE + "/login/oauth/access_token", json=ROTATED)
        responses.get(API + "/user", json=USER)
        client = self.installation.get_client()
        assert client.get_user() == USER
        assert client.get_user() == USER
        assert [c.request.method for c in responses.calls] == ["POST", "GET", "GET"]
        assert all(
            c.request.headers["Authorization"] == "Bearer rotated-access"
            for c in responses.calls[1:]
        )
        self.identity.refresh_from_db()
        assert self.identity.data["refresh_token"] == "rotated-refresh"
        assert self.identity.data["expires"] > time.time() + 3000
        assert abs(self.identity.data["issued_at"] - time.time()) < 60

    @responses.activate
    def test_token_valid_beyond_the_margin_is_used_as_is(self):
        self.expire_in(600)
        responses.get(API + "/user", json=USER)
        assert self.installation.get_client().get_user() == USER
        assert [c.request.method for c in responses.calls] == ["GET"]
        assert responses.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"

    @responses.activate
    def test_expired_refresh_token_asks_for_reinstall_without_using_the_expired_token(self):
        self.expire_in(-30)
        self.mock_repo()
        # Gitea's answer once REFRESH_TOKEN_EXPIRATION_TIME has passed.
        responses.post(
            BASE + "/login/oauth/access_token",
            status=400,
            json={"error": "invalid_grant", "error_description": f"token expired {REFRESH}"},
        )
        with self.assertLogs("sentry_gitea.integration", level="WARNING") as logs:
            with self.assertRaisesMessage(IntegrationError, RENEW):
                self.installation.get_issue("41#7")
        assert [c.request.url for c in responses.calls] == [BASE + "/login/oauth/access_token"]
        messages = [r.getMessage() for r in logs.records]
        assert "sentry-gitea: token refresh failed" in messages
        assert REFRESH not in "\n".join(logs.output) and TOKEN not in "\n".join(logs.output)
        self.identity.refresh_from_db()
        assert self.identity.data["refresh_token"] == REFRESH

    @responses.activate
    def test_failed_refresh_of_a_still_valid_token_keeps_working_without_retrying(self):
        self.expire_in(120)
        responses.post(BASE + "/login/oauth/access_token", status=503)
        responses.get(API + "/user", json=USER)
        client = self.installation.get_client()
        with self.assertLogs("sentry_gitea.integration.identity", level="WARNING"):
            assert client.get_user() == USER
        assert client.get_user() == USER
        responses.replace(responses.GET, API + "/user", status=401)
        with self.assertRaises(ApiError):
            client.get_user()
        # One refresh attempt per operation: later reads and a 401 do not refresh again.
        assert [c.request.method for c in responses.calls] == ["POST", "GET", "GET", "GET"]
        assert all(
            c.request.headers["Authorization"] == f"Bearer {TOKEN}" for c in responses.calls[1:]
        )
