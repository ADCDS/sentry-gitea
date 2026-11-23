"""The signed-webhook fallback, its task, the internal app and the rule migration tool."""

import contextlib
import io
import json
from unittest.mock import patch

import responses
from django.test import override_settings
from sentry.models.group import Group
from sentry.models.groupmeta import GroupMeta
from sentry.models.options.project_option import ProjectOption
from sentry.models.rule import Rule
from sentry.sentry_apps.models.sentry_app_installation import SentryAppInstallation
from sentry.taskworker.runtime import app as taskworker
from sentry.testutils.cases import TestCase
from sentry.testutils.silo import no_silo_test
from sentry.workflow_engine.models import Action

from sentry_gitea.alerts import core, migrate
from sentry_gitea.alerts.app import APP_NAME, ensure_app, find_app
from sentry_gitea.alerts.tasks import deliver_alert
from sentry_gitea.alerts.webhook import MAX_BODY, PATH

BASE = "https://forge.example/git"
API = BASE + "/api/v1/repos/owner/repo"
TOKEN = "synthetic-fallback-credential"
BOT = {"id": 5, "login": "sentry-bot"}
LOGGER = "sentry_gitea.plugins.alerts"
WEBHOOK_ACTION = {"target_identifier": "gitea_alerts", "target_display": None, "target_type": None}


def configure(project):
    for key, value in {
        "gitea:enabled": True,
        "gitea_alerts:enabled": True,
        "gitea:base_url": BASE,
        "gitea:repo": "owner/repo",
        "gitea:token": TOKEN,
        "gitea_alerts:severity_labels": False,
    }.items():
        ProjectOption.objects.set_value(project, key, value)


