# Project plugins

The plugins mode works per project, with a Gitea personal access token. It files a
Gitea issue when an issue alert fires, comments on that issue while it is open, and
files a new one when a closed issue's problem comes back. On Sentry 26.6 and older it
also creates, links and unlinks issues from Sentry's legacy issue sidebar. It is tested
with self-hosted Sentry **26.3.1, 26.5.0, 26.7.0 and 26.11.0** and Gitea **28.1.0**.

Sentry 26.7 removed legacy plugins. The mode keeps its name, its stored settings and
its issue links there; what a project sees depends on the Sentry version:

| | Sentry 26.6 and older | Sentry 26.7 and newer |
| --- | --- | --- |
| Code that runs | The `gitea` and `gitea_alerts` legacy plugins. With the `SENTRY_DEFAULT_INTEGRATIONS` line present, the alert bridge delivers the rules that name Gitea Alerts instead of the plugin | The package's own alert code. No plugin class is loaded |
| Project settings | Legacy Integrations pages, or the [settings pages](#settings-pages) | The [settings pages](#settings-pages) |
| Alerts from existing rules | Delivered | Delivered, by the [alert bridge](#how-alerts-reach-gitea) |
| Picking Gitea in the rule editor | "Gitea Alerts" legacy service | An internal Sentry App: [Rules on Sentry 26.7 and newer](#rules-on-sentry-267-and-newer) |
| Sidebar Create / Link / Unlink | Yes | No. Sentry no longer shows plugin actions; the [organization integration](INTEGRATION.md) creates and links issues |
| `sentry.conf.py` | Nothing required | Three lines required, see [Install](#install) |

Settings stay in the project options `gitea:*` and `gitea_alerts:*`, and each issue's
link in GroupMeta under `gitea:issue_*`, on every version. Upgrading Sentry or this
package migrates nothing: the link written before an upgrade is the one alerts keep
commenting on after it.

## Install

Build a wheel from this checkout with `uv build --wheel`. Copy the resulting wheel
into the self-hosted checkout's `sentry/` directory. Add an installation command to
that checkout's executable `sentry/enhance-image.sh`, preserving any existing image
customizations. For version 0.3.1 the command is:

```sh
python -m pip install --no-deps --no-index --force-reinstall \
    /usr/src/sentry/sentry_gitea-0.3.1-py3-none-any.whl
```

Use the exact filename produced by the build when installing a different version.
Sentry itself comes from its own image (`ghcr.io/getsentry/sentry:<version>`); do not
install it with pip.
The image already provides `requests` and `urllib3`. Rebuild the enhanced image using
the self-hosted checkout's installation procedure (`./install.sh`) and recreate every
Sentry service that runs application code, including web, taskworkers and the
scheduler. Installing only in the web container leaves alert delivery unavailable in
workers.

The plugins need no OAuth app and no Gitea webhook.

**Sentry 26.6 and older.** Distribution entry points register both legacy plugins
automatically and no `sentry.conf.py` line is required. `ROOT_URLCONF =
"sentry_gitea.urls"` is optional there: it adds the [settings pages](#settings-pages)
and the ping route and preserves Sentry's routes.

**Sentry 26.7 and newer.** There are no plugin entry points to load. Add these
three lines to `sentry/sentry.conf.py` (the integration's block contains their
equivalents, so skip them if you already added that):

```python
SENTRY_DEFAULT_INTEGRATIONS += ("sentry_gitea.integration.provider.GiteaIntegrationProvider",)
ROOT_URLCONF = "sentry_gitea.urls"
TASKWORKER_IMPORTS = (*TASKWORKER_IMPORTS, "sentry_gitea.alerts.tasks")
```

The first is the only start-up hook such a Sentry gives a package. It installs the
[alert bridge](#how-alerts-reach-gitea) in every Sentry process; without it, alert
rules fire and nothing reaches Gitea. As a side effect Gitea is listed in the
Integrations directory. Without the OAuth settings, starting an installation there
shows a configuration error and changes nothing. The second line serves the settings
pages, the only place to edit project settings on such a Sentry. The third makes the
taskworkers load the task that delivers an alert.

Add the lines **before** upgrading Sentry to 26.7, while the legacy plugins still
work, so that no alert is lost in between: see
[Upgrading Sentry itself](../README.md#upgrading-sentry-itself).

Optional settings: [`GITEA_PLUGIN_ALLOWED_BASE_URLS`](#allowed-base-urls),
`GITEA_ALERTS_BRIDGE`, `GITEA_ALERTS_WEBHOOK_TARGETS` and `GITEA_ALERTS_WEBHOOK_URL`
(the last three are described under [How alerts reach Gitea](#how-alerts-reach-gitea)).
To upgrade or remove the plugins, follow [Upgrade](../README.md#upgrade) and
[Uninstall](../README.md#uninstall) in the README.

## Enable and configure Gitea

Open the project's [settings page](#settings-pages),
`<sentry>/extensions/gitea/settings/<organization>/<project>/`, and tick **Issues**. On
Sentry 26.6 and older the legacy page does the same: in the project's settings, open
**Legacy Integrations / Plugins**, select **Gitea**, and enable it (its URL ends in
`/settings/<organization>/projects/<project>/plugins/gitea/`). Both pages edit the same
stored settings.

Create a Gitea personal access token with **read:user**, **read:repository** and
**write:issue** scopes. Its user also needs access to the repository and permission
to write issues and labels there; token scopes alone do not grant repository rights.
Gitea must use its built-in issue tracker for that repository.

| Field | Value |
| --- | --- |
| Base URL | Gitea URL reachable from Sentry containers, e.g. `http://gitea:3000` or `https://forge.example/git`. A hosting subpath is retained; trailing slashes are removed. Userinfo, query strings and fragments are rejected. A private or loopback address must be allowed (see below). |
| Gitea URL for links (`public_url`) | Optional browser-facing Gitea URL, e.g. `https://forge.example/git`. Leave empty when `base_url` is already reachable by users. Normalized and validated exactly like the base URL. |
| Personal access token | The PAT. It is never returned in configuration responses, including a prefix. Leave it blank after saving to keep it; enter it again whenever you change the base URL. |
| Repository | `owner/name`; saving verifies the token and fetches the repository. |
| Default labels | Optional comma-separated names applied to every newly created issue, including alerts. |

HTTP is supported for self-hosted internal networks; use HTTPS when traffic crosses
an untrusted network. Rotate the PAT by saving a new value. Do not paste tokens into
URLs or issue fields. If saving fails, the error names the HTTP status or a short
category (for example, unreachable or unauthorized). Gitea's response body is never
shown.

**The PAT is stored in plaintext** as a Sentry project option. Sentry's optional
database field encryption does not cover project options. Anyone with access to the
Sentry database, its backups or Sentry's administrative shell can read it, so use a
dedicated Gitea user whose rights are limited to the repositories it files into.

### Settings pages

`<sentry>/extensions/gitea/settings/<organization>/` lists the projects you may
configure, with each one's repository and whether issues and alerts are on.
`<sentry>/extensions/gitea/settings/<organization>/<project>/` edits one project. The
pages need `ROOT_URLCONF = "sentry_gitea.urls"` and work on every supported Sentry.
They are plain server-rendered forms, with no script and nothing from Sentry's
frontend, so they look unlike the rest of Sentry.

- **Who.** A signed-in user with `project:write` or `project:admin` on the project,
  from an organization role or a team role. That is the rule of Sentry's project
  settings API, which the legacy pages went through. The list shows only projects
  where you have it.
- **One form** holds what the two legacy pages held: the fields above under **Issues**,
  and under **Alerts** the options listed in
  [Enable Gitea Alerts](#enable-gitea-alerts-and-create-an-alert-rule). Alerts cannot
  be ticked without Issues, because they file with the same credentials.
- **Save** validates as the legacy page did, including the live check of the token
  and the repository when Issues is ticked, and writes an audit-log entry. A refusal
  from Gitea is shown and nothing is saved. Unticking Issues stops alerts and keeps
  the connection fields for later.
- **The token is write-only.** The page says whether one is saved and never sends it
  back. A blank token field keeps the saved token, but only while the base URL stays
  the same.
- **Test saved settings** only reads the token's Gitea user and the repository. It
  never files anything.
- **Forget issue links** removes the stored Gitea link of every Sentry issue in the
  project. It is the way out after a move to another base URL or repository, when
  alerts refuse to write through links made for the old one (see
  [Sidebar actions](#sidebar-actions)); Sentry 26.7 and newer have no per-issue
  relink. The next alert for an issue then finds the Gitea issue that carries its
  marker in the repository configured now and links it again, or files a new one.
  Settings stay, and nothing changes in Gitea.
- **Delete saved settings** removes all of the project's `gitea:*` and
  `gitea_alerts:*` options, the token included, which is what the legacy pages'
  **Reset Configuration** did. Issue links stay, and nothing changes in Gitea.

### Allowed base URLs

A project admin chooses the base URL, and Sentry's web and worker containers call it
from inside your network. To keep that from reaching internal services, the plugins
send requests in one of two ways:

- If the base URL's origin (`scheme://host[:port]`) is listed in
  `GITEA_PLUGIN_ALLOWED_BASE_URLS`, requests go out directly and may reach private
  or loopback addresses.
- Every other base URL goes through Sentry's outbound HTTP guard. It refuses
  addresses in `SENTRY_DISALLOWED_IPS` (by default private, loopback, link-local and
  other reserved ranges) and does not follow redirects.

By default the allowlist contains the origins of `GITEA_BASE_URL` and
`GITEA_PUBLIC_URL` when the organization integration is configured, and is empty
otherwise. Setting `GITEA_PLUGIN_ALLOWED_BASE_URLS` replaces that default, so list
every origin you need. Invalid entries are ignored and logged as a WARNING
(`sentry-gitea: ignoring an invalid GITEA_PLUGIN_ALLOWED_BASE_URLS entry`). If your
Gitea has a private address, list its origin in `sentry/sentry.conf.py` and rebuild:

```python
GITEA_PLUGIN_ALLOWED_BASE_URLS = ("http://gitea:3000", "https://forge.internal.example")
```

A base URL that is not allowed fails with "Sentry refused to connect: the Gitea URL
resolves to a private or reserved address", both when saving the configuration and
when an alert or sidebar action runs.

When the base URL changes, the saved token is not reused: enter it again in the same
save. This keeps a token from being sent to a Gitea URL it was not created for.

### Links

New issue links use `<public_url or base_url>/<owner>/<repo>/issues/<number>` with
the repository path quoted for URLs. This applies to manual create/link, alert filing
and recovery. API calls still use `base_url`; Gitea's returned `html_url` is ignored.
Gitea 1.26 and later default to `[server] PUBLIC_URL_DETECTION = auto`, so API responses
can contain an internal hostname when Sentry uses one. Setting it to `never` makes
Gitea's own URLs use `ROOT_URL`, but links generated by these plugins do not depend
on that setting. Issue bodies and backlink comments continue to point to Sentry.

### Sidebar actions

**Sentry 26.6 and older only.** Sentry 26.7 removed the legacy sidebar together with
the plugins. Links made before the upgrade stay stored, and alerts keep using them,
but that Sentry shows them nowhere; to create or link issues by hand there, use the
[organization integration](INTEGRATION.md), whose links are separate.

From a Sentry issue's legacy sidebar, **Create** opens title, description, assignee
and label fields. The default title includes the qualified Sentry short ID; the body
includes the issue URL, level, release, environment, location and last event ID.
Values taken from the event (title, culprit, release, environment and message) are
rendered as inline code, so they cannot @mention Gitea users or create automatic
links. Assignee choices are repository usernames. Project default labels are always
added to submitted labels, then resolved to numeric Gitea label IDs. Missing
repository labels are created on first use. Organization labels are not searched.

**Link** accepts a positive issue number in the configured repository and an optional
backlink comment. Pull requests cannot be linked as issues. **Unlink** removes only
the Sentry link; it does not delete or close the Gitea issue. The pinned Sentry sidebar
uses its inherited GET unlink route; API clients can also use POST.

Links display as `owner/name#number`. All six string values are stored in GroupMeta:
`gitea:issue_id`, `gitea:issue_title`, `gitea:issue_url`, `gitea:issue_repo`,
`gitea:issue_repo_id`, and `gitea:issue_base_url`. The number is local to the repository.
Changing configuration preserves existing link display; relink an issue to refresh
its stored URL after changing `public_url`. Changing only `public_url` does not change
the repository binding or interrupt repeat alerts. Automatic writes stop if the
stored base URL, repository path or numeric repository identity no longer matches.
Explicitly relink to the intended repository before resuming alerts, or use
**Forget issue links** on the [settings page](#settings-pages) for the whole project,
which is the only way on Sentry 26.7 and newer.

## Enable Gitea Alerts and create an alert rule

Tick **Alerts** on the project's [settings page](#settings-pages), or on Sentry 26.6
and older enable the **Gitea Alerts** (`gitea_alerts`) legacy plugin on the same
project. Keep **Issues** (the **Gitea** plugin) enabled and configured; alerts have no
credentials of their own. Enabling or disabling either takes effect in running
taskworkers without a restart.

| Field | Default | Effect |
| --- | --- | --- |
| Alert labels | `source:sentry` | Additional comma-separated names for new alert issues. An empty string disables these extra labels. |
| Severity and class labels | On | Adds `severity:critical/high/medium/low` and `class:crash/other`. |
| Comment on repeat events | On | Adds the firing event's details to a linked open issue. |
| Advisory where-to-look hint | Off | Adds a bounded creation-time comparison when a commit-ish release and source location are available. |
| New issue when a closed issue regresses (`refile_on_regression`) | On | When the linked Gitea issue is closed and Sentry recorded a regression for the group **after** that close, files a new issue that references the closed one and moves the link to it. |
| New issue for any event an hour after closing (`refile_after_close`) | Off | Also files a new issue when the Sentry issue was never resolved: any alert more than an hour after the linked Gitea issue was closed. Use it when people close Gitea issues without resolving Sentry. |

Create a Sentry **issue alert** for the desired error condition and project. On
Sentry 26.7 and newer, first read
[Rules on Sentry 26.7 and newer](#rules-on-sentry-267-and-newer): the rule editor there
offers Gitea only through an internal Sentry App. On Sentry 26.6 and older, select
the action for sending a notification through a specific legacy service and choose
**Gitea Alerts**, whose service identifier is **`gitea_alerts`**. Its legacy rule
representation is:

```json
{
  "id": "sentry.rules.actions.notify_event_service.NotifyEventServiceAction",
  "service": "gitea_alerts"
}
```

Sentry's newer action picker can categorize this legacy plugin as **WEBHOOK**; that
does not mean a Gitea webhook URL must be configured. The alternative is **Send a
notification (for all legacy integrations)**, represented by
`sentry.rules.actions.notify_event.NotifyEventAction`. That alternative also invokes
other enabled legacy notification plugins. Choose one action path for this purpose;
adding both can deliver duplicate comments. Rules of either kind keep reaching Gitea
after an upgrade to Sentry 26.7 or newer (see
[How alerts reach Gitea](#how-alerts-reach-gitea)).

The first firing event creates an issue and saves the same link the sidebar reads.
Subsequent events fetch the linked issue: open issues receive comments when enabled.
A closed issue is never reopened or commented on. Instead, a **new** issue is filed
when the problem has come back since the close (see "Closed issues" below); otherwise
the event is skipped. Comments include event ID, level,
environment, release, the Sentry event URL and a hidden event marker. Multiple rules
may comment independently. Sentry's rule repeat interval and the limit of
**10 Gitea notifications per project per 60 seconds** (inherited from Sentry's plugin
base on older versions, applied by this package on every path) are separate controls.

**Choosing the trigger (Sentry 26.x).** Sentry 26 runs issue alerts through its
workflow engine. When a rule is converted, the trigger **"The event occurs"**
(`EveryEventCondition`) is silently dropped if it is combined with any other trigger,
so "A new issue is created" + "The event occurs" behaves like "A new issue is created"
alone and **never fires for repeat events**. Pick one:

| Goal | Trigger(s) |
|---|---|
| File an issue once per Sentry issue, no repeat comments | "A new issue is created" |
| File on first event **and** comment on later events | "The event occurs" **as the only trigger** (add filters to narrow it) |

Remember the rule's action interval (minimum 5 minutes) still limits how often one
rule acts on the same Sentry issue.

**Low-traffic instances.** Sentry's post-process step waits on Snuba's commit log.
On a nearly idle self-hosted instance, the alert for the most recent event can run
only when the next event arrives. Busy instances are not affected.

The plugin's **Test Configuration** button, like **Test saved settings** on the
settings page, only reads the current Gitea user and repository. It never files a
ticket. Verify actual delivery with a real test error and the chosen rule after
checking the configuration.

## How alerts reach Gitea

Sentry's workflow engine stores the two rule actions above as two action types, and
this package wraps the handler of each when Sentry starts (the **alert bridge**):

| Rule action | Stored as | Sentry 26.6 and older | Sentry 26.7 and newer, without the bridge | With the bridge |
| --- | --- | --- | --- | --- |
| Send a notification via **Gitea Alerts** | webhook action, target `gitea_alerts` | Handed to the legacy plugin | Read as a Sentry App slug that does not exist: the rule fires and nothing reaches Gitea. The only trace is a WARNING `webhook_action_handler.sentry_app_not_found` in Sentry's log | Delivered by the bridge, on every version |
| Send a notification (for **all legacy integrations**) | plugin action | Every enabled legacy notification plugin, Gitea Alerts included | Only the legacy webhooks | Sentry's handler runs unchanged; where no legacy plugin exists, the bridge then also files in Gitea for projects with Alerts enabled |

Actions for any other target reach Sentry's own handler untouched. The bridge does
not call Gitea itself: Sentry runs each rule action in a task with a 30-second
deadline and retries it when the deadline passes, and a delivery can take longer
than that. The bridge queues the task `sentry_gitea.alerts.tasks.deliver_alert` and
returns. The task runs the same code, lock and limits the legacy plugin used, has its
own deadline, and is never retried: a failed delivery is logged as described under
[Recovery, diagnostics and limits](#recovery-diagnostics-and-limits), and the next
alert for the issue delivers again. Nothing the bridge or the task does can fail the
rule or repeat its other actions.

The bridge is installed by `GiteaIntegrationProvider.setup()`, so it needs the
`SENTRY_DEFAULT_INTEGRATIONS` line from [Install](#install), also on Sentry 26.6 and
older; without that line the legacy plugin delivers there, as in 0.2.0. To check it:

- `GET <sentry>/extensions/gitea/ping/` reports `"alert_bridge": true` for the web
  process;
- every Sentry process logs `sentry-gitea: alert bridge installed` once at start; look
  for it in a taskworker's log, since that is where rules are evaluated.

A bridge that cannot be installed logs an ERROR, `sentry-gitea: alert bridge not
installed`, and never stops Sentry from starting.

`GITEA_ALERTS_BRIDGE = False` in `sentry.conf.py` leaves Sentry's handlers alone. On
Sentry 26.6 and older the legacy plugin then delivers as before. On newer versions
only the [signed webhook](#fallback-the-signed-webhook) remains, and rules that use
"all legacy integrations" no longer reach Gitea.

### Rules on Sentry 26.7 and newer

Existing rules keep working through the bridge and need no change. What such a
Sentry no longer has is a "Gitea Alerts" entry in the rule editor: it lists alertable
Sentry Apps, not legacy plugins. An internal Sentry App named **Gitea Alerts** puts
the entry back. The package's tool reports the rules that still name the plugin,
creates the app and repoints the rules. Run it in a Sentry container, for example with
`docker compose run --rm web python -m sentry_gitea.alerts.migrate ...`:

```sh
# 1. Report only: the app, if any, and every rule that names the legacy plugin.
python -m sentry_gitea.alerts.migrate --org <organization slug>
# 2. Create the internal app, owned by that user. Prints its slug.
python -m sentry_gitea.alerts.migrate --org <organization slug> --create-app --user <email>
# 3. Add the slug to sentry.conf.py and recreate the Sentry services (below).
# 4. Show what repointing would change, then do it.
python -m sentry_gitea.alerts.migrate --org <organization slug> --repoint <app slug>
python -m sentry_gitea.alerts.migrate --org <organization slug> --repoint <app slug> --apply
```

Step 3, so that the bridge delivers the repointed rules itself:

```python
GITEA_ALERTS_WEBHOOK_TARGETS = ("<app slug>",)
```

Nothing is written without `--create-app` or `--apply`. Creating the app needs an
absolute URL for its webhook: Sentry's `system.url-prefix`, or
`GITEA_ALERTS_WEBHOOK_URL` ending in `/extensions/gitea/alerts/`; without one the tool
refuses rather than create an app that could never deliver. Repointing is accepted only
to that organization's Gitea Alerts app. It changes the target of each matching
workflow action and of the matching legacy rule row, in one transaction, and touches
no other rule.

The order matters. A repointed rule whose slug the bridge does not answer for is
posted to the app's webhook instead, which many self-hosted installations cannot
reach (see the fallback below). `--apply` therefore refuses until the slug is in
`GITEA_ALERTS_WEBHOOK_TARGETS` of the Sentry it runs in, and both forms of
`--repoint` say which path the rules will take. `--via-webhook` overrides the
refusal when the webhook is what you want.

New rules pick **Gitea Alerts** in the editor like any other Sentry App. The app has
no scopes and no token; it exists only to be selectable and, for the fallback below,
to sign.

### Fallback: the signed webhook

The bridge replaces handlers inside Sentry, which a future Sentry could rearrange.
The internal app gives a second path that uses only Sentry's public Sentry App
contract: when a rule targets the app and the bridge does not claim it (the slug is
not in `GITEA_ALERTS_WEBHOOK_TARGETS`, or `GITEA_ALERTS_BRIDGE` is `False`), Sentry
signs the alert with the app's client secret and posts it to the app's webhook URL,
`<sentry>/extensions/gitea/alerts/`. That endpoint accepts only an internal app whose
webhook URL is this endpoint, verifies the signature, checks that the project belongs
to the app's organization, and queues the task
`sentry_gitea.alerts.tasks.deliver_alert`, which files or comments exactly like the
bridge.

Requirements of this path:

- `ROOT_URLCONF = "sentry_gitea.urls"` and the `TASKWORKER_IMPORTS` line from the
  integration's settings block (the task lives in the module that line imports).
- **Sentry's workers must be able to post to Sentry's own URL through Sentry's
  outbound-request guard**, which refuses private, loopback, link-local and
  carrier-grade-NAT addresses (`SENTRY_DISALLOWED_IPS`). A Sentry that is reachable
  only on such an address cannot use the webhook path unless you narrow that setting;
  use the bridge there. `GITEA_ALERTS_WEBHOOK_URL` sets the app's URL when it is
  created, if the default (`system.url-prefix` plus the path) is not the one workers
  should use.
- Rules that use "all legacy integrations" have no webhook equivalent.

The endpoint answers `202` for an accepted alert, `401` for a missing, unknown or
wrong signature, `404` for a project outside the app's organization, `400` for a
malformed body and `413` for one over 4 MiB, and logs
`sentry-gitea: alert webhook refused` with a reason for every refusal. It never
returns data and never calls Gitea itself. Sentry signs the body only, without a
timestamp, so a request captured between Sentry's workers and its web service can be
replayed; a replay delivers the same alert again, which adds one comment to the linked
issue within the per-project limit.

## Classification and advisory hints

| Event level | Proven unhandled | Proven handled | Unknown handled state |
| --- | --- | --- | --- |
| fatal | critical | medium | medium |
| error | high | low | medium |
| warning | medium | low | medium |
| info, debug, other | low | low | low |

Only an explicit unhandled exception produces `class:crash`. Any false handled
verdict wins; all explicit true verdicts mean handled; a mixture of true and missing
verdicts means unknown. Plain log events with no exceptions count as handled.
Unknown classification is explained in the body. The event release takes precedence
over the group's first release; otherwise the body says `(unknown)`.

Default label colors are `source:*` #5319e7, critical/crash #b60205, high #d93f0b,
medium #fbca04, low #0e8a16, other #c5def5 and custom labels #ededed. Existing labels
keep their colors.

Hints accept a 7–40 character lowercase hexadecimal release SHA, optionally after a
package `@` prefix. They infer up to three file paths, inspect up to 250 commits in a
direct `release..default-branch` comparison, and display at most five matching commits.
Missing file evidence, unsupported releases, failures and exhausted hint budgets
omit the hint. Hints reserve time for the issue creation request. They never alter
classification, labels, closure or assignment and do not implement native blame or
run a fix agent.

## Recovery, diagnostics and limits

The rules in this section hold on every path: the legacy plugins, the bridge and the
webhook task run the same delivery code.

Manual mutations and alert delivery share a 60-second per-group Sentry lock. Every
operation rereads the link after acquiring it. If the lock is busy, a manual action
returns 409 ("Another Gitea operation is in progress. Retry shortly."). An alert
retries for up to 10 seconds and is then **skipped**, logging the WARNING
`sentry-gitea: alert skipped; group lock busy`; it is not retried later, but the next
matching event is delivered normally. Link fields are saved in a database transaction.

New issue bodies contain a hidden `sentry-gitea-meta v1` JSON block with a stable
dedup ID derived from the Sentry URL prefix and numeric project/group IDs. Preserve
that block when editing in Gitea. Edited Sentry form descriptions retain their prose
and receive the authoritative block.

If remote creation succeeds but its response or local save fails, the next attempt
searches open and closed issues for the exact metadata ID before creating again,
preferring an open match (a re-filed group has several marked issues).
Only issues **authored by the token's Gitea user** are accepted, so a marker copied
into someone else's issue is ignored and a new issue is filed. A recovered issue is
linked without an additional comment. This is recovery, not an exactly-once
guarantee: Gitea search indexing delays, removed metadata, changed Sentry URL
prefixes, a different token user, expired locks or ambiguous network outcomes can
still require manual reconciliation. Unlinking an automatically filed issue does not
erase its remote marker; a later alert can recover that same issue, including a
closed one.

The client uses connect/read timeouts of **5/15 seconds**, a **2 MiB** cap per
response, and at most **20 pages of 50 requested rows**. Each operation is also capped
at **32 MiB** and **5,000 rows** in total across its responses. Server-clamped page sizes are
supported; exhausting a budget fails visibly rather than treating partial results as
complete. Repository assignees are unpaginated. The client requests identity encoding
and rejects unexpected compressed responses. A shared 43-second operation budget
checks available body reads and leaves room within the lock lease for a final socket
read and persistence. DNS resolution and HTTP header parsing still depend on the host
networking stack; these are not a process-level hard deadline. No HTTP write is
retried automatically.

At most 20 distinct labels, each up to 100 characters, can be requested. Concurrent
first use from different Sentry groups can create duplicate same-named labels because
Gitea does not enforce uniqueness; resolution chooses the lowest existing ID. Precreate
shared labels to avoid that race. If Gitea returns 201 but discards labels or assignees
for insufficient repository permissions, delivery reports an error. Recovery can link
that created issue, but does not repair its missing labels/assignees; correct permissions
and adjust the remote issue manually.

**Closed issues.** The plugins have no two-way status sync, so closing a Gitea issue does
not resolve the Sentry issue, and before 0.2.0 a closed link silenced every later alert
for that group. Now, when the linked issue is closed, the next alert files a new issue if:

- `refile_on_regression` (default on): Sentry marked the group as regressed after the
  Gitea issue's `closed_at`. That is the signal Sentry itself uses after a resolve,
  including release- and commit-aware resolution, so a fix still waiting for its deploy
  does not trigger it; or
- `refile_after_close` (default off): the alert runs more than an hour after `closed_at`,
  by Sentry's clock. This covers the common case of a Gitea issue closed while the Sentry
  issue stays unresolved, which can never regress.

The new issue has the same title and labels, a `**Recurrence of:** #N (closed YYYY-MM-DD)`
line (Gitea shows it as a cross-reference on the old issue), and `recurrence_of` in its
metadata block. The link moves to it, so later events comment there: at most one new
issue per close. Nothing is filed when `closed_at` is missing, or when the organization
**integration** links the same Gitea issue to the group, since its status sync owns that
issue. Recovery after a lost link only adopts an **open** issue carrying the marker, never
the closed one being replaced. To silence a group for good, archive it in Sentry.

Diagnostics use `sentry_gitea.plugins.issue` and `sentry_gitea.plugins.alerts`. Failed
alert calls log **ERROR** with `sentry-gitea: alert delivery failed` and `project_id`,
`group_id`, `status`. A status of zero denotes a local/transport/budget failure.
Successful delivery logs **INFO** with `sentry-gitea: filed issue` or
`sentry-gitea: commented on issue`; a re-filed issue's `filed issue` line also carries
`recurrence_of` and `refile_reason` (`regressed` or `after_close`). A repeat event that is
not commented on (closed issue, or repeat comments turned off) logs
`sentry-gitea: skipped repeat event` with a `reason`; for a closed issue,
`refile_skipped` says why no new issue was filed (`disabled`, `no closed_at`,
`integration linked` or `no recurrence signal`). An optional hint failure can log an error
followed by successful filing. No token, headers, raw request exception or
credential-bearing URL is logged. Neither Sentry's legacy callback nor the bridge
passes a failure on to the rule, so consult these logs; nothing promises a retry.
A project that a rule targets but that has no enabled, configured Gitea settings logs
**INFO** `sentry-gitea: alert skipped; Gitea is not enabled and configured for the
project`, and the per-project limit logs `sentry-gitea: alert skipped; rate limited`.
Both logger names are the same on every Sentry version. Event markers support
inspection of ambiguous comment outcomes, but the plugins do not deduplicate comments
across separate rule callbacks.

The plugins do not provide OAuth, organization-level installation, two-way
status/assignee sync, repository webhooks, commit ingestion, CODEOWNERS or source
links; the [organization integration](INTEGRATION.md) does.

## Tests

Host unit tests cover the pure client and rendering. The Sentry-level suite runs in
the Sentry image of each tested version and covers alert delivery (file, comment,
re-file, locking, recovery), the bridge for both rule actions, the settings pages,
the signed webhook, the internal app and the migration tool; on versions that have
legacy plugins it also covers create/link/unlink, the legacy settings form and rule
action routing. The end-to-end suite runs the plugins against a real self-hosted
Sentry 26.3.1 and Gitea. See [TESTING.md](TESTING.md).
