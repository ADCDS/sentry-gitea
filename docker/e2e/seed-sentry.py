"""Executed inside the web container by `sentry exec`, which initializes Django."""

import hashlib
import json
import os
from pathlib import Path

from django.utils import timezone
from sentry import options
from sentry.models.apitoken import ApiToken
from sentry.models.organization import Organization
from sentry.models.organizationmember import OrganizationMember
from sentry.models.organizationmemberteam import OrganizationMemberTeam
from sentry.models.project import Project
from sentry.models.projectkey import ProjectKey
from sentry.models.projectteam import ProjectTeam
from sentry.models.team import Team
from sentry.options import UpdateChannel
from sentry.types.token import AuthTokenType
from sentry.users.models.user import User
from sentry.users.models.useremail import UserEmail

# Source-checked permission maps: ProjectPermission, ProjectAlertRulePermission,
# GroupPermission. project:admin is needed for plugin DELETE and also covers rule
# CRUD; event:write covers group actions/reads; event:admin covers GroupPermission
# DELETE (integration unlink); org:read covers the smoke org API. org:integrations
# covers OrganizationIntegrationsPermission (integration config POST, repository
# DELETE) and OrganizationIntegrationsLoosePermission DELETE (code mappings) for the
# integration sync E2E. A stored token lacking a scope is rotated below.
E2E_TOKEN_SCOPES = {"org:read", "org:integrations", "project:admin", "event:write", "event:admin"}


def ensure_url_prefix(url: str) -> None:
    # A fresh install without the web setup wizard has no URL prefix, which makes
    # every DSN empty. Set it the same way `sentry config set` does, only if unset.
    if not options.get("system.url-prefix"):
        options.set("system.url-prefix", url, channel=UpdateChannel.CLI)


def ensure_membership(user: User, organization: Organization, team: Team) -> None:
    member, _ = OrganizationMember.objects.update_or_create(
        organization=organization,
        user_id=user.id,
        defaults={"role": "owner", "user_email": user.email, "user_is_active": True},
    )
    OrganizationMemberTeam.objects.update_or_create(
        organizationmember=member, team=team, defaults={"is_active": True}
    )


def ensure_browser_owner(organization: Organization, team: Team, password: str) -> User:
    # Browser installs use a normal organization owner. Sentry's superuser audit
    # middleware records full request URLs, including OAuth callback code/state.
    owner, _ = User.objects.update_or_create(
        username="e2e-owner",
        defaults={
            "email": "e2e-owner@example.invalid",
            "name": "E2E owner",
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
        },
    )
    if owner.is_password_expired or not owner.check_password(password):
        owner.set_password(password)
        owner.save(update_fields=["password", "last_password_change", "is_password_expired"])
    UserEmail.objects.update_or_create(
        user=owner, email=owner.email, defaults={"is_verified": True}
    )
    ensure_membership(owner, organization, team)
    return owner


def reset_gitea_integration_state() -> None:
    """Test environment only: start every bootstrap without Gitea installs/identities.

    Sentry links one Gitea account to one Sentry user ("linked to a different Sentry
    user"), so identities left by earlier runs (or another E2E user) block installs.
    """
    from sentry.integrations.models.integration import Integration
    from sentry.users.models.identity import Identity, IdentityProvider

    providers = IdentityProvider.objects.filter(type="gitea")
    Identity.objects.filter(idp__in=providers).delete()
    providers.delete()
    for integration in Integration.objects.filter(provider="gitea"):
        integration.delete()


def main() -> None:
    ensure_url_prefix(os.environ["SENTRY_E2E_URL"].rstrip("/"))
    reset_gitea_integration_state()
    user = User.objects.get(username=os.environ["SENTRY_E2E_EMAIL"])
    if not user.is_superuser or not user.is_active:
        raise RuntimeError("The bootstrap user must be an active superuser")
    if user.username == "e2e-owner":
        raise RuntimeError("Sentry browser owner must differ from the bootstrap superuser")
    password = os.environ["SENTRY_E2E_PASSWORD"]
    if user.is_password_expired or not user.check_password(password):
        user.set_password(password)
        user.save(update_fields=["password", "last_password_change", "is_password_expired"])
    organization, _ = Organization.objects.get_or_create(
        slug=os.environ["SENTRY_E2E_ORG"], defaults={"name": "Gitea E2E"}
    )
    team, _ = Team.objects.get_or_create(
        organization=organization, slug="e2e", defaults={"name": "E2E"}
    )
    ensure_membership(user, organization, team)
    owner_password = os.environ["SENTRY_E2E_OWNER_PASSWORD"]
    owner = ensure_browser_owner(organization, team, owner_password)
    project, _ = Project.objects.get_or_create(
        organization=organization,
        slug=os.environ["SENTRY_E2E_PROJECT"],
        defaults={"name": "Gitea demo", "platform": "python"},
    )
    ProjectTeam.objects.get_or_create(project=project, team=team)
    key = ProjectKey.objects.filter(project=project, status=0).first()
    if key is None:
        key = ProjectKey.objects.create(project=project, label="Gitea E2E")

    token_value = os.environ.get("SENTRY_E2E_TOKEN", "")
    token_name = f"sentry-gitea-e2e:{organization.slug}:{project.slug}"
    managed_tokens = ApiToken.objects.filter(
        user=user, name=token_name, scoping_organization_id=organization.id
    )
    token = (
        managed_tokens.filter(hashed_token=hashlib.sha256(token_value.encode()).hexdigest()).first()
        if token_value
        else None
    )
    if (
        token is None
        or (token.expires_at and token.expires_at <= timezone.now())
        or not E2E_TOKEN_SCOPES.issubset(token.get_scopes())
    ):
        # Recover lost local state without accumulating usable harness tokens.
        managed_tokens.delete()
        token = ApiToken.objects.create(
            user=user,
            name=token_name,
            scoping_organization_id=organization.id,
            token_type=AuthTokenType.USER,
            scope_list=sorted(E2E_TOKEN_SCOPES),
            expires_at=None,
            refresh_token=None,
        )
        token_value = token.plaintext_token

    url = os.environ["SENTRY_E2E_URL"].rstrip("/")
    values = {
        "SENTRY_URL": url,
        "SENTRY_TOKEN": token_value,
        "SENTRY_ORG": organization.slug,
        "SENTRY_PROJECT": project.slug,
        "SENTRY_DSN": key.dsn_public,
        "SENTRY_ADMIN_EMAIL": user.username,
        "SENTRY_ADMIN_PASSWORD": password,
        "SENTRY_OWNER_USERNAME": owner.username,
        "SENTRY_OWNER_PASSWORD": owner_password,
    }
    # No token/DSN is sent to stdout, stderr, or container logs.
    output = Path(os.environ["SENTRY_E2E_OUTPUT"])
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(values, stream)


main()
