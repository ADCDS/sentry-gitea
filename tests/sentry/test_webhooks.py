"""Signed webhook endpoint: per-repository secrets, dispatch, commits, PRs and fan-out."""

import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import Client
from sentry.constants import ObjectStatus
from sentry.models.commit import Commit
from sentry.models.commitauthor import CommitAuthor
from sentry.models.commitfilechange import CommitFileChange
from sentry.models.pullrequest import PullRequest
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import ApiError
from sentry.testutils.helpers.task_runner import BurstTaskRunner
from sentry.testutils.silo import no_silo_test
from sync_fixtures import (
    REPO_SECRET,
    SHA_A,
    SHA_B,
    SHA_C,
    WEBHOOK_REPO,
    ZERO,
    SyncFixture,
    gitea_push_commit,
)
from test_integration_provider import METADATA, PUBLIC

from sentry_gitea.api.webhooks import MAX_PAYLOAD_BYTES
from sentry_gitea.integration import commits

LOGGER = "sentry_gitea.integration.webhooks"
BACKFILL = "sentry_gitea.integration.tasks.backfill_push"


def backfills(burst):
    return [(list(args), kwargs) for task, args, kwargs in burst.queue if task.name == BACKFILL]


def signature(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@no_silo_test
class TestWebhookAuthentication(SyncFixture):
    def url(self, integration_id=None):
        return self.hook_url(integration_id=integration_id)

    def test_signature_is_checked_before_json_and_never_logged(self):
        self.add_repo()
        anonymous = Client(enforce_csrf_checks=True)
        body = b"{not json"
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            assert (
                anonymous.post(self.url(), body, content_type="application/json").status_code == 401
            )
            signature = hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
            response = anonymous.post(
                self.url(), body, content_type="application/json", HTTP_X_GITEA_SIGNATURE=signature
            )
            assert response.status_code == 401
            # The right key over the exact bytes passes authentication; then JSON fails.
            assert self.signed("push", body).status_code == 400
        output = "\n".join(logs.output)
        assert "sentry-gitea: webhook rejected" in output
        assert METADATA["webhook_secret"] not in output and "not json" not in output
        assert REPO_SECRET not in output
        reasons = [r.reason for r in logs.records]
        assert reasons == ["signature", "signature", "payload"]
        assert not Commit.objects.exists()

    def test_tampered_body_and_signature_formats_are_rejected(self):
        self.add_repo()
        payload = self.push_payload()
        good = json.dumps(payload).encode()
        signature = hmac.new(METADATA["webhook_secret"].encode(), good, hashlib.sha256).hexdigest()
        tampered = good.replace(b"Fix error", b"Fix errox")
        assert self.signed("push", tampered, signature=signature).status_code == 401
        # The integration-wide secret is only a key for the per-repository secrets.
        assert self.signed("push", good, signature=signature).status_code == 401
        signature = hmac.new(REPO_SECRET.encode(), good, hashlib.sha256).hexdigest()
        assert self.signed("push", good, signature="sha256=" + signature).status_code == 401
        assert self.signed("push", good, signature=signature.upper()).status_code == 204
        assert self.signed("push", good, signature="0" * 64).status_code == 401

    def test_empty_secret_never_authenticates(self):
        self.add_repo()
        self.integration.update(metadata={**METADATA, "webhook_secret": ""})
        empty_key = hmac.new(b"", b"{}", hashlib.sha256).hexdigest()
        assert self.signed("push", b"{}", signature=empty_key).status_code == 401

    def test_body_size_is_bounded_before_reading_further(self):
        body = b" " * (MAX_PAYLOAD_BYTES + 1)
        signature = hmac.new(REPO_SECRET.encode(), body, hashlib.sha256).hexdigest()
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            assert self.signed("push", body, signature=signature).status_code == 400
        assert logs.records[0].reason == "payload"

    def test_unknown_disabled_or_foreign_integrations_are_404(self):
        payload = self.push_payload()
        assert self.signed("push", payload, integration_id=999999).status_code == 404
        assert self.signed("push", payload, integration_id="9" * 25).status_code == 404
        other = self.create_integration(
            organization=self.organization, provider="gitlab", external_id="other"
        )
        assert self.signed("push", payload, integration_id=other.id).status_code == 404
        self.org_integration.update(status=ObjectStatus.DISABLED)
        assert self.signed("push", payload).status_code == 404
        self.org_integration.update(status=ObjectStatus.ACTIVE)
        self.integration.update(status=ObjectStatus.DISABLED)
        assert self.signed("push", payload).status_code == 404

    def test_only_post_and_csrf_exempt(self):
        assert self.client.get(self.url()).status_code == 405
        assert (
            Client(enforce_csrf_checks=True)
            .post(self.url(), b"{}", content_type="application/json")
            .status_code
            == 401
        )

    def test_unknown_events_repositories_owners_and_unhooked_repos_are_acknowledged(self):
        payload = self.push_payload()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            assert self.signed("push", payload).status_code == 204  # repository not added
            repo = self.add_repo(hook_id=None)
            assert self.signed("push", payload).status_code == 204  # no hook installed
            repo.update(config={**repo.config, "hook_id": 4})
            assert self.signed("release", {"anything": True}).status_code == 204
            assert self.signed("issue_comment", payload).status_code == 204
            moved = {**payload, "repository": {**WEBHOOK_REPO, "owner": {"id": 99}}}
            assert self.signed("push", moved).status_code == 204
            assert self.signed("issues", self.issue_payload("edited")).status_code == 204
        assert [r.reason for r in logs.records] == [
            "repository",
            "repository",
            "event",
            "event",
            "owner",
            "action",
        ]
        assert all(r.getMessage() == "sentry-gitea: webhook ignored" for r in logs.records)
        assert not Commit.objects.exists()

    def test_malformed_payload_for_known_events(self):
        self.add_repo()
        for event, payload in [
            ("push", {"repository": WEBHOOK_REPO}),
            ("push", self.push_payload(before="nope")),
            ("issues", {**self.issue_payload("closed"), "issue": None}),
            ("pull_request", {**self.pull_payload(), "pull_request": {"number": "9"}}),
            ("push", [1, 2]),
        ]:
            assert self.signed(event, payload).status_code == 400, (event, payload)

    def test_each_repository_has_its_own_secret_and_url(self):
        repo_41 = self.add_repo()
        other = {**WEBHOOK_REPO, "id": 42, "full_name": "team/other", "name": "other"}
        repo_42 = Repository.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            provider="integrations:gitea",
            name="team/other",
            external_id="42",
            config={"path": "team/other", "repo_id": 42, "hook_id": 5},
        )
        secret_42 = hmac.new(
            METADATA["webhook_secret"].encode(), b"repo:42", hashlib.sha256
        ).hexdigest()
        assert secret_42 != REPO_SECRET
        own = self.push_payload(repository=other)
        body = json.dumps(own).encode()
        url_42 = self.hook_url(42)
        assert self.signed("push", body, url=url_42, secret=secret_42).status_code == 204
        assert Commit.objects.filter(repository_id=repo_42.id).count() == 1
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            # Repository 41's secret cannot sign for 42's URL...
            assert self.signed("push", body, url=url_42).status_code == 401
            # ...nor make 41's URL process a payload naming repository 42.
            assert self.signed("push", own).status_code == 400
            # And 42's (leaked) secret cannot forge deliveries for repository 41.
            forged = json.dumps(self.push_payload()).encode()
            assert self.signed("push", forged, url=url_42, secret=secret_42).status_code == 400
            assert self.signed("push", forged, secret=secret_42).status_code == 401
        assert [r.reason for r in logs.records] == [
            "signature",
            "repository",
            "repository",
            "signature",
        ]
        assert not Commit.objects.filter(repository_id=repo_41.id).exists()

    def test_urls_without_a_repository_id_are_gone(self):
        self.add_repo()
        body = json.dumps(self.push_payload()).encode()
        legacy = f"/extensions/gitea/webhook/{self.integration.id}/"
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            for key in (METADATA["webhook_secret"], REPO_SECRET):
                response = self.signed("push", body, url=legacy, signature=signature(key, body))
                assert response.status_code == 404
            for repo_id in ("0", "041", "9" * 19):
                assert self.signed("push", body, url=self.hook_url(repo_id)).status_code == 404
        assert {r.reason for r in logs.records} == {"url"}
        assert self.client.get(legacy).status_code == 405
        assert not Commit.objects.exists()


