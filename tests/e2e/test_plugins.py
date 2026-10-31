"""The plugins' contract exercised through real Sentry and Gitea HTTP endpoints."""

import json
import secrets
from datetime import UTC, datetime

import pytest
from flows import FlowAPI, PluginFlow, check, poll, without_credentials
from harness import (
    gitea_compose,
    network_environment,
    recent_plugin_logs,
    run,
    self_hosted_compose,
    wait_for_gitea,
)
from helpers import IntegrationEnvironment
from redaction import Credential

pytestmark = pytest.mark.e2e

# The E2E Gitea under a second network alias that is NOT in the installer's
# GITEA_PLUGIN_ALLOWED_BASE_URLS: same server, same token, different origin.
UNLISTED_GITEA = "http://gitea-unlisted:3000"


def sentry_web_reaches(url: str) -> None:
    """Prove a URL answers from Sentry's web container (so only a guard can refuse it)."""
    program = (
        "import json, sys, urllib.request; "
        "response = urllib.request.urlopen(sys.argv[1] + '/api/healthz', timeout=15); "
        "assert response.status == 200; "
        "assert json.load(response)['status'] == 'pass'"
    )
    run(
        self_hosted_compose() + ["exec", "-T", "web", "python", "-c", program, url],
        label="Sentry container to unlisted Gitea alias check (recreate Gitea: make e2e-gitea-up)",
        timeout=30,
    )


def test_configure_and_validate(plugin_flow: PluginFlow) -> None:
    flow = plugin_flow
    metadata = flow.plugin_gitea.request("GET", "/api/v1/token")
    assert set(metadata["scopes"]) == {"read:user", "read:repository", "write:issue"}
    for slug in ("gitea", "gitea_alerts"):
        config = flow.sentry.request("GET", flow.plugin_path(slug))
        assert config["enabled"] is True
        without_credentials(
            config, flow.credentials, where=f"response GET {flow.plugin_path(slug)} HTTP 200"
        )
    config = flow.sentry.request("GET", flow.plugin_path("gitea"))
    fields = {field["name"]: field for field in config["config"]}
    assert fields["token"]["type"] == "secret"
    check(fields["token"].get("value") is None, "GET config exposed a token field value")
    assert fields["base_url"]["value"] == flow.env.gitea_internal_url
    assert fields["public_url"]["value"] == flow.env.gitea_url
    assert fields["repo"]["value"] == flow.repo
    assert fields["default_labels"]["value"] == flow.default_label

    invalid_token = secrets.token_hex(20)
    for changes, field in (
        ({"token": invalid_token}, "token"),
        ({"repo": f"{flow.env.gitea_owner}/missing-{flow.case_id}"}, "repo"),
    ):
        result = flow.sentry.request(
            "PUT", flow.plugin_path("gitea"), expected=(400,), json=flow.issue_config() | changes
        )
        without_credentials(
            result,
            (*flow.credentials, Credential("invalid plugin token", invalid_token)),
            where=f"response PUT {flow.plugin_path('gitea')} HTTP 400",
        )
        errors = result.get("errors", {})
        check(
            bool(errors.get(field) or errors.get("__all__")), f"400 lacks a {field}/__all__ error"
        )

    # Exercise both documented enable/disable APIs, then leave valid configuration.
    for slug in ("gitea_alerts", "gitea"):
        flow.sentry.request("DELETE", flow.plugin_path(slug), expected=(204,))
        assert flow.sentry.request("GET", flow.plugin_path(slug))["enabled"] is False
    flow.configure()


def test_base_url_outside_the_allowlist_is_refused(plugin_flow: PluginFlow) -> None:
    """SSRF guard: only allowlisted origins may reach private addresses."""
    flow = plugin_flow
    sentry_web_reaches(UNLISTED_GITEA)
    dummy = secrets.token_hex(20)
    credentials = (*flow.credentials, Credential("dummy plugin token", dummy))
    # The real token at the unlisted alias would validate if the guard were missing. The
    # metadata address only ever receives a random value, never a real credential.
    for base_url, token in (
        (UNLISTED_GITEA, flow.env.gitea_plugin_token),
        ("http://169.254.169.254", dummy),
    ):
        result = flow.sentry.request(
            "PUT",
            flow.plugin_path("gitea"),
            expected=(400,),
            json=flow.issue_config() | {"base_url": base_url, "token": token},
        )
        without_credentials(
            result, credentials, where=f"response PUT {flow.plugin_path('gitea')} HTTP 400"
        )
        errors = result.get("errors", {})
        message = str(errors.get("base_url") or errors.get("__all__") or "")
        # Pin the guard's own message so an unrelated 400 cannot satisfy this test.
        check(
            "Sentry refused to connect" in message and "GITEA_PLUGIN_ALLOWED_BASE_URLS" in message,
            f"Non-allowlisted base_url {base_url} was not refused by the SSRF guard",
        )
    # A changed origin requires re-entering the token: the saved one is never sent there.
    without_token = flow.issue_config() | {"base_url": UNLISTED_GITEA}
    without_token.pop("token")
    result = flow.sentry.request(
        "PUT", flow.plugin_path("gitea"), expected=(400,), json=without_token
    )
    without_credentials(result, credentials, where="response PUT without token HTTP 400")
    check(bool(result.get("errors")), "Changing base_url without a token returned no error")
    config = flow.sentry.request("GET", flow.plugin_path("gitea"))
    fields = {field["name"]: field for field in config["config"]}
    assert fields["base_url"]["value"] == flow.env.gitea_internal_url, "Refused URL was saved"