@no_silo_test
@override_settings(ROOT_URLCONF="sentry_gitea.urls")
class TestAlertWebhook(TestCase):
    def setUp(self):
        super().setUp()
        self.app, created = ensure_app(self.organization.id, self.user)
        assert created
        self.install = SentryAppInstallation.objects.get(sentry_app=self.app)

    def body(self, **event):
        event = {
            "event_id": "d" * 32,
            "project": self.project.id,
            "issue_id": str(self.group.id),
            **event,
        }
        return json.dumps(
            {
                "action": "triggered",
                "installation": {"uuid": self.install.uuid},
                "data": {"event": event, "triggered_rule": "Failure rule"},
                "actor": {"type": "application", "id": "sentry", "name": "Sentry"},
            }
        )

    def post(self, body, *, signature=None, resource="event_alert"):
        headers = {"HTTP_SENTRY_HOOK_RESOURCE": resource}
        if signature is not False:
            headers["HTTP_SENTRY_HOOK_SIGNATURE"] = signature or self.app.build_signature(body)
        with patch("sentry_gitea.alerts.webhook.deliver_alert") as task:
            response = self.client.post(PATH, data=body, content_type="application/json", **headers)
        return response, task

    def test_the_app_is_internal_alertable_and_points_at_this_endpoint(self):
        assert self.app.name == APP_NAME and self.app.is_internal and self.app.is_alertable
        assert self.app.webhook_url.endswith(PATH)
        again, created = ensure_app(self.organization.id, self.user)
        assert (again.id, created) == (self.app.id, False)
        other = self.create_organization()
        assert find_app(other.id) is None

    def test_the_app_is_never_created_with_an_unusable_webhook_url(self):
        other = self.create_organization()
        for setting in ("/extensions/gitea/alerts/", "https://sentry.example/elsewhere/", "x"):
            with override_settings(GITEA_ALERTS_WEBHOOK_URL=setting):
                with self.assertRaises(ValueError):
                    ensure_app(other.id, self.user)
        with self.options({"system.url-prefix": ""}), self.assertRaises(ValueError):
            ensure_app(other.id, self.user)
        assert find_app(other.id) is None
        internal = "http://web:9000/extensions/gitea/alerts/"
        with override_settings(GITEA_ALERTS_WEBHOOK_URL=internal):
            app, created = ensure_app(other.id, self.user)
        assert created and app.webhook_url == internal

    def test_a_signed_alert_is_queued_with_its_ids(self):
        response, task = self.post(self.body())
        assert response.status_code == 202
        task.delay.assert_called_once_with(
            self.organization.id, self.project.id, self.group.id, "d" * 32
        )

    def test_unsigned_tampered_or_foreign_requests_are_refused(self):
        body = self.body()
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            for response, task in (
                self.post(body, signature=False),
                self.post(body, signature="0" * 64),
                self.post(
                    body.replace("Failure rule", "Another rule"),
                    signature=self.app.build_signature(body),
                ),
                self.post(body.replace(self.install.uuid, "no-such-installation")),
            ):
                assert response.status_code == 401
                assert response.json() == {"detail": "Not accepted."}
                task.delay.assert_not_called()
        assert self.app.build_signature(body) not in "".join(str(vars(r)) for r in logs.records)

    def test_the_address_works_without_its_trailing_slash(self):
        # Sentry does not follow redirects when it posts a webhook, and counts a 301 as sent.
        body = self.body()
        with patch("sentry_gitea.alerts.webhook.deliver_alert") as task:
            response = self.client.post(
                PATH.rstrip("/"),
                data=body,
                content_type="application/json",
                HTTP_SENTRY_HOOK_RESOURCE="event_alert",
                HTTP_SENTRY_HOOK_SIGNATURE=self.app.build_signature(body),
            )
        assert response.status_code == 202
        task.delay.assert_called_once()

    def test_another_apps_valid_signature_is_not_accepted(self):
        other = self.create_internal_integration(
            name="Some other integration",
            organization=self.organization,
            webhook_url="https://elsewhere.example/hook",
        )
        other_install = SentryAppInstallation.objects.get(sentry_app=other)
        body = self.body().replace(self.install.uuid, other_install.uuid)
        response, task = self.post(body, signature=other.build_signature(body))
        assert response.status_code == 401
        task.delay.assert_not_called()

    def test_only_this_organizations_projects_are_accepted(self):
        foreign = self.create_project(organization=self.create_organization())
        response, task = self.post(self.body(project=foreign.id))
        assert response.status_code == 404
        task.delay.assert_not_called()

    def test_malformed_oversized_and_non_alert_requests(self):
        for body, status in (("{not json", 400), (json.dumps({"installation": []}), 400)):
            response, task = self.post(body, signature="x")
            assert response.status_code == status
            task.delay.assert_not_called()
        no_event = json.dumps({"installation": {"uuid": self.install.uuid}, "data": {}})
        response, task = self.post(no_event)
        assert response.status_code == 400
        response, task = self.post(self.body(), resource="issue")
        assert response.status_code == 202 and "Ignored" in response.json()["detail"]
        task.delay.assert_not_called()
        # Inputs that must be refused before they reach a parser's limits or the database.
        for body, signature, status in (
            ("[" * 100_000, "x", 400),
            (json.dumps({"installation": {"uuid": "\u0000"}}), "x", 401),
            (json.dumps({"installation": {"uuid": "a" * 200}}), "x", 401),
        ):
            response, task = self.post(body, signature=signature)
            assert response.status_code == status, body[:40]
            task.delay.assert_not_called()
        response, task = self.post(self.body(project=1e999))
        assert response.status_code == 400
        task.delay.assert_not_called()
        with patch("sentry_gitea.alerts.webhook.deliver_alert") as task:
            big = self.client.post(
                PATH, data="x", content_type="application/json", CONTENT_LENGTH=str(MAX_BODY + 1)
            )
        assert big.status_code == 413
        assert self.client.get(PATH).status_code == 405


