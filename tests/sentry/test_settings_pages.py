"""The project settings pages that replace the legacy plugin settings (gone in Sentry 26.7)."""

import responses
from django.test import override_settings
from django.urls import reverse
from sentry.models.auditlogentry import AuditLogEntry
from sentry.models.groupmeta import GroupMeta
from sentry.models.options.project_option import ProjectOption
from sentry.testutils.cases import TestCase
from sentry.testutils.silo import no_silo_test

from sentry_gitea.alerts import core

BASE = "https://forge.example/git"
OTHER = "https://other-forge.example"
TOKEN = "synthetic-settings-credential"
BOT = {"id": 5, "login": "sentry-bot"}
REPO = {"id": 41, "full_name": "owner/repo", "default_branch": "main"}
FORM = {
    "action": "save",
    "issue_enabled": "on",
    "base_url": BASE,
    "public_url": "",
    "token": TOKEN,
    "repo": "owner/repo",
    "default_labels": "bug, sentry",
    "alerts_enabled": "on",
    "labels": "source:sentry",
    "severity_labels": "on",
    "comment_on_repeat": "on",
}


def option(project, key):
    ProjectOption.objects.clear_local_cache()
    return ProjectOption.objects.get_value(project, key, None)


@no_silo_test
@override_settings(ROOT_URLCONF="sentry_gitea.urls")
class TestProjectSettingsPage(TestCase):
    def setUp(self):
        super().setUp()
        self.login_as(self.user)
        self.path = reverse(
            "sentry-gitea-project-settings",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_id_or_slug": self.project.slug,
            },
        )
        self.index = reverse(
            "sentry-gitea-settings", kwargs={"organization_slug": self.organization.slug}
        )

    def save_directly(self, base=BASE):
        for key, value in {
            "gitea:enabled": True,
            "gitea:base_url": base,
            "gitea:repo": "owner/repo",
            "gitea:token": TOKEN,
            "gitea_alerts:enabled": True,
        }.items():
            ProjectOption.objects.set_value(self.project, key, value)

    def mock_gitea(self, base=BASE, user_status=200):
        responses.get(base + "/api/v1/user", json=BOT, status=user_status)
        responses.get(base + "/api/v1/repos/owner/repo", json=REPO)

    def test_only_users_who_may_write_the_project_get_the_page(self):
        self.save_directly()
        outsider = self.create_user()
        member = self.create_user()
        self.create_member(
            user=member, organization=self.organization, role="member", teams=[self.team]
        )
        for user in (outsider, member):
            self.login_as(user)
            assert self.client.get(self.path).status_code != 200
            denied = self.client.post(self.path, {**FORM, "repo": "owner/elsewhere"})
            assert denied.status_code != 200
            assert option(self.project, "gitea:repo") == "owner/repo"
        self.client.logout()
        anonymous = self.client.get(self.path)
        assert anonymous.status_code == 302 and "/auth/login/" in anonymous["Location"]

    def test_a_team_admin_may_configure_and_a_project_without_a_team_is_not_locked(self):
        # What Sentry's project settings API allows, and so what the legacy pages allowed.
        self.save_directly()
        team_admin = self.create_user()
        member = self.create_member(user=team_admin, organization=self.organization, role="member")
        self.create_team_membership(team=self.team, member=member, role="admin")
        self.login_as(team_admin)
        assert self.client.get(self.path).status_code == 200
        assert self.path in self.client.get(self.index).content.decode()

        self.login_as(self.user)  # the organization's owner
        orphan = self.create_project(organization=self.organization, teams=[])
        orphan_path = reverse(
            "sentry-gitea-project-settings",
            kwargs={
                "organization_slug": self.organization.slug,
                "project_id_or_slug": orphan.slug,
            },
        )
        assert self.client.get(orphan_path).status_code == 200
        assert orphan_path in self.client.get(self.index).content.decode()
        self.login_as(team_admin)  # no role over a project outside their team
        assert self.client.get(orphan_path).status_code != 200
        assert orphan_path not in self.client.get(self.index).content.decode()

    def test_the_page_shows_saved_values_and_never_the_token(self):
        self.save_directly()
        page = self.client.get(self.path)
        assert page.status_code == 200
        assert page["Cache-Control"] == "no-store"
        body = page.content.decode()
        assert f'value="{BASE}"' in body and 'value="owner/repo"' in body
        assert "A token is saved" in body
        assert TOKEN not in body
        assert 'name="csrfmiddlewaretoken"' in body

    @responses.activate
    def test_saving_checks_gitea_then_stores_what_the_alerts_read(self):
        self.mock_gitea()
        saved = self.client.post(self.path, FORM)
        assert saved.status_code == 302 and saved["Location"] == self.path + "?saved=1"
        assert [c.request.url for c in responses.calls] == [
            BASE + "/api/v1/user",
            BASE + "/api/v1/repos/owner/repo",
        ]
        assert option(self.project, "gitea:base_url") == BASE
        assert option(self.project, "gitea:token") == TOKEN
        assert option(self.project, "gitea:default_labels") == "bug, sentry"
        assert option(self.project, "gitea:enabled") is True
        assert option(self.project, "gitea_alerts:enabled") is True
        assert option(self.project, "gitea_alerts:fix_hint") is False
        assert option(self.project, "gitea_alerts:severity_labels") is True
        # Exactly what delivery reads.
        assert core.alerts_ready(self.project) is True
        assert core.configuration(self.project)["repo"] == "owner/repo"
        entry = AuditLogEntry.objects.get(organization_id=self.organization.id)
        assert entry.target_object == self.project.id and entry.actor_id == self.user.id
        assert entry.data == {"integration": "gitea", "project": self.project.slug}
        assert "Settings saved." in self.client.get(saved["Location"]).content.decode()

    @responses.activate
    def test_a_blank_token_is_kept_only_for_the_same_base_url(self):
        self.save_directly()
        self.mock_gitea()
        kept = self.client.post(self.path, {**FORM, "token": "", "default_labels": "kept"})
        assert kept.status_code == 302
        assert option(self.project, "gitea:token") == TOKEN
        assert option(self.project, "gitea:default_labels") == "kept"
        responses.calls.reset()
        moved = self.client.post(self.path, {**FORM, "token": "", "base_url": OTHER})
        assert moved.status_code == 400
        assert core.TOKEN_REENTRY in moved.content.decode()
        assert not responses.calls, "the saved token must not be sent to a new server"
        assert option(self.project, "gitea:base_url") == BASE

    @responses.activate
    def test_a_gitea_refusal_is_shown_and_nothing_is_saved(self):
        self.save_directly()
        self.mock_gitea(user_status=401)
        refused = self.client.post(self.path, {**FORM, "token": "typed-secret", "repo": "a/b"})
        assert refused.status_code == 400
        body = refused.content.decode()
        assert "Gitea rejected the token" in body
        assert "typed-secret" not in body and TOKEN not in body
        assert 'value="a/b"' in body, "what was typed is kept for correction"
        assert option(self.project, "gitea:repo") == "owner/repo"
        assert option(self.project, "gitea:token") == TOKEN
        assert not AuditLogEntry.objects.filter(organization_id=self.organization.id).exists()

    def test_alerts_cannot_be_enabled_without_the_issue_settings(self):
        form = {k: v for k, v in FORM.items() if k != "issue_enabled"}
        refused = self.client.post(self.path, form)
        assert refused.status_code == 400
        assert "Enable the issue settings first" in refused.content.decode()
        assert option(self.project, "gitea_alerts:enabled") is None

    def test_switching_off_keeps_the_connection_for_later(self):
        self.save_directly()
        off = self.client.post(self.path, {"action": "save", "labels": "source:sentry"})
        assert off.status_code == 302
        assert option(self.project, "gitea:enabled") is False
        assert option(self.project, "gitea_alerts:enabled") is False
        assert option(self.project, "gitea:base_url") == BASE
        assert option(self.project, "gitea:token") == TOKEN
        assert core.alerts_ready(self.project) is False

    def test_typed_values_are_escaped_when_the_form_comes_back(self):
        hostile = '"><script>alert(1)</script>'
        back = self.client.post(self.path, {**FORM, "base_url": hostile, "repo": hostile})
        assert back.status_code == 400
        body = back.content.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body

    def test_posts_need_the_csrf_token(self):
        self.save_directly()
        self.client.handler.enforce_csrf_checks = True
        assert self.client.post(self.path, FORM).status_code == 403
        assert option(self.project, "gitea:default_labels") is None

    @responses.activate
    def test_the_test_button_reads_gitea_and_writes_nothing(self):
        self.save_directly()
        self.mock_gitea()
        tested = self.client.post(self.path, {"action": "test"})
        assert tested.status_code == 302 and tested["Location"] == self.path + "?tested=1"
        assert {c.request.method for c in responses.calls} == {"GET"}
        assert not AuditLogEntry.objects.filter(organization_id=self.organization.id).exists()
        ProjectOption.objects.set_value(self.project, "gitea:token", "")
        unsaved = self.client.post(self.path, {"action": "test"})
        assert unsaved.status_code == 400
        assert "Save a base URL, a repository and a token first" in unsaved.content.decode()

    def test_deleting_removes_every_setting_and_the_token_but_no_issue_link(self):
        self.save_directly()
        ProjectOption.objects.set_value(self.project, "gitea_alerts:labels", "source:sentry")
        ProjectOption.objects.set_value(self.project, "unrelated:option", "kept")
        GroupMeta.objects.set_value(self.group, "gitea:issue_id", "7")
        refused = self.client.post(self.path, {"action": "delete"})
        assert refused.status_code == 400 and "Tick the box" in refused.content.decode()
        assert ProjectOption.objects.get_value(self.project, "gitea:token")
        deleted = self.client.post(self.path, {"action": "delete", "confirm": "on"})
        assert deleted.status_code == 302 and deleted["Location"] == self.path + "?deleted=1"
        keys = set(ProjectOption.objects.filter(project=self.project).values_list("key", flat=True))
        assert "unrelated:option" in keys
        assert not {k for k in keys if k.startswith(("gitea:", "gitea_alerts:"))}
        assert core.alerts_ready(self.project) is False
        assert GroupMeta.objects.get_value(self.group, "gitea:issue_id", None) == "7"
        entry = AuditLogEntry.objects.get(organization_id=self.organization.id)
        assert entry.data["deleted"] is True
        page = self.client.get(deleted["Location"]).content.decode()
        assert "were deleted from Sentry" in page and "A token is saved" not in page

    def test_forgetting_links_unblocks_alerts_after_a_move_and_touches_nothing_else(self):
        self.save_directly()
        for field, value in {
            "id": "7",
            "repo": "owner/repo",
            "base_url": "http://old:3000",
        }.items():
            GroupMeta.objects.set_value(self.group, f"gitea:issue_{field}", value)
        GroupMeta.objects.set_value(self.group, "unrelated:key", "kept")
        elsewhere = self.create_group(project=self.create_project(organization=self.organization))
        GroupMeta.objects.set_value(elsewhere, "gitea:issue_id", "9")

        refused = self.client.post(self.path, {"action": "unlink"})
        assert refused.status_code == 400 and "Tick the box" in refused.content.decode()
        done = self.client.post(self.path, {"action": "unlink", "confirm": "on"})
        assert done.status_code == 302 and done["Location"] == self.path + "?unlinked=1"
        GroupMeta.objects.clear_local_cache()
        assert core.read_link(self.group) is None
        GroupMeta.objects.populate_cache([self.group, elsewhere])
        assert GroupMeta.objects.get_value(self.group, "unrelated:key", None) == "kept"
        assert GroupMeta.objects.get_value(elsewhere, "gitea:issue_id", None) == "9"
        assert option(self.project, "gitea:token") == TOKEN, "settings are not links"
        entry = AuditLogEntry.objects.get(organization_id=self.organization.id)
        assert entry.data["links_cleared"] == 1
        assert "links of 1 Sentry issue" in self.client.get(done["Location"]).content.decode()

    def test_the_index_lists_only_projects_the_user_may_configure(self):
        self.save_directly()
        listing = self.client.get(self.index)
        assert listing.status_code == 200
        body = listing.content.decode()
        assert self.path in body and "owner/repo" in body and TOKEN not in body
        member = self.create_user()
        self.create_member(
            user=member, organization=self.organization, role="member", teams=[self.team]
        )
        self.login_as(member)
        other = self.client.get(self.index)
        assert self.path not in other.content.decode()
        assert self.client.post(self.index).status_code in (403, 405)
