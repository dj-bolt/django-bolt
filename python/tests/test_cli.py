"""The ``django-bolt`` console script."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest
from click.testing import CliRunner

from django_bolt import cli


def _load_cli_as_installed(site_packages: Path) -> ModuleType:
    """Load a copy of ``cli.py`` from ``<site-packages>/django_bolt/cli.py``, where a wheel puts it.

    A wheel has no ``pyproject.toml``, so the copy has no repository files above it.
    """
    package_dir = site_packages / "django_bolt"
    package_dir.mkdir(parents=True)
    installed_cli = package_dir / "cli.py"
    shutil.copyfile(cli.__file__, installed_cli)
    spec = importlib.util.spec_from_file_location("installed_django_bolt_cli", installed_cli)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_prints_the_installed_distribution_version(tmp_path):
    installed_cli = _load_cli_as_installed(tmp_path / "site-packages")

    result = CliRunner().invoke(installed_cli.main, ["version"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert result.output == f"Django-Bolt version: {importlib.metadata.version('django-bolt')}\n"


def test_version_of_the_imported_module_prints_the_distribution_version():
    result = CliRunner().invoke(cli.main, ["version"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert result.output == f"Django-Bolt version: {importlib.metadata.version('django-bolt')}\n"


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_of_the_imported_module_lists_the_version_command(flag):
    result = CliRunner().invoke(cli.main, [flag], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert result.output.startswith("Usage: main [OPTIONS] COMMAND [ARGS]...\n"), result.output
    assert "Django-Bolt command line interface." in result.output
    assert "-h, --help  Show this message and exit." in result.output
    assert "version  Show Django-Bolt version." in result.output

    command_help = CliRunner().invoke(cli.main, ["version", flag], catch_exceptions=False)

    assert command_help.exit_code == 0, command_help.output
    assert command_help.output.startswith("Usage: main version [OPTIONS]\n"), command_help.output
    assert "Show Django-Bolt version." in command_help.output
