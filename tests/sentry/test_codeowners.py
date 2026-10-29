"""CODEOWNERS retrieval: location order, email owners only, Sentry's code-mapping endpoint."""

import responses
from sentry.models.commit import Commit
from sentry.shared_integrations.exceptions import ApiError
from sentry.tasks.codeowners.code_owners_auto_sync import code_owners_auto_sync
from sentry.testutils.silo import no_silo_test
from sync_fixtures import CONTENTS_EXT, SHA_B, WEBHOOK_REPO, SyncFixture, contents_ext_file
from test_integration_provider import API, PUBLIC

CONTENTS = CONTENTS_EXT
RAW = API + "/repos/team/repo/raw/"
EMAIL_ONLY = "# Owners\n*.py owner@example.com\n/docs/ docs@example.com # inline comment @ignored\n"


@no_silo_test
class TestCodeowners(SyncFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.add_repo()
        responses.start()
        self.addCleanup(responses.reset)
        self.addCleanup(responses.stop)
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)

    def mapping(self):
        return self.create_code_mapping(
            self.project,
            repo=self.repo,
            organization_integration=self.org_integration,
            default_branch="main",
        )

    def endpoint(self, mapping):
        return (
            f"/api/0/organizations/{self.organization.slug}/code-mappings/{mapping.id}/codeowners/"
        )

    def test_locations_are_tried_in_documented_order(self):
        assert self.installation.codeowners_locations == [
            "CODEOWNERS",
            ".gitea/CODEOWNERS",
            ".github/CODEOWNERS",
            "docs/CODEOWNERS",
        ]
        responses.get(CONTENTS + "CODEOWNERS", status=404)
        responses.get(CONTENTS + ".gitea/CODEOWNERS", json=contents_ext_file(".gitea/CODEOWNERS"))
        responses.get(RAW + ".gitea/CODEOWNERS", body=EMAIL_ONLY)
        assert self.installation.get_codeowner_file(self.repo, "main") == {
            "filepath": ".gitea/CODEOWNERS",
            "raw": EMAIL_ONLY,
            "html_url": PUBLIC + "/team/repo/src/branch/main/.gitea/CODEOWNERS",
        }
        tried = [
            c.request.url.split("/contents-ext/")[1]
            for c in responses.calls
            if "/contents-ext/" in c.request.url
        ]
        assert tried == ["CODEOWNERS?ref=main", ".gitea/CODEOWNERS?ref=main"]

    def test_code_mapping_endpoint_returns_email_owners(self):
        mapping = self.mapping()
        for path in ["CODEOWNERS", ".gitea/CODEOWNERS", ".github/CODEOWNERS"]:
            responses.get(CONTENTS + path, status=404)
        responses.get(CONTENTS + "docs/CODEOWNERS", json=contents_ext_file("docs/CODEOWNERS"))
        responses.get(RAW + "docs/CODEOWNERS", body=EMAIL_ONLY)
        result = self.client.get(self.endpoint(mapping))
        assert result.status_code == 200, result.data
        assert result.data["filepath"] == "docs/CODEOWNERS" and result.data["raw"] == EMAIL_ONLY

    def test_handles_are_rejected_and_existing_ownership_is_preserved(self):
        mapping = self.mapping()
        previous = self.create_codeowners(self.project, mapping, raw="*.py owner@example.com")
        responses.get(CONTENTS + "CODEOWNERS", json=contents_ext_file("CODEOWNERS"))
        responses.get(
            RAW + "CODEOWNERS",
            body="*.py owner@example.com @alice\n/ui/ @team/web @alice @bob @c @d @e\n",
        )
        message = "Gitea CODEOWNERS supports email owners only; found @handles: "
        with self.assertRaisesMessage(ApiError, message + "@alice, @team/web, @bob, @c, @d"):
            self.installation.get_codeowner_file(self.repo, "main")
        result = self.client.get(self.endpoint(mapping))
        assert result.status_code == 400
        assert result.data["detail"].startswith(message + "@alice")
        # Automatic refreshes fail the same way and leave the imported rules untouched.
        commit = Commit.objects.create(
            organization_id=self.organization.id, repository_id=self.repo.id, key=SHA_B
        )
        with self.assertRaises(ApiError):
            code_owners_auto_sync(commit_id=commit.id)
        previous.refresh_from_db()
        assert previous.raw == "*.py owner@example.com"

    def test_auto_sync_refreshes_email_only_files(self):
        mapping = self.mapping()
        codeowners = self.create_codeowners(self.project, mapping, raw="*.py old@example.com")
        responses.get(CONTENTS + "CODEOWNERS", json=contents_ext_file("CODEOWNERS"))
        responses.get(RAW + "CODEOWNERS", body="*.py owner@example.com\n")
        commit = Commit.objects.create(
            organization_id=self.organization.id, repository_id=self.repo.id, key=SHA_B
        )
        code_owners_auto_sync(commit_id=commit.id)
        codeowners.refresh_from_db()
        assert codeowners.raw == "*.py owner@example.com\n"

    def test_missing_file_is_not_found(self):
        mapping = self.mapping()
        for path in self.installation.codeowners_locations:
            responses.get(CONTENTS + path, status=404)
        assert self.installation.get_codeowner_file(self.repo, "main") is None
        assert self.client.get(self.endpoint(mapping)).status_code == 404
