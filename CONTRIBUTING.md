# Contributing to sentry-gitea

Thanks for helping! Bug reports, fixes and documentation improvements are welcome. Please open an
issue before starting larger features, so that we can agree on scope first.

## Set up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and GNU make. For the Sentry-level tests you
also need Docker with Compose ≥ 2.32.2.

```sh
uv sync
make unit lint                        # fast host checks; run before every push
uv run pytest tests/e2e -m "not e2e"  # host-only tests of the E2E harness
make sentry-tests                     # tests/sentry in the pinned ghcr.io/getsentry/sentry:26.3.1 image
```

CI runs lint, unit, harness and Sentry-level tests on every pull request. The end-to-end suite needs
a dedicated self-hosted Sentry and Gitea stack, so it runs locally only. If your change touches
runtime behavior, please run it (see [docs/TESTING.md](docs/TESTING.md)) and say so in the PR.

## Guidelines

- **Write tests first.** Every behavior change needs a test in the lowest layer that can prove it:
  `tests/unit` (pure code, HTTP mocked with `responses`), then `tests/sentry`, then `tests/e2e`.
- **Keep `sentry_gitea/api/` and `render.py` free of Sentry imports**, so that unit tests run anywhere.
- **Use a timeout on every HTTP call.** Never log, echo or commit tokens, OAuth codes or secrets.
  Error messages may include a status and a short category, never credentials or upstream response
  bodies.
- Code is formatted with ruff (line length 100). It targets Python 3.12+ and uses type hints. Ruff
  also checks Python code blocks in Markdown files.
- If behavior or configuration changes, update the matching doc in `docs/` and the README. Call
  the two modes "plugins" and "integration".

## Licensing (important)

This project is Apache-2.0. Sentry 26.x is licensed under FSL-1.1, which is **not** an open-source
license.

- **Do not copy or closely paraphrase code from Sentry releases that are still under FSL-1.1**,
  including 26.x. You may read them to learn interfaces and signatures.
- You may adapt code from **Sentry 24.10.0, or a release you verified is already Apache-2.0** (each
  FSL release converts to Apache-2.0 two years after it was published). When you do, add the
  upstream path to the "Adapted files" list in [NOTICE](NOTICE) and leave a
  `# Adapted from sentry <version>: <path>` comment in the file.
- **Never copy from the archived `getsentry/sentry-plugins` repository** (BSL-1.1).
- Gitea (MIT) is fine to use as a reference for API and webhook shapes.

By submitting a contribution, you agree that it is licensed under the Apache License 2.0, as described in
section 5 of the [LICENSE](LICENSE).

## Reporting security issues

Do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
