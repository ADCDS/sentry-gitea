SHELL := /bin/bash
.DEFAULT_GOAL := unit
.NOTPARALLEL:

# GNU make assignments, kept local and gitignored. Values are exported to scripts.
-include .e2e/local.env
export

PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
UV ?= uv
SENTRY_TEST_WORKERS ?= 2

.PHONY: unit lint harness-tests sentry-tests e2e-gitea-up e2e-bootstrap e2e-install e2e-uninstall e2e-test e2e-down

unit:
	$(PYTHON) -m pytest tests/unit -q

lint:
	$(RUFF) check .
	$(RUFF) format --check .

harness-tests:
	$(PYTHON) -m pytest tests/e2e/test_harness.py -q

sentry-tests:
	$(PYTHON) docker/e2e/harness.py sentry-tests

e2e-gitea-up:
	$(PYTHON) docker/e2e/harness.py gitea-up

e2e-bootstrap:
	bash docker/e2e/bootstrap-gitea.sh
	bash docker/e2e/bootstrap-sentry.sh

e2e-install:
	bash docker/e2e/install-into-self-hosted.sh

e2e-uninstall:
	bash docker/e2e/install-into-self-hosted.sh uninstall

e2e-test:
	$(PYTHON) -m pytest tests/e2e/test_smoke.py -m e2e -v -rs

e2e-down:
	$(PYTHON) docker/e2e/harness.py gitea-down
