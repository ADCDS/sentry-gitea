"""Pure client methods for sync, hooks, source files and bounded commit ranges."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

from sentry_gitea.api.client import (
    GiteaClient,
    git_ref,
    is_sha,
    repository_file_path,
)
from sentry_gitea.api.errors import GiteaApiError

API = "https://forge.example/api/v1"
REPO = API + "/repos/team/repo"
TOKEN = "synthetic-token"


def client():
    return GiteaClient("https://forge.example", TOKEN, "team/repo")


def query(call):
    return parse_qs(urlsplit(call.request.url).query)


@responses.activate
def test_issue_state_assignees_and_comment_edits():
    with client() as gitea:
        responses.patch(REPO + "/issues/7", json={"number": 7, "state": "closed"}, status=201)
        assert gitea.update_issue(7, state="closed")["state"] == "closed"
        assert json.loads(responses.calls[-1].request.body) == {"state": "closed"}
        gitea.update_issue(7, assignees=[])
        assert json.loads(responses.calls[-1].request.body) == {"assignees": []}
        for kwargs in [{}, {"state": "merged"}, {"assignees": "alice"}, {"assignees": ["a b"]}]:
            with pytest.raises(ValueError):
                gitea.update_issue(7, **kwargs)
        responses.get(REPO + "/issues/comments/12", json={"id": 12, "body": "x"})
        assert gitea.get_comment(12)["id"] == 12
        responses.patch(REPO + "/issues/comments/12", json={"id": 12})
        gitea.update_comment(12, "updated")
        assert json.loads(responses.calls[-1].request.body) == {"body": "updated"}
    assert all(TOKEN in c.request.headers["Authorization"] for c in responses.calls)


@responses.activate
def test_hook_lifecycle_sets_secret_only_on_create():
    url = "https://sentry.example/extensions/gitea/webhook/5/"
    with client() as gitea:
        responses.get(REPO + "/hooks", json=[{"id": 4}], headers={"X-Total-Count": "1"})
        assert gitea.list_hooks() == [{"id": 4}]
        responses.post(REPO + "/hooks", json={"id": 4}, status=201)
        gitea.create_hook(url, "hook-secret")
        assert json.loads(responses.calls[-1].request.body) == {
            "type": "gitea",
            "active": True,
            "events": ["push", "issues_only", "issue_assign", "pull_request"],
            "branch_filter": "",
            "config": {"url": url, "content_type": "json", "secret": "hook-secret"},
        }
        responses.patch(REPO + "/hooks/4", json={"id": 4})
        gitea.update_hook(4, url, "hook-secret")
        assert "secret" not in json.loads(responses.calls[-1].request.body)["config"]
        responses.delete(REPO + "/hooks/4", status=204)
        assert gitea.delete_hook(4) is True
        responses.replace(responses.DELETE, REPO + "/hooks/4", status=404)
        assert gitea.delete_hook(4) is False
        responses.replace(responses.DELETE, REPO + "/hooks/4", status=500)
        with pytest.raises(GiteaApiError):
            gitea.delete_hook(4)
        with pytest.raises(ValueError):
            gitea.create_hook(url, "")


def entry(path, kind="file", **extra):
    """A Gitea 28 ContentsResponse as contents-ext returns it without `includes`."""
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "sha": "f" * 40,
        "type": kind,
        "mode": {"file": "100644", "dir": "040000", "symlink": "120000"}.get(kind, "160000"),
        "size": 12 if kind == "file" else 0,
        "encoding": None,
        "content": None,
        "target": None,
        **extra,
    }


@responses.activate
def test_file_metadata_raw_contents_and_refs():
    ext = REPO + "/contents-ext/"
    with client() as gitea:
        responses.get(ext + "src/a%20b.py", json={"file_contents": entry("src/a b.py")})
        assert gitea.check_file("src/a b.py", "release/one") == entry("src/a b.py")
        # Metadata only: no `includes=file_content`, so Gitea never sends the file body.
        assert query(responses.calls[-1]) == {"ref": ["release/one"]}
        responses.get(ext + "missing.py", status=404, json={"message": "not found"})
        assert gitea.check_file("missing.py", None) is None
        assert query(responses.calls[-1]) == {}
        responses.get(ext + "dir", json={"dir_contents": [entry("dir/a.py")]})
        assert gitea.check_file("dir", "main") is None
        responses.get(ext + "link", json={"file_contents": entry("link", "symlink")})
        assert gitea.check_file("link", "main") is None
        responses.get(ext + "module", json={"file_contents": entry("module", "submodule")})
        assert gitea.check_file("module", "main") is None
        for bad in [[entry("x")], {"file_contents": ["x"]}]:
            responses.replace(responses.GET, ext + "dir", json=bad)
            with pytest.raises(GiteaApiError, match="invalid file metadata"):
                gitea.check_file("dir", "main")
        responses.get(ext + "denied.py", status=403)
        with pytest.raises(GiteaApiError):
            gitea.check_file("denied.py", "main")
        assert all("/contents/" not in call.request.url for call in responses.calls)
        responses.get(REPO + "/raw/CODEOWNERS", body="*.py owner@example.com\n")
        assert gitea.get_file("CODEOWNERS", "main") == "*.py owner@example.com\n"
        responses.get(REPO + "/raw/binary", body=b"\xff\xfe")
        with pytest.raises(GiteaApiError, match="invalid text"):
            gitea.get_file("binary", None)
        responses.get(REPO + "/branches/release/one", json={"name": "release/one"})
        assert gitea.get_branch("release/one")["name"] == "release/one"
        responses.get(REPO + "/tags/v1.0", json={"name": "v1.0"})
        assert gitea.get_tag("v1.0")["name"] == "v1.0"
        calls = len(responses.calls)
        for path in ["../secret", "/absolute", "a/../b", "a/./b", "a//b", "a\\b", "a/\x00b", ""]:
            with pytest.raises(ValueError):
                gitea.check_file(path, "main")
        for ref in ["-x", "a..b", "a b", "a:b", "a/", "x" * 300]:
            with pytest.raises(ValueError):
                gitea.check_file("file.py", ref)
        assert len(responses.calls) == calls


def test_validators():
    assert repository_file_path("src/ü %.py") == "src/%C3%BC%20%25.py"
    assert git_ref("release/1.2") == "release/1.2"
    assert is_sha("a" * 40) and is_sha("b" * 64) and not is_sha("A" * 40)
    assert not is_sha("a" * 39) and not is_sha(None)


def commits(*shas):
    return [{"sha": sha, "commit": {}} for sha in shas]


SHAS = [c * 40 for c in "abcde"]
HEAD, BASE = "1" * 40, "2" * 40


def paged(rows, size, *, total=True):
    def callback(request):
        params = parse_qs(urlsplit(request.url).query)
        assert params["sha"] == [HEAD] and params["files"] == ["true"]
        assert params["stat"] == ["false"] and params["verification"] == ["false"]
        start = (int(params["page"][0]) - 1) * size
        headers = {"X-Total-Count": str(len(rows))} if total else {}
        return 200, headers, json.dumps(commits(*rows[start : start + size]))

    return callback


@responses.activate
def test_commit_range_follows_server_clamped_pages_to_total():
    responses.add_callback(responses.GET, REPO + "/commits", callback=paged(SHAS, 2))
    pages = []
    with client() as gitea:
        result = gitea.commit_range(HEAD, exclude=BASE, max_commits=10, on_page=pages.append)
        retained = gitea.commit_range(HEAD, exclude=BASE, max_commits=10)
    assert [r["sha"] for page in pages for r in page] == SHAS
    assert result.complete and result.total == 5 and result.fetched == 5
    assert [len(page) for page in pages] == [2, 2, 1]
    # Without a consumer, the walk returns its rows instead.
    assert [r["sha"] for r in retained.rows] == SHAS and retained.fetched == 5
    assert query(responses.calls[0])["not"] == [BASE]
    assert len(responses.calls) == 6


@responses.activate
def test_commit_range_bound_reports_incomplete_with_total():
    responses.add_callback(responses.GET, REPO + "/commits", callback=paged(SHAS, 2))
    pages = []
    with client() as gitea:
        result = gitea.commit_range(HEAD, max_commits=3, on_page=pages.append)
        assert [r["sha"] for page in pages for r in page] == SHAS[:3]
        assert result.rows == [] and result.fetched == 3
        assert not result.complete and result.total == 5
        assert [len(page) for page in pages] == [2, 1]
        assert "not" not in query(responses.calls[0])
        calls = len(responses.calls)
        early = gitea.commit_range(HEAD, max_commits=3, stop_when_over=True)
        assert not early.complete and early.rows == [] and len(responses.calls) == calls + 1


@responses.activate
def test_commit_range_without_total_probes_for_exact_fit():
    responses.add_callback(responses.GET, REPO + "/commits", callback=paged(SHAS, 2, total=False))
    with client() as gitea:
        exact = gitea.commit_range(HEAD, max_commits=5)
        assert exact.complete and len(exact.rows) == 5
        short = gitea.commit_range(HEAD, max_commits=4)
        assert not short.complete and short.total is None and len(short.rows) == 4


@responses.activate
def test_commit_range_rejects_inconsistent_pages_and_bad_input():
    with client() as gitea:
        responses.get(REPO + "/commits", json=[], headers={"X-Total-Count": "3"})
        with pytest.raises(GiteaApiError, match="incomplete"):
            gitea.commit_range(HEAD, max_commits=10)
        responses.replace(responses.GET, REPO + "/commits", json=commits(SHAS[0]))
        with pytest.raises(GiteaApiError, match="repeated"):
            gitea.commit_range(HEAD, max_commits=10)
        responses.replace(responses.GET, REPO + "/commits", json=commits("short"))
        with pytest.raises(GiteaApiError, match="invalid commit"):
            gitea.commit_range(HEAD, max_commits=10)
        responses.replace(responses.GET, REPO + "/commits", json={"commits": []})
        with pytest.raises(GiteaApiError):
            gitea.commit_range(HEAD, max_commits=10)
        responses.replace(responses.GET, REPO + "/commits", status=409, json={"message": "empty"})
        with pytest.raises(GiteaApiError) as error:
            gitea.commit_range(HEAD, max_commits=10)
        assert error.value.status == 409
        calls = len(responses.calls)
        for kwargs in [{"max_commits": 0}, {"max_commits": 5, "exclude": "x y"}]:
            with pytest.raises(ValueError):
                gitea.commit_range(HEAD, **kwargs)
        with pytest.raises(ValueError):
            gitea.commit_range("-rf", max_commits=5)
        assert len(responses.calls) == calls


@responses.activate
def test_streamed_commit_walk_retains_no_rows_it_handed_over():
    """Regression: rows passed to `on_page` were also accumulated in the result."""
    many = [f"{i:040x}" for i in range(1, 121)]
    responses.add_callback(responses.GET, REPO + "/commits", callback=paged(many, 50))
    pages = []
    with client() as gitea:
        result = gitea.commit_range(HEAD, max_commits=1000, on_page=pages.append)
    assert [len(page) for page in pages] == [50, 50, 20]
    assert result.fetched == result.total == 120 and result.complete
    assert result.rows == []
    responses.replace(responses.GET, REPO + "/commits", json=commits(SHAS[0]))
    with client() as gitea:
        # No total: the exact-fit probe still answers without keeping the delivered page.
        exact = gitea.commit_range(HEAD, max_commits=1, on_page=pages.append)
    assert exact.rows == [] and exact.fetched == 1 and exact.complete
