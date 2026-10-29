"""Pure webhook authentication and Gitea 28 payload normalization (modules/structs/hook.go)."""

import hashlib
import hmac
import json

import pytest

from sentry_gitea.api.webhooks import (
    MAX_PAYLOAD_BYTES,
    PayloadError,
    is_zero_sha,
    normalize_api_commit,
    normalize_push_commit,
    parse_issue,
    parse_json,
    parse_pull_request,
    parse_push,
    repository_ref,
    verify_signature,
)

A, B, C = "a" * 40, "b" * 40, "c" * 40
ZERO = "0" * 40
SECRET = "synthetic-hook-key"
REPOSITORY = {
    "id": 41,
    "owner": {"id": 8, "login": "team", "email": "8+team@noreply.example"},
    "name": "repo",
    "full_name": "team/repo",
    "html_url": "http://gitea:3000/team/repo",
    "default_branch": "main",
}


def push_commit(sha, message="Fix error", **changes):
    return {
        "id": sha,
        "message": message,
        "url": f"http://gitea:3000/team/repo/commit/{sha}",
        "author": {"name": "Author", "email": "Author@Example.com", "username": "author"},
        "committer": {"name": "Author", "email": "author@example.com", "username": "author"},
        "verification": None,
        "timestamp": "2026-10-28T10:00:00+02:00",
        "added": ["new.py"],
        "removed": ["old.py"],
        "modified": ["app.py"],
        **changes,
    }


def push_payload(**changes):
    commits = changes.pop("commits", [push_commit(B)])
    return {
        "ref": "refs/heads/main",
        "before": A,
        "after": B,
        "compare_url": "http://gitea:3000/team/repo/compare/a...b",
        "commits": commits,
        "total_commits": len(commits),
        "head_commit": commits[0] if commits else None,
        "repository": REPOSITORY,
        "pusher": {"id": 2, "login": "alice"},
        "sender": {"id": 2, "login": "alice"},
        **changes,
    }


def test_signature_is_hex_hmac_sha256_over_exact_bytes():
    body = b'{"value": 1}'
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, signature, SECRET)
    assert verify_signature(body, signature.upper(), SECRET)
    assert not verify_signature(body + b"\n", signature, SECRET)
    assert not verify_signature(b'{"value":1}', signature, SECRET)
    for invalid in [None, "", "sha256=" + signature, "x" * 64, signature[:-1]]:
        assert not verify_signature(body, invalid, SECRET)
    # A missing or empty secret never authenticates, even with a matching empty-key MAC.
    empty = hmac.new(b"", body, hashlib.sha256).hexdigest()
    for secret in ["", None, 123]:
        assert not verify_signature(body, empty, secret)


@pytest.mark.parametrize(
    "body", [b"{", b"[]", b"null", b'"text"', b"\xff\xfe", b" " * (MAX_PAYLOAD_BYTES + 1)]
)
def test_malformed_or_oversized_json_is_rejected(body):
    with pytest.raises(PayloadError) as error:
        parse_json(body)
    # Messages are fixed strings; they never echo payload content.
    assert "\\xff" not in str(error.value) and len(str(error.value)) < 60


def test_repository_identity_is_validated():
    ref = repository_ref({"repository": REPOSITORY})
    assert (ref.id, ref.owner_id, ref.full_name, ref.default_branch) == (41, 8, "team/repo", "main")
    for changes in [
        {"id": True},
        {"id": "41"},
        {"id": 0},
        {"full_name": "../repo"},
        {"full_name": "team/repo/extra"},
        {"owner": {"id": None}},
        {"owner": None},
    ]:
        with pytest.raises(PayloadError):
            repository_ref({"repository": {**REPOSITORY, **changes}})
    with pytest.raises(PayloadError):
        repository_ref({})


def test_push_normalizes_commits_and_reports_truncation():
    commits = [push_commit(c) for c in (B, C, "d" * 40, "e" * 40, "f" * 40)]
    event = parse_push(push_payload(commits=commits, total_commits=7))
    assert event.truncated and not event.created and not event.deleted
    first = event.commits[0]
    assert first["id"] == B and first["repository"] == "team/repo"
    assert first["author_email"] == "Author@Example.com" and first["author_name"] == "Author"
    assert first["timestamp"].utcoffset().total_seconds() == 7200
    assert first["patch_set"] == [
        {"path": "new.py", "type": "A"},
        {"path": "old.py", "type": "D"},
        {"path": "app.py", "type": "M"},
    ]
    complete = parse_push(push_payload())
    assert not complete.truncated and len(complete.commits) == 1


def test_push_skip_marker_branch_creation_and_deletion():
    skipped = parse_push(push_payload(commits=[push_commit(B, "wip #skipsentry")]))
    assert skipped.commits == [] and not skipped.truncated
    created = parse_push(push_payload(before=ZERO, total_commits=9))
    assert created.created and created.truncated
    deleted = parse_push(push_payload(after=ZERO, commits=[], total_commits=0))
    assert deleted.deleted and deleted.commits == []
    assert is_zero_sha(ZERO) and is_zero_sha("0" * 64) and not is_zero_sha(A)


