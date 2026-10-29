"""Opt-in issue synchronization: status, assignees (explicit email mappings) and outbound notes.

Every option defaults to false. Inbound changes always re-read the Gitea issue, so a replayed or
reordered webhook applies Gitea's current state rather than the state in the payload. Inbound
assignments carry `AssignmentSource.from_integration`, which the inherited `should_sync` uses to
stop them from echoing back to Gitea.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from sentry.constants import ObjectStatus
from sentry.integrations.mixins.issues import (
    IssueSyncIntegration,
    ResolveSyncAction,
    where_should_sync,
)
from sentry.integrations.services.assignment_source import AssignmentSource
from sentry.locks import locks
from sentry.models.group import Group
from sentry.models.groupassignee import GroupAssignee
from sentry.models.groupmeta import GroupMeta
from sentry.models.project import Project
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import ApiError, IntegrationError, IntegrationFormError
from sentry.users.services.user.service import user_service
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea.api.client import issue_number

from .identity import log_failure
from .utils import parse_issue_key

logger = logging.getLogger(__name__)
SYNC_FAILED = "sentry-gitea: sync failed"
SYNC_OPTIONS = {
    "sync_status_forward": "Close and reopen Gitea issues when Sentry issues are resolved",
    "sync_status_reverse": "Resolve and unresolve Sentry issues when Gitea issues close/reopen",
    "sync_comments": "Post Sentry notes (and their edits) as Gitea comments",
    "sync_assignee_forward": "Assign the mapped Gitea user when a Sentry issue is assigned",
    "sync_assignee_reverse": "Assign the mapped Sentry user when a Gitea issue is assigned",
}
MAPPINGS_KEY = "user_mappings"
MAX_MAPPINGS = 500
GITEA_USERNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}[A-Za-z0-9_])?", re.ASCII)
NOTE_MARKER = "<!-- sentry-gitea-note {} -->"
# Neutralize our own marker syntax inside user text, so a note cannot claim another note's comment.
MARKER_TEXT = re.compile(r"<!--(\s*sentry-gitea-)", re.IGNORECASE)


def parse_user_mappings(value: Any) -> dict[str, str]:
    """`{"<sentry user email>": "<gitea username>"}`, given as an object or JSON text."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("Enter a JSON object.") from None
    if not isinstance(value, dict):
        raise ValueError("Enter a JSON object.")
    if len(value) > MAX_MAPPINGS:
        raise ValueError(f"Use at most {MAX_MAPPINGS} mappings.")
    result: dict[str, str] = {}
    for email, username in value.items():
        if not isinstance(email, str) or not isinstance(username, str):
            raise ValueError("Map email strings to username strings.")
        email = email.strip()
        try:
            validate_email(email)
        except ValidationError:
            raise ValueError("Keys must be email addresses.") from None
        if len(email) > 254 or not GITEA_USERNAME.fullmatch(username):
            raise ValueError("Values must be Gitea usernames.")
        if email.casefold() in result:
            raise ValueError("Each email may appear only once.")
        result[email.casefold()] = username
    return result


