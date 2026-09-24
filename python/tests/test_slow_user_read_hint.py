"""In dev mode, Bolt reports a sync ``request.user`` read that blocks the event loop.

A read in a helper, a template, or a serializer is not in the source of the
handler, so Bolt cannot load that user before the handler. When such a read
blocks the loop for longer than a threshold, Bolt logs the line of the read,
one time for each line.
"""

from __future__ import annotations

import inspect
import logging
import time
from types import SimpleNamespace

import jwt
import pytest
from django.test import override_settings

from django_bolt import BoltAPI, Request
from django_bolt.auth import JWTAuthentication
from django_bolt.testing import TestClient

SECRET = "slow-user-read-hint-secret-longer-than-32-characters"


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


@pytest.mark.parametrize("debug", [True, False], ids=["dev", "production"])
def test_a_slow_blocking_read_is_reported_one_time_in_dev_mode(caplog, monkeypatch, debug):
    monkeypatch.delenv("DJANGO_BOLT_DEV_WORKER", raising=False)
    caplog.set_level(logging.WARNING, logger="django_bolt")
    with override_settings(DEBUG=debug), TestClient(_api(delay=0.08)) as client:
        for _ in range(3):
            response = client.get("/me", headers=_headers())
            assert response.status_code == 200, response.text

    hints = _hints(caplog)
    if not debug:
        assert hints == []
        return
    assert len(hints) == 1, [r.getMessage() for r in hints]
    message = hints[0].getMessage()
    assert f"test_slow_user_read_hint.py:{_READ_LINE}" in message
    assert "await request.auser()" in message


def test_a_fast_read_is_not_reported(caplog):
    caplog.set_level(logging.WARNING, logger="django_bolt")
    with override_settings(DEBUG=True), TestClient(_api(delay=0)) as client:
        assert client.get("/me", headers=_headers()).status_code == 200

    assert _hints(caplog) == []
