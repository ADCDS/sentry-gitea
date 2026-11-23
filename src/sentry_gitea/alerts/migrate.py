"""Find, and optionally repoint, alert rules that still name the legacy `gitea_alerts` plugin.

With the bridge active those rules keep delivering on every Sentry, so nothing has to be
migrated for alerts to work. What changes on Sentry 26.7 and newer is the rule editor: it no
longer knows a plugin called "Gitea Alerts". Repointing the rules to the internal Sentry App
(see `app.py`) makes them editable again.

    python -m sentry_gitea.alerts.migrate --org <slug>                    # report only
    python -m sentry_gitea.alerts.migrate --org <slug> --create-app --user <email>
    # add the printed slug to GITEA_ALERTS_WEBHOOK_TARGETS and restart Sentry, then:
    python -m sentry_gitea.alerts.migrate --org <slug> --repoint <app slug> --apply

Run it where Sentry's settings are loaded, for example
`docker compose run --rm web python -m sentry_gitea.alerts.migrate --org <slug>`.
Without `--apply` nothing is written. The order matters: a repointed rule is delivered by the
bridge only when its slug is in `GITEA_ALERTS_WEBHOOK_TARGETS`; otherwise Sentry posts it to
the app's webhook, which many self-hosted installations cannot reach. `--apply` therefore
refuses until the slug is in that setting, unless `--via-webhook` says the webhook is wanted.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import Any

# Nothing from Sentry is imported at module level: as a command this module is loaded before
# Sentry is configured, and Sentry's models cannot be imported until it is.
PLUGIN_SLUG = "gitea_alerts"
SERVICE_ACTION = "sentry.rules.actions.notify_event_service.NotifyEventServiceAction"


@dataclasses.dataclass
class Found:
    actions: list[Any]  # workflow-engine webhook actions
    rules: list[Any]  # issue alert rules (the pre-workflow-engine rows Sentry still keeps)

    def __bool__(self) -> bool:
        return bool(self.actions or self.rules)


def _rule_names_plugin(rule: Any, service: str) -> bool:
    actions = (rule.data or {}).get("actions") or []
    return any(isinstance(a, dict) and a.get("service") == service for a in actions)


def find(organization_id: int, service: str = PLUGIN_SLUG) -> Found:
    """Everything in the organization that targets `service`, in both places Sentry keeps it."""
    from sentry.models.rule import Rule
    from sentry.workflow_engine.models import Action

    actions = list(
        Action.objects.filter(
            type=Action.Type.WEBHOOK,
            config__target_identifier=service,
            dataconditiongroupaction__condition_group__organization_id=organization_id,
        )
        .distinct()
        .order_by("id")
    )
    rules = [
        rule
        for rule in Rule.objects.filter(project__organization_id=organization_id).order_by("id")
        if _rule_names_plugin(rule, service)
    ]
    return Found(actions, rules)


def repoint(organization_id: int, target: str, *, apply: bool = False) -> Found:
    """Change every `gitea_alerts` target in the organization to `target`.

    Returns what was (or, without `apply`, would be) changed. Both representations change in
    one transaction, so the rule list and the workflow engine cannot disagree afterwards.
    """
    from django.db import router, transaction
    from sentry.models.rule import Rule
    from sentry.workflow_engine.models import Action

    if not target or target == PLUGIN_SLUG:
        raise ValueError("Give the slug of the Sentry App to repoint to.")
    found = find(organization_id)
    if not apply or not found:
        return found
    with (
        transaction.atomic(using=router.db_for_write(Action)),
        transaction.atomic(using=router.db_for_write(Rule)),
    ):
        for action in found.actions:
            action.config = {**action.config, "target_identifier": target}
            action.save(update_fields=["config"])
        for rule in found.rules:
            data = dict(rule.data)
            data["actions"] = [
                {**a, "service": target}
                if isinstance(a, dict) and a.get("service") == PLUGIN_SLUG
                else a
                for a in data.get("actions") or []
            ]
            rule.data = data
            rule.save(update_fields=["data"])
    return found


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        prog="python -m sentry_gitea.alerts.migrate",
        description="Report, and optionally repoint, alert rules that name the legacy "
        "gitea_alerts plugin. Writes nothing without --create-app or --apply.",
    )
    cli.add_argument("--org", required=True, help="organization slug")
    cli.add_argument("--create-app", action="store_true", help="create the internal app")
    cli.add_argument("--user", help="email of the user who owns the created app")
    cli.add_argument("--repoint", metavar="SLUG", help="Sentry App slug to repoint rules to")
    cli.add_argument("--apply", action="store_true", help="write the repointing")
    cli.add_argument(
        "--via-webhook",
        action="store_true",
        help="repoint although the slug is not in GITEA_ALERTS_WEBHOOK_TARGETS",
    )
    return cli


def run(args: argparse.Namespace) -> int:
    """The command itself. Sentry must be configured already."""
    from sentry.models.organization import Organization
    from sentry.users.models.user import User

    from .app import ensure_app, find_app

    if args.create_app and not args.user:
        print("--create-app needs --user <email>", file=sys.stderr)
        return 2
    if args.apply and not args.repoint:
        print("--apply needs --repoint <app slug>", file=sys.stderr)
        return 2
    organization = Organization.objects.filter(slug=args.org).first()
    if organization is None:
        print(f"no organization with the slug '{args.org}'", file=sys.stderr)
        return 1
    app = find_app(organization.id)
    if args.create_app and app is None:
        user = User.objects.filter(email__iexact=args.user, is_active=True).first()
        if user is None:
            print(f"no active user with the email '{args.user}'", file=sys.stderr)
            return 1
        try:
            app, _ = ensure_app(organization.id, user)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 1
        print(f"created the internal app '{app.name}' with the slug {app.slug}")
        print(
            f'next: set GITEA_ALERTS_WEBHOOK_TARGETS = ("{app.slug}",) in sentry.conf.py, '
            "restart Sentry, then repoint"
        )
    if app is None:
        print("internal app: none (create it with --create-app --user <email>)")
    else:
        print(f"internal app: {app.slug} (webhook {app.webhook_url})")
    found = find(organization.id)
    print(
        f"targets of '{PLUGIN_SLUG}' in {organization.slug}: "
        f"{len(found.actions)} workflow action(s), {len(found.rules)} rule(s)"
    )
    for rule in found.rules:
        print(f"  rule {rule.id} in project {rule.project_id}: {rule.label}")
    if args.repoint:
        if app is None or args.repoint != app.slug:
            # Any other slug would be delivered nowhere: the editor would show a rule that
            # looks fine and files nothing.
            print(
                f"'{args.repoint}' is not this organization's Gitea Alerts app; nothing changed",
                file=sys.stderr,
            )
            return 1
        from .bridge import switched_on, targets

        bridged = switched_on() and args.repoint in targets()
        if bridged:
            print("delivery after repointing: the alert bridge (the slug is in its targets)")
        else:
            print(
                "delivery after repointing: the signed webhook to "
                f"{app.webhook_url}, which Sentry's workers must be able to reach. "
                f"'{args.repoint}' is not in GITEA_ALERTS_WEBHOOK_TARGETS of this Sentry, "
                "or the bridge is off."
            )
            if args.apply and not args.via_webhook:
                print(
                    "nothing changed: add the slug to GITEA_ALERTS_WEBHOOK_TARGETS and restart "
                    "Sentry first, or pass --via-webhook",
                    file=sys.stderr,
                )
                return 1
        changed = repoint(organization.id, args.repoint, apply=args.apply)
        verb = "repointed" if args.apply else "would repoint"
        print(
            f"{verb} {len(changed.actions)} action(s) and {len(changed.rules)} rule(s) "
            f"to '{args.repoint}'"
        )
        if not args.apply:
            print("nothing was written; add --apply")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    from sentry.runner import configure

    configure()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