@no_silo_test
class TestPushWebhook(SyncFixture):
    def test_push_upserts_commits_authors_and_file_changes_idempotently(self):
        repo = self.add_repo()
        payload = self.push_payload()
        with BurstTaskRunner() as burst:
            assert self.signed("push", payload).status_code == 204
            assert self.signed("push", payload).status_code == 204
            assert backfills(burst) == []
        commit = Commit.objects.get(repository_id=repo.id)
        assert commit.key == SHA_B and commit.message == "Fix error"
        assert commit.author.email == "author@example.com" and commit.author.name == "Author"
        assert set(
            CommitFileChange.objects.filter(commit_id=commit.id).values_list("filename", "type")
        ) == {("new.py", "A"), ("old.py", "D"), ("app.py", "M")}
        assert CommitAuthor.objects.filter(organization_id=self.organization.id).count() == 1

    def test_truncated_push_enqueues_backfill_but_branch_creation_does_not(self):
        repo = self.add_repo()
        commits = [gitea_push_commit(f"{i:040x}") for i in range(1, 6)]
        with BurstTaskRunner() as burst:
            assert (
                self.signed("push", self.push_payload(commits, total_commits=7)).status_code == 204
            )
            assert backfills(burst) == [([repo.id, SHA_A, SHA_B], {})]
            burst.queue.clear()
            created = self.push_payload(commits, total_commits=40, before=ZERO)
            assert self.signed("push", created).status_code == 204
            assert backfills(burst) == []
        assert Commit.objects.filter(repository_id=repo.id).count() == 5

    def test_skip_marker_and_branch_deletion(self):
        repo = self.add_repo()
        skipped = gitea_push_commit(SHA_C, "Experiment #skipsentry")
        with BurstTaskRunner() as burst:
            assert self.signed("push", self.push_payload([skipped])).status_code == 204
            deleted = self.push_payload([], after=ZERO, total_commits=0)
            assert self.signed("push", deleted).status_code == 204
            assert backfills(burst) == []
        assert not Commit.objects.filter(repository_id=repo.id).exists()

    def test_gitea_codeowners_change_schedules_ownership_sync_once(self):
        self.add_repo()
        commit = gitea_push_commit(SHA_B, added=[], removed=[], modified=[".gitea/CODEOWNERS"])
        with patch("sentry_gitea.integration.commits.code_owners_auto_sync.delay") as sync:
            assert self.signed("push", self.push_payload([commit])).status_code == 204
            assert self.signed("push", self.push_payload([commit])).status_code == 204
        sync.assert_called_once_with(commit_id=Commit.objects.get(key=SHA_B).id)

    def test_fan_out_to_every_organization_sharing_the_integration(self):
        other_org = self.create_organization(owner=self.user)
        self.integration.add_organization(other_org, self.user, default_auth_id=self.identity.id)
        first, second = self.add_repo(), self.add_repo(organization=other_org)
        assert self.signed("push", self.push_payload()).status_code == 204
        assert Commit.objects.filter(repository_id=first.id).count() == 1
        assert Commit.objects.filter(repository_id=second.id).count() == 1
        authors = CommitAuthor.objects.filter(email="author@example.com")
        assert {a.organization_id for a in authors} == {self.organization.id, other_org.id}

    def test_one_organization_failing_does_not_stop_the_others(self):
        other_org = self.create_organization(owner=self.user)
        self.integration.add_organization(other_org, self.user, default_auth_id=self.identity.id)
        first, second = self.add_repo(), self.add_repo(organization=other_org)
        original = commits.store_commits

        def failing_first(repo, rows):
            if repo.id == first.id:
                raise failure
            return original(repo, rows)

        for failure, status in ((ApiError("Gitea unavailable", code=502), 503), (KeyError(), 500)):
            Commit.objects.all().delete()
            with (
                self.subTest(error=type(failure).__name__),
                patch("sentry_gitea.integration.webhooks.store_commits", failing_first),
                self.assertLogs(LOGGER, level="WARNING") as logs,
            ):
                assert self.signed("push", self.push_payload()).status_code == status
            assert not Commit.objects.filter(repository_id=first.id).exists()
            assert Commit.objects.filter(repository_id=second.id).count() == 1
            [record] = logs.records
            assert record.getMessage() == "sentry-gitea: sync failed"
            assert (record.organization_id, record.repository_id) == (
                self.organization.id,
                first.id,
            )
            assert record.error_type == type(failure).__name__
        # Both failure kinds in one delivery: the unexpected one decides the status.
        third_org = self.create_organization(owner=self.user)
        self.integration.add_organization(third_org, self.user, default_auth_id=self.identity.id)
        third = self.add_repo(organization=third_org)

        def failing_two(repo, rows):
            if repo.id in {first.id, second.id}:
                raise ApiError("x", code=502) if repo.id == first.id else KeyError()
            return original(repo, rows)

        with (
            patch("sentry_gitea.integration.webhooks.store_commits", failing_two),
            self.assertLogs(LOGGER, level="WARNING"),
        ):
            assert self.signed("push", self.push_payload()).status_code == 500
        assert Commit.objects.filter(repository_id=third.id).count() == 1

    def test_rename_updates_repository_path_and_public_url(self):
        repo = self.add_repo()
        renamed = {**WEBHOOK_REPO, "full_name": "team/renamed", "default_branch": "trunk"}
        assert self.signed("push", self.push_payload(repository=renamed)).status_code == 204
        repo.refresh_from_db()
        assert repo.name == "team/renamed" and repo.url == PUBLIC + "/team/renamed"
        assert repo.config["path"] == "team/renamed" and repo.config["default_branch"] == "trunk"
        assert repo.config["hook_id"] == 4 and repo.external_id == "41"
        assert Commit.objects.get(repository_id=repo.id).key == SHA_B


