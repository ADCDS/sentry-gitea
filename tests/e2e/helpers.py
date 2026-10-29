from dataclasses import dataclass, field

from redaction import Credential


@dataclass(frozen=True)
class E2EEnvironment:
    sentry_url: str
    sentry_token: str = field(repr=False)
    sentry_org: str
    sentry_project: str
    sentry_dsn: str = field(repr=False)
    gitea_url: str
    gitea_internal_url: str
    gitea_token: str = field(repr=False)
    gitea_owner: str
    gitea_repo: str
    gitea_plugin_token: str = field(default="", repr=False)

    @property
    def credentials(self) -> tuple[Credential, ...]:
        return tuple(
            item
            for item in (
                Credential("gitea admin token", self.gitea_token),
                Credential("gitea plugin token", self.gitea_plugin_token),
                Credential("sentry token", self.sentry_token),
            )
            if item.value
        )


@dataclass(frozen=True)
class IntegrationEnvironment:
    stack: E2EEnvironment
    sentry_admin_email: str
    sentry_admin_password: str = field(repr=False)
    sentry_owner_username: str
    sentry_owner_password: str = field(repr=False)
    gitea_e2e_username: str
    gitea_e2e_password: str = field(repr=False)
    gitea_e2e_repo: str
    gitea_e2e_default_repo: str
    gitea_e2e_org: str
    gitea_e2e_member_org: str
    gitea_oauth_app_id: str
    gitea_client_id: str = field(repr=False)
    gitea_client_secret: str = field(repr=False)

    @property
    def credentials(self) -> tuple[Credential, ...]:
        return self.stack.credentials + tuple(
            item
            for item in (
                Credential("sentry admin password", self.sentry_admin_password),
                Credential("sentry owner password", self.sentry_owner_password),
                Credential("e2e user password", self.gitea_e2e_password),
                Credential("oauth client secret", self.gitea_client_secret),
            )
            if item.value
        )

    @property
    def integrations_path(self) -> str:
        return f"/api/0/organizations/{self.stack.sentry_org}/integrations/"


def recent_oauth_logs(since: str, credentials: tuple[Credential, ...]) -> str:
    """Check complete captured web output for leaks before returning only OAuth warnings."""
    import subprocess

    from flows import check, poll_budget, without_credentials
    from harness import ROOT, self_hosted_compose

    try:
        result = subprocess.run(
            self_hosted_compose()
            + [
                "logs",
                "--no-color",
                "--timestamps",
                "--since",
                since,
                "--tail",
                "2000",
                "web",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=poll_budget(10),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Read Sentry web logs failed or timed out; output withheld") from None
    check(result.returncode == 0, "Read Sentry web logs failed; output withheld")
    without_credentials(result.stdout, credentials, service="web")
    return "\n".join(
        line for line in result.stdout.splitlines() if "sentry-gitea: oauth failed" in line
    )
