"""The directory of the test database lives for one pytest session.

mutmut runs pytest in forked children that end with `os._exit`. That skips the
atexit handlers, so the session itself must remove the directory. A second
session in the same process must still get a database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Two sessions in one process, which then ends as a mutmut child ends.
_TWO_SESSIONS = """
import os
import pytest

args = ["python/tests/test_health.py", "-q", "-p", "no:cacheprovider", "-p", "no:xdist"]
codes = [int(pytest.main(args)) for _ in range(2)]
os._exit(max(codes))
"""


def test_each_session_removes_its_database_directory(tmp_path):
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", _TWO_SESSIONS],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(tmp_path.glob("django_bolt_test_*")) == []