@no_silo_test
class TestPullRequestWebhook(SyncFixture):
    def test_pull_requests_are_upserted_by_number(self):
        repo = self.add_repo()
        assert self.signed("pull_request", self.pull_payload()).status_code == 204
        assert self.signed("pull_request", self.pull_payload()).status_code == 204
        row = PullRequest.objects.get(repository_id=repo.id)
        assert (row.key, row.title, row.message) == ("9", "Fix error", "Details")
        assert row.author.email == "a@x.example" and row.merge_commit_sha is None
        merged = self.pull_payload("closed", merged=True, merge_commit_sha=SHA_C, title="Done")
        assert self.signed("pull_request", merged).status_code == 204
        row.refresh_from_db()
        assert row.merge_commit_sha == SHA_C and row.title == "Done"
        # An older delivery cannot clear a recorded merge or author.
        hidden = {"id": 3, "login": "author", "email": "3+author@noreply.gitea.local"}
        assert (
            self.signed("pull_request", self.pull_payload("edited", user=hidden)).status_code == 204
        )
        row.refresh_from_db()
        assert row.merge_commit_sha == SHA_C and row.author.email == "a@x.example"
        assert PullRequest.objects.count() == 1
        provider = repo.get_provider()
        assert provider.pull_request_url(repo, row) == PUBLIC + "/team/repo/pulls/9"

    def test_placeholder_email_is_not_a_commit_author(self):
        repo = self.add_repo()
        hidden = {"id": 3, "login": "Author", "email": "3+author@noreply.gitea.local"}
        assert self.signed("pull_request", self.pull_payload(user=hidden)).status_code == 204
        assert PullRequest.objects.get(repository_id=repo.id).author is None
        assert not CommitAuthor.objects.exists()


@no_silo_test
class TestWebhookRepositoryScope(SyncFixture):
    def test_rows_of_other_integrations_or_inactive_rows_are_not_touched(self):
        repo = self.add_repo()
        repo.update(status=ObjectStatus.HIDDEN)
        assert self.signed("push", self.push_payload()).status_code == 204
        assert not Commit.objects.exists()
        Repository.objects.filter(id=repo.id).update(status=ObjectStatus.ACTIVE)
        assert self.signed("push", self.push_payload()).status_code == 204
        assert Commit.objects.filter(repository_id=repo.id).count() == 1
