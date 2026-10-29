"""The bounded `backfill_push` taskworker task, run directly and from a truncated push."""

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import responses
from sentry.constants import ObjectStatus
from sentry.models.commit import Commit
from sentry.models.commitfilechange import CommitFileChange
from sentry.testutils.helpers.task_runner import BurstTaskRunner
from sentry.testutils.silo import no_silo_test
from sync_fixtures import (
    COMMITS,
    SHA_A,
    SHA_B,
    WEBHOOK_REPO,
    ZERO,
    SyncFixture,
    api_commit,
    gitea_push_commit,
    sha,
)
from test_integration_provider import API

from sentry_gitea.integration.tasks import BACKFILL_LIMIT, backfill_push

LOGGER = "sentry_gitea.integration.tasks"


def paged_commits(rows, page_size, total=None):
    def callback(request):
        params = parse_qs(urlsplit(request.url).query)
        assert params["sha"] == [SHA_B] and params["not"] == [SHA_A]
        assert params["files"] == ["true"]
        start = (int(params["page"][0]) - 1) * page_size
        headers = {"X-Total-Count": str(len(rows) if total is None else total)}
        return 200, headers, json.dumps(rows[start : start + page_size])

    return callback


@no_silo_test
class TestBackfill(SyncFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.add_repo()

    def mock_range(self, rows, page_size=2, total=None):
        # Source features do not need the issue tracker.
        responses.get(API + "/repositories/41", json={**WEBHOOK_REPO, "has_issues": False})
        responses.add_callback(
            responses.GET, COMMITS, callback=paged_commits(rows, page_size, total)
        )

    @responses.activate
    def test_pages_whole_range_and_replays_idempotently(self):
        rows = [
            api_commit(sha(i), [{"filename": f"f{i}.py", "status": "added"}]) for i in (1, 2, 3)
        ]
        rows.append(api_commit(sha(4), message="skip #skipsentry"))
        self.mock_range(rows)
        with self.assertNoLogs(LOGGER, level="WARNING"):
            backfill_push(self.repo.id, SHA_A, SHA_B)
            backfill_push(self.repo.id, SHA_A, SHA_B)
        keys = set(Commit.objects.filter(repository_id=self.repo.id).values_list("key", flat=True))
        assert keys == {sha(1), sha(2), sha(3)}
        assert CommitFileChange.objects.filter(filename="f2.py", type="A").count() == 1
        assert len([c for c in responses.calls if "/commits" in c.request.url]) == 4

    @responses.activate
    def test_bound_of_one_thousand_is_logged_as_incomplete(self):
        rows = [api_commit(sha(i)) for i in range(1, BACKFILL_LIMIT + 1)]
        self.mock_range(rows, page_size=50, total=1500)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            backfill_push(self.repo.id, SHA_A, SHA_B)
        [record] = logs.records
        assert record.getMessage() == "sentry-gitea: backfill incomplete"
        assert (record.limit, record.fetched_count, record.total_count) == (1000, 1000, 1500)
        assert record.imported_count == 1000
        assert Commit.objects.filter(repository_id=self.repo.id).count() == BACKFILL_LIMIT

    @responses.activate
    def test_transient_errors_retry_three_times_with_backoff(self):
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.get(COMMITS, status=502, body="bad gateway")
        with patch.object(backfill_push, "apply_async") as retry:
            with self.assertLogs(LOGGER, level="WARNING") as logs:
                backfill_push(self.repo.id, SHA_A, SHA_B)
            assert retry.call_args.kwargs == {
                "args": [self.repo.id, SHA_A, SHA_B],
                "kwargs": {"attempt": 1},
                "countdown": 60,
            }
            backfill_push(self.repo.id, SHA_A, SHA_B, attempt=1)
            assert retry.call_args.kwargs["countdown"] == 300
            backfill_push(self.repo.id, SHA_A, SHA_B, attempt=2)
            assert retry.call_args.kwargs["countdown"] == 900
            retry.reset_mock()
            backfill_push(self.repo.id, SHA_A, SHA_B, attempt=3)
            retry.assert_not_called()
        record = next(r for r in logs.records if r.name == LOGGER)
        assert record.getMessage() == "sentry-gitea: sync failed" and record.retrying is True
        assert "bad gateway" not in "\n".join(logs.output)

    @responses.activate
    def test_permanent_errors_do_not_retry(self):
        responses.get(API + "/repositories/41", status=404)
        with patch.object(backfill_push, "apply_async") as retry:
            with self.assertLogs(LOGGER, level="WARNING"):
                backfill_push(self.repo.id, SHA_A, SHA_B)
        retry.assert_not_called()

    @responses.activate
    def test_ignores_zero_shas_unhooked_and_inactive_repositories(self):
        backfill_push(self.repo.id, ZERO, SHA_B)
        backfill_push(self.repo.id, SHA_A, ZERO)
        backfill_push(self.repo.id, "not-a-sha", SHA_B)
        self.repo.update(config={**self.repo.config, "hook_id": None})
        backfill_push(self.repo.id, SHA_A, SHA_B)
        self.repo.update(config={**self.repo.config, "hook_id": 4}, status=ObjectStatus.HIDDEN)
        backfill_push(self.repo.id, SHA_A, SHA_B)
        backfill_push(999999, SHA_A, SHA_B)
        assert not responses.calls

    @responses.activate
    def test_truncated_push_delivery_backfills_through_the_task(self):
        delivered = [gitea_push_commit(sha(i)) for i in range(3, 8)]
        rows = [api_commit(sha(i)) for i in range(1, 8)]
        self.mock_range(rows, page_size=3)
        with BurstTaskRunner() as burst:
            payload = self.push_payload(delivered, total_commits=7)
            assert self.signed("push", payload).status_code == 204
            assert Commit.objects.filter(repository_id=self.repo.id).count() == 5
            queued = [job for job in burst.queue if job[0].name == backfill_push.name]
            assert len(queued) == 1
            for task, args, kwargs in queued:
                task(*args, **kwargs)
        assert Commit.objects.filter(repository_id=self.repo.id).count() == 7

    def test_registered_in_the_integrations_namespace(self):
        assert backfill_push.name == "sentry_gitea.integration.tasks.backfill_push"
        assert backfill_push.namespace.name == "integrations"
