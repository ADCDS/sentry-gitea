"""The alert bridge: the workflow-engine actions that used to reach the `gitea_alerts` plugin.

These run on every supported Sentry, with or without `sentry.plugins.bases`: settings are
written straight to the project options the legacy plugins used, and the action is executed
through the workflow engine's own handler registry. The bridge only queues a task; the tests
that look at Gitea run the handler inside Sentry's task runner, so the queued task runs too.
"""

from types import SimpleNamespace
from unittest.mock import patch

import responses
from django.test import override_settings
from sentry.models.activity import Activity
from sentry.models.groupmeta import GroupMeta
from sentry.models.options.project_option import ProjectOption
from sentry.services import eventstore
from sentry.testutils.cases import TestCase
from sentry.types.activity import ActivityType
from sentry.workflow_engine.models import Action
from sentry.workflow_engine.registry import action_handler_registry

from sentry_gitea.alerts import bridge, core, tasks

BASE = "https://forge.example/git"
API = BASE + "/api/v1/repos/owner/repo"
TOKEN = "synthetic-bridge-credential"
BOT = {"id": 5, "login": "sentry-bot"}
LOGGER = "sentry_gitea.plugins.alerts"


def invocation(event, target="gitea_alerts"):
    return SimpleNamespace(
        action=SimpleNamespace(id=1, config={"target_identifier": target}),
        event_data=SimpleNamespace(event=event, group=getattr(event, "group", None)),
        detector=SimpleNamespace(id=1),
        notification_uuid="00000000-0000-0000-0000-000000000000",
    )


