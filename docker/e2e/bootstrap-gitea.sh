#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "${PYTHON:-$repo_root/.venv/bin/python}" "$repo_root/docker/e2e/harness.py" bootstrap-gitea
