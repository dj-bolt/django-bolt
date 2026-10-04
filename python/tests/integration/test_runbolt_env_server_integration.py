"""runbolt stops at startup when a DJANGO_BOLT_* environment variable is not valid.

The test client reads the variables of the app (see test_settings_types.py).
These variables configure only the server process, so only runbolt reads them.
"""

from __future__ import annotations

import subprocess

import pytest

from .apps import app_module
from .helpers import _terminate_process

pytestmark = pytest.mark.server_integration


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("DJANGO_BOLT_BLOCKING_THREADS", "0", "an int of 1 or more"),
        ("DJANGO_BOLT_KEEP_ALIVE", "-1", "an int of 0 or more"),
        ("DJANGO_BOLT_SHUTDOWN_TIMEOUT", "soon", "an int of 0 or more"),
        ("DJANGO_BOLT_REUSE_PORT", "maybe", "1, 0, true or false"),
    ],
)
def test_invalid_environment_variable_stops_runbolt(make_server_project, name, value, expected):
    project = make_server_project(api_module=app_module("hello"))

    process, _port = project.spawn(env={name: value})
    try:
        _stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        _stdout, stderr = _terminate_process(process)
        pytest.fail(f"runbolt kept running with {name}={value!r}\nstderr:\n{stderr}")

    assert process.returncode != 0, stderr
    assert f"{name} must be {expected}, got '{value}'." in stderr, stderr
