"""Safe exceptions for callers that may log or display the error."""

import json
import re
from urllib.parse import quote, quote_plus


def redact(message: str, token: str = "") -> str:
    # Decode JSON's escaped slashes/unicode before redacting proxy URLs or echoed secrets.
    try:
        message = json.dumps(json.loads(message), ensure_ascii=False)
    except (ValueError, RecursionError):
        message = message.replace(r"\/", "/")
    # Redact before truncation, including URL-encoded credentials echoed by a proxy.
    for secret in {token, quote(token, safe=""), quote_plus(token)} - {""}:
        message = message.replace(secret, "[redacted]")
    message = re.sub(r"https?://[^\s<>\"']+", "[url]", message, flags=re.IGNORECASE)
    message = re.sub(
        r"""(?i)(authorization|token|password|secret)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}]+)""",
        r"\1=[redacted]",
        message,
    )
    return " ".join(message.split())[:300]


class GiteaApiError(Exception):
    """HTTP status is zero for a transport failure or an exhausted local budget.

    `upstream` marks a message derived from the server's response body (redacted, but still
    remote-controlled text). User-facing surfaces must show only the status for those.
    """

    def __init__(self, status: int, message: str, *, upstream: bool = False):
        self.status = status
        self.message = message[:300]
        self.upstream = upstream
        super().__init__(f"Gitea ({status}): {self.message}")
