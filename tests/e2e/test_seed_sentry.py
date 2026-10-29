"""Exercise our bootstrap orchestration with in-memory models, without importing Sentry."""

import hashlib
import json
import runpy
import secrets
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import harness
import pytest


class Row(SimpleNamespace):
    def check_password(self, value):
        return getattr(self, "password", None) == hashlib.sha256(value.encode()).hexdigest()

    def set_password(self, value):
        self.password = hashlib.sha256(value.encode()).hexdigest()
        self.last_password_change = datetime.now(UTC)
        self.is_password_expired = False
        self.password_changes = getattr(self, "password_changes", 0) + 1

    def save(self, *, update_fields):
        self.saved_fields = set(update_fields)

    def get_scopes(self):
        return self.scope_list


class Table:
    def __init__(self, rows=None, *, defaults=None):
        self.rows = [] if rows is None else rows
        self.defaults = defaults or {}

    def create(self, **values):
        row = Row(id=len(self.rows) + 1, **(self.defaults | values))
        self.rows.append(row)
        return row

    def filter(self, **where):
        return Table(
            [
                row
                for row in self.rows
                if all(getattr(row, key) == value for key, value in where.items())
            ]
        )

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(list(self.rows))

    def delete(self):
        self.rows.clear()

    def get(self, **where):
        matches = self.filter(**where).rows
        assert len(matches) == 1, "Expected one managed fixture row"
        return matches[0]

    def get_or_create(self, *, defaults=None, **where):
        row = self.filter(**where).first()
        return (row, False) if row else (self.create(**where, **(defaults or {})), True)

    def update_or_create(self, *, defaults, **where):
        row, created = self.get_or_create(defaults=defaults, **where)
        for key, value in defaults.items():
            setattr(row, key, value)
        return row, created


@pytest.fixture
def seed_models(monkeypatch):
    def module(name, **attributes):
        parent, _, _ = name.rpartition(".")
        if parent and parent not in sys.modules:
            module(parent)
        stub = ModuleType(name)
        stub.__path__ = []
        stub.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, stub)

    module("django.utils", timezone=SimpleNamespace(now=lambda: datetime.now(UTC)))
    options = {"system.url-prefix": "http://sentry.test"}
    module("sentry", options=SimpleNamespace(get=options.get))
    module("sentry.options", UpdateChannel=SimpleNamespace(CLI="cli"))
    module("sentry.types.token", AuthTokenType=SimpleNamespace(USER="user"))
    tables = {}
    for path, name in (
        ("models.apitoken", "ApiToken"),
        ("models.organization", "Organization"),
        ("models.organizationmember", "OrganizationMember"),
        ("models.organizationmemberteam", "OrganizationMemberTeam"),
        ("models.project", "Project"),
        ("models.projectkey", "ProjectKey"),
        ("models.projectteam", "ProjectTeam"),
        ("models.team", "Team"),
        ("users.models.user", "User"),
        ("users.models.useremail", "UserEmail"),
    ):
        tables[name] = Table(defaults={"is_password_expired": False} if name == "User" else {})
        module("sentry." + path, **{name: SimpleNamespace(objects=tables[name])})
    # Stale-integration reset (reset_gitea_integration_state) touches these.
    for name in ("Integration", "Identity", "IdentityProvider"):
        tables[name] = Table()
    module(
        "sentry.integrations.models.integration",
        Integration=SimpleNamespace(objects=tables["Integration"]),
    )
    module(
        "sentry.users.models.identity",
        Identity=SimpleNamespace(objects=tables["Identity"]),
        IdentityProvider=SimpleNamespace(objects=tables["IdentityProvider"]),
    )
    return tables


