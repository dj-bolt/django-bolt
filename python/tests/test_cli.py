from __future__ import annotations

import tomllib
from pathlib import Path

from click.testing import CliRunner

from django_bolt.cli import main


def test_version_command():
    """version prints the prefix followed by the pyproject version."""
    with (Path(__file__).parents[2] / "pyproject.toml").open("rb") as f:
        expected = tomllib.load(f)["project"]["version"]

    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0
    assert "Django-Bolt version:" in result.output

    reported = result.output.split("Django-Bolt version:", 1)[1].strip()
    assert reported == expected


def test_help_lists_version_command():
    """--help exits cleanly and mentions the version subcommand."""
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.output
