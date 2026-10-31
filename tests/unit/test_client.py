import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from sentry_gitea.api.client import (
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    OPERATION_BUDGET,
    OPERATION_MAX_BYTES,
    OPERATION_MAX_ROWS,
    PAGE_SIZE,
    REQUEST_ALLOWANCE,
    TIMEOUT,
    GiteaClient,
    normalize_base_url,
    normalize_repo,
    parse_gitea_time,
    url_origin,
)
from sentry_gitea.api.errors import GiteaApiError

BASE = "https://forge.example/git"
API = BASE + "/api/v1"
TOKEN = "synthetic-test-credential"
REPO = {"id": 41, "full_name": "owner/repo", "default_branch": "main"}


@pytest.fixture
def client():
    with GiteaClient(BASE + "/", TOKEN, "owner/repo") as value:
        yield value


def issue(**overrides):
    # Real Gitea issues always carry `"pull_request": null`.
    return {"number": 7, "title": "Problem", "state": "open", "pull_request": None, **overrides}


@responses.activate
def test_read_methods_use_session_auth_timeouts_and_subpath(client, monkeypatch):
    responses.get(API + "/user", json={"id": 2, "login": "alice"})
    responses.get(API + "/repos/owner/repo", json=REPO)
    responses.get(API + "/repos/owner/repo/issues/7", json=issue())
    calls = []
    original = client.session.request

    def request(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.session, "request", request)
    assert client.get_current_user()["login"] == "alice"
    assert client.get_repo() == REPO
    assert client.get_issue("7")["number"] == 7
    assert all(c["timeout"] == TIMEOUT == (5, 15) for c in calls)
    assert all(c["stream"] and c["allow_redirects"] is False for c in calls)
    assert all(c.request.headers["Authorization"] == f"token {TOKEN}" for c in responses.calls)


@responses.activate
def test_write_methods_send_ids_and_usernames(client):
    responses.post(
        API + "/repos/owner/repo/issues",
        json=issue(labels=[{"id": 11}], assignees=[{"login": "alice"}]),
        status=201,
    )
    responses.post(API + "/repos/owner/repo/issues/7/comments", json={"id": 80}, status=201)
    client.create_issue("Problem", "Body", label_ids=[11], assignees=["alice"])
    client.create_comment(7, "Another event")
    assert json.loads(responses.calls[0].request.body) == {
        "title": "Problem",
        "body": "Body",
        "labels": [11],
        "assignees": ["alice"],
    }
    assert json.loads(responses.calls[1].request.body) == {"body": "Another event"}


@responses.activate
@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429, 500, 502])
def test_http_errors_redact_and_bound_body(client, status):
    responses.get(
        API + "/user",
        body=f"{TOKEN} https://user:password@forge.example/?token={TOKEN} " + "x" * 1000,
        status=status,
    )
    with pytest.raises(GiteaApiError) as caught:
        client.get_current_user()
    error = caught.value
    assert error.status == status
    assert TOKEN not in str(error)
    assert "password" not in str(error)
    assert "https://" not in str(error)
    assert len(error.message) <= 300


@responses.activate
def test_error_redaction_handles_json_escaped_urls_and_secret_fields(client):
    message = json.dumps(
        {
            "url": "https://proxy-user:proxy-password@forge.example/path",
            "password": "another-password",
            "message": TOKEN,
        }
    ).replace("/", r"\/")
    responses.get(API + "/user", status=500, body=message)
    with pytest.raises(GiteaApiError) as caught:
        client.get_current_user()
    for secret in [TOKEN, "proxy-password", "another-password", "proxy-user"]:
        assert secret not in str(caught.value)


@responses.activate
@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_transport_errors_do_not_include_request_details(client, error):
    responses.get(API + "/user", body=error(f"request failed with {TOKEN}"))
    with pytest.raises(GiteaApiError) as caught:
        client.get_current_user()
    assert caught.value.status == 0
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


@responses.activate
def test_redirects_never_forward_credentials(client):
    responses.get(API + "/user", status=302, headers={"Location": "https://elsewhere/user"})
    with pytest.raises(GiteaApiError, match="redirect"):
        client.get_current_user()
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("body", ["not json", "[]", "null"])
def test_bad_response_shapes(client, body):
    responses.get(API + "/user", body=body)
    with pytest.raises(GiteaApiError):
        client.get_current_user()


