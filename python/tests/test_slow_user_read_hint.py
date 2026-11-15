"""In dev mode, Bolt reports a sync ``request.user`` read that blocks the event loop.

A read in a helper, a template, or a serializer is not in the source of the
handler, so Bolt cannot load that user before the handler. When such a read
blocks the loop for longer than a threshold, Bolt logs the line of the read,
one time for each line.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import time
from types import SimpleNamespace

import jwt
import pytest
from django.test import override_settings

from django_bolt import BoltAPI, Request, concurrency
from django_bolt.auth import JWTAuthentication
from django_bolt.testing import TestClient

SECRET = "slow-user-read-hint-secret-longer-than-32-characters"


@pytest.fixture(autouse=True)
def _bolt_logs_reach_caplog(monkeypatch):
    """Bolt's logging setup stops ``django_bolt`` records at its own handler.

    An earlier test can leave that setup in place. ``caplog`` listens on the
    root logger, so the records must propagate for these tests.
    """
    monkeypatch.setattr(logging.getLogger("django_bolt"), "propagate", True)


@pytest.fixture(autouse=True)
def _each_test_reports_again(monkeypatch):
    """Bolt reports each line one time for each process.

    Each test starts with no line reported. Thus a second pytest session in
    the same process (as mutmut runs) reports the line again.
    """
    monkeypatch.setattr(concurrency, "_reported_slow_reads", set())


@pytest.fixture(autouse=True)
def _mutmut_trampolines_are_framework_frames(monkeypatch):
    """mutmut calls each function of Bolt through a trampoline in its own package.

    Bolt reports the first caller outside Bolt, Django, and asgiref. Without
    this, that caller is the trampoline, and the tests fail for each mutant.
    """
    parts = (*concurrency._FRAMEWORK_PATH_PARTS, f"{os.sep}mutmut{os.sep}")
    monkeypatch.setattr(concurrency, "_FRAMEWORK_PATH_PARTS", parts)


class _SlowAuth(JWTAuthentication):
    def __init__(self, delay: float, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay

    def get_user_sync(self, user_id):
        time.sleep(self.delay)
        return SimpleNamespace(username="bob")


def _username_of(request) -> str:
    return request.user.username


_READ_LINE = inspect.getsourcelines(_username_of)[1] + 1


def _headers() -> dict[str, str]:
    token = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _api(delay: float) -> BoltAPI:
    api = BoltAPI()

    @api.get("/me", auth=[_SlowAuth(delay, secret=SECRET)])
    async def me(request: Request):
        # The helper reads the user, so the handler source shows no read.
        return {"username": _username_of(request)}

    return api


def _hints(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and "blocked the event loop" in r.getMessage()]


@pytest.mark.parametrize(
    ("debug", "dev_worker", "reported"),
    [
        pytest.param(True, None, True, id="debug"),
        # ``runbolt --dev`` sets this variable in its worker.
        pytest.param(False, "1", True, id="dev-worker"),
        pytest.param(False, "0", False, id="no-dev-worker"),
        pytest.param(False, None, False, id="production"),
    ],
)
def test_a_slow_blocking_read_is_reported_one_time_in_dev_mode(caplog, monkeypatch, debug, dev_worker, reported):
    if dev_worker is None:
        monkeypatch.delenv("DJANGO_BOLT_DEV_WORKER", raising=False)
    else:
        monkeypatch.setenv("DJANGO_BOLT_DEV_WORKER", dev_worker)
    caplog.set_level(logging.WARNING, logger="django_bolt")
    with override_settings(DEBUG=debug), TestClient(_api(delay=0.08)) as client:
        for _ in range(3):
            response = client.get("/me", headers=_headers())
            assert response.status_code == 200, response.text

    hints = _hints(caplog)
    if not reported:
        assert hints == []
        return
    assert len(hints) == 1, [r.getMessage() for r in hints]
    message = hints[0].getMessage()
    assert f"test_slow_user_read_hint.py:{_READ_LINE}" in message
    assert "await request.auser()" in message
    # The read took 80 ms or more.
    blocked = re.search(r"blocked the event loop for (\d+) ms", message)
    assert blocked is not None, message
    assert int(blocked.group(1)) >= 80, message


def test_a_fast_read_is_not_reported(caplog):
    caplog.set_level(logging.WARNING, logger="django_bolt")
    with override_settings(DEBUG=True), TestClient(_api(delay=0)) as client:
        assert client.get("/me", headers=_headers()).status_code == 200

    assert _hints(caplog) == []
