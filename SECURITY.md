# Security policy

## Reporting a vulnerability

**Please do not report security vulnerabilities in public issues, pull requests or discussions.**

Report them privately through GitHub's private vulnerability reporting. Go to the repository's
**Security** tab, choose **Advisories**, then **Report a vulnerability**, or use
<https://github.com/ADCDS/sentry-gitea/security/advisories/new>.

Please include:

- the affected version or commit, and your Sentry and Gitea versions;
- whether the issue affects the project plugins, the organization integration, or both;
- steps to reproduce, and the impact you expect.

Never include real tokens, OAuth secrets or production data. Redact them, or use throwaway credentials.

This is a volunteer-maintained project. We aim to acknowledge reports within a week and to coordinate
a fix and an advisory with you before any public disclosure.

## Supported versions

The project is in alpha (0.1.x). Security fixes are made on `main` and go into the next release.
Older pre-releases are not patched.

## Scope

In scope: the code in this repository. That includes handling of Gitea tokens and OAuth credentials,
webhook signature verification, server-side requests to configured Gitea URLs, and rendering of
Sentry data into Gitea issues and comments.

Out of scope: vulnerabilities in Sentry or Gitea themselves. Report those upstream to
[Sentry](https://github.com/getsentry/sentry/security/policy) or
[Gitea](https://github.com/go-gitea/gitea/security/policy). The deployment properties below are
known and documented; reports that they can be misconfigured are welcome as documentation fixes.

## Deployment hardening

- **Secrets at rest.** Plugin personal access tokens are stored as plaintext Sentry project
  options. The integration's OAuth tokens (`Identity.data`) and webhook key (integration metadata)
  are Sentry encrypted JSON fields, which store plaintext unless Sentry's
  `database.encryption.method` option is `fernet` and Fernet keys are configured
  (`DATABASE_ENCRYPTION_SETTINGS`). The OAuth client secret lives in `sentry.conf.py`, which
  self-hosted copies into the locally built image. Protect the database, its backups, the image
  and administrative access. Give the plugins a dedicated Gitea user with only the repository
  rights it needs.
- **Webhook secrets.** Every repository hook has its own secret, an HMAC of the Gitea repository
  ID keyed with the installation's webhook key, and the endpoint rejects payloads for any other
  repository. Gitea shows a hook's secret to the repository's admins in its web UI, so treat
  repository admins as able to forge deliveries for their own repository.
- **Server-side requests from the plugins.** Project admins choose a plugin's Gitea base URL.
  Only origins listed in `GITEA_PLUGIN_ALLOWED_BASE_URLS` (when unset, the origins of
  `GITEA_BASE_URL` and `GITEA_PUBLIC_URL`) may reach private or loopback addresses. Every other
  base URL goes through Sentry's outbound HTTP guard (`SENTRY_DISALLOWED_IPS`, no redirects).
  Keep the allowlist to your Gitea origins. A saved token is never sent to a changed base URL,
  and configuration errors do not echo Gitea's response bodies.
- **Gitea's webhook egress.** Do not set Gitea's `[security] ALLOWED_HOST_LIST` to `private`:
  every Gitea user's webhooks could then reach your private services. Send webhooks to Sentry's
  public URL, or put Gitea and Sentry's web entry point on a dedicated network and allow exactly
  its subnet. See
  [Reaching Sentry from Gitea](docs/INTEGRATION.md#reaching-sentry-from-gitea).
- **Event text.** Values taken from events are rendered as inline code in Gitea, so they cannot
  @mention users or create links. Hidden markers in issues and comments are trusted only when
  written by the plugin token's user or the integration's Gitea user.
- **Logs.** The OAuth callback URL carries a single-use, PKCE-bound `code` and `state`. Reverse
  proxies (including self-hosted's nginx) log it, and Sentry's superuser audit log records it for
  superusers. Install the integration as an organization owner and treat proxy and web logs as
  sensitive. The package itself never logs tokens, secrets, authorization codes or request and
  response bodies.