@responses.activate
@pytest.mark.parametrize("announced", [True, False])
def test_response_size_is_bounded_before_json(client, announced):
    headers = {"Content-Length": str(MAX_RESPONSE_BYTES + 1)} if announced else {}
    responses.get(API + "/user", body="x" * (MAX_RESPONSE_BYTES + 1), headers=headers)
    with pytest.raises(GiteaApiError, match="size"):
        client.get_current_user()


@responses.activate
def test_pagination_continues_after_server_clamps_limit(client):
    responses.get(
        API + "/repos/owner/repo/labels",
        json=[{"id": 1, "name": "one"}],
        headers={"X-Total-Count": "2", "Link": '<https://untrusted/labels>; rel="next"'},
    )
    responses.get(
        API + "/repos/owner/repo/labels",
        json=[{"id": 2, "name": "two"}],
        headers={"X-Total-Count": "2"},
    )
    assert [row["id"] for row in client.list_labels()] == [1, 2]
    assert [parse_qs(urlsplit(c.request.url).query)["page"] for c in responses.calls] == [
        ["1"],
        ["2"],
    ]


@responses.activate
def test_pagination_without_total_requires_empty_page(client):
    responses.get(API + "/repos/owner/repo/labels", json=[{"id": 1, "name": "one"}])
    responses.get(API + "/repos/owner/repo/labels", json=[])
    assert len(client.list_labels()) == 1
    assert len(responses.calls) == 2


@responses.activate
def test_pagination_cap_fails_closed_instead_of_returning_partial_result(client):
    def page(request):
        number = int(parse_qs(urlsplit(request.url).query)["page"][0])
        return 200, {}, json.dumps([{"id": number, "name": str(number)}])

    responses.add_callback(responses.GET, API + "/repos/owner/repo/labels", callback=page)
    with pytest.raises(GiteaApiError, match="pagination"):
        client.list_labels()
    assert len(responses.calls) == MAX_PAGES


@responses.activate
def test_inconsistent_total_fails_closed(client):
    responses.get(API + "/repos/owner/repo/labels", json=[], headers={"X-Total-Count": "2"})
    with pytest.raises(GiteaApiError, match="incomplete"):
        client.list_labels()


@responses.activate
def test_assignees_are_unpaginated(client):
    responses.get(API + "/repos/owner/repo/assignees", json=[{"id": 9, "login": "alice"}])
    assert client.list_assignees() == [{"id": 9, "login": "alice"}]
    assert len(responses.calls) == 1


@responses.activate
def test_resolve_label_ids_deduplicates_names_and_selects_lowest_duplicate_id(client):
    responses.get(
        API + "/repos/owner/repo/labels",
        json=[{"id": 9, "name": "bug"}, {"id": 3, "name": "bug"}],
        headers={"X-Total-Count": "2"},
    )
    responses.post(
        API + "/repos/owner/repo/labels", json={"id": 12, "name": "source:sentry"}, status=201
    )
    assert client.resolve_label_ids(["bug", "source:sentry", "bug"]) == [3, 12]
    assert json.loads(responses.calls[1].request.body) == {
        "name": "source:sentry",
        "color": "#5319e7",
    }


@responses.activate
@pytest.mark.parametrize(
    ("name", "color"),
    [
        ("source:custom", "#5319e7"),
        ("severity:critical", "#b60205"),
        ("severity:high", "#d93f0b"),
        ("severity:medium", "#fbca04"),
        ("severity:low", "#0e8a16"),
        ("class:crash", "#b60205"),
        ("class:other", "#c5def5"),
        ("custom", "#ededed"),
    ],
)
def test_create_label_colors(client, name, color):
    responses.post(API + "/repos/owner/repo/labels", json={"id": 1, "name": name}, status=201)
    assert client.create_label(name)["id"] == 1
    assert json.loads(responses.calls[0].request.body)["color"] == color


@responses.activate
def test_missing_labels_can_be_read_only(client):
    responses.get(API + "/repos/owner/repo/labels", json=[])
    with pytest.raises(GiteaApiError, match="Missing"):
        client.resolve_label_ids(["bug"], create_missing=False)
    assert all(call.request.method == "GET" for call in responses.calls)


@responses.activate
def test_label_conflict_is_reread_once(client):
    responses.get(API + "/repos/owner/repo/labels", json=[])
    responses.post(API + "/repos/owner/repo/labels", status=409)
    responses.get(
        API + "/repos/owner/repo/labels",
        json=[{"id": 4, "name": "bug"}],
        headers={"X-Total-Count": "1"},
    )
    assert client.resolve_label_ids(["bug"]) == [4]


