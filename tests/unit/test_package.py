import tomllib
from importlib.metadata import distribution
from pathlib import Path

import sentry_gitea
from sentry_gitea import __version__, meta

ROOT = Path(__file__).resolve().parents[2]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_package_metadata_without_importing_sentry() -> None:
    package = distribution("sentry-gitea")
    assert package.version == __version__
    entry_points = {
        ep.name: ep.value for ep in package.entry_points if ep.group == "sentry.plugins"
    }
    assert entry_points == {
        "gitea": "sentry_gitea.plugins.issue:GiteaIssuePlugin",
        "gitea_alerts": "sentry_gitea.plugins.alerts:GiteaAlertsPlugin",
    }


def test_first_public_alpha_version_is_consistent() -> None:
    assert __version__ == PROJECT["version"] == "0.1.0"


def test_meta_constants_match_pyproject_urls_and_author() -> None:
    urls = PROJECT["urls"]
    assert meta.PROJECT_URL == urls["Homepage"] == urls["Repository"]
    assert meta.ISSUES_URL == urls["Issues"] == meta.PROJECT_URL + "/issues"
    assert [author["name"] for author in PROJECT["authors"]] == [meta.AUTHOR]
    assert all(url.startswith(meta.PROJECT_URL) for url in urls.values())


def test_no_placeholder_owner_or_private_tooling_names_in_the_package() -> None:
    package = Path(sentry_gitea.__file__).parent
    for path in package.rglob("*.py"):
        text = path.read_text()
        assert "github.com/OWNER" not in text, path
        assert "pipesafu" not in text.lower(), path


def test_directly_imported_dependencies_are_declared() -> None:
    build = tomllib.loads((ROOT / "pyproject.toml").read_text())["build-system"]
    assert build["requires"] == ["hatchling>=1.27"]
    assert {"requests>=2.31", "urllib3>=2"} <= set(PROJECT["dependencies"])