@pytest.mark.parametrize(
    "changes",
    [
        {"before": "abc"},
        {"after": None},
        {"total_commits": 0},
        {"total_commits": "5"},
        {"commits": [push_commit("not-a-sha")]},
        {"commits": [push_commit(B, timestamp="yesterday")]},
        {"commits": [push_commit(B, timestamp="2026-10-28T10:00:00")]},
        {"commits": [push_commit(B, added=[""])]},
        {"commits": [push_commit(B, author="alice")]},
        {"commits": ["text"]},
    ],
)
def test_push_rejects_malformed_payloads(changes):
    with pytest.raises(PayloadError):
        parse_push(push_payload(**changes))


def test_issue_events_keep_null_pull_request_and_number():
    payload = {
        "action": "closed",
        "number": 7,
        "issue": {"id": 9000, "number": 7, "state": "closed", "pull_request": None},
        "repository": REPOSITORY,
        "sender": {"id": 2},
    }
    event = parse_issue(payload)
    assert (event.action, event.number, event.is_pull_request) == ("closed", 7, False)
    payload["issue"]["pull_request"] = {"merged": False}
    assert parse_issue(payload).is_pull_request
    for issue in [None, {"number": "7"}, {"number": -1}]:
        with pytest.raises(PayloadError):
            parse_issue({**payload, "issue": issue})


def pull_payload(**changes):
    pull = {
        "id": 300,
        "number": 9,
        "user": {"id": 2, "login": "Alice", "full_name": "Alice A", "email": "2+alice@noreply.x"},
        "title": "Fix",
        "body": "Fixes TEAM-1",
        "state": "open",
        "merged": False,
        "merge_commit_sha": None,
        "created_at": "2026-10-28T09:00:00Z",
        **changes,
    }
    return {"action": "opened", "number": 9, "pull_request": pull, "repository": REPOSITORY}


def test_pull_request_merge_sha_only_when_merged_and_placeholder_emails_hidden():
    event = parse_pull_request(pull_payload())
    assert (event.number, event.title, event.body) == (9, "Fix", "Fixes TEAM-1")
    assert event.author_email is None and event.author_name == "Alice A"
    assert event.merge_commit_sha is None and event.created_at.year == 2026
    merged = parse_pull_request(
        pull_payload(merged=True, merge_commit_sha=B, user={"id": 3, "email": "bob@example.com"})
    )
    assert merged.merge_commit_sha == B and merged.author_email == "bob@example.com"
    assert parse_pull_request(pull_payload(merge_commit_sha=B)).merge_commit_sha is None
    for changes in [{"merged": True, "merge_commit_sha": "zz"}, {"user": None}, {"title": 5}]:
        with pytest.raises(PayloadError):
            parse_pull_request(pull_payload(**changes))


def api_commit(sha, files, message="Release fix"):
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {
                "name": "Author",
                "email": "author@example.com",
                "date": "2026-10-28T10:00Z",
            },
            "committer": {"name": "Author", "email": "author@example.com", "date": "2026-10-28"},
        },
        "author": {"id": 2, "login": "author"},
        "files": files,
    }


def test_api_commit_file_statuses():
    commit = normalize_api_commit(
        api_commit(
            B,
            [
                {"filename": "new.py", "status": "added"},
                {"filename": "gone.py", "status": "removed"},
                {"filename": "older.py", "status": "deleted"},
                {"filename": "app.py", "status": "modified"},
                {"filename": "b.py", "previous_filename": "a.py", "status": "renamed"},
                {"filename": "odd.py", "status": "typechange"},
            ],
        ),
        "team/repo",
    )
    assert commit["patch_set"] == [
        {"path": "new.py", "type": "A"},
        {"path": "gone.py", "type": "D"},
        {"path": "older.py", "type": "D"},
        {"path": "app.py", "type": "M"},
        {"path": "a.py", "type": "D"},
        {"path": "b.py", "type": "A"},
        {"path": "odd.py", "type": "M"},
    ]
    assert commit["timestamp"].utcoffset().total_seconds() == 0
    assert normalize_api_commit(api_commit(B, [], "skip #skipsentry"), "team/repo") is None
    for bad in [api_commit("short", []), {**api_commit(B, []), "files": None}, {"sha": B}]:
        with pytest.raises(PayloadError):
            normalize_api_commit(bad, "team/repo")


def test_push_commit_without_author_email():
    commit = normalize_push_commit(push_commit(B, author={"name": "", "email": ""}), "team/repo")
    assert commit["author_email"] is None and commit["author_name"] is None
    assert json.dumps({k: v for k, v in commit.items() if k != "timestamp"})
