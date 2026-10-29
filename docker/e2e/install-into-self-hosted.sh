#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
case "${1:-install}" in
    install|uninstall) ;;
    *) echo 'Usage: install-into-self-hosted.sh [install|uninstall]' >&2; exit 2 ;;
esac
exec "${PYTHON:-$repo_root/.venv/bin/python}" "$repo_root/docker/e2e/harness.py" "${1:-install}"
