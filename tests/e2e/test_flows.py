"""Host regressions for E2E observations; no running stack or credentials needed."""

import secrets
import subprocess
from collections.abc import Iterator

import flows
import pytest
import requests
import responses
from flows import PluginFlow
from helpers import E2EEnvironment
from redaction import Credential


@pytest.fixture
def flow() -> Iterator[PluginFlow]:
    instance = PluginFlow(
        E2EEnvironment(
            sentry_url="http://sentry.test",
            sentry_token="",
            sentry_org="e2e",
            sentry_project="demo",
            sentry_dsn="",
            gitea_url="http://gitea.test",
            gitea_internal_url="http://gitea:3000",
            gitea_token="",
            gitea_owner="e2e",
            gitea_repo="demo",
        )
    )
    yield instance
    for api in (instance.sentry, instance.gitea, instance.plugin_gitea):
        api.session.close()


@pytest.fixture
def group() -> dict:
    return {
        "id": "5",
        "project": {"id": "2"},
        "permalink": "http://sentry.test/organizations/e2e/issues/5/",
    }


def worker_line(message: str, extras: str) -> str:
    return (
        "taskworker-1 | 16:07:36 [INFO] sentry_gitea.plugins.alerts: "
        f"sentry-gitea: {message} ({extras})"
    )


@responses.activate
def test_issue_comments_are_fetched_once_without_pagination(flow: PluginFlow) -> None:
    comments = [{"id": 1, "body": "Backlink"}, {"id": 2, "body": "Repeat"}]
    responses.get(
        flow.gitea.url + flow.issues_path + "/7/comments",
        json=comments,
        match=[responses.matchers.query_param_matcher({})],
    )
    assert flow.comments(7) == comments
    assert len(responses.calls) == 1


@responses.activate
def test_issue_comments_have_a_size_bound(flow: PluginFlow) -> None:
    responses.get(
        flow.gitea.url + flow.issues_path + "/7/comments",
        json=[{"id": 1, "body": "Comment"}] * 5001,
    )
    with pytest.raises(pytest.fail.Exception, match="comments exceeded 5000"):
        flow.comments(7)


@responses.activate
def test_repository_issues_remain_paginated(flow: PluginFlow, group: dict) -> None:
    matching = {"number": 7, "title": flow.message, "body": "", "state": "closed"}
    batches = [[{"number": 8, "title": "Unrelated", "body": ""}], [matching], []]
    for page, batch in enumerate(batches, start=1):
        responses.get(
            flow.gitea.url + flow.issues_path,
            json=batch,
            match=[
                responses.matchers.query_param_matcher(
                    {"page": page, "limit": 50, "state": "all", "type": "issues"}
                )
            ],
        )
    assert flow.matching_issues(group) == [matching]
    assert len(responses.calls) == 3


@responses.activate
@pytest.mark.parametrize("status", [200, 201])
def test_rule_creation_accepts_both_sentry_paths(flow: PluginFlow, status: int) -> None:
    responses.post(flow.sentry.url + flow.project_path + "/rules/", json={"id": "9"}, status=status)
    flow.replace_rule("specific")
    assert flow.rule_id == "9"


@responses.activate
def test_alert_filing_waits_for_gitea_without_legacy_history(
    flow: PluginFlow, group: dict, monkeypatch
) -> None:
    issue = {"number": 7, "title": flow.message, "body": ""}
    monkeypatch.setattr(flow, "replace_rule", lambda action: None)
    monkeypatch.setattr(flow, "send_and_wait", lambda: group)
    monkeypatch.setattr(flows, "recent_plugin_logs", lambda *args, **kwargs: "")
    monkeypatch.setattr(flows.time, "sleep", lambda seconds: None)
    responses.get(
        flow.gitea.url + flow.issues_path,
        json=[issue],
        match=[
            responses.matchers.query_param_matcher(
                {"page": 1, "limit": 50, "state": "all", "type": "issues"}
            )
        ],
    )
    responses.get(
        flow.gitea.url + flow.issues_path,
        json=[],
        match=[
            responses.matchers.query_param_matcher(
                {"page": 2, "limit": 50, "state": "all", "type": "issues"}
            )
        ],
    )
    group_url = flow.sentry.url + flow.group_path(group)
    # The remote create is visible before Sentry's asynchronous metadata write.
    responses.get(group_url, json={"pluginIssues": [{"slug": "gitea"}]})
    responses.get(
        group_url,
        json={
            "pluginIssues": [{"slug": "gitea", "issue": {"issue_id": "7", "label": "e2e/demo#7"}}]
        },
    )
    assert flow.file_alert("specific") == (group, issue)
    assert sum(call.request.url == group_url for call in responses.calls) == 2
    assert not any("group-history" in call.request.url for call in responses.calls)


