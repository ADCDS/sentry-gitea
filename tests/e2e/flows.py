"""Original HTTP test flows; upstream Sentry is used only as an interface reference."""

import json
import re
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import requests
import sentry_sdk
from harness import API, APIError, recent_plugin_logs
from helpers import E2EEnvironment
from redaction import Credential, exposure_message

POLL_SECONDS = 180
MAX_COMMENTS = 5000
# A web worker recycle or nginx upstream refresh can drop one connection or answer one
# request with a gateway error. Idempotent requests retry that within this budget (and
# never past an enclosing poll's deadline); polls retry it until their own deadline.
TRANSIENT_RETRY_SECONDS = 30.0
TRANSIENT_STATUSES = frozenset({502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_deadline: ContextVar[float | None] = ContextVar("e2e_poll_deadline", default=None)
META = re.compile(r"<!-- sentry-gitea-meta v1 (\{.*?\}) -->", re.DOTALL)


def check(condition: bool, message: str) -> None:
    # pytest's assertion rewriting must never render credential-bearing values.
    if not condition:
        pytest.fail(message, pytrace=False)


def without_credentials(
    value: Any,
    credentials: tuple[Credential, ...],
    *,
    where: str = "E2E response",
    service: str | None = None,
) -> None:
    serialized = value if isinstance(value, str) else json.dumps(value)
    message = exposure_message(serialized, credentials, where=where, service=service)
    check(message is None, message or "")


class PollExpired(Exception):
    pass


class TransientError(RuntimeError):
    """A connection failure or gateway error; the message never holds response content."""


class GatewayError(APIError, TransientError):
    """HTTP 502/503/504 where the request did not expect it."""


def poll_budget(maximum: float = 10) -> float:
    deadline = _deadline.get()
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise PollExpired
    return remaining


def retry_transient[T](method: str, attempt: Callable[[], T]) -> T:
    """Retry an idempotent request's transient failures within a bounded budget."""
    end = time.monotonic() + TRANSIENT_RETRY_SECONDS
    while True:
        try:
            return attempt()
        except TransientError:
            deadline = _deadline.get()
            limit = end if deadline is None else min(end, deadline)
            if method.upper() not in IDEMPOTENT_METHODS or time.monotonic() + 1 >= limit:
                raise
            time.sleep(1)


def poll[T](
    description: str,
    probe: Callable[[], T],
    *,
    seconds: float = POLL_SECONDS,
    failure_detail: Callable[[], str] | None = None,
) -> T:
    check(0 < seconds <= POLL_SECONDS, "E2E polls must be bounded to at most 180 seconds")
    end = time.monotonic() + seconds
    context = _deadline.set(end)
    transient = ""
    try:
        while time.monotonic() < end:
            try:
                result = probe()
            except PollExpired:
                break
            except TransientError as error:
                # Retried until the deadline: a dropped connection is not a test failure.
                transient, result = str(error), None
            if result:
                return result
            time.sleep(max(0, min(2, end - time.monotonic())))
    finally:
        _deadline.reset(context)
    detail = failure_detail() if failure_detail else ""
    if transient:
        detail = "\n".join(filter(None, (detail, f"Last transient error: {transient}")))
    message = f"Timed out after {seconds:g}s waiting for {description}"
    pytest.fail(message + (f"\n{detail}" if detail else ""), pytrace=False)


def log_field(line: str, key: str, value: str) -> bool:
    """Match complete key=value fields, including Sentry's quoted string values."""
    escaped = re.escape(value)
    return bool(
        re.search(
            rf"\b{re.escape(key)}['\"]?\s*[:=]\s*(?:'{escaped}'|\"{escaped}\"|{escaped})"
            r"(?=[\s,)}]|$)",
            line,
        )
    )


class FlowAPI(API):
    """Redact before assertions and share the poll deadline across paginated requests."""

    def __init__(
        self, url: str, token: str, credentials: tuple[Credential, ...], scheme="Bearer"
    ) -> None:
        super().__init__(url, token, scheme)
        self.credentials = credentials

    def request(self, method: str, path: str, *, expected=(200,), **kwargs):
        return retry_transient(
            method, lambda: self.request_once(method, path, expected=expected, **kwargs)
        )

    def request_once(self, method: str, path: str, *, expected=(200,), **kwargs):
        remaining = poll_budget(15)
        try:
            # Each connect/read budget is half the remaining budget, at most 5s.
            response = self.session.request(
                method,
                self.url + path,
                timeout=(min(5, remaining / 2), min(5, remaining / 2)),
                allow_redirects=False,
                **kwargs,
            )
        except (requests.ConnectionError, requests.Timeout):
            if _deadline.get() and time.monotonic() >= _deadline.get():
                raise PollExpired from None
            raise TransientError(f"{method} {path} could not connect; response withheld") from None
        except requests.RequestException:
            raise RuntimeError(f"{method} {path} failed; response withheld") from None
        without_credentials(
            response.text,
            self.credentials,
            where=f"response {method} {urlsplit(response.url).path} HTTP {response.status_code}",
        )
        if response.status_code not in expected:
            if response.status_code in TRANSIENT_STATUSES:
                raise GatewayError(method, path, response.status_code)
            raise APIError(method, path, response.status_code)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise RuntimeError(f"{method} {path} did not return JSON; response withheld") from None


class PipelineNudger:
    """Keep an idle self-hosted Sentry moving while a test waits for an alert.

    In self-hosted 26.3.1 the post-process forwarder is synchronized with Snuba's
    commit log (`--synchronize-commit-log-topic`). On an idle instance that log
    only advances when the *next* event arrives, so a test's alert would fire one
    event late. A filler event every few seconds (one shared fingerprint, no
    e2e_case/e2e_delivery tags, so no test rule matches it) lets each test
    observe its own delivery.
    """

    INTERVAL = 4.0

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="e2e-pipeline-nudger", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=20)

    def _run(self) -> None:
        with sentry_sdk.Client(
            dsn=self.dsn,
            default_integrations=False,
            auto_enabling_integrations=False,
            send_client_reports=False,
            shutdown_timeout=5,
        ) as client:
            while not self._stop.wait(self.INTERVAL):
                client.capture_event(
                    {
                        "message": "sentry-gitea e2e pipeline nudge",
                        "level": "info",
                        "platform": "python",
                        "fingerprint": ["sentry-gitea-e2e-pipeline-nudge"],
                    }
                )
                client.flush(timeout=5)


class PluginFlow:
    def __init__(self, env: E2EEnvironment) -> None:
        self.env = env
        self.case_id = uuid4().hex
        self.message = f"sentry-gitea-e2e-{self.case_id}"
        self.default_label = f"e2e:{self.case_id}"
        self.environment = "e2e"
        self.release = "sentry-gitea-e2e@0.1"
        self.credentials = env.credentials
        self.sentry = FlowAPI(env.sentry_url, env.sentry_token, self.credentials)
        self.gitea = FlowAPI(env.gitea_url, env.gitea_token, self.credentials, "token")
        self.plugin_gitea = FlowAPI(
            env.gitea_url, env.gitea_plugin_token, self.credentials, "token"
        )
        self.project_path = f"/api/0/projects/{env.sentry_org}/{env.sentry_project}"
        self.repo = f"{env.gitea_owner}/{env.gitea_repo}"
        self.issues_path = f"/api/v1/repos/{self.repo}/issues"
        self.rule_ids: set[str] = set()
        self.rule_id = ""
        self.delivery_id = uuid4().hex
        self.delivery_since = datetime.now(UTC).isoformat()
        self.last_event_id = ""
        self.original_enabled: dict[str, bool] = {}
        self.nudger: PipelineNudger | None = None

    def plugin_path(self, slug: str) -> str:
        return f"{self.project_path}/plugins/{slug}/"

    def issue_config(self) -> dict[str, str]:
        return {
            "base_url": self.env.gitea_internal_url,
            "public_url": self.env.gitea_url,
            "token": self.env.gitea_plugin_token,
            "repo": self.repo,
            "default_labels": self.default_label,
        }

    def configure(self) -> None:
        for slug in ("gitea", "gitea_alerts"):
            previous = self.sentry.request("GET", self.plugin_path(slug))
            self.original_enabled.setdefault(slug, previous["enabled"])
            self.sentry.request("POST", self.plugin_path(slug), expected=(201,), json={})
        self.sentry.request("PUT", self.plugin_path("gitea"), json=self.issue_config())
        self.sentry.request(
            "PUT",
            self.plugin_path("gitea_alerts"),
            json={
                "labels": "source:sentry",
                "severity_labels": True,
                "comment_on_repeat": True,
                "fix_hint": False,
            },
        )
        if self.nudger is None:
            self.nudger = PipelineNudger(self.ingest_dsn())
            self.nudger.start()

    def ingest_dsn(self) -> str:
        source = urlsplit(self.env.sentry_dsn)
        check(bool(source.username and source.path), "Seeded SENTRY_DSN is invalid")
        # The public key/project come from the seeded DSN; ingress always uses the host binding.
        return urlunsplit(("http", f"{source.username}@127.0.0.1:9000", source.path, "", ""))

    def close(self) -> None:
        failures = []
        if self.nudger is not None:
            self.nudger.stop()
            self.nudger = None
        try:
            for rule_id in list(self.rule_ids):
                try:
                    self.delete_rule(rule_id)
                except RuntimeError as error:
                    failures.append(str(error))
            for slug, enabled in self.original_enabled.items():
                try:
                    self.sentry.request(
                        "POST" if enabled else "DELETE",
                        self.plugin_path(slug),
                        expected=(201,) if enabled else (204,),
                    )
                except RuntimeError as error:
                    failures.append(str(error))
        finally:
            for api in (self.sentry, self.gitea, self.plugin_gitea):
                api.session.close()
        check(not failures, "E2E rule/plugin cleanup failed: " + "; ".join(failures))

    def send_and_wait(self) -> dict:
        check(
            self.env.sentry_url == "http://127.0.0.1:9000",
            "Plugin E2E requires SENTRY_URL=http://127.0.0.1:9000",
        )
        dsn = self.ingest_dsn()
        event_id = uuid4().hex
        self.delivery_since = datetime.now(UTC).isoformat()
        with sentry_sdk.Client(
            dsn=dsn,
            default_integrations=False,
            auto_enabling_integrations=False,
            send_client_reports=False,
            shutdown_timeout=15,
            transport_queue_size=10,
        ) as client:
            captured = client.capture_event(
                {
                    "event_id": event_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": self.message,
                    "level": "error",
                    "platform": "python",
                    "fingerprint": [self.case_id],
                    "release": self.release,
                    "environment": self.environment,
                    "tags": {"e2e_case": self.case_id, "e2e_delivery": self.delivery_id},
                    "exception": {
                        "values": [
                            {
                                "type": "RuntimeError",
                                "value": self.message,
                                "mechanism": {"type": "generic", "handled": False},
                            }
                        ]
                    },
                }
            )
            check(captured == event_id, "Sentry SDK discarded the E2E event")
            client.flush(timeout=15)
        self.last_event_id = event_id

        def find_event() -> dict | None:
            events = self.sentry.request(
                "GET", f"{self.project_path}/events/", params={"query": self.message, "limit": 100}
            )
            return next((event for event in events if event["eventID"] == event_id), None)

        event = poll(f"Sentry project event {event_id} to be indexed", find_event)
        groups = poll(
            f"Sentry project issue for event {event_id}",
            lambda: self.sentry.request(
                "GET", f"{self.project_path}/issues/", params={"query": event_id}
            ),
        )
        check(len(groups) == 1, "Event lookup did not return exactly one Sentry group")
        check(str(groups[0]["id"]) == str(event["groupID"]), "Issue/event group IDs disagree")
        return groups[0]

    def group_path(self, group: dict) -> str:
        return f"/api/0/organizations/{self.env.sentry_org}/issues/{group['id']}/"

    def action_path(self, group: dict, action: str) -> str:
        return f"{self.group_path(group)}plugins/gitea/{action}/"

    def action_fields(self, group: dict, action: str) -> dict:
        fields = self.sentry.request("GET", self.action_path(group, action))
        return {field["name"]: field for field in fields}

    def issue_url(self, group: dict) -> str:
        return group["permalink"]

    def plugin_issue(self, group: dict) -> dict:
        details = self.sentry.request("GET", self.group_path(group))
        entries = [item for item in details["pluginIssues"] if item["slug"] == "gitea"]
        check(len(entries) == 1, "Group details must expose exactly one gitea plugin entry")
        return entries[0]

    def assert_link(self, group: dict, number: int) -> None:
        linked = self.plugin_issue(group).get("issue")
        check(bool(linked), "Sentry group has no Gitea issue link")
        assert str(linked["issue_id"]) == str(number)
        assert linked["label"] == f"{self.repo}#{number}"
        # Users click this link: it must be Gitea's public html_url, not the internal
        # base URL Sentry's containers use (http://gitea:3000).
        assert linked["url"] == self.issue(number)["html_url"], "Link is not browser-reachable"
        assert urlsplit(linked["url"]).netloc == urlsplit(self.env.gitea_url).netloc, (
            "Stored plugin link must use the public Gitea host"
        )
        assert urlsplit(linked["url"]).netloc != "gitea:3000", "Stored plugin link is internal"

    def issue(self, number: int | str) -> dict:
        return self.gitea.request("GET", f"{self.issues_path}/{number}")

    def pages(self, path: str, **params) -> list[dict]:
        items = []
        # Reuse the outer deadline, or bound standalone pagination too.
        previous = _deadline.get()
        context = _deadline.set(previous or time.monotonic() + POLL_SECONDS)
        try:
            for page in range(1, 101):
                batch = self.gitea.request("GET", path, params=params | {"page": page, "limit": 50})
                if not batch:
                    return items
                items.extend(batch)
        except PollExpired:
            if previous is not None:
                raise
            pytest.fail(f"Gitea pagination exceeded 180s: {path}", pytrace=False)
        finally:
            _deadline.reset(context)
        pytest.fail("Gitea pagination exceeded 100 pages; use a dedicated E2E repository")

    def comments(self, number: int) -> list[dict]:
        # Gitea 28.1.0 issueGetComments returns all comments; its Swagger exposes
        # only since/before, unlike the repository issue list's page/limit.
        comments = self.gitea.request("GET", f"{self.issues_path}/{number}/comments")
        check(isinstance(comments, list), "Gitea issue comments did not return a list")
        check(
            len(comments) <= MAX_COMMENTS,
            f"Gitea issue comments exceeded {MAX_COMMENTS}; use a dedicated E2E issue",
        )
        return comments

    def comments_with_marker(self, number: int, marker: str) -> list[dict] | None:
        comments = self.comments(number)
        return comments if any(marker in item["body"] for item in comments) else None

    def dedup_id(self, group: dict) -> str:
        prefix = self.issue_url(group).rsplit("/organizations/", 1)[0]
        return f"{prefix}/{group['project']['id']}/{group['id']}"

    def matching_issues(self, group: dict) -> list[dict]:
        # Swagger operation issueListIssues explicitly supports page and limit.
        issues = self.pages(self.issues_path, state="all", type="issues")
        # Also count malformed/missing metadata on issues bearing this unique
        # message or backlink; a duplicate must not hide behind a broken marker.
        return [
            issue
            for issue in issues
            if self.message in issue["title"]
            or self.issue_url(group) in (issue.get("body") or "")
            or json.dumps(self.dedup_id(group)) in (issue.get("body") or "")
        ]

    def assert_single_issue(self, group: dict, number: int) -> None:
        issues = self.matching_issues(group)
        assert [issue["number"] for issue in issues] == [number], "Alert created duplicate issues"

    def assert_alert_issue(self, group: dict, issue: dict) -> None:
        labels = {label["name"] for label in issue["labels"]}
        assert {"source:sentry", self.default_label} <= labels
        severity_labels = {f"severity:{value}" for value in ("critical", "high", "medium", "low")}
        assert len(labels & severity_labels) == 1
        assert len(labels & {"class:crash", "class:other"}) == 1
        body = issue.get("body") or ""
        assert self.issue_url(group) in body
        for value in (self.last_event_id, "error", self.release, self.environment):
            assert value in body
        marker = META.search(body)
        check(marker is not None, "Alert issue is missing the sentry-gitea-meta v1 marker")
        try:
            metadata = json.loads(marker[1])
        except ValueError:
            pytest.fail("sentry-gitea-meta contains invalid JSON", pytrace=False)
        assert metadata["source"] == "sentry"
        assert metadata["sentry_issue_url"] == self.issue_url(group)
        assert metadata["dedup_id"] == self.dedup_id(group)
        assert issue["title"].startswith(f"[{group['shortId']}] ")
        assert len(issue["title"]) <= 255
        self.assert_single_issue(group, issue["number"])
        self.assert_link(group, issue["number"])

    def delete_rule(self, rule_id: str) -> None:
        self.sentry.request("DELETE", f"{self.project_path}/rules/{rule_id}/", expected=(202, 404))
        self.rule_ids.discard(rule_id)

    def replace_rule(self, action: str) -> None:
        if self.rule_id:
            self.delete_rule(self.rule_id)
        self.delivery_id = uuid4().hex
        action_data = (
            {
                "id": "sentry.rules.actions.notify_event_service.NotifyEventServiceAction",
                "service": "gitea_alerts",
            }
            if action == "specific"
            else {"id": "sentry.rules.actions.notify_event.NotifyEventAction"}
        )
        # 26.3.1 enforces frequency >= 5 minutes. Each delivery gets a fresh rule
        # and tag while keeping the same fingerprint/group. This exercises real
        # notification delivery without waiting five minutes or editing server state.
        rule = self.sentry.request(
            "POST",
            f"{self.project_path}/rules/",
            # 26.3.1 returns 200 on the legacy rule path and 201 on the workflow-engine path.
            expected=(200, 201),
            json={
                "name": f"Gitea E2E {self.case_id} {self.delivery_id}",
                "actionMatch": "any",
                "filterMatch": "all",
                "frequency": 5,
                # Every-event must be the ONLY trigger: 26.3.1's workflow-engine dual write
                # drops EVERY_EVENT when combined with other conditions (SKIPPED_CONDITIONS),
                # which would leave "first seen" only and never fire on repeat events.
                "conditions": [{"id": "sentry.rules.conditions.every_event.EveryEventCondition"}],
                "filters": [
                    {
                        "id": "sentry.rules.filters.tagged_event.TaggedEventFilter",
                        "key": key,
                        "match": "eq",
                        "value": value,
                    }
                    for key, value in (
                        ("e2e_case", self.case_id),
                        ("e2e_delivery", self.delivery_id),
                    )
                ],
                "actions": [action_data],
            },
        )
        self.rule_id = str(rule["id"])
        self.rule_ids.add(self.rule_id)

    def group_logs(self, group: dict) -> list[str]:
        logs = recent_plugin_logs(
            self.delivery_since, credentials=self.credentials, timeout=poll_budget()
        )
        without_credentials(logs, self.credentials, service="taskworker")
        return [
            line
            for line in logs.splitlines()
            if log_field(line, "project_id", str(group["project"]["id"]))
            and log_field(line, "group_id", str(group["id"]))
        ]

    def poll_alert[T](
        self,
        description: str,
        group: dict,
        probe: Callable[[], T],
        *,
        seconds: float = POLL_SECONDS,
    ) -> T:
        diagnostics = "No matching taskworker plugin lines observed for this delivery."

        def observed() -> T:
            nonlocal diagnostics
            result = probe()
            if not result:
                lines = self.group_logs(group)
                if lines:
                    diagnostics = "Recent taskworker plugin lines:\n" + "\n".join(lines[-5:])
            return result

        # Cache safe diagnostics within the poll budget, rather than making an
        # extra Docker call after the deadline. This includes error_message when
        # initial filing or commenting fails while Gitea itself is still reachable.
        return poll(description, observed, seconds=seconds, failure_detail=lambda: diagnostics)

    def wait_for_plugin_log(self, group: dict, message: str, **fields: str) -> str:
        diagnostics = "No matching taskworker plugin lines observed for this delivery."

        def logged() -> str:
            nonlocal diagnostics
            lines = self.group_logs(group)
            if lines:
                diagnostics = "Recent taskworker plugin lines:\n" + "\n".join(lines[-5:])
            return next(
                (
                    line
                    for line in lines
                    if re.search(rf"sentry-gitea: {re.escape(message)}(?:\s+\(|$)", line)
                    and all(log_field(line, key, value) for key, value in fields.items())
                ),
                "",
            )

        return poll(
            f"taskworker sentry-gitea: {message} for group {group['id']} {fields}",
            logged,
            failure_detail=lambda: diagnostics,
        )

    def file_alert(self, action: str) -> tuple[dict, dict]:
        self.replace_rule(action)
        group = self.send_and_wait()
        # 26.3.1 workflow deliveries need not write legacy RuleFireHistory.
        issues = self.poll_alert(
            "Gitea issue for the new alert group", group, lambda: self.matching_issues(group)
        )
        check(len(issues) == 1, "First alert created duplicate Gitea issues")
        issue = issues[0]

        def linked() -> bool:
            link = self.plugin_issue(group).get("issue")
            return bool(
                link
                and str(link["issue_id"]) == str(issue["number"])
                and link["label"] == f"{self.repo}#{issue['number']}"
            )

        # Gitea's create is visible before the worker persists the Sentry link.
        self.poll_alert("Sentry sidebar link after alert creation", group, linked)
        return group, issue

    def observe_issue(
        self, group: dict, issue: dict, comments: list[dict], *, seconds: int, state="open"
    ) -> None:
        end = time.monotonic() + seconds

        def unchanged() -> bool:
            assert self.issue(issue["number"])["state"] == state, "Alert changed issue state"
            assert self.comments(issue["number"]) == comments, "Unexpected comment after delivery"
            self.assert_single_issue(group, issue["number"])
            return time.monotonic() >= end

        poll("stable Gitea issue/comments after plugin delivery", unchanged, seconds=seconds + 15)