def comment_locator(integration_id: int, issue_key: str, note_id: int) -> str:
    """GroupMeta key for one note on one linked issue (57 characters)."""
    digest = hashlib.sha256(f"{integration_id}|{issue_key}|{note_id}".encode()).digest()
    return "gitea:comment:" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def escape_markdown(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#+!|~])", r"\\\1", text)


class GiteaIssueSync(IssueSyncIntegration):
    comment_key = "sync_comments"
    outbound_status_key = "sync_status_forward"
    inbound_status_key = "sync_status_reverse"
    outbound_assignee_key = "sync_assignee_forward"
    inbound_assignee_key = "sync_assignee_reverse"

    def should_sync(self, attribute: str, sync_source: AssignmentSource | None = None) -> bool:
        return bool(
            self.model.status == ObjectStatus.ACTIVE
            and self.org_integration is not None
            and self.org_integration.status == ObjectStatus.ACTIVE
            and super().should_sync(attribute, sync_source)
        )

    # Organization configuration.

    def get_organization_config(self) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = [
            {
                "name": key,
                "type": "boolean",
                "label": label,
                "default": False,
                "help": (
                    "Requires Add Repository (which installs the signed Gitea webhook) "
                    "for the issue's repository."
                    if key in {"sync_status_reverse", "sync_assignee_reverse"}
                    else ""
                ),
            }
            for key, label in SYNC_OPTIONS.items()
        ]
        fields.append(
            {
                "name": MAPPINGS_KEY,
                "type": "textarea",
                "label": "User mappings",
                "default": "{}",
                "autosize": True,
                "help": (
                    'JSON object {"<Sentry user email>": "<Gitea username>"}, at most '
                    f"{MAX_MAPPINGS} entries. Sentry emails must be verified; Gitea emails are "
                    "never inferred from usernames."
                ),
            }
        )
        return fields

    def get_config_data(self) -> Mapping[str, Any]:
        data = dict(super().get_config_data())
        if isinstance(data.get(MAPPINGS_KEY), dict):
            # The textarea edits text; the stored value stays a JSON object.
            data[MAPPINGS_KEY] = json.dumps(data[MAPPINGS_KEY], indent=2, sort_keys=True)
        return data

    def update_organization_config(self, data: Any) -> None:
        data = dict(data)
        for key in SYNC_OPTIONS:
            if key in data and type(data[key]) is not bool:
                raise IntegrationFormError({key: ["Use true or false."]})
        if MAPPINGS_KEY in data:
            try:
                data[MAPPINGS_KEY] = parse_user_mappings(data[MAPPINGS_KEY])
            except ValueError as error:
                raise IntegrationFormError({MAPPINGS_KEY: [str(error)]}) from None
        super().update_organization_config(data)

    def _mappings(self) -> dict[str, str]:
        config = self.org_integration.config if self.org_integration else {}
        try:
            return parse_user_mappings(config.get(MAPPINGS_KEY))
        except ValueError:
            return {}

    # Shared helpers.

    @contextmanager
    def _sync_operation(self, operation: str, **ids: Any) -> Iterator[Any]:
        """Yield a client logging `sentry-gitea: sync failed`; errors are logged and re-raised."""
        try:
            client = self.get_client()
            client.failure_message = SYNC_FAILED
            yield client
        except (ApiError, IntegrationError, ValueError, UnableToAcquireLock) as error:
            log_failure(
                logger,
                SYNC_FAILED,
                error,
                operation=operation,
                integration_id=self.model.id,
                organization_id=self.organization_id,
                **ids,
            )
            raise

    def _hook_installed(self, key: str) -> bool:
        repo_id, _ = parse_issue_key(key)
        return any(
            isinstance(config, dict) and config.get("hook_id")
            for config in Repository.objects.filter(
                organization_id=self.organization_id,
                integration_id=self.model.id,
                provider="integrations:gitea",
                external_id=str(repo_id),
                status=ObjectStatus.ACTIVE,
            ).values_list("config", flat=True)
        )

    def _current_issue(self, client: Any, key: str) -> tuple[str, int, dict[str, Any]]:
        """Re-read the linked issue through its stable repository ID."""
        repo_id, number = parse_issue_key(key)
        repo = client.get_repo_by_id(repo_id)
        issue = client.get_issue(number, repo=repo["full_name"])
        if str(issue.get("number")) != str(number):
            raise IntegrationError("Gitea returned a different issue.")
        return repo["full_name"], number, issue

    # Status.

    def sync_status_outbound(self, external_issue: Any, is_resolved: bool, project_id: int) -> None:
        if not self.should_sync("outbound_status"):
            return
        desired = "closed" if is_resolved else "open"
        with self._sync_operation("status_outbound", issue_key=external_issue.key) as client:
            path, number, issue = self._current_issue(client, external_issue.key)
            if issue.get("state") != desired:
                client.update_issue(number, repo=path, state=desired)

    def get_resolve_sync_action(self, data: Mapping[str, Any]) -> ResolveSyncAction:
        key, action = data.get("issue_key"), data.get("action")
        if not self.should_sync("inbound_status") or action not in {"closed", "reopened"}:
            return ResolveSyncAction.NOOP
        try:
            parse_issue_key(key)
        except ValueError:
            return ResolveSyncAction.NOOP
        if not self._hook_installed(key):
            return ResolveSyncAction.NOOP
        with self._sync_operation("status_inbound", issue_key=key) as client:
            # The payload's action only triggers a re-read; Gitea's current state decides.
            _, _, issue = self._current_issue(client, key)
        return {"closed": ResolveSyncAction.RESOLVE, "open": ResolveSyncAction.UNRESOLVE}.get(
            issue.get("state"), ResolveSyncAction.NOOP
        )

    # Assignees.

    def _gitea_username(self, user: Any) -> str | None:
        mappings = self._mappings()
        emails = sorted(
            (e.email for e in user.get_verified_emails()),
            key=lambda email: (email.casefold() != str(user.email).casefold(), email.casefold()),
        )
        return next((mappings[e.casefold()] for e in emails if e.casefold() in mappings), None)

    def sync_assignee_outbound(
        self, external_issue: Any, user: Any, assign: bool = True, **kwargs: Any
    ) -> None:
        if not self.should_sync("outbound_assignee", kwargs.get("assignment_source")):
            return
        desired: list[str] = []
        if assign:
            username = self._gitea_username(user) if user is not None else None
            if username is None:
                logger.warning(
                    SYNC_FAILED,
                    extra={
                        "operation": "assignee_outbound",
                        "reason": "unmapped_user",
                        "user_id": getattr(user, "id", None),
                        "integration_id": self.model.id,
                        "organization_id": self.organization_id,
                    },
                )
                return
            desired = [username]
        with self._sync_operation("assignee_outbound", issue_key=external_issue.key) as client:
            path, number, issue = self._current_issue(client, external_issue.key)
            current = [row.get("login") for row in issue.get("assignees") or [] if row]
            if sorted(str(c).casefold() for c in current) != [d.casefold() for d in desired]:
                client.update_issue(number, repo=path, assignees=desired)

    def sync_assignee_inbound(self, key: str) -> None:
        """Handle a Gitea `assigned`/`unassigned` delivery for this organization only."""
        if not self.should_sync("inbound_assignee") or not self._hook_installed(key):
            return
        organizations = where_should_sync(
            self.model, "inbound_assignee", organization_id=self.organization_id
        )
        groups = list(Group.objects.get_groups_by_external_issue(self.model, organizations, key))
        if not groups:
            return
        with self._sync_operation("assignee_inbound", issue_key=key) as client:
            _, _, issue = self._current_issue(client, key)
            assignees = issue.get("assignees")
            # Gitea serializes an empty assignee slice as null.
            assignees = [] if assignees is None else assignees
            if not isinstance(assignees, list) or any(
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or not isinstance(row.get("login"), str)
                for row in assignees
            ):
                raise IntegrationError("Gitea returned invalid assignees.")
        source = AssignmentSource.from_integration(self.model)
        if not assignees:
            for group in groups:
                GroupAssignee.objects.deassign(group, assignment_source=source)
            return
        by_login: dict[str, list[str]] = {}
        for email, login in self._mappings().items():
            by_login.setdefault(login.casefold(), []).append(email)
        # Verified, active organization members per mapped Gitea assignee, lowest Gitea ID first.
        ranked: list[list[Any]] = []
        for row in sorted(assignees, key=lambda row: row["id"]):
            emails = by_login.get(row["login"].casefold())
            if emails:
                users = user_service.get_many_by_email(
                    emails=emails,
                    is_verified=True,
                    is_active=True,
                    organization_id=self.organization_id,
                )
                ranked.append(sorted(users, key=lambda user: user.id))
        everyone = [user for users in ranked for user in users]
        access = Project.objects.get_by_users(everyone) if everyone else {}
        for group in groups:
            current = (
                GroupAssignee.objects.filter(group=group).values_list("user_id", flat=True).first()
            )
            for users in ranked:
                eligible = [u for u in users if group.project_id in access.get(u.id, ())]
                if eligible:
                    # Several emails may map to one login: keep the current one if it is among them.
                    user = next((u for u in eligible if u.id == current), eligible[0])
                    GroupAssignee.objects.assign(group, user, assignment_source=source)
                    break
            else:
                logger.info(
                    "sentry-gitea: webhook ignored",
                    extra={
                        "reason": "unmapped_assignee",
                        "group_id": group.id,
                        "integration_id": self.model.id,
                    },
                )

    # Outbound notes. Inbound Gitea comments are never imported.

    def _note_body(self, user_id: int, note: Any) -> str:
        user = user_service.get_user(user_id) if user_id else None
        name = escape_markdown(user.get_display_name() if user else "Sentry user")
        text = MARKER_TEXT.sub(r"&lt;!--\1", str((note.data or {}).get("text") or ""))
        return f"**{name}** wrote in Sentry:\n\n{text}\n\n{NOTE_MARKER.format(note.id)}"

    def _sync_comment(self, issue_id: str, user_id: int, note: Any, *, edit: bool) -> Any:
        if not self.should_sync("comment"):
            return None
        group = Group.objects.get(id=note.group_id)
        if group.project.organization_id != self.organization_id:
            raise IntegrationError("The note belongs to another organization.")
        repo_id, number = parse_issue_key(issue_id)
        marker = NOTE_MARKER.format(note.id)
        locator = comment_locator(self.model.id, issue_id, note.id)

        lock = locks.get(locator, duration=60, name="gitea_comment")
        with (
            self._sync_operation("comment", issue_key=issue_id, note_id=note.id) as client,
            lock.blocking_acquire(initial_delay=0.1, timeout=10),
        ):

            def ours(comment: Any) -> bool:
                body = comment.get("body") if isinstance(comment, dict) else None
                issue_url = comment.get("issue_url") if isinstance(comment, dict) else None
                return (
                    isinstance(body, str)
                    and body.rstrip().endswith(marker)
                    and (not isinstance(issue_url, str) or issue_url.endswith(f"/issues/{number}"))
                    # Anyone can post the marker; never edit or adopt another user's comment.
                    and client.authored(comment)
                )

            path = client.get_repo_by_id(repo_id)["full_name"]
            body = self._note_body(user_id, note)
            stored = (
                GroupMeta.objects.filter(group=group, key=locator)
                .values_list("value", flat=True)
                .first()
            )
            remote = None
            if stored and str(stored).isdigit():
                found = client.get_comment(int(stored), repo=path)
                remote = found if ours(found) else None
            if remote is None:
                # Recover a comment posted before its locator was saved (e.g. a retried task).
                remote = next((c for c in client.list_comments(number, repo=path) if ours(c)), None)
            if remote is None:
                remote = client.create_comment(number, body, repo=path)
            elif edit and remote.get("body") != body:
                remote = client.update_comment(int(issue_number(remote["id"])), body, repo=path)
            comment_id = issue_number(remote.get("id"))
            GroupMeta.objects.set_value(group, locator, comment_id)
            return remote

    def create_comment(self, issue_id: str, user_id: int, group_note: Any) -> Any:
        return self._sync_comment(issue_id, user_id, group_note, edit=False)

    def update_comment(self, issue_id: str, user_id: int, group_note: Any) -> Any:
        return self._sync_comment(issue_id, user_id, group_note, edit=True)
