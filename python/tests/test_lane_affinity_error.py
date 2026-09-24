"""The error of a sync ``request.user`` read that cannot reach the request lane names its route.

A thread from ``asyncio.to_thread`` cannot wait for the lane, so a read there
raises. The read can be deep in a helper, so the error must say which route
and which handler ran it. In dev mode, Bolt also logs the fix one time for
each route.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import jwt
import pytest
from django.test import override_settings
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request
from django_bolt.auth import JWTAuthentication
from django_bolt.concurrency import LaneAffinityError
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.testing import TestClient

SECRET = "lane-affinity-error-secret-longer-than-32-characters"


class _NoOpMiddleware(MiddlewareMixin):
    def process_request(self, request):
        pass


class _Auth(JWTAuthentication):
    def get_user_sync(self, user_id):
        return SimpleNamespace(username="bob")


def _headers() -> dict[str, str]:
    token = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _api() -> BoltAPI:
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_NoOpMiddleware])])

    @api.get("/profile/{name}", auth=[_Auth(secret=SECRET)])
    async def profile(name: str, request: Request):
        # A helper on another thread reads the user where the handler does not show it.
        return {"name": name, "user": await asyncio.to_thread(_username_of, request)}

    return api


def _username_of(request) -> str:
    return request.user.username


def _error_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.exc_info and record.exc_info[0] is LaneAffinityError]


def test_the_error_is_a_runtime_error():
    assert issubclass(LaneAffinityError, RuntimeError)


def test_the_error_names_the_route_and_the_handler(caplog):
    caplog.set_level(logging.ERROR)
    with TestClient(_api()) as client:
        response = client.get("/profile/ann", headers=_headers())

    assert response.status_code == 500
    records = _error_records(caplog)
    assert records, [record.getMessage() for record in caplog.records]
    message = str(records[0].exc_info[1])
    assert "await request.auser()" in message
    assert "GET /profile/{name}" in message
    assert "profile" in message
    assert "test_lane_affinity_error.py:" in message


def test_dev_mode_logs_the_fix_one_time_for_each_route(caplog):
    caplog.set_level(logging.WARNING, logger="django_bolt")
    with override_settings(DEBUG=True), TestClient(_api()) as client:
        for _ in range(3):
            assert client.get("/profile/ann", headers=_headers()).status_code == 500

    hints = [r for r in caplog.records if r.levelno == logging.WARNING and "await request.auser()" in r.getMessage()]
    assert len(hints) == 1, [r.getMessage() for r in hints]
    assert "GET /profile/{name}" in hints[0].getMessage()


@pytest.mark.parametrize("debug", [False])
def test_no_hint_outside_dev_mode(caplog, debug, monkeypatch):
    monkeypatch.delenv("DJANGO_BOLT_DEV_WORKER", raising=False)
    caplog.set_level(logging.WARNING, logger="django_bolt")
    with override_settings(DEBUG=debug), TestClient(_api()) as client:
        assert client.get("/profile/ann", headers=_headers()).status_code == 500

    assert not [r for r in caplog.records if r.levelno == logging.WARNING and "await request.auser()" in r.getMessage()]