def test_marker_from_another_author_is_not_recovered(
    plugin_flow: PluginFlow, integration_env: IntegrationEnvironment
) -> None:
    """A valid metadata marker on someone else's issue must not be adopted as the link."""
    flow = plugin_flow
    other = integration_env.gitea_e2e_username
    token_user = flow.plugin_gitea.request("GET", "/api/v1/user")
    check(other != token_user["login"], "Marker author must differ from the plugin token user")
    group = flow.send_and_wait()
    dedup_id = flow.dedup_id(group)
    marker = {"source": "sentry", "dedup_id": dedup_id, "sentry_issue_url": flow.issue_url(group)}
    collaborator = f"/api/v1/repos/{flow.repo}/collaborators/{other}"
    # Gitea lets any repository reader open issues; read access is all the author needs.
    flow.gitea.request("PUT", collaborator, expected=(204,), json={"permission": "read"})
    author = FlowAPI(flow.env.gitea_url, "", integration_env.credentials, "token")
    author.session.auth = (other, integration_env.gitea_e2e_password)
    planted = None
    try:
        planted = author.request(
            "POST",
            flow.issues_path,
            expected=(201,),
            json={
                "title": f"Planted {flow.message}",
                "body": f"Planted by {other}\n\n<!-- sentry-gitea-meta v1 {json.dumps(marker)} -->",
            },
        )
        check(
            planted["user"]["login"] == other, "Gitea attributed the planted issue to another user"
        )
        # Without the author check the plugin's own marker search would recover this issue.
        poll(
            "Gitea's issue search to return the planted marker issue",
            lambda: any(
                issue["number"] == planted["number"]
                for issue in flow.pages(flow.issues_path, state="all", type="issues", q=dedup_id)
            ),
            seconds=90,
        )
        fields = flow.action_fields(group, "create")
        result = flow.sentry.request(
            "POST",
            flow.action_path(group, "create"),
            json={
                "title": fields["title"].get("default"),
                "description": fields["description"].get("default"),
            },
        )
        assert str(result["id"]) != str(planted["number"]), "Plugin adopted another user's issue"
        issue = flow.issue(result["id"])
        assert issue["user"]["id"] == token_user["id"], "New issue not filed by the token user"
        flow.assert_link(group, issue["number"])
        untouched = flow.issue(planted["number"])
        assert untouched["body"] == planted["body"] and untouched["state"] == "open"
        assert flow.comments(planted["number"]) == [], "Plugin commented on the planted issue"
    finally:
        try:
            if planted is not None:
                flow.gitea.request(
                    "PATCH",
                    f"{flow.issues_path}/{planted['number']}",
                    expected=(201,),
                    json={"state": "closed"},
                )
        finally:
            author.session.close()
            flow.gitea.request("DELETE", collaborator, expected=(204,))


def test_sidebar_create(plugin_flow: PluginFlow) -> None:
    flow = plugin_flow
    # A real create after this update proves that omitting a saved secret keeps it.
    saved = flow.issue_config()
    saved.pop("token")
    flow.sentry.request("PUT", flow.plugin_path("gitea"), json=saved)
    group = flow.send_and_wait()
    fields = flow.action_fields(group, "create")
    title = fields["title"].get("default")
    description = fields["description"].get("default")
    check(isinstance(title, str) and bool(title.strip()), "Create form has no default title")
    check(isinstance(description, str) and bool(description.strip()), "Create form has no body")
    result = flow.sentry.request(
        "POST", flow.action_path(group, "create"), json={"title": title, "description": description}
    )
    issue = flow.issue(result["id"])
    assert issue["title"] == title
    assert flow.default_label in {label["name"] for label in issue["labels"]}
    assert flow.issue_url(group) in issue["body"]
    flow.assert_link(group, issue["number"])


def test_link_existing_and_unlink(plugin_flow: PluginFlow) -> None:
    flow = plugin_flow
    group = flow.send_and_wait()
    issue = flow.gitea.request(
        "POST", flow.issues_path, expected=(201,), json={"title": f"Existing {flow.message}"}
    )
    fields = flow.action_fields(group, "link")
    assert "issue_id" in fields
    default_comment = fields["comment"].get("default", "")
    assert flow.issue_url(group) in default_comment
    comment = f"{default_comment}\nE2E backlink {flow.case_id}"
    flow.sentry.request(
        "POST",
        flow.action_path(group, "link"),
        json={"issue_id": str(issue["number"]), "comment": comment},
    )
    poll(
        "Gitea backlink comment after link",
        lambda: any(comment in item["body"] for item in flow.comments(issue["number"])),
    )
    flow.assert_link(group, issue["number"])
    flow.sentry.request("POST", flow.action_path(group, "unlink"), json={})
    check(not flow.plugin_issue(group).get("issue"), "Unlink left the Gitea link in Sentry")
    remaining = flow.issue(issue["number"])
    assert remaining["id"] == issue["id"]
    assert remaining["state"] == "open"