def test_closed_skip_requires_matching_group_project_issue_and_reason(
    flow: PluginFlow, group: dict, monkeypatch
) -> None:
    correct = worker_line(
        "skipped repeat event", "project_id=2 group_id=5 issue_id='7' reason='closed'"
    )
    unrelated = [
        correct.replace("group_id=5", "group_id=50"),
        correct.replace("project_id=2", "project_id=20"),
        correct.replace("issue_id='7'", "issue_id='70'"),
        correct.replace("reason='closed'", "reason='closed-other'"),
        correct.replace("skipped repeat event", "commented on issue"),
    ]
    calls = []
    flow.delivery_since = "2026-10-29T16:07:30+00:00"

    def recent_logs(since, **kwargs):
        calls.append((since, kwargs))
        return "\n".join(unrelated + ([correct] if len(calls) > 1 else []))

    monkeypatch.setattr(flows, "recent_plugin_logs", recent_logs)
    monkeypatch.setattr(flows.time, "sleep", lambda seconds: None)
    assert (
        flow.wait_for_plugin_log(group, "skipped repeat event", issue_id="7", reason="closed")
        == correct
    )
    assert len(calls) == 2
    assert all(since == flow.delivery_since for since, _ in calls)
    assert all(kwargs["credentials"] == flow.credentials for _, kwargs in calls)