@responses.activate
def test_recoverable_label_failure_is_reported_even_if_reread_also_fails(client):
    statuses = []
    client.on_recovered_error = statuses.append
    responses.get(API + "/repos/owner/repo/labels", json=[])
    responses.post(API + "/repos/owner/repo/labels", status=409)
    responses.get(API + "/repos/owner/repo/labels", status=503)
    with pytest.raises(GiteaApiError) as caught:
        client.resolve_label_ids(["bug"])
    assert caught.value.status == 503
    assert statuses == [409]


@responses.activate
def test_marker_search_verifies_exact_json_and_includes_closed_issues(client):
    dedup = "https://sentry.example/1/2"
    marker = (
        "<!-- sentry-gitea-meta v1 " + json.dumps({"source": "sentry", "dedup_id": dedup}) + " -->"
    )
    responses.get(
        API + "/repos/owner/repo/issues",
        json=[
            issue(title=dedup),
            issue(number=8, body=marker.replace("/1/2", "/1/23")),
            issue(number=9, body=marker, pull_request={"url": "ignored"}),
            issue(number=10, body=marker, state="closed"),
        ],
        headers={"X-Total-Count": "4"},
    )
    assert client.search_issues_by_marker(dedup)["number"] == 10
    query = parse_qs(urlsplit(responses.calls[0].request.url).query)
    assert query["state"] == ["all"]
    assert query["type"] == ["issues"]


@responses.activate
def test_marker_search_empty(client):
    responses.get(API + "/repos/owner/repo/issues", json=[])
    assert client.search_issues_by_marker("https://sentry.example/1/2") is None


@responses.activate
def test_marker_search_checks_all_blocks_not_just_a_pasted_example(client):
    example = '<!-- sentry-gitea-meta v1 {"source":"sentry","dedup_id":"example"} -->'
    matching = '<!-- sentry-gitea-meta v1 {"source":"sentry","dedup_id":"wanted"} -->'
    responses.get(
        API + "/repos/owner/repo/issues",
        json=[issue(body=example + matching)],
        headers={"X-Total-Count": "1"},
    )
    assert client.search_issues_by_marker("wanted")["number"] == 7


@responses.activate
def test_search_cap_cannot_turn_unexamined_issues_into_permission_to_create(client):
    responses.get(API + "/repos/owner/repo/issues", json=[issue(title="false positive")])
    with pytest.raises(GiteaApiError, match="pagination"):
        client.search_issues_by_marker("wanted")
    assert len(responses.calls) == MAX_PAGES


@responses.activate
def test_issue_lookup_rejects_pull_requests(client):
    responses.get(API + "/repos/owner/repo/issues/7", json=issue(pull_request={}))
    with pytest.raises(GiteaApiError, match="pull request"):
        client.get_issue(7)


@responses.activate
def test_create_detects_silently_discarded_labels_or_assignees(client):
    responses.post(
        API + "/repos/owner/repo/issues", json=issue(labels=[], assignees=[]), status=201
    )
    with pytest.raises(GiteaApiError, match="created.*permissions"):
        client.create_issue("Problem", "marker", label_ids=[4], assignees=["alice"])
    assert len(responses.calls) == 1


@responses.activate
def test_compare_uses_direct_range_and_quotes_branch(client):
    responses.get(API + "/repos/owner/repo/compare/abcdef0..release%2Fnext", json={"commits": []})
    assert client.compare_commits("abcdef0", "release/next") == {"commits": []}


def test_budget_stops_calls_before_start(client, monkeypatch):
    client.deadline = 0
    with pytest.raises(GiteaApiError, match="budget"):
        client.get_repo()


@responses.activate
@pytest.mark.parametrize("remaining", [REQUEST_ALLOWANCE, REQUEST_ALLOWANCE - 0.001])
def test_request_admission_reserves_connect_and_read_time(client, monkeypatch, remaining):
    monkeypatch.setattr("sentry_gitea.api.client.time", SimpleNamespace(monotonic=lambda: 100.0))
    client.deadline = 100.0 + remaining
    responses.get(API + "/repos/owner/repo", json=REPO)
    if remaining < REQUEST_ALLOWANCE:
        with pytest.raises(GiteaApiError, match="operation time budget exhausted"):
            client.get_repo()
        assert not responses.calls
    else:
        assert client.get_repo() == REPO
        assert len(responses.calls) == 1