@no_silo_test
class TestDeliverAlertTask(TestCase):
    def setUp(self):
        super().setUp()
        configure(self.project)
        self.addCleanup(GroupMeta.objects.clear_local_cache)
        # No Snuba here; the task then files from the issue alone, which is also what it
        # does when the event store has nothing yet.
        latest = patch.object(Group, "get_latest_event", return_value=None)
        latest.start()
        self.addCleanup(latest.stop)

    def mock_create(self):
        responses.get(BASE + "/api/v1/user", json=BOT)
        responses.get(API, json={"id": 41, "full_name": "owner/repo", "default_branch": "main"})
        responses.get(API + "/issues", json=[])
        responses.get(
            API + "/labels",
            json=[{"id": 1, "name": "source:sentry"}],
            headers={"X-Total-Count": "1"},
        )
        responses.post(
            API + "/issues",
            status=201,
            json={
                "number": 7,
                "title": "Remote issue",
                "state": "open",
                "labels": [{"id": 1}],
                "pull_request": None,
                "html_url": BASE + "/owner/repo/issues/7",
                "user": BOT,
            },
        )

    def test_the_task_is_registered_with_the_integration_tasks(self):
        import sentry_gitea.integration.tasks  # noqa: F401  (what TASKWORKER_IMPORTS loads)

        namespace, _, name = "integrations:sentry_gitea.alerts.tasks.deliver_alert".partition(":")
        assert taskworker.taskregistry.get_task(namespace, name).name == deliver_alert.name

    @responses.activate
    def test_it_files_an_issue_from_ids_alone(self):
        self.mock_create()
        deliver_alert(self.organization.id, self.project.id, self.group.id, None)
        assert core.read_link(self.group)["id"] == "7"

    @responses.activate
    def test_an_event_store_failure_does_not_lose_the_alert(self):
        self.mock_create()
        with patch.object(Group, "get_latest_event", side_effect=RuntimeError("snuba down")):
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                deliver_alert(self.organization.id, self.project.id, self.group.id, None)
        assert "using the issue only" in logs.records[0].getMessage()
        assert core.read_link(self.group)["id"] == "7"

    @responses.activate
    def test_a_failing_read_of_the_named_event_does_not_lose_the_alert(self):
        self.mock_create()
        with patch("sentry_gitea.alerts.tasks.eventstore") as store:
            store.backend.get_event_by_id.side_effect = RuntimeError("node store down")
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                deliver_alert(self.organization.id, self.project.id, self.group.id, "d" * 32)
        store.backend.get_event_by_id.assert_called_once()
        assert "using the issue only" in logs.records[0].getMessage()
        assert core.read_link(self.group)["id"] == "7"

    @responses.activate
    def test_wrong_organization_unconfigured_project_and_failures_do_nothing(self):
        other = self.create_organization()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            deliver_alert(other.id, self.project.id, self.group.id, None)
        assert "another organization" in logs.records[-1].getMessage()
        ProjectOption.objects.set_value(self.project, "gitea:enabled", False)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            deliver_alert(self.organization.id, self.project.id, self.group.id, None)
        assert "not enabled and configured" in logs.records[-1].getMessage()
        assert not responses.calls
        ProjectOption.objects.set_value(self.project, "gitea:enabled", True)
        responses.get(BASE + "/api/v1/user", json=BOT)
        responses.get(API, status=500)
        with self.assertLogs(LOGGER, level="ERROR"):
            deliver_alert(self.organization.id, self.project.id, self.group.id, None)
        assert core.read_link(self.group) is None


