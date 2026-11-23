"""Alert filing that does not depend on Sentry's legacy plugin classes.

Sentry 26.7 removed `sentry.plugins.bases`, and with it the two legacy plugins this package
shipped. What they stored stays where it is: the project options under `gitea:` and
`gitea_alerts:`, and each issue's link in GroupMeta under `gitea:issue_*`. The modules here
read and write exactly those keys, so nothing has to be migrated and the plugins (on a Sentry
that still has them) and the bridge agree on every link.

- `core`      settings, the Gitea client, the per-issue link and its lock
- `delivery`  what one alert does: file, comment, or file again after a close
- `bridge`    takes the alert-rule actions that used to reach the `gitea_alerts` plugin
- `pages`     project settings pages, replacing the plugins' settings pages
- `webhook`   the same delivery behind a signed Sentry App webhook, as a fallback
- `tasks`     the taskworker task the webhook queues
- `app`       the internal Sentry App that makes Gitea selectable in the rule editor again
- `migrate`   reports, and can repoint, rules that still name the plugin
"""