@responses.activate
def test_optional_read_cutoff_preserves_primary_request_budget(client, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("sentry_gitea.api.client.time", SimpleNamespace(monotonic=lambda: now[0]))
    client.deadline = now[0] + OPERATION_BUDGET
    cutoff = client.deadline - REQUEST_ALLOWANCE - TIMEOUT[1] - 2

    def delayed_compare(request):
        now[0] = cutoff + TIMEOUT[1]
        return 200, {}, json.dumps({"commits": []})

    responses.add_callback(
        responses.GET, API + "/repos/owner/repo/compare/abcdef0..main", callback=delayed_compare
    )
    responses.post(API + "/repos/owner/repo/issues", json=issue(), status=201)
    with pytest.raises(GiteaApiError, match="operation time budget exhausted"):
        client.compare_commits("abcdef0", "main", read_deadline=cutoff)
    assert client.deadline == 100.0 + OPERATION_BUDGET
    assert client.deadline - now[0] >= REQUEST_ALLOWANCE
    assert client.create_issue("Problem", "Body")["number"] == 7
    assert [call.request.method for call in responses.calls] == ["GET", "POST"]


def test_stream_deadline_is_checked_between_available_reads(client, monkeypatch):
    now = [0.0]
    client.deadline = 40
    monkeypatch.setattr("sentry_gitea.api.client.time", SimpleNamespace(monotonic=lambda: now[0]))

    def read1(size, decode_content):
        assert decode_content is False
        now[0] += 21
        return b" "

    class Reply:
        status_code = 200
        headers = {}
        raw = SimpleNamespace(read1=read1)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(client.session, "request", lambda *args, **kwargs: Reply())
    with pytest.raises(GiteaApiError, match="budget"):
        client.get_repo()
    assert now[0] == 42


@responses.activate
def test_unexpected_compression_is_rejected_before_unbounded_inflation(client):
    responses.get(API + "/user", body="compressed", headers={"Content-Encoding": "gzip"})
    with pytest.raises(GiteaApiError, match="encoding"):
        client.get_current_user()
    assert responses.calls[0].request.headers["Accept-Encoding"] == "identity"


@responses.activate
def test_available_read_transport_errors_are_sanitized(client, monkeypatch):
    responses.get(API + "/user", json={"id": 1})

    def fail_read(*args, **kwargs):
        raise ReadTimeoutError(None, "https://user:password@proxy", TOKEN)

    monkeypatch.setattr(HTTPResponse, "read1", fail_read)
    with pytest.raises(GiteaApiError) as caught:
        client.get_current_user()
    assert caught.value.status == 0
    assert TOKEN not in str(caught.value)
    assert "password" not in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://forge.example",
        "https://user:pass@forge.example",
        "https://forge.example?x=1",
        "https://forge.example#frag",
        "https:///missing",
        "https://forge.example/../api",
        "https://forge.example/%2e%2e/api",
        "https://forge.example\\@evil",
        "https://forge.example\n",
    ],
)
def test_invalid_base_urls(value):
    with pytest.raises(ValueError):
        normalize_base_url(value)


def test_url_and_repo_normalization():
    assert normalize_base_url("http://FORGE.example/git/") == "http://forge.example/git"
    assert normalize_repo(" owner/repo ") == "owner/repo"


@pytest.mark.parametrize("repo", ["owner", "owner/repo/extra", "../repo", "o/..", "o/r?token=x"])
def test_invalid_repositories(repo):
    with pytest.raises(ValueError):
        normalize_repo(repo)


@pytest.mark.parametrize("number", [0, -1, "https://evil", "1/other", True])
def test_issue_number_validation_prevents_arbitrary_targets(client, number):
    with pytest.raises(ValueError):
        client.get_issue(number)


@responses.activate
def test_plain_issue_with_null_pull_request_is_accepted(client):
    # Regression: Gitea sends "pull_request": null on every plain issue.
    responses.get(API + "/repos/owner/repo/issues/7", json=issue(pull_request=None))
    assert client.get_issue(7)["number"] == 7


