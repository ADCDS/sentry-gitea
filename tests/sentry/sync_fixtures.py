"""Shared records and Gitea 28 API/payload shapes (modules/structs) for the sync suites."""

import hashlib
import hmac
import json

import responses
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.models.grouplink import GroupLink
from sentry.models.repository import Repository
from test_integration_provider import API, METADATA, PUBLIC, REPO, USER, IntegrationFixture

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40
ZERO = "0" * 40
HOOKS = API + "/repos/team/repo/hooks"
COMMITS = API + "/repos/team/repo/commits"
CONTENTS_EXT = API + "/repos/team/repo/contents-ext/"
# The documented derivation, computed here independently of the implementation.
REPO_SECRET = hmac.new(METADATA["webhook_secret"].encode(), b"repo:41", hashlib.sha256).hexdigest()
# Gitea's PUBLIC_URL_DETECTION=auto can put an internal host into html_url; never use it.
INTERNAL = "http://gitea:3000"
WEBHOOK_REPO = {
    **REPO,
    "owner": {"id": 8, "login": "team", "email": "8+team@noreply.gitea.local"},
    "html_url": INTERNAL + "/team/repo",
    "has_issues": True,
    "permissions": {"admin": True, "push": True, "pull": True},
}


def gitea_user(user_id=USER["id"], login=USER["login"]):
    """`api.User` as Gitea 28 embeds it in issues and comments."""
    return {
        "id": user_id,
        "login": login,
        "login_name": "",
        "full_name": "",
        "email": f"{user_id}+{login}@noreply.gitea.local",
        "avatar_url": f"{INTERNAL}/avatars/{login}",
        "html_url": f"{INTERNAL}/{login}",
        "is_admin": False,
        "restricted": False,
        "active": True,
        "visibility": "public",
    }


def gitea_comment(comment_id, body, *, user=None, number=7):
    """`api.Comment` (structs/issue_comment.go); `user` is the poster."""
    return {
        "id": comment_id,
        "html_url": f"{INTERNAL}/team/repo/issues/{number}#issuecomment-{comment_id}",
        "pull_request_url": "",
        "issue_url": f"{INTERNAL}/team/repo/issues/{number}",
        "user": user or gitea_user(),
        "original_author": "",
        "original_author_id": 0,
        "body": body,
        "assets": [],
        "created_at": "2026-10-28T10:00:00Z",
        "updated_at": "2026-10-28T10:00:00Z",
    }


def gitea_hook(hook_id, url, hook_type="gitea"):
    """`api.Hook` (services/webhook/general.go ToHook): the secret is never returned."""
    return {
        "id": hook_id,
        "name": "",
        "type": hook_type,
        "url": f"{INTERNAL}/team/repo/settings/hooks/{hook_id}",
        "branch_filter": "",
        "config": {"url": url, "content_type": "json"},
        "events": ["push"],
        "authorization_header": "",
        "active": True,
        "updated_at": "2026-10-28T10:00:00Z",
        "created_at": "2026-10-28T10:00:00Z",
    }


def gitea_entry(path, entry_type="file"):
    """`api.ContentsResponse` without content, as contents-ext returns it by default."""
    name = path.rsplit("/", 1)[-1]
    return {
        "name": name,
        "path": path,
        "sha": "d" * 40,
        "type": entry_type,
        "mode": {"file": "100644", "dir": "040000", "symlink": "120000"}[entry_type],
        "size": 12 if entry_type == "file" else 0,
        "encoding": None,
        "content": None,
        "target": "target.py" if entry_type == "symlink" else None,
        "url": f"{INTERNAL}/api/v1/repos/team/repo/contents/{path}?ref=main",
        "html_url": f"{INTERNAL}/team/repo/src/branch/main/{path}",
        "git_url": f"{INTERNAL}/api/v1/repos/team/repo/git/blobs/{'d' * 40}",
        "download_url": f"{INTERNAL}/team/repo/raw/branch/main/{path}",
        "submodule_git_url": None,
        "_links": {},
    }


def contents_ext_file(path, entry_type="file"):
    """`GET /repos/{o}/{r}/contents-ext/{path}` for a single entry (structs/repo_file.go)."""
    return {"file_contents": gitea_entry(path, entry_type)}


def contents_ext_dir(path, names=("x.py",)):
    return {"dir_contents": [gitea_entry(f"{path}/{name}") for name in names]}


def gitea_push_commit(sha, message="Fix error", **changes):
    return {
        "id": sha,
        "message": message,
        "url": f"{INTERNAL}/team/repo/commit/{sha}",
        "author": {"name": "Author", "email": "author@example.com", "username": "author"},
        "committer": {"name": "Author", "email": "author@example.com", "username": "author"},
        "verification": None,
        "timestamp": "2026-10-28T10:00:00Z",
        "added": ["new.py"],
        "removed": ["old.py"],
        "modified": ["app.py"],
        **changes,
    }


