"""The directory of the test database lives for one pytest session.

mutmut runs pytest in forked children that end with `os._exit`. That skips the
atexit handlers, so the session itself must remove the directory. A second
session in the same process must still get a database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Two sessions in one process, which then ends as a mutmut child ends. For each
# session it records the database at the session end (pytest_sessionfinish runs
# before pytest_unconfigure removes the directory) and the directories left after it.
_TWO_SESSIONS = """
import glob
import json
import os
import sys
import tempfile

import pytest


def found(*parts):
    return len(glob.glob(os.path.join(tempfile.gettempdir(), "django_bolt_test_*", *parts)))


class Probe:
    databases = 0

    def pytest_sessionfinish(self):
        self.databases = found("db.sqlite3")


args = ["python/tests/test_health.py", "-q", "-p", "no:cacheprovider", "-p", "no:xdist"]
sessions = []
for _ in range(2):
    probe = Probe()
    code = int(pytest.main(args, plugins=[probe]))
    sessions.append({"code": code, "databases": probe.databases, "left": found()})
sys.stdout.write(json.dumps(sessions) + "\\n")
sys.stdout.flush()
os._exit(0)
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
    sessions = json.loads(result.stdout.strip().splitlines()[-1])
    # Each session made its database, and removed its directory before the next.
    assert sessions == [{"code": 0, "databases": 1, "left": 0}] * 2, result.stdout + result.stderr
