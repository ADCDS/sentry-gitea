import hashlib

import pytest

from sentry_gitea.integration.utils import (
    OAUTH_SCOPES,
    external_id,
    issue_key,
    normalize_tokens,
    parse_issue_key,
    validate_scopes,
)


def test_external_identity_is_stable_bounded_and_namespace_specific():
    value = external_id("https://FORGE.example/git/", "org", 8)
    assert value == hashlib.sha256(b"v1|https://forge.example/git|org|8").hexdigest()
    assert len(value) == 64
    assert value != external_id("https://forge.example/git", "user", 8)
    assert value != external_id("https://forge.example", "org", 8)


def test_canonical_issue_key_uses_repo_id_and_local_number():
    assert issue_key(41, 7) == "41#7"
    assert parse_issue_key("41#7") == (41, 7)


@pytest.mark.parametrize("key", ["owner/repo#7", "0#1", "1#0", "01#7", "1#2#3", None])
def test_invalid_issue_keys_are_rejected(key):
    with pytest.raises(ValueError):
        parse_issue_key(key)


def test_token_normalizer_handles_scope_omission_and_rotated_refresh_token():
    tokens = normalize_tokens(
        {"access_token": "test-access", "refresh_token": "test-next", "expires_in": 3600}, now=100
    )
    assert tokens == {"access_token": "test-access", "refresh_token": "test-next", "expires": 3700}
    updated = normalize_tokens(
        {"access_token": "test-next-access", "expires_in": 20}, previous=tokens, now=200
    )
    assert updated["refresh_token"] == "test-next"
    assert updated["expires"] == 220
    assert validate_scopes(OAUTH_SCOPES) == list(OAUTH_SCOPES)


@pytest.mark.parametrize("scopes", [[], ["all"], ["read:user", "made:up"], "openid"])
def test_invalid_or_incomplete_requested_scopes_never_fall_back_to_all(scopes):
    with pytest.raises(ValueError):
        validate_scopes(scopes)


@pytest.mark.parametrize(
    "tokens",
    [
        {},
        {"access_token": "test"},
        {"access_token": "test", "refresh_token": "test", "expires_in": "oops"},
        {"access_token": "test", "refresh_token": "test", "expires_in": -1},
    ],
)
def test_malformed_token_responses_are_rejected_without_echoing_them(tokens):
    with pytest.raises(ValueError, match="Gitea returned invalid OAuth credentials"):
        normalize_tokens(tokens)
