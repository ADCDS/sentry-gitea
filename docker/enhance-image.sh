#!/usr/bin/env bash
set -euo pipefail

# Example only. The installer writes a block with the exact built wheel filename.
python -m pip install --no-deps --no-index --force-reinstall \
    /usr/src/sentry/sentry_gitea-*.whl
