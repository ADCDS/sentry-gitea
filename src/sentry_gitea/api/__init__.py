"""Sentry-independent Gitea transport."""

from .client import GiteaClient
from .errors import GiteaApiError

__all__ = ["GiteaApiError", "GiteaClient"]
