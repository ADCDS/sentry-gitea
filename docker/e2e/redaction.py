"""Credential leak diagnostics shared by CLI log capture and host E2E clients."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

SECRET_KINDS = frozenset(
    {
        "gitea admin token",
        "gitea plugin token",
        "sentry token",
        "e2e user password",
        "sentry admin password",
        "sentry owner password",
        "oauth client secret",
        "oauth state",
        "oauth authorization code",
        "oauth PKCE verifier",
        "oauth access token",
        "oauth refresh token",
        "invalid plugin token",
        "dummy plugin token",
    }
)
LOGGER = re.compile(
    r"\[(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]\s+([A-Za-z_][A-Za-z0-9_.-]*):"
    r"|[\"']logger(?:_name)?[\"']\s*:\s*[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"']"
)
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}) ")


@dataclass(frozen=True)
class Credential:
    kind: str
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind not in SECRET_KINDS:
            raise ValueError("Credential kind must be an explicit secret category")
        if not isinstance(self.value, str):
            raise TypeError("Credential values must be strings")


def log_logger(text: str, position: int) -> str:
    """Use a continuation's preceding logger; unlabelled access output stays unknown."""
    logger = "unknown"
    for line in text[:position].splitlines():
        if match := LOGGER.search(line):
            logger = next(group for group in match.groups() if group)
        else:
            # Docker prefixes every continuation line with the service name.
            content = line.partition(" | ")[2] if " | " in line else line
            content = TIMESTAMP.sub("", content, count=1)
            if content and not content[0].isspace() and not content.startswith("Traceback"):
                logger = "unknown"
    return logger


def exposure_message(
    text: str, credentials: Iterable[Credential], *, where: str = "", service: str | None = None
) -> str | None:
    secrets = tuple(credentials)
    if any(not isinstance(item, Credential) for item in secrets):
        raise TypeError("Use labeled credentials, never bare strings or dictionary keys")
    for credential in secrets:
        if not credential.value or (position := text.find(credential.value)) < 0:
            continue
        location = (
            f"log service={service} logger={log_logger(text, position)}" if service else where
        )
        # Path/logger diagnostics must remain safe even if upstream puts a secret there.
        for item in secrets:
            if item.value:
                location = location.replace(item.value, "<redacted>")
        return f"E2E credential exposure: {credential.kind}; {location}; content withheld"
    return None
