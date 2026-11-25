import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from session_audit import __version__
from session_audit.cli import main

ROOT = Path(__file__).resolve().parent.parent


def test_version_prints_package_metadata_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"session-audit {version('session-audit')}"


def test_version_is_never_the_placeholder() -> None:
    assert __version__ not in {"0.0.0", "", "unknown"}


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"], (
        f"the installed metadata says {__version__} but pyproject.toml says "
        f"{pyproject['project']['version']}: run `uv sync` after a release bump"
    )


def test_no_args_returns_usage_error() -> None:
    assert main([]) == 2
