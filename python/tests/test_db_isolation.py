"""The database fixtures isolate tests, also when a test gets them with ``getfixturevalue`` (#941)."""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from django.db import connection

_CASES = Path(__file__).with_name("_db_isolation_cases.py")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_transactional_db_from_getfixturevalue_does_not_wrap_the_test_in_a_transaction(request):
    request.getfixturevalue("transactional_db")

    assert connection.in_atomic_block is False


def test_transactional_db_from_signature_does_not_wrap_the_test_in_a_transaction(transactional_db):
    assert connection.in_atomic_block is False


def test_db_wraps_the_test_in_a_transaction(db):
    assert connection.in_atomic_block is True


def _outcomes(report: Path) -> dict[str, str]:
    """Map the name of each test in a JUnit XML report to its outcome and message."""
    outcomes = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        outcomes[case.get("name")] = "passed" if problem is None else f"{problem.tag}: {problem.get('message')}"
    return outcomes


def test_a_row_of_one_transactional_test_is_gone_in_the_next_one(tmp_path):
    """The writer adds a row through the async ORM. The next test must not see that row.

    The tests get ``transactional_db`` with ``getfixturevalue``. A child pytest run
    keeps the order of the tests and the teardown of each test. A test that
    already runs in one transaction cannot get ``transactional_db``.
    """
    report = tmp_path / "report.xml"
    # The child must run each case. Options such as -x from PYTEST_ADDOPTS stop it at the first expected failure.
    env = {name: value for name, value in os.environ.items() if name != "PYTEST_ADDOPTS"}

    subprocess.run(
        [sys.executable, "-m", "pytest", str(_CASES), "-p", "no:cacheprovider", "-q", f"--junitxml={report}"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
        timeout=120,
    )

    outcomes = _outcomes(report)
    assert outcomes["test_writer"] == "passed"
    assert outcomes["test_reader_starts_with_an_empty_table"] == "passed"
    for refused in ("test_db_then_transactional_db_is_refused", "test_django_db_then_transactional_db_is_refused"):
        assert outcomes[refused].startswith("failure: Failed: transactional_db came after db"), outcomes[refused]
    assert outcomes["test_django_db_transaction_then_transactional_db_is_accepted"] == "passed"