class TestAlertBridge(TestCase):
    def setUp(self):
        super().setUp()
        bridge.uninstall()
        self.stock = action_handler_registry.get(Action.Type.WEBHOOK)
        self.addCleanup(bridge.uninstall)
        self.addCleanup(GroupMeta.objects.clear_local_cache)
        for key, value in {
            "gitea:enabled": True,
            "gitea_alerts:enabled": True,
            "gitea:base_url": BASE,
            "gitea:repo": "owner/repo",
            "gitea:token": TOKEN,
            # Only the fixed `source:sentry` label, so the label lookup needs no creation.
            "gitea_alerts:severity_labels": False,
        }.items():
            ProjectOption.objects.set_value(self.project, key, value)
        event = eventstore.backend.create_event(
            project_id=self.project.id,
            group_id=self.group.id,
            event_id="c" * 32,
            data={
                "title": "Bridge failure",
                "type": "error",
                "level": "error",
                "timestamp": 1700000000,
                "release": "app@abcdef0",
                "culprit": "src/server.py:42",
                "tags": [["environment", "production"]],
            },
        )
        event.project = self.project
        event.data.save()  # the task reads the event back from the node store by its ID
        self.event = event.for_group(self.group)
        runner = self.tasks()
        runner.__enter__()
        self.addCleanup(runner.__exit__, None, None, None)

    def mock_create(self, status=201):
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
            status=status,
            json={
                "number": 7,
                "title": "Remote issue",
                "state": "open",
                # The client verifies that Gitea kept the labels it sent.
                "labels": [{"id": 1}],
                "pull_request": None,
                "html_url": BASE + "/owner/repo/issues/7",
                "user": BOT,
            },
        )

    def handler(self):
        assert bridge.install() is True
        return action_handler_registry.get(Action.Type.WEBHOOK)

    def test_install_wraps_the_registered_handler_once(self):
        assert bridge.active() is False
        first = self.handler()
        assert first is not self.stock and issubclass(first, self.stock)
        assert bridge.active() is True
        # Everything Sentry reads off the handler class is still the original's.
        assert first.config_schema == self.stock.config_schema
        assert first.group == self.stock.group
        assert self.handler() is first, "a second install must not wrap the wrapper"
        bridge.uninstall()
        assert action_handler_registry.get(Action.Type.WEBHOOK) is self.stock
        assert bridge.active() is False

    @responses.activate
    def test_gitea_alerts_action_files_an_issue_and_saves_the_link(self):
        self.mock_create()
        with patch.object(self.stock, "execute") as stock_execute:
            assert self.handler().execute(invocation(self.event)) is None
        stock_execute.assert_not_called()
        posts = [c for c in responses.calls if c.request.method == "POST"]
        assert [c.request.url for c in posts] == [API + "/issues"]
        link = core.read_link(self.group)
        assert link["id"] == "7" and link["repo"] == "owner/repo" and link["repo_id"] == "41"
        # Stored where the legacy issue plugin kept it, so both read the same link.
        assert GroupMeta.objects.get_value(self.group, "gitea:issue_id", None) == "7"

    def test_the_handler_only_queues_the_delivery(self):
        with patch.object(tasks.deliver_alert, "delay") as delay:
            assert self.handler().execute(invocation(self.event)) is None
        delay.assert_called_once_with(
            self.organization.id, self.project.id, self.group.id, "c" * 32
        )

    def test_a_failure_to_queue_is_logged_and_never_raised_into_the_rule(self):
        with patch.object(tasks.deliver_alert, "delay", side_effect=RuntimeError("broker down")):
            with self.assertLogs(LOGGER, level="ERROR") as logs:
                assert self.handler().execute(invocation(self.event)) is None
        assert logs.records[-1].getMessage() == "sentry-gitea: alert could not be queued"

    @responses.activate
    def test_the_comment_names_the_event_the_rule_fired_for(self):
        self.mock_create()
        handler = self.handler()
        handler.execute(invocation(self.event))
        responses.get(API + "/issues/7", json={"number": 7, "state": "open", "user": BOT})
        responses.post(API + "/issues/7/comments", json={"id": 1}, status=201)
        handler.execute(invocation(self.event))
        assert "c" * 32 in responses.calls[-1].request.body.decode()

    @responses.activate
    def test_a_second_alert_comments_on_the_open_issue(self):
        self.mock_create()
        handler = self.handler()
        handler.execute(invocation(self.event))
        responses.get(API + "/issues/7", json={"number": 7, "state": "open", "user": BOT})
        responses.post(API + "/issues/7/comments", json={"id": 1}, status=201)
        handler.execute(invocation(self.event))
        urls = [c.request.url for c in responses.calls if c.request.method == "POST"]
        assert urls == [API + "/issues", API + "/issues/7/comments"]

    def test_other_targets_reach_the_original_handler_untouched(self):
        handler = self.handler()
        other = invocation(self.event, target="some-sentry-app")
        with patch.object(self.stock, "execute", return_value="stock result") as stock_execute:
            assert handler.execute(other) == "stock result"
        stock_execute.assert_called_once_with(other)

    @responses.activate
    @override_settings(GITEA_ALERTS_WEBHOOK_TARGETS=("gitea-alerts-app",))
    def test_configured_extra_targets_are_delivered_here_too(self):
        self.mock_create()
        with patch.object(self.stock, "execute") as stock_execute:
            self.handler().execute(invocation(self.event, target="gitea-alerts-app"))
        stock_execute.assert_not_called()
        assert core.read_link(self.group)["id"] == "7"

    @responses.activate
    def test_a_project_without_gitea_settings_is_skipped_quietly(self):
        ProjectOption.objects.set_value(self.project, "gitea_alerts:enabled", False)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.handler().execute(invocation(self.event))
        assert not responses.calls
        assert "not enabled and configured" in logs.records[-1].getMessage()

    @responses.activate
    def test_a_gitea_failure_is_logged_and_never_raised_into_the_rule(self):
        self.mock_create(status=500)
        with self.assertLogs(LOGGER, level="ERROR") as logs:
            assert self.handler().execute(invocation(self.event)) is None
        record = logs.records[-1]
        assert record.getMessage() == "sentry-gitea: alert delivery failed"
        assert record.status == 500 and record.group_id == self.group.id
        assert TOKEN not in "".join(str(vars(r)) for r in logs.records)
        assert core.read_link(self.group) is None

    @responses.activate
    def test_rate_limited_alerts_are_skipped(self):
        with patch("sentry_gitea.alerts.delivery.rate_limited", return_value=True):
            with self.assertLogs(LOGGER, level="INFO") as logs:
                self.handler().execute(invocation(self.event))
        assert not responses.calls
        assert "rate limited" in logs.records[-1].getMessage()

    @responses.activate
    def test_activity_triggered_actions_are_ignored(self):
        activity = Activity(
            project=self.project, group=self.group, type=ActivityType.SET_RESOLVED.value
        )
        self.handler().execute(invocation(activity))
        assert not responses.calls

    def test_the_bridge_can_be_switched_off(self):
        stock_plugin = action_handler_registry.get(Action.Type.PLUGIN)
        for value in (False, 0, None, "false", "off", "0"):
            with override_settings(GITEA_ALERTS_BRIDGE=value):
                assert bridge.install() is False, value
            assert action_handler_registry.get(Action.Type.WEBHOOK) is self.stock
            assert action_handler_registry.get(Action.Type.PLUGIN) is stock_plugin
            assert bridge.active() is False

    def test_install_never_raises_whatever_the_settings_or_sentry_do(self):
        for value in (True, 7, {"a": 1}, ["ok-app", 3, None], "one-app"):
            bridge.uninstall()
            with override_settings(GITEA_ALERTS_WEBHOOK_TARGETS=value):
                assert bridge.install() is True, value
                assert "gitea_alerts" in bridge.targets()
                assert all(isinstance(target, str) for target in bridge.targets())
        with override_settings(GITEA_ALERTS_WEBHOOK_TARGETS=["ok-app", 3, None]):
            assert bridge.targets() == {"gitea_alerts", "ok-app"}
        bridge.uninstall()
        with patch.object(bridge, "_wrap", side_effect=TypeError("unexpected registry")):
            with self.assertLogs(LOGGER, level="ERROR") as logs:
                assert bridge.install() is False
        assert logs.records[-1].getMessage() == "sentry-gitea: alert bridge not installed"
        assert bridge.active() is False

    # "Send a notification (for all legacy integrations)": the plugin action.

    def plugin_handler(self):
        stock = action_handler_registry.get(Action.Type.PLUGIN)
        assert bridge.install() is True
        handler = action_handler_registry.get(Action.Type.PLUGIN)
        assert handler is not stock and issubclass(handler, stock)
        return handler, stock

    def test_the_plugin_handler_is_wrapped_and_restored_too(self):
        handler, stock = self.plugin_handler()
        assert handler.config_schema == stock.config_schema and handler.group == stock.group
        bridge.install()
        assert action_handler_registry.get(Action.Type.PLUGIN) is handler
        bridge.uninstall()
        assert action_handler_registry.get(Action.Type.PLUGIN) is stock

    @responses.activate
    def test_without_legacy_plugins_the_plugin_action_also_files_in_gitea(self):
        self.mock_create()
        handler, stock = self.plugin_handler()
        action = invocation(self.event, target=None)
        with (
            patch.object(stock, "execute", return_value="stock result") as stock_execute,
            patch.object(bridge, "legacy_plugin_registered", return_value=False),
        ):
            assert handler.execute(action) == "stock result"
        stock_execute.assert_called_once_with(action)
        assert core.read_link(self.group)["id"] == "7"

    @responses.activate
    def test_with_legacy_plugins_the_plugin_action_is_left_to_the_plugin(self):
        handler, stock = self.plugin_handler()
        action = invocation(self.event, target=None)
        with (
            patch.object(stock, "execute") as stock_execute,
            patch.object(bridge, "legacy_plugin_registered", return_value=True),
        ):
            handler.execute(action)
        stock_execute.assert_called_once_with(action)
        assert not responses.calls, "the plugin delivers; a second delivery would duplicate it"

    @responses.activate
    def test_the_plugin_action_survives_a_failing_original_and_stays_quiet_elsewhere(self):
        self.mock_create()
        handler, stock = self.plugin_handler()
        with (
            patch.object(stock, "execute", side_effect=RuntimeError("legacy webhooks failed")),
            patch.object(bridge, "legacy_plugin_registered", return_value=False),
        ):
            with self.assertRaises(RuntimeError):
                handler.execute(invocation(self.event, target=None))
            assert core.read_link(self.group)["id"] == "7"
            # A project that does not use Gitea: nothing sent, and no log line per alert.
            ProjectOption.objects.set_value(self.project, "gitea_alerts:enabled", False)
            before = len(responses.calls)
            with patch.object(stock, "execute"), self.assertNoLogs(LOGGER, level="INFO"):
                handler.execute(invocation(self.event, target=None))
            assert len(responses.calls) == before

    def test_legacy_plugin_detection_matches_this_sentry(self):
        try:
            import sentry.plugins.bases.notify  # noqa: F401
        except ImportError:
            assert bridge.legacy_plugin_registered() is False
        else:
            assert bridge.legacy_plugin_registered() is True