def api_commit(sha, files=None, message="Fix error"):
    """`api.Commit` with `files=true`: Gitea 28 reports only added/removed/modified
    (services/convert/git_commit.go); a rename is a removal plus an addition."""
    return {
        "url": f"{INTERNAL}/api/v1/repos/team/repo/git/commits/{sha}",
        "sha": sha,
        "html_url": f"{INTERNAL}/team/repo/commit/{sha}",
        "commit": {
            "message": message,
            "author": {
                "name": "Author",
                "email": "author@example.com",
                "date": "2026-10-28T10:00Z",
            },
            "committer": {"name": "Author", "email": "author@example.com", "date": "2026-10-28"},
        },
        "author": {"id": 3, "login": "author"},
        "committer": {"id": 3, "login": "author"},
        "parents": [],
        "files": files if files is not None else [{"filename": "file.py", "status": "modified"}],
    }


def sha(index):
    return f"{index:040x}"


class SyncFixture(IntegrationFixture):
    def add_repo(self, organization=None, hook_id=4, **config):
        return Repository.objects.create(
            organization_id=(organization or self.organization).id,
            integration_id=self.integration.id,
            provider="integrations:gitea",
            name="team/repo",
            external_id="41",
            url=PUBLIC + "/team/repo",
            config={
                "path": "team/repo",
                "repo_id": 41,
                "default_branch": "main",
                **({"hook_id": hook_id} if hook_id else {}),
                **config,
            },
        )

    def link_issue(self, key="41#7", group=None, organization=None):
        group = group or self.group
        external = ExternalIssue.objects.create(
            organization_id=(organization or group.organization).id,
            integration_id=self.integration.id,
            key=key,
            title="Remote issue",
            metadata={"display_name": "team/repo#7", "url": PUBLIC + "/team/repo/issues/7"},
        )
        GroupLink.objects.create(
            group=group,
            project_id=group.project_id,
            linked_id=external.id,
            linked_type=GroupLink.LinkedType.issue,
            relationship=GroupLink.Relationship.references,
        )
        return external

    def enable(self, installation=None, **options):
        (installation or self.installation).update_organization_config(options)
        self.installation = self.integration.get_installation(self.organization.id)

    def hook_url(self, repo_id=41, integration_id=None):
        return f"/extensions/gitea/webhook/{integration_id or self.integration.id}/{repo_id}/"

    def signed(self, event, payload, *, signature=None, secret=None, integration_id=None, url=None):
        """Deliver like Gitea: to repository 41's hook URL, signed with its derived secret."""
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        key = (secret or REPO_SECRET).encode()
        return self.client.post(
            url or self.hook_url(integration_id=integration_id),
            body,
            content_type="application/json",
            HTTP_X_GITEA_EVENT=event,
            HTTP_X_GITEA_SIGNATURE=signature or hmac.new(key, body, hashlib.sha256).hexdigest(),
            HTTP_X_GITEA_DELIVERY="00000000-0000-0000-0000-000000000001",
        )

    def push_payload(self, commits=None, **changes):
        commits = [gitea_push_commit(SHA_B)] if commits is None else commits
        return {
            "ref": "refs/heads/main",
            "before": SHA_A,
            "after": SHA_B,
            "compare_url": f"{INTERNAL}/team/repo/compare/{SHA_A}...{SHA_B}",
            "commits": commits,
            "total_commits": len(commits),
            "head_commit": commits[0] if commits else None,
            "repository": WEBHOOK_REPO,
            "pusher": {"id": 2, "login": "alice"},
            "sender": {"id": 2, "login": "alice"},
            **changes,
        }

    def issue_payload(self, action, number=7, **issue):
        return {
            "action": action,
            "number": number,
            "issue": {
                "id": 9000,
                "number": number,
                "title": "Remote problem",
                "state": "closed" if action == "closed" else "open",
                "assignees": None,
                "pull_request": None,
                "html_url": f"{INTERNAL}/team/repo/issues/{number}",
                **issue,
            },
            "repository": WEBHOOK_REPO,
            "sender": {"id": 2, "login": "alice"},
            "commit_id": "",
        }

    def pull_payload(self, action="opened", **pull):
        return {
            "action": action,
            "number": 9,
            "pull_request": {
                "id": 300,
                "number": 9,
                "user": {"id": 3, "login": "author", "full_name": "Author", "email": "a@x.example"},
                "title": "Fix error",
                "body": "Details",
                "state": "open",
                "merged": False,
                "merged_at": None,
                "merge_commit_sha": None,
                "created_at": "2026-10-28T09:00:00Z",
                "html_url": f"{INTERNAL}/team/repo/pulls/9",
                **pull,
            },
            "repository": WEBHOOK_REPO,
            "sender": {"id": 3, "login": "author"},
        }

    def mock_issue(self, **changes):
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(API + "/repos/team/repo/issues/7", json=self.remote_issue(**changes))
