"""Small host checks for harness operations; these do not need Docker or Sentry."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import harness
import pytest
import responses


def test_marked_block_update_and_removal_preserve_user_content() -> None:
    original = "#!/bin/bash\n# My customization\necho ready\n"
    installed = harness.marked_block(original, "echo installed\n")
    assert harness.marked_block(installed, "echo installed\n") == installed
    updated = harness.marked_block(installed, "echo updated\n")
    assert "echo installed" not in updated
    assert updated.count(harness.BEGIN) == 1
    assert harness.marked_block(updated, None) == original


@pytest.mark.parametrize("text", ["# BEGIN sentry-gitea\n", "# END sentry-gitea\n"])
def test_incomplete_markers_are_rejected(text) -> None:
    with pytest.raises(ValueError, match="markers"):
        harness.marked_block(text, "new\n")


def test_env_round_trip_is_private_and_never_evaluates_shell(tmp_path: Path) -> None:
    values = {"VALUE": "spaces ' and $(touch unexpected)", "URL": "http://127.0.0.1:9000"}
    path = tmp_path / "settings.env"
    harness.write_env(path, values)
    assert harness.read_env(path) == values
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "unexpected").exists()


def test_env_rejects_multiline_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single line"):
        harness.write_env(tmp_path / "settings.env", {"VALUE": "one\ntwo"})


def test_compose_reads_both_self_hosted_env_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").touch()
    (tmp_path / ".env.custom").touch()
    monkeypatch.setenv("SELF_HOSTED_DIR", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONFIG", "/some/local/docker/config")
    command = harness.self_hosted_compose()
    assert command[:2] == ["docker", "compose"]
    assert command.index(str(tmp_path / ".env")) < command.index(str(tmp_path / ".env.custom"))
    assert os.environ["DOCKER_CONFIG"] == "/some/local/docker/config"


def test_select_only_active_sentry_images() -> None:
    services = {
        "web": {"image": "sentry-self-hosted-local"},
        "taskworker": {"image": "sentry-self-hosted-local"},
        "traces": {"image": "sentry-self-hosted-local", "profiles": ["feature-complete"]},
        "sentry-cleanup": {"image": "sentry-cleanup-self-hosted-local"},
        "postgres": {"image": "postgres:14.19-bookworm"},
    }
    assert harness.sentry_services(services, {"errors-only"}) == [
        "web",
        "taskworker",
        "sentry-cleanup",
    ]


@responses.activate
def test_gitea_repo_bootstrap_creates_initial_commit() -> None:
    api = harness.API("http://gitea.test", "unused-test-value", "token")
    responses.get("http://gitea.test/api/v1/orgs/e2e", status=404)
    responses.post("http://gitea.test/api/v1/orgs", json={"username": "e2e"}, status=201)
    responses.get("http://gitea.test/api/v1/repos/e2e/demo", status=404)
    responses.post(
        "http://gitea.test/api/v1/orgs/e2e/repos",
        json={"name": "demo", "empty": False},
        status=201,
        match=[
            responses.matchers.json_params_matcher(
                {"name": "demo", "auto_init": True, "default_branch": "main", "private": True}
            )
        ],
    )
    harness.ensure_gitea_repo(api, "e2e", "demo", "e2e-admin")


@responses.activate
def test_gitea_existing_repo_is_not_recreated() -> None:
    api = harness.API("http://gitea.test", "unused-test-value", "token")
    responses.get("http://gitea.test/api/v1/orgs/e2e", json={"username": "e2e"})
    responses.get("http://gitea.test/api/v1/repos/e2e/demo", json={"empty": False})
    harness.ensure_gitea_repo(api, "e2e", "demo", "e2e-admin")
    assert len(responses.calls) == 2


@responses.activate
def test_http_failure_does_not_disclose_response_body_or_auth() -> None:
    api = harness.API("http://gitea.test", "unused-test-value", "token")
    responses.get("http://gitea.test/api/v1/user", body="do not display this body", status=500)
    with pytest.raises(RuntimeError) as error:
        api.request("GET", "/api/v1/user")
    assert "500" in str(error.value)
    assert "do not display" not in str(error.value)
    assert "unused-test-value" not in str(error.value)


@responses.activate
def test_existing_empty_gitea_repo_gets_a_commit() -> None:
    api = harness.API("http://gitea.test", "unused-test-value", "token")
    responses.get("http://gitea.test/api/v1/orgs/e2e", json={"username": "e2e"})
    responses.get("http://gitea.test/api/v1/repos/e2e/demo", json={"empty": True})
    responses.post(
        "http://gitea.test/api/v1/repos/e2e/demo/contents/README.md", json={}, status=201
    )
    harness.ensure_gitea_repo(api, "e2e", "demo", "e2e-admin")
    assert len(responses.calls) == 3


@responses.activate
def test_gitea_user_owned_repo() -> None:
    api = harness.API("http://gitea.test", "unused-test-value", "token")
    responses.get("http://gitea.test/api/v1/repos/e2e-admin/demo", status=404)
    responses.post("http://gitea.test/api/v1/user/repos", json={"empty": False}, status=201)
    harness.ensure_gitea_repo(api, "e2e-admin", "demo", "e2e-admin")
    assert len(responses.calls) == 2


@responses.activate
@pytest.mark.parametrize("status", [200, 500])
def test_bootstrap_reuses_token_and_does_not_rotate_on_server_error(tmp_path, monkeypatch, status):
    monkeypatch.setattr(harness, "STATE", tmp_path)
    monkeypatch.setattr(harness, "network_environment", lambda: {})
    monkeypatch.setattr(harness, "wait_for_gitea", lambda url: None)
    monkeypatch.setenv("GITEA_URL", "http://gitea.test")
    monkeypatch.setenv("GITEA_ADMIN_USERNAME", "e2e-admin")
    monkeypatch.setenv("GITEA_OWNER", "e2e")
    monkeypatch.setenv("GITEA_REPO", "demo")
    monkeypatch.delenv("GITEA_TOKEN", raising=False)

    def unexpected_cli(*args, **kwargs):
        pytest.fail("A saved valid token or a server error must not create another token")

    monkeypatch.setattr(harness, "run", unexpected_cli)
    harness.write_env(tmp_path / "gitea.env", {"GITEA_TOKEN": "unused-test-value"})
    responses.get(
        "http://gitea.test/api/v1/user",
        json={"login": "e2e-admin", "is_admin": True},
        status=status,
    )
    if status == 500:
        with pytest.raises(RuntimeError, match="HTTP 500"):
            harness.bootstrap_gitea()
    else:
        responses.get("http://gitea.test/api/v1/orgs/e2e", json={"username": "e2e"})
        responses.get("http://gitea.test/api/v1/repos/e2e/demo", json={"empty": False})
        harness.bootstrap_gitea()
        assert harness.read_env(tmp_path / "gitea.env")["GITEA_TOKEN"] == "unused-test-value"


@pytest.mark.parametrize("web_running", [True, False])
def test_install_rerun_and_uninstall_keep_user_settings(tmp_path, monkeypatch, web_running):
    directory = tmp_path / "sentry"
    directory.mkdir()
    enhance = directory / "enhance-image.sh"
    settings = directory / "sentry.conf.py"
    original_enhance = "#!/bin/bash\necho custom-enhancement\n"
    original_settings = "ROOT_URLCONF = 'custom.urls'\n"
    enhance.write_text(original_enhance)
    settings.write_text(original_settings)
    monkeypatch.setenv("SELF_HOSTED_DIR", str(tmp_path))
    monkeypatch.setattr(harness, "active_profiles", lambda: {"errors-only"})
    monkeypatch.setattr(
        harness,
        "compose_config",
        lambda: {
            "services": {
                "web": {"image": "sentry-self-hosted-local"},
                "taskworker": {"image": "sentry-self-hosted-local"},
                "sentry-cleanup": {"image": "sentry-cleanup-self-hosted-local"},
                "postgres": {"image": "postgres:14.19-bookworm"},
            }
        },
    )
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "ps" in command:
            if "--all" not in command and not web_running:
                return "nginx\npostgres\n"
            return "web\ntaskworker\nsentry-cleanup\nnginx\npostgres\n"
        return ""

    def fake_build(path):
        wheel = path / "sentry_gitea-0.1.0.dev0-py3-none-any.whl"
        wheel.write_text("test placeholder")
        return wheel

    monkeypatch.setattr(harness, "run", fake_run)
    monkeypatch.setattr(harness, "build_wheel", fake_build)
    harness.install()
    first = (enhance.read_text(), settings.read_text())
    harness.install()
    assert (enhance.read_text(), settings.read_text()) == first
    assert first[0].count(harness.BEGIN) == first[1].count(harness.BEGIN) == 1
    harness.install(uninstall=True)
    assert enhance.read_text() == original_enhance
    assert settings.read_text() == original_settings
    builds = [command[-1] for command in commands if "build" in command]
    assert builds == ["web", "sentry-cleanup"] * 3
    for command in commands:
        assert "down" not in command
        if "--force-recreate" in command:
            assert "postgres" not in command
            assert "taskworker" in command


def test_sentry_test_cleanup_is_isolated_even_on_failure(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "sentry-self-hosted")
    monkeypatch.setattr(harness, "build_wheel", lambda path: None)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "run" in command:
            raise RuntimeError("test failure")
        return ""

    monkeypatch.setattr(harness, "run", fake_run)
    with pytest.raises(RuntimeError, match="test failure"):
        harness.sentry_tests()
    assert commands[-1][-1] == "down"
    assert all(command[command.index("-p") + 1] == "sentry-gitea-tests" for command in commands)


def test_network_subnets_are_exact_private_cidrs(monkeypatch) -> None:
    commands = []

    def inspect(output):
        def fake_run(command, **kwargs):
            commands.append(command)
            return output

        monkeypatch.setattr(harness, "run", fake_run)

    inspect('[{"Subnet":"172.27.0.0/16","Gateway":"172.27.0.1"},{"Subnet":"fd00:1::/64"}]\n')
    assert harness.network_subnets("sentry-self-hosted_default") == "172.27.0.0/16,fd00:1::/64"
    assert commands[-1] == [
        "docker",
        "network",
        "inspect",
        "--format",
        "{{json .IPAM.Config}}",
        "sentry-self-hosted_default",
    ]
    for output in ("[]", "null", '[{"Gateway":"172.27.0.1"}]'):
        inspect(output)
        with pytest.raises(RuntimeError, match="no IPAM subnet"):
            harness.network_subnets("net")
    inspect("not json")
    with pytest.raises(RuntimeError, match="JSON"):
        harness.network_subnets("net")
    for subnet in ("8.8.8.0/24", "0.0.0.0/0", "172.27.0.1/16", "::/0", "private"):
        inspect(json.dumps([{"Subnet": subnet}]))
        with pytest.raises(ValueError):
            harness.network_subnets("net")


def test_network_environment_subnets_override_and_down_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("SELF_HOSTED_NETWORK", "sentry-net")
    monkeypatch.delenv("SELF_HOSTED_SUBNETS", raising=False)
    inspected = []
    monkeypatch.setattr(
        harness, "network_subnets", lambda name: inspected.append(name) or "172.27.0.0/16"
    )
    assert harness.network_environment()["SELF_HOSTED_SUBNETS"] == "172.27.0.0/16"
    assert inspected == ["sentry-net"]
    # Commands that never start Gitea need not inspect a possibly missing network.
    assert harness.network_environment(subnets=False)["SELF_HOSTED_SUBNETS"] == "unused"
    assert inspected == ["sentry-net"]
    monkeypatch.setenv("SELF_HOSTED_SUBNETS", " 10.20.0.0/16 , 10.20.0.0/16,192.168.5.0/24")
    assert harness.network_environment()["SELF_HOSTED_SUBNETS"] == "10.20.0.0/16,192.168.5.0/24"
    for invalid in ("*", "private", "0.0.0.0/0", "1.1.1.0/24", ""):
        monkeypatch.setenv("SELF_HOSTED_SUBNETS", invalid or ",")
        with pytest.raises(ValueError):
            harness.network_environment()


def test_gitea_compose_allows_only_the_computed_subnets() -> None:
    text = (harness.ROOT / "docker/e2e/gitea.compose.yml").read_text()
    active = [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert (
        "GITEA__security__ALLOWED_HOST_LIST: "
        "${SELF_HOSTED_SUBNETS:?Set SELF_HOSTED_SUBNETS or use the Makefile}"
    ) in active
    assert not any("ALLOWED_HOST_LIST" in line and "security" not in line for line in active)
    assert "GITEA__security__EGRESS_MODE: lax" in active
    assert "aliases: [gitea, gitea-unlisted]" in active


@pytest.mark.skipif(not shutil.which("sh") or not shutil.which("awk"), reason="needs sh and awk")
def test_ini_scrub_removes_only_the_deprecated_webhook_host_list(tmp_path: Path) -> None:
    ini = tmp_path / "app.ini"
    original = (
        "APP_NAME = Gitea\n"
        "[security]\n"
        "EGRESS_MODE = lax\n"
        "ALLOWED_HOST_LIST = 172.27.0.0/16\n"
        "\n"
        "[ Webhook ]\n"
        "DELIVER_TIMEOUT = 20\n"
        "allowed_host_list = *\n"
        "; ALLOWED_HOST_LIST = commented\n"
        "[oauth2]\n"
        "ALLOWED_HOST_LIST = not-the-webhook-section\n"
    )
    ini.write_text(original)
    ini.chmod(0o640)
    inode = ini.stat().st_ino
    script = harness.INI_SCRUB.replace("/data/gitea/conf/app.ini", str(ini))

    def scrub() -> str:
        return subprocess.run(
            ["sh", "-c", script], check=True, capture_output=True, text=True
        ).stdout

    assert scrub().split() == [harness.INI_SCRUBBED]
    assert ini.read_text() == original.replace("allowed_host_list = *\n", "")
    # Rewritten in place: Gitea's owner and mode survive.
    assert ini.stat().st_ino == inode and stat.S_IMODE(ini.stat().st_mode) == 0o640
    assert scrub() == ""
    ini.unlink()
    assert scrub() == ""


@pytest.mark.parametrize("stale", [True, False])
def test_gitea_up_scrubs_a_stale_webhook_host_list_and_restarts_only_then(monkeypatch, stale):
    monkeypatch.setattr(
        harness,
        "network_environment",
        lambda: {"SELF_HOSTED_NETWORK": "net", "SELF_HOSTED_SUBNETS": "172.27.0.0/16"},
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "exec" in command:
            return harness.INI_SCRUBBED + "\n" if stale else ""
        return ""

    monkeypatch.setattr(harness, "run", fake_run)
    harness.gitea_up()
    actions = [command[command.index("-f") + 2] for command, _ in calls]
    assert actions == (["up", "exec", "restart", "up"] if stale else ["up", "exec"])
    exec_command = calls[1][0]
    assert exec_command[-6:] == ["--user", "git", "gitea", "sh", "-c", harness.INI_SCRUB]
    assert all(kwargs["env"]["SELF_HOSTED_SUBNETS"] == "172.27.0.0/16" for _, kwargs in calls)
    # The scrub's output is captured, never printed (app.ini holds Gitea's secrets).
    assert calls[1][1].get("capture", True) is True