@no_silo_test
class TestRuleMigration(TestCase):
    def setUp(self):
        super().setUp()
        self.action = self.create_action(type=Action.Type.WEBHOOK, config=dict(WEBHOOK_ACTION))
        self.create_data_condition_group_action(
            action=self.action,
            condition_group=self.create_data_condition_group(organization=self.organization),
        )
        self.rule = Rule.objects.create(
            project=self.project,
            label="Failure rule",
            data={
                "actions": [
                    {"id": migrate.SERVICE_ACTION, "service": "gitea_alerts"},
                    {"id": migrate.SERVICE_ACTION, "service": "mail"},
                ]
            },
        )
        # Decoys: another target here, and the plugin's slug in another organization.
        self.other_target = self.create_action(
            type=Action.Type.WEBHOOK, config={**WEBHOOK_ACTION, "target_identifier": "other-app"}
        )
        self.create_data_condition_group_action(
            action=self.other_target,
            condition_group=self.create_data_condition_group(organization=self.organization),
        )
        elsewhere = self.create_organization()
        self.foreign = self.create_action(type=Action.Type.WEBHOOK, config=dict(WEBHOOK_ACTION))
        self.create_data_condition_group_action(
            action=self.foreign,
            condition_group=self.create_data_condition_group(organization=elsewhere),
        )

    def test_find_reports_this_organizations_plugin_targets_only(self):
        found = migrate.find(self.organization.id)
        assert [a.id for a in found.actions] == [self.action.id]
        assert [r.id for r in found.rules] == [self.rule.id]

    def test_repoint_writes_nothing_without_apply(self):
        report = migrate.repoint(self.organization.id, "gitea-alerts-abc")
        assert [a.id for a in report.actions] == [self.action.id]
        self.action.refresh_from_db()
        assert self.action.config["target_identifier"] == "gitea_alerts"

    def test_repoint_changes_both_representations_and_nothing_else(self):
        migrate.repoint(self.organization.id, "gitea-alerts-abc", apply=True)
        for row in (self.action, self.rule, self.other_target, self.foreign):
            row.refresh_from_db()
        assert self.action.config["target_identifier"] == "gitea-alerts-abc"
        assert [a["service"] for a in self.rule.data["actions"]] == ["gitea-alerts-abc", "mail"]
        assert self.other_target.config["target_identifier"] == "other-app"
        assert self.foreign.config["target_identifier"] == "gitea_alerts"
        assert not migrate.find(self.organization.id)
        with self.assertRaises(ValueError):
            migrate.repoint(self.organization.id, "gitea_alerts", apply=True)

    def command(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = migrate.run(
                migrate.parser().parse_args(["--org", self.organization.slug, *argv])
            )
        return code, out.getvalue(), err.getvalue()

    def test_the_command_reports_creates_the_app_and_repoints_only_to_it(self):
        code, out, _ = self.command()
        assert code == 0 and "internal app: none" in out
        assert "1 workflow action(s), 1 rule(s)" in out and "Failure rule" in out
        assert find_app(self.organization.id) is None, "a report creates nothing"

        code, _, err = self.command("--create-app")
        assert code == 2 and "--user" in err
        code, _, err = self.command("--create-app", "--user", "nobody@example.invalid")
        assert code == 1 and "no active user" in err
        with self.options({"system.url-prefix": ""}):
            code, _, err = self.command("--create-app", "--user", self.user.email)
        assert code == 1 and "system.url-prefix" in err
        assert find_app(self.organization.id) is None
        code, out, _ = self.command("--create-app", "--user", self.user.email)
        app = find_app(self.organization.id)
        assert code == 0 and app is not None and f"slug {app.slug}" in out

        assert "GITEA_ALERTS_WEBHOOK_TARGETS" in out, "it says what to do before repointing"

        code, _, err = self.command("--repoint", "some-other-app", "--apply")
        assert code == 1 and "not this organization's Gitea Alerts app" in err
        code, out, _ = self.command("--repoint", app.slug)
        assert code == 0 and "would repoint 1 action(s) and 1 rule(s)" in out
        assert "the signed webhook" in out
        self.action.refresh_from_db()
        assert self.action.config["target_identifier"] == "gitea_alerts"

        # Until the bridge answers for the slug, repointing would move the rules to the webhook.
        code, out, err = self.command("--repoint", app.slug, "--apply")
        assert code == 1 and "nothing changed" in err
        self.action.refresh_from_db()
        assert self.action.config["target_identifier"] == "gitea_alerts"

        with override_settings(GITEA_ALERTS_WEBHOOK_TARGETS=(app.slug,)):
            code, out, _ = self.command("--repoint", app.slug, "--apply")
        assert code == 0 and "repointed 1 action(s) and 1 rule(s)" in out
        assert "the alert bridge" in out
        self.action.refresh_from_db()
        assert self.action.config["target_identifier"] == app.slug
        code, out, _ = self.command()
        assert "0 workflow action(s), 0 rule(s)" in out

    def test_the_command_repoints_to_the_webhook_only_when_told_to(self):
        app, _ = ensure_app(self.organization.id, self.user)
        code, out, _ = self.command("--repoint", app.slug, "--apply", "--via-webhook")
        assert code == 0 and "repointed 1 action(s) and 1 rule(s)" in out
        self.action.refresh_from_db()
        assert self.action.config["target_identifier"] == app.slug

    def test_the_command_refuses_an_unknown_organization_and_a_bare_apply(self):
        args = migrate.parser().parse_args(["--org", "no-such-organization"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert migrate.run(args) == 1
        assert "no organization" in err.getvalue()
        code, _, err = self.command("--apply")
        assert code == 2 and "--repoint" in err