@responses.activate
def test_marker_search_keeps_plain_issues_and_skips_pull_requests():
    dedup = "https://sentry.example/1/2"
    marker = (
        "<!-- sentry-gitea-meta v1 " + json.dumps({"source": "sentry", "dedup_id": dedup}) + " -->"
    )
    responses.get(
        API + "/repos/owner/repo/issues",
        json=[
            issue(number=3, body=marker, pull_request={"merged": False}),
            issue(number=4, body=marker, pull_request=None),
        ],
        headers={"X-Total-Count": "2"},
    )
    found = GiteaClient(BASE, "t0k3n", "owner/repo").search_issues_by_marker(dedup)
    assert found is not None and found["number"] == 4


@responses.activate
def test_oauth_grants_are_form_posts_with_pkce_timeout_and_no_redirects(monkeypatch):
    from sentry_gitea.api.client import GiteaOAuthClient

    calls = []
    original = requests.Session.request

    def request(session, *args, **kwargs):
        calls.append(kwargs)
        return original(session, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", request)
    responses.post(BASE + "/login/oauth/access_token", json={"access_token": "test-access"})
    with GiteaOAuthClient(BASE, "test-app", "test-secret") as oauth:
        oauth.exchange_code("test-code", "https://sentry.example/extensions/gitea/setup/", "v" * 64)
        oauth.refresh_token("test-refresh")
    grants = [parse_qs(c.request.body) for c in responses.calls]
    assert grants[0]["grant_type"] == ["authorization_code"]
    assert grants[0]["code_verifier"] == ["v" * 64]
    assert grants[0]["code"] == ["test-code"]
    assert grants[1]["grant_type"] == ["refresh_token"]
    assert grants[1]["refresh_token"] == ["test-refresh"]
    assert all(c["timeout"] == TIMEOUT and c["allow_redirects"] is False for c in calls)
    assert all("Authorization" not in c.request.headers for c in responses.calls)
    assert all(
        "application/x-www-form-urlencoded" in c.request.headers["Content-Type"]
        for c in responses.calls
    )


@responses.activate
@pytest.mark.parametrize("status", [302, 400, 401, 500])
def test_oauth_failures_never_expose_response_or_credentials(status):
    from sentry_gitea.api.client import GiteaOAuthClient

    secrets = ["test-secret", "test-code", "test-verifier", "unknown-returned-token"]
    responses.post(BASE + "/login/oauth/access_token", body=" ".join(secrets), status=status)
    with GiteaOAuthClient(BASE, "test-app", secrets[0]) as oauth:
        with pytest.raises(GiteaApiError) as caught:
            oauth.exchange_code(secrets[1], "https://sentry.example/callback/", secrets[2])
    assert caught.value.status == status
    assert all(secret not in str(caught.value) for secret in secrets)
    assert len(responses.calls) == 1


@responses.activate
def test_unbound_client_requires_explicit_repository_and_keeps_requests_separate():
    with GiteaClient(BASE, TOKEN, token_type="Bearer") as client:
        responses.get(API + "/repos/owner/one", json={"id": 1})
        responses.get(API + "/repos/owner/two/issues/7", json=issue())
        assert client.get_repo(repo="owner/one")["id"] == 1
        assert client.get_issue(7, repo="owner/two")["number"] == 7
        with pytest.raises(ValueError):
            client.get_repo()
    assert all(c.request.headers["Authorization"] == f"Bearer {TOKEN}" for c in responses.calls)


@responses.activate
def test_user_orgs_membership_and_strict_owner_permission(client):
    responses.get(API + "/user", json={"id": 2, "login": "alice"})
    responses.get(
        API + "/user/orgs", json=[{"id": 8, "username": "team"}], headers={"X-Total-Count": "1"}
    )
    responses.get(API + "/orgs/team", json={"id": 8, "username": "team"})
    responses.get(API + "/orgs/team/members/alice", status=204)
    responses.get(
        API + "/users/alice/orgs/team/permissions", json={"is_admin": True, "is_owner": False}
    )
    assert client.get_user()["id"] == 2
    assert client.list_user_orgs()[0]["id"] == 8
    assert client.get_org("team")["id"] == 8
    assert client.is_org_owner("team", "alice") is False
    responses.replace(
        responses.GET, API + "/users/alice/orgs/team/permissions", json={"is_owner": True}
    )
    assert client.is_org_owner("team", "alice") is True


@responses.activate
@pytest.mark.parametrize("status", [303, 404])
def test_nonmember_never_follows_membership_redirect(client, status):
    responses.get(
        API + "/orgs/team/members/alice",
        status=status,
        headers={"Location": "https://elsewhere.example/"},
    )
    assert client.is_org_owner("team", "alice") is False
    assert len(responses.calls) == 1


@responses.activate
def test_repository_search_is_owner_exclusive_and_page_bounded(client):
    responses.get(
        API + "/repos/search", json={"ok": True, "data": [REPO]}, headers={"X-Total-Count": "200"}
    )
    assert client.search_repositories(8, query="repo", page_number_limit=1) == [REPO]
    query = parse_qs(urlsplit(responses.calls[0].request.url).query)
    assert query["uid"] == ["8"] and query["exclusive"] == ["true"]
    assert query["q"] == ["repo"] and query["page"] == ["1"]
    assert len(responses.calls) == 1


@responses.activate
def test_numeric_and_text_search_exclude_prs_and_use_issue_number(client):
    responses.get(API + "/repos/owner/repo/issues/7", json=issue(id=999))
    assert client.search_issues("#7")[0]["number"] == 7
    responses.get(
        API + "/repos/owner/repo/issues",
        json=[issue(), issue(number=8, pull_request={})],
        headers={"X-Total-Count": "2"},
    )
    assert [row["number"] for row in client.search_issues("Problem")] == [7]
    query = parse_qs(urlsplit(responses.calls[-1].request.url).query)
    assert query["q"] == ["Problem"] and query["type"] == ["issues"]
    assert query["state"] == ["all"]


@responses.activate
def test_numeric_search_pr_or_missing_returns_no_choices(client):
    responses.get(API + "/repos/owner/repo/issues/7", json=issue(pull_request={}))
    assert client.search_issues("7") == []
    responses.replace(responses.GET, API + "/repos/owner/repo/issues/7", status=404)
    assert client.search_issues("7") == []


@responses.activate
def test_comments_and_numeric_repo_lookup(client):
    responses.get(API + "/repositories/41", json=REPO)
    responses.get(
        API + "/repos/owner/repo/issues/7/comments",
        json=[{"id": 4, "body": "note"}],
        headers={"X-Total-Count": "1"},
    )
    assert client.get_repo_by_id(41) == REPO
    assert client.list_comments(7) == [{"id": 4, "body": "note"}]


def marker_for(dedup):
    return (
        "<!-- sentry-gitea-meta v1 " + json.dumps({"source": "sentry", "dedup_id": dedup}) + " -->"
    )


@responses.activate
def test_marker_search_trusts_only_issues_opened_by_the_given_author(client):
    """R2-03: anyone who can open an issue can paste a marker; only our user's count."""
    dedup = "https://sentry.example/1/2"
    responses.get(
        API + "/repos/owner/repo/issues",
        json=[
            issue(number=3, body=marker_for(dedup), user={"id": 66, "login": "mallory"}),
            issue(number=4, body=marker_for(dedup), user=None),
            issue(number=5, body=marker_for(dedup), user={"id": "7"}),
            issue(number=6, body=marker_for(dedup), user={"id": True}),
            issue(number=7, body=marker_for(dedup), user={"id": 7, "login": "bot"}),
        ],
        headers={"X-Total-Count": "5"},
    )
    assert client.search_issues_by_marker(dedup, author_id=7)["number"] == 7
    assert client.search_issues_by_marker(dedup, author_id=8) is None
    # Without an author, the first marker wins (legacy behavior for callers that opt out).
    assert client.search_issues_by_marker(dedup)["number"] == 3
    for invalid in [0, -1, "7", True, 7.0]:
        with pytest.raises(ValueError):
            client.search_issues_by_marker(dedup, author_id=invalid)


@responses.activate
def test_marker_search_accepts_an_explicit_repository_for_unbound_clients():
    dedup = "https://sentry.example/1/2"
    responses.get(
        API + "/repos/team/other/issues",
        json=[issue(body=marker_for(dedup), user={"id": 7})],
        headers={"X-Total-Count": "1"},
    )
    with GiteaClient(BASE, TOKEN, token_type="Bearer") as unbound:
        assert unbound.search_issues_by_marker(dedup, author_id=7, repo="team/other")
        with pytest.raises(ValueError):
            unbound.search_issues_by_marker(dedup, author_id=7)


@responses.activate
def test_current_user_id_is_read_once_per_client_and_validated(client):
    responses.get(API + "/user", json={"id": 7, "login": "bot"})
    assert client.current_user_id() == client.current_user_id() == 7
    assert len(responses.calls) == 1
    for bad in [{"id": "7"}, {"id": 0}, {"id": True}, {}]:
        responses.replace(responses.GET, API + "/user", json=bad)
        with GiteaClient(BASE, TOKEN, "owner/repo") as fresh:
            with pytest.raises(GiteaApiError, match="invalid user"):
                fresh.current_user_id()


@responses.activate
def test_byte_budget_spans_paginated_responses_with_default_limits(client):
    """R2-05: each page fits MAX_RESPONSE_BYTES, but one operation cannot read them all."""
    big = "x" * (MAX_RESPONSE_BYTES - 1024)

    def page(request):
        number = int(parse_qs(urlsplit(request.url).query)["page"][0])
        rows = [{"id": number, "name": big}]
        return 200, {"X-Total-Count": str(MAX_PAGES * PAGE_SIZE)}, json.dumps(rows)

    responses.add_callback(responses.GET, API + "/repos/owner/repo/labels", callback=page)
    with pytest.raises(GiteaApiError, match="data budget") as caught:
        client.list_labels()
    assert caught.value.status == 0
    assert OPERATION_MAX_BYTES < MAX_PAGES * MAX_RESPONSE_BYTES
    assert len(responses.calls) == OPERATION_MAX_BYTES // len(big) + 1 < MAX_PAGES
    # The budget belongs to the operation: later requests on this client are refused too.
    responses.get(API + "/repos/owner/repo", json=REPO)
    with pytest.raises(GiteaApiError, match="data budget"):
        client.get_repo()


@responses.activate
def test_byte_budget_rejects_announced_lengths_before_reading():
    responses.get(API + "/user", json={"id": 1})
    responses.get(API + "/repos/owner/repo", body="x" * 600, headers={"Content-Length": "600"})
    with GiteaClient(BASE, TOKEN, "owner/repo", max_bytes=500) as small:
        small.get_current_user()
        with pytest.raises(GiteaApiError, match="data budget"):
            small.get_repo()


@responses.activate
def test_row_cap_spans_pages_and_walks(client):
    """R2-05: tiny rows are cheap to send and expensive to hold; cap them per operation."""
    responses.get(
        API + "/repos/owner/repo/labels",
        json=[{} for _ in range(OPERATION_MAX_ROWS + 1)],
        headers={"X-Total-Count": str(OPERATION_MAX_ROWS + 1)},
    )
    with pytest.raises(GiteaApiError, match="more rows") as caught:
        client.list_labels()
    assert caught.value.status == 0
    rows = [{"id": i} for i in range(OPERATION_MAX_ROWS // 2 + 1)]
    responses.get(API + "/repos/owner/repo/hooks", json=rows, headers={"X-Total-Count": "1"})
    responses.get(API + "/repos/owner/repo/assignees", json=rows)
    with GiteaClient(BASE, TOKEN, "owner/repo") as fresh:
        assert len(fresh.list_hooks()) == len(rows)
        with pytest.raises(GiteaApiError, match="more rows"):
            fresh.list_assignees()


@responses.activate
def test_label_lookup_stops_reading_pages_once_every_name_is_found(client):
    def page(request):
        number = int(parse_qs(urlsplit(request.url).query)["page"][0])
        rows = [{"id": number * 100 + i, "name": f"label-{number}-{i}"} for i in range(PAGE_SIZE)]
        if number == 1:
            rows[3] = {"id": 9, "name": "bug"}
            rows[4] = {"id": 4, "name": "bug"}
        return 200, {"X-Total-Count": str(10 * PAGE_SIZE)}, json.dumps(rows)

    responses.add_callback(responses.GET, API + "/repos/owner/repo/labels", callback=page)
    assert client.label_ids_by_name(["bug"]) == {"bug": 4}
    assert client.resolve_label_ids(["bug", "label-2-0"], create_missing=False) == [4, 200]
    assert len(responses.calls) == 1 + 2
    # Only wanted labels are kept, and a missing one still requires the whole listing.
    assert client.label_ids_by_name(["absent"]) == {}
    assert len(responses.calls) == 3 + 10


@responses.activate
def test_label_conflict_reread_looks_only_for_the_conflicting_name(client):
    responses.get(
        API + "/repos/owner/repo/labels",
        json=[{"id": 1, "name": "bug"}],
        headers={"X-Total-Count": "1"},
    )
    responses.post(API + "/repos/owner/repo/labels", status=409)
    responses.get(
        API + "/repos/owner/repo/labels",
        json=[{"id": 1, "name": "bug"}, {"id": 2, "name": "new"}],
        headers={"X-Total-Count": "2"},
    )
    assert client.resolve_label_ids(["bug", "new"]) == [1, 2]


@responses.activate
def test_only_response_bodies_are_marked_upstream(client):
    responses.get(API + "/user", status=500, body="body from the server")
    with pytest.raises(GiteaApiError) as caught:
        client.get_current_user()
    assert caught.value.upstream is True
    responses.replace(responses.GET, API + "/user", status=500, body="")
    with pytest.raises(GiteaApiError) as caught:
        client.get_current_user()
    assert caught.value.upstream is False
    responses.replace(responses.GET, API + "/user", status=302, headers={"Location": "/x"})
    with pytest.raises(GiteaApiError) as caught:
        client.get_current_user()
    assert caught.value.upstream is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://gitea:3000", "http://gitea:3000"),
        ("http://GITEA:3000/sub/path/", "http://gitea:3000"),
        ("https://forge.example:443/git", "https://forge.example"),
        ("http://forge.example:80", "http://forge.example"),
        ("http://forge.example:443", "http://forge.example:443"),
        ("http://[::1]:3000/", "http://[::1]:3000"),
    ],
)
def test_url_origin(value, expected):
    assert url_origin(value) == expected


@pytest.mark.parametrize("value", ["gitea:3000", "ftp://gitea", "http://u:p@gitea", "", None])
def test_url_origin_rejects_non_http_urls(value):
    with pytest.raises(ValueError):
        url_origin(value)


def test_caller_may_hand_over_session_ownership():
    closed = []
    session = requests.Session()
    session.close = lambda: closed.append(True)
    with GiteaClient(BASE, TOKEN, "owner/repo", session=session):
        pass
    assert closed == []
    with GiteaClient(BASE, TOKEN, "owner/repo", session=session, owns_session=True):
        pass
    assert closed == [True]


@responses.activate
def test_marker_search_prefers_an_open_match_over_an_earlier_closed_one(client):
    # A group re-filed after a close has two marked issues; recovery must find the open one.
    dedup = "https://sentry.example/1/2"
    responses.get(
        API + "/repos/owner/repo/issues",
        json=[
            issue(number=7, body=marker_for(dedup), state="closed"),
            issue(number=8, body=marker_for(dedup)),
        ],
        headers={"X-Total-Count": "2"},
    )
    assert client.search_issues_by_marker(dedup)["number"] == 8


@responses.activate
def test_marker_search_state_and_exclude_never_return_the_replaced_issue(client):
    dedup = "https://sentry.example/1/2"
    responses.get(
        API + "/repos/owner/repo/issues",
        json=[
            # Gitea filters by state server-side; the client re-checks in case it does not.
            issue(number=7, body=marker_for(dedup), state="closed"),
            issue(number=9, body=marker_for(dedup)),
        ],
        headers={"X-Total-Count": "2"},
    )
    found = client.search_issues_by_marker(dedup, state="open", exclude=["9"])
    assert found is None
    query = parse_qs(urlsplit(responses.calls[0].request.url).query)
    assert query["state"] == ["open"]
    assert client.search_issues_by_marker(dedup, state="open", exclude=[7])["number"] == 9
    assert client.search_issues_by_marker(dedup, state="closed")["number"] == 7


def test_marker_search_rejects_unknown_states(client):
    for state in ["", "OPEN", "merged", None]:
        with pytest.raises(ValueError, match="state"):
            client.search_issues_by_marker("x", state=state)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-05-23T14:18:10Z", "2026-05-23T14:18:10+00:00"),
        ("2026-05-23T14:18:10z", "2026-05-23T14:18:10+00:00"),
        ("2026-10-31T12:20:07-03:00", "2026-10-31T12:20:07-03:00"),
        ("2026-10-31T15:20:07.123456+00:00", "2026-10-31T15:20:07.123456+00:00"),
    ],
)
def test_parse_gitea_time_returns_aware_datetimes(value, expected):
    assert parse_gitea_time(value).isoformat() == expected


@pytest.mark.parametrize(
    "value", [None, "", "   ", "yesterday", "2026-05-23T14:18:10", "2026-05-23", 1700000000, {}]
)
def test_parse_gitea_time_rejects_naive_or_malformed_values(value):
    assert parse_gitea_time(value) is None