def test_seed_reuses_non_superuser_owner_and_repairs_membership_without_rotating_admin_token(
    seed_models, monkeypatch, tmp_path, capsys
):
    tables = seed_models
    admin = tables["User"].create(
        username="admin@example.invalid",
        email="admin@example.invalid",
        is_active=True,
        is_superuser=True,
        is_password_expired=False,
    )
    organization = tables["Organization"].create(slug="e2e")
    project = tables["Project"].create(organization=organization, slug="demo")
    tables["ProjectKey"].create(project=project, status=0, dsn_public="http://public@sentry.test/1")
    token_value = secrets.token_urlsafe(32)
    token = tables["ApiToken"].create(
        user=admin,
        name="sentry-gitea-e2e:e2e:demo",
        scoping_organization_id=organization.id,
        hashed_token=hashlib.sha256(token_value.encode()).hexdigest(),
        expires_at=None,
        scope_list={"org:read", "org:integrations", "project:admin", "event:write", "event:admin"},
    )
    admin_password, owner_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    for key, value in {
        "EMAIL": admin.username,
        "ORG": "e2e",
        "PROJECT": "demo",
        "URL": "http://sentry.test",
        "TOKEN": token_value,
        "PASSWORD": admin_password,
        "OWNER_PASSWORD": owner_password,
    }.items():
        monkeypatch.setenv("SENTRY_E2E_" + key, value)

    owner_id = None
    for attempt in range(3):
        output = tmp_path / f"seed-{attempt}.json"
        monkeypatch.setenv("SENTRY_E2E_OUTPUT", str(output))
        runpy.run_path(str(harness.ROOT / "docker/e2e/seed-sentry.py"))
        values = json.loads(output.read_text())
        assert values["SENTRY_OWNER_USERNAME"] == "e2e-owner"
        assert values["SENTRY_OWNER_PASSWORD"] == owner_password
        assert values["SENTRY_TOKEN"] == token_value
        assert values["SENTRY_ADMIN_PASSWORD"] == admin_password
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        owner = tables["User"].get(username="e2e-owner")
        assert owner.id != admin.id
        assert owner_id is None or owner_id == owner.id
        owner_id = owner.id
        assert owner.is_active and not owner.is_superuser and not owner.is_staff
        assert owner.check_password(owner_password) and not owner.is_password_expired
        assert owner.saved_fields >= {"password", "last_password_change", "is_password_expired"}
        assert owner.password_changes == (2 if attempt == 2 else 1)
        assert tables["UserEmail"].get(user=owner, email=owner.email).is_verified
        member = tables["OrganizationMember"].get(user_id=owner.id, organization=organization)
        assert member.role == "owner" and member.user_is_active and member.user_email == owner.email
        team = tables["Team"].get(slug="e2e", organization=organization)
        membership = tables["OrganizationMemberTeam"].get(organizationmember=member, team=team)
        assert membership.is_active
        assert len(tables["User"].rows) == 2
        assert len(tables["OrganizationMember"].rows) == 2
        assert len(tables["OrganizationMemberTeam"].rows) == 2
        assert tables["ApiToken"].rows == [token]
        assert token.user is admin and admin.is_superuser
        if attempt == 1:
            owner.is_superuser = owner.is_staff = owner.is_password_expired = True
            owner.is_active = member.user_is_active = membership.is_active = False
            member.role = "member"
            tables["UserEmail"].get(user=owner, email=owner.email).is_verified = False
    assert not capsys.readouterr().out


@pytest.mark.parametrize("fail_first", [False, True])
def test_owner_password_is_private_reused_and_kept_after_partial_bootstrap(
    tmp_path, monkeypatch, capsys, fail_first
):
    monkeypatch.setattr(harness, "STATE", tmp_path)
    monkeypatch.setattr(harness, "self_hosted_compose", lambda: ["docker", "compose"])
    admin_password, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    saved = {"SENTRY_ADMIN_PASSWORD": admin_password, "SENTRY_TOKEN": token}
    harness.write_env(tmp_path / "sentry.env", saved)
    seed_environments = []
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "env" in kwargs:
            current = harness.read_env(tmp_path / "sentry.env")
            assert current["SENTRY_OWNER_PASSWORD"] == kwargs["env"]["SENTRY_E2E_OWNER_PASSWORD"]
            assert stat.S_IMODE((tmp_path / "sentry.env").stat().st_mode) == 0o600
        if "sentry" in command and command[-1] == "exec":
            seed_environments.append(kwargs["env"])
            assert "SENTRY_E2E_OWNER_PASSWORD" in command
            if fail_first and len(seed_environments) == 1:
                raise RuntimeError("Simulated seed failure")
        if "cp" in command:
            env = seed_environments[-1]
            Path(command[-1]).write_text(
                json.dumps(
                    saved
                    | {
                        "SENTRY_OWNER_USERNAME": "e2e-owner",
                        "SENTRY_OWNER_PASSWORD": env["SENTRY_E2E_OWNER_PASSWORD"],
                    }
                )
            )
        return ""

    monkeypatch.setattr(harness, "run", run)
    for attempt in range(2):
        if fail_first and attempt == 0:
            with pytest.raises(RuntimeError, match="Simulated seed failure"):
                harness.bootstrap_sentry()
        else:
            harness.bootstrap_sentry()
        assert commands[-1][-3:-1] == ["rm", "-f"]
    owner_password = seed_environments[0]["SENTRY_E2E_OWNER_PASSWORD"]
    assert len(owner_password) >= 32 and owner_password != admin_password
    assert seed_environments[1]["SENTRY_E2E_OWNER_PASSWORD"] == owner_password
    result = harness.read_env(tmp_path / "sentry.env")
    assert result["SENTRY_OWNER_PASSWORD"] == owner_password
    assert result["SENTRY_TOKEN"] == token
    output = capsys.readouterr()
    for secret in (owner_password, admin_password, token):
        assert secret not in output.out + output.err
        assert all(secret not in " ".join(command) for command in commands)
