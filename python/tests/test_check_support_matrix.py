"""scripts/check_support_matrix.py reports drift of the Django>= floor.

The weekly support-matrix workflow runs the script against endoflife.date.
These tests give it fixed release data, so they need no network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_support_matrix.py"
_spec = importlib.util.spec_from_file_location("check_support_matrix", SCRIPT)
check_support_matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_support_matrix)

RELEASES = {
    "django": [
        {"name": "6.1", "isMaintained": True, "custom": {"supportedPythonVersions": "3.12 - 3.14"}},
        {"name": "6.0", "isMaintained": True, "custom": {"supportedPythonVersions": "3.12 - 3.14"}},
        {"name": "5.2", "isMaintained": True, "custom": {"supportedPythonVersions": "3.10 - 3.14"}},
        {"name": "5.1", "isMaintained": False, "eolFrom": "2025-12-03"},
    ],
    "python": [
        {"name": "3.14", "isMaintained": True},
        {"name": "3.13", "isMaintained": True},
        {"name": "3.12", "isMaintained": True},
        {"name": "3.9", "isMaintained": False, "eolFrom": "2025-10-31"},
    ],
}


def _check(tmp_path, monkeypatch, floor, djangos=("5.2", "6.0", "6.1")):
    monkeypatch.setattr(check_support_matrix, "fetch_releases", lambda product, _max_attempts: RELEASES[product])
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f"""
[project]
classifiers = [
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    {"".join(f'"Framework :: Django :: {series}",' for series in djangos)}
]
dependencies = ["Django>={floor}", "msgspec>=0.20"]
""",
        encoding="utf-8",
    )
    return check_support_matrix.check(pyproject)


@pytest.mark.parametrize("floor", ["5.2", "5.2.0", "5.2.19"])
def test_a_floor_in_the_oldest_declared_series_is_current(tmp_path, monkeypatch, floor):
    """A patch floor such as 5.2.19 keeps out releases with known CVEs. It is not drift."""
    drift, notes = _check(tmp_path, monkeypatch, floor)
    assert drift == []
    assert notes == []


@pytest.mark.parametrize("floor", ["5", "5.1", "5.1.9", "6.0", "6.0.1"])
def test_a_floor_outside_the_oldest_declared_series_is_drift(tmp_path, monkeypatch, floor):
    drift, _ = _check(tmp_path, monkeypatch, floor)
    assert [d.subject for d in drift] == [f"Django>={floor}"]


@pytest.mark.parametrize(
    ("floor", "is_drift"), [("6", False), ("6.0", False), ("6.0.4", False), ("5.2", True), ("6.1", True)]
)
def test_a_floor_without_a_minor_names_the_x_0_series(tmp_path, monkeypatch, floor, is_drift):
    """`Django>=6` is the same floor as `Django>=6.0`."""
    drift, _ = _check(tmp_path, monkeypatch, floor, djangos=("6.0", "6.1"))
    floor_drift = [d.subject for d in drift if d.subject.startswith("Django>=")]
    assert floor_drift == ([f"Django>={floor}"] if is_drift else [])
