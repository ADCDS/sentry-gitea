"""Idempotent storage of normalized Gitea commits, file changes and pull requests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import router, transaction
from sentry.models.commit import Commit
from sentry.models.commitauthor import CommitAuthor
from sentry.models.commitfilechange import CommitFileChange
from sentry.models.pullrequest import PullRequest
from sentry.tasks.codeowners.code_owners_auto_sync import code_owners_auto_sync
from sentry.utils.strings import truncatechars

from sentry_gitea.api.webhooks import PullRequestEvent

# Our CODEOWNERS lookup order (see GiteaSource). Core only auto-syncs the built-in providers'
# locations from post_save; file changes here are bulk-inserted, so we schedule all of ours.
CODEOWNERS_LOCATIONS = ("CODEOWNERS", ".gitea/CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")


def commit_author(organization_id: int, email: str | None, name: str | None) -> Any:
    # Same normalization as release commits (set_commits): lowercase, at most 75 characters.
    email = truncatechars((email or "").strip(), 75).lower()
    if not email:
        return None
    author, _ = CommitAuthor.objects.get_or_create(
        organization_id=organization_id,
        email=email,
        defaults={"name": (name or "")[:128] or None},
    )
    return author


def store_commit(repo: Any, data: dict[str, Any]) -> bool:
    """Upsert one normalized commit and its file changes; True when anything changed."""
    using = router.db_for_write(Commit)
    with transaction.atomic(using=using):
        author = commit_author(repo.organization_id, data["author_email"], data["author_name"])
        values: dict[str, Any] = {"message": data["message"], "date_added": data["timestamp"]}
        if author is not None:
            values["author"] = author
        commit, created = Commit.objects.get_or_create(
            organization_id=repo.organization_id,
            repository_id=repo.id,
            key=data["id"],
            defaults=values,
        )
        changed = {k: v for k, v in values.items() if getattr(commit, k) != v}
        if not created and changed:
            commit.update(**changed)
        existing = dict(
            CommitFileChange.objects.filter(commit_id=commit.id).values_list("filename", "type")
        )
        wanted = {patch["path"]: patch["type"] for patch in data["patch_set"]}
        new = [
            CommitFileChange(
                organization_id=repo.organization_id,
                commit_id=commit.id,
                filename=path,
                type=kind,
            )
            for path, kind in wanted.items()
            if path not in existing
        ]
        if new:
            CommitFileChange.objects.bulk_create(new, ignore_conflicts=True, batch_size=100)
        retyped = {p: k for p, k in wanted.items() if p in existing and existing[p] != k}
        for path, kind in retyped.items():
            CommitFileChange.objects.filter(commit_id=commit.id, filename=path).update(type=kind)
        touched = {change.filename: change.type for change in new} | retyped
        if any(touched.get(path) in {"A", "M"} for path in CODEOWNERS_LOCATIONS):
            commit_id = commit.id
            transaction.on_commit(
                lambda: code_owners_auto_sync.delay(commit_id=commit_id), using=using
            )
        return created or bool(changed) or bool(touched)


def store_commits(repo: Any, commits: Iterable[dict[str, Any]]) -> int:
    return sum(1 for commit in commits if store_commit(repo, commit))


def upsert_pull_request(repo: Any, event: PullRequestEvent) -> Any:
    """Insert or refresh a pull request row. A recorded merge or author is never cleared."""
    existing = PullRequest.objects.filter(repository_id=repo.id, key=str(event.number)).first()
    author = commit_author(repo.organization_id, event.author_email, event.author_name)
    values: dict[str, Any] = {
        "title": event.title,
        "message": event.body,
        "author": author if author is not None else getattr(existing, "author", None),
        "merge_commit_sha": event.merge_commit_sha or getattr(existing, "merge_commit_sha", None),
    }
    if event.created_at is not None:
        values["date_added"] = event.created_at
    if existing is not None and all(getattr(existing, k) == v for k, v in values.items()):
        # Identical replay: skip the save so post_save group-reference handling does not rerun.
        return existing
    pull_request, _ = PullRequest.objects.update_or_create(
        organization_id=repo.organization_id,
        repository_id=repo.id,
        key=str(event.number),
        defaults=values,
    )
    return pull_request