def test_failed_alert_timeout_includes_only_matching_safe_error_log(
    flow: PluginFlow, group: dict, monkeypatch
) -> None:
    error = worker_line(
        "alert delivery failed",
        "project_id=2 group_id=5 status=0 error_type='GiteaApiError' "
        "error_message='Gitea operation time budget exhausted'",
    )
    unrelated = error.replace("group_id=5", "group_id=50").replace("exhausted", "unrelated")
    monkeypatch.setattr(
        flows, "recent_plugin_logs", lambda *args, **kwargs: unrelated + "\n" + error
    )
    now = [0.0]
    monkeypatch.setattr(flows.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(flows.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    with pytest.raises(pytest.fail.Exception) as failure:
        flow.poll_alert("Gitea issue", group, lambda: [], seconds=2)
    assert error in str(failure.value)
    assert "unrelated" not in str(failure.value)
    assert "Timed out after 2s" in str(failure.value)


def test_alert_diagnostics_never_render_credentials(
    flow: PluginFlow, group: dict, monkeypatch
) -> None:
    credential = secrets.token_hex(20)
    flow.credentials = (Credential("gitea plugin token", credential),)
    leaked = worker_line(
        "alert delivery failed", f"project_id=2 group_id=5 error_message='{credential}'"
    )
    monkeypatch.setattr(flows, "recent_plugin_logs", lambda *args, **kwargs: leaked)
    with pytest.raises(pytest.fail.Exception) as failure:
        flow.poll_alert("Gitea issue", group, lambda: [])
    assert credential not in str(failure.value)
    assert "content withheld" in str(failure.value)


def test_plugin_config_separates_internal_api_and_public_links(flow: PluginFlow) -> None:
    config = flow.issue_config()
    assert config["base_url"] == "http://gitea:3000"
    assert config["public_url"] == flow.env.gitea_url


def test_plugin_link_must_have_public_host_even_if_remote_html_url_is_wrong(
    flow: PluginFlow, group: dict, monkeypatch
) -> None:
    internal = "http://gitea:3000/e2e/demo/issues/7"
    monkeypatch.setattr(
        flow,
        "plugin_issue",
        lambda group: {
            "issue": {"issue_id": "7", "label": "e2e/demo#7", "url": internal},
        },
    )
    monkeypatch.setattr(flow, "issue", lambda number: {"html_url": internal})
    with pytest.raises(AssertionError, match="public"):
        flow.assert_link(group, 7)


@responses.activate
def test_response_leak_names_secret_kind_and_path_status_without_query(flow: PluginFlow) -> None:
    from redaction import Credential

    value = secrets.token_urlsafe(32)
    flow.sentry.credentials = (Credential("sentry token", value),)
    path = "/api/0/organizations/e2e/integrations/"
    responses.get(flow.sentry.url + path, status=500, json={"error": value})
    with pytest.raises(pytest.fail.Exception) as failure:
        flow.sentry.request("GET", path, params={"state": "withheld-query"})
    message = str(failure.value)
    assert "sentry token" in message
    assert f"response GET {path} HTTP 500" in message
    assert value not in message
    assert "withheld-query" not in message


@pytest.mark.parametrize("service", ["web", "taskworker"])
def test_log_leak_reports_kind_service_and_logger_before_filtering(service, monkeypatch) -> None:
    from harness import recent_plugin_logs
    from helpers import recent_oauth_logs
    from redaction import Credential

    value = secrets.token_urlsafe(32)
    output = (
        f"{service}-1 | 2026-10-29T12:00:00.123456789Z [WARNING] sentry.access: warning\n"
        f"{service}-1 | 2026-10-29T12:00:00.123456790Z   continuation with {value}\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=output,
        ),
    )
    credentials = (Credential("oauth state", value),)
    expected = pytest.fail.Exception if service == "web" else RuntimeError
    with pytest.raises(expected) as failure:
        if service == "web":
            recent_oauth_logs("2026-10-29T00:00:00Z", credentials)
        else:
            recent_plugin_logs("2026-10-29T00:00:00Z", credentials=credentials)
    message = str(failure.value)
    assert "oauth state" in message
    assert f"service={service} logger=sentry.access" in message
    assert value not in message
    assert "continuation with" not in message


def test_secret_records_hide_values_and_reject_bare_strings() -> None:
    from redaction import Credential, exposure_message

    value = secrets.token_urlsafe(32)
    credential = Credential("e2e user password", value)
    assert value not in repr(credential)
    with pytest.raises(TypeError, match="labeled credentials"):
        exposure_message("e2e-owner", ("e2e-owner",), where="test response")


class FakeClock:
    """Stands in for flows.time: sleeping advances a deterministic monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(seconds, 0.5)


def test_poll_retries_transient_errors_until_the_probe_succeeds(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr(flows, "time", clock)
    attempts = []

    def probe() -> str:
        attempts.append(clock.now)
        if len(attempts) < 3:
            raise flows.TransientError("GET /api/0/x/ could not connect; response withheld")
        return "ready"

    assert flows.poll("a flaky endpoint", probe, seconds=30) == "ready"
    assert len(attempts) == 3


def test_poll_timeout_names_only_the_last_transient_error(monkeypatch) -> None:
    monkeypatch.setattr(flows, "time", FakeClock())

    def probe() -> None:
        raise flows.TransientError("GET /api/0/x/ could not connect; response withheld")

    with pytest.raises(pytest.fail.Exception) as failure:
        flows.poll("a dead endpoint", probe, seconds=10, failure_detail=lambda: "detail")
    assert str(failure.value) == (
        "Timed out after 10s waiting for a dead endpoint\ndetail\n"
        "Last transient error: GET /api/0/x/ could not connect; response withheld"
    )


def test_poll_still_fails_fast_on_non_transient_errors(monkeypatch) -> None:
    monkeypatch.setattr(flows, "time", FakeClock())

    def probe() -> None:
        raise RuntimeError("GET /api/0/x/: HTTP 500")

    with pytest.raises(RuntimeError, match="HTTP 500"):
        flows.poll("a broken endpoint", probe, seconds=10)


@responses.activate
@pytest.mark.parametrize("failure", ["connection", 502, 503, 504])
def test_idempotent_requests_retry_transient_failures(flow: PluginFlow, monkeypatch, failure):
    clock = FakeClock()
    monkeypatch.setattr(flows, "time", clock)
    url = flow.sentry.url + "/api/0/organizations/e2e/issues/95/"
    if failure == "connection":
        responses.get(url, body=requests.ConnectionError("private-detail"))
    else:
        responses.get(url, body="private-body", status=failure)
    responses.get(url, json={"id": "95"})
    assert flow.sentry.request("GET", "/api/0/organizations/e2e/issues/95/") == {"id": "95"}
    assert len(responses.calls) == 2 and clock.slept == [1]


@responses.activate
def test_non_idempotent_requests_are_not_retried(flow: PluginFlow, monkeypatch) -> None:
    monkeypatch.setattr(flows, "time", FakeClock())
    url = flow.sentry.url + "/api/0/projects/e2e/demo/rules/"
    responses.post(url, body=requests.ConnectionError("private-detail"))
    with pytest.raises(flows.TransientError) as error:
        flow.sentry.request("POST", "/api/0/projects/e2e/demo/rules/", json={})
    assert str(error.value) == (
        "POST /api/0/projects/e2e/demo/rules/ could not connect; response withheld"
    )
    assert len(responses.calls) == 1


@responses.activate
def test_transient_retry_budget_is_bounded_and_withholds_bodies(flow: PluginFlow, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(flows, "time", clock)
    url = flow.sentry.url + "/api/0/organizations/e2e/"
    responses.get(url, body="private-body", status=502)
    start = clock.now
    with pytest.raises(flows.GatewayError) as error:
        flow.sentry.request("GET", "/api/0/organizations/e2e/")
    assert error.value.status == 502 and "private-body" not in str(error.value)
    assert clock.now - start <= flows.TRANSIENT_RETRY_SECONDS
    assert 1 < len(responses.calls) <= flows.TRANSIENT_RETRY_SECONDS


@responses.activate
def test_unexpected_non_gateway_status_is_not_retried(flow: PluginFlow, monkeypatch) -> None:
    monkeypatch.setattr(flows, "time", FakeClock())
    url = flow.sentry.url + "/api/0/organizations/e2e/"
    responses.get(url, status=500)
    with pytest.raises(flows.APIError) as error:
        flow.sentry.request("GET", "/api/0/organizations/e2e/")
    assert not isinstance(error.value, flows.TransientError)
    assert len(responses.calls) == 1


@responses.activate
def test_transient_retry_never_outlives_the_enclosing_poll(flow: PluginFlow, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(flows, "time", clock)
    url = flow.sentry.url + "/api/0/organizations/e2e/issues/95/"
    responses.get(url, body=requests.ConnectionError("private-detail"))
    with pytest.raises(pytest.fail.Exception, match="Last transient error: GET"):
        flows.poll(
            "group details",
            lambda: flow.sentry.request("GET", "/api/0/organizations/e2e/issues/95/"),
            seconds=5,
        )
    assert clock.now - 1000.0 <= 5 + 2