@pytest.mark.parametrize("action", ["specific", "all_legacy"])
def test_alert_create_and_repeat(plugin_flow: PluginFlow, action: str) -> None:
    flow = plugin_flow
    group, issue = flow.file_alert(action)
    flow.assert_alert_issue(group, issue)
    before = flow.comments(issue["number"])
    flow.replace_rule(action)
    repeated = flow.send_and_wait()
    assert repeated["id"] == group["id"], "Repeat event did not keep the original fingerprint"
    marker = f"<!-- sentry-gitea-event {flow.last_event_id} -->"
    comments = flow.poll_alert(
        "exactly one new repeat-event comment",
        repeated,
        lambda: flow.comments_with_marker(issue["number"], marker),
    )
    added = [item for item in comments if item["id"] not in {old["id"] for old in before}]
    assert len(added) == 1, "Repeat delivery created more than one comment"
    assert marker in added[0]["body"]
    for value in (flow.last_event_id, "error", flow.environment, flow.release):
        assert value in added[0]["body"]
    assert f"{flow.issue_url(group)}events/{flow.last_event_id}/" in added[0]["body"]
    flow.assert_single_issue(group, issue["number"])
    flow.assert_link(group, issue["number"])
    flow.observe_issue(group, issue, comments, seconds=15)


def test_closed_issue_skips_repeat(plugin_flow: PluginFlow) -> None:
    flow = plugin_flow
    group, issue = flow.file_alert("specific")
    # Gitea's issue edit endpoint answers 201, not 200.
    flow.gitea.request(
        "PATCH", f"{flow.issues_path}/{issue['number']}", expected=(201,), json={"state": "closed"}
    )
    before = flow.comments(issue["number"])
    flow.replace_rule("specific")
    repeated = flow.send_and_wait()
    assert repeated["id"] == group["id"]
    # Require positive evidence that this delivery reached the closed-issue check.
    flow.wait_for_plugin_log(
        repeated, "skipped repeat event", issue_id=str(issue["number"]), reason="closed"
    )
    flow.observe_issue(group, issue, before, seconds=15, state="closed")


def test_regression_after_close_files_a_new_issue(plugin_flow: PluginFlow) -> None:
    flow = plugin_flow
    group, issue = flow.file_alert("specific")
    flow.gitea.request(
        "PATCH", f"{flow.issues_path}/{issue['number']}", expected=(201,), json={"state": "closed"}
    )
    # Resolving in Sentry makes the next event with the same fingerprint a regression,
    # recorded after the Gitea close.
    flow.sentry.request("PUT", flow.group_path(group), json={"status": "resolved"})
    flow.replace_rule("specific")
    repeated = flow.send_and_wait()
    assert repeated["id"] == group["id"]
    flow.wait_for_plugin_log(
        repeated,
        "filed issue",
        recurrence_of=str(issue["number"]),
        refile_reason="regressed",
    )
    refiled = flow.poll_alert(
        "re-filed Gitea issue",
        group,
        lambda: [i for i in flow.matching_issues(group) if i["number"] != issue["number"]],
    )
    check(len(refiled) == 1, "Re-filing created duplicate issues")
    new = refiled[0]
    assert new["state"] == "open"
    assert f"**Recurrence of:** #{issue['number']}" in new["body"]
    assert flow.issue(issue["number"])["state"] == "closed", "The closed issue was reopened"

    def moved() -> bool:
        link = flow.plugin_issue(group).get("issue")
        return bool(link and str(link["issue_id"]) == str(new["number"]))

    flow.poll_alert("Sentry link moved to the re-filed issue", group, moved)
    flow.assert_link(group, new["number"])


def test_alert_failure_is_visible_and_redacted(plugin_flow: PluginFlow) -> None:
    flow = plugin_flow
    flow.replace_rule("specific")
    compose = gitea_compose()
    environment = network_environment()
    since = datetime.now(UTC).isoformat()
    try:
        run(
            compose + ["stop", "--timeout", "10", "gitea"],
            label="Stop E2E Gitea for alert failure test",
            env=environment,
            timeout=30,
        )
        group = flow.send_and_wait()
        failure = flow.wait_for_plugin_log(group, "alert delivery failed")
        check("status=" in failure, "Alert failure log has no status field")
    finally:
        # Restore even after a stop timeout, event ingestion failure, or assertion failure.
        run(
            compose + ["start", "gitea"],
            label="Restore E2E Gitea after alert failure test",
            env=environment,
            timeout=60,
        )
        wait_for_gitea(flow.env.gitea_url)
    # Keep assertions from rendering raw worker output, even if a regression leaks a secret.
    without_credentials(
        recent_plugin_logs(since, credentials=flow.credentials),
        flow.credentials,
        service="taskworker",
    )
