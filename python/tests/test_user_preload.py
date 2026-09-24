"""Bolt loads ``request.user`` before an async handler that reads it.

The handler then reads a loaded user, so its sync read does not block the
event loop. A backend with only an async ``get_user`` shows it: a sync read
of such a user raises, and a loaded user does not.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import alogin
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request
from django_bolt.auth import JWTAuthentication
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.testing import TestClient

SECRET = "user-preload-test-secret-longer-than-32-characters"
_local = threading.local()


class _TenantMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _local.tenant = request.path.rsplit("/", 1)[-1]


class _AsyncOnlyAuth(JWTAuthentication):
    """A backend with an async ``get_user`` only. A sync read of its user raises."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    async def get_user(self, user_id, auth_context):
        self.calls += 1
        # The coroutine runs on the event loop. Thread-sensitive work reaches the lane.
        tenant = await sync_to_async(_read_tenant, thread_sensitive=True)()
        return SimpleNamespace(username="bob", tenant=tenant)


def _read_tenant() -> str:
    return getattr(_local, "tenant", "<unset>")


def _headers() -> dict[str, str]:
    token = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("with_django_middleware", [False, True], ids=["plain", "django_middleware"])
def test_an_async_handler_that_reads_the_user_gets_it_loaded(with_django_middleware):
    middleware = [DjangoMiddlewareStack([_TenantMiddleware])] if with_django_middleware else []
    api = BoltAPI(middleware=middleware)
    auth = _AsyncOnlyAuth(secret=SECRET)

    @api.get("/tenant/{tenant}", auth=[auth])
    async def endpoint(tenant: str, request: Request):
        await asyncio.sleep(0)
        return {"username": request.user.username, "tenant": request.user.tenant}

    with TestClient(api) as client:
        response = client.get("/tenant/acme", headers=_headers())

    assert response.status_code == 200, response.text
    expected_tenant = "acme" if with_django_middleware else "<unset>"
    assert response.json() == {"username": "bob", "tenant": expected_tenant}
    assert auth.calls == 1


def test_an_async_handler_with_no_await_gets_its_user_loaded():
    """A handler with no await takes the sync fast path. A read of the user moves it to the async path."""
    api = BoltAPI()

    @api.get("/me", auth=[_AsyncOnlyAuth(secret=SECRET)])
    async def me(request: Request):
        return {"username": request.user.username}

    with TestClient(api) as client:
        response = client.get("/me", headers=_headers())

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "bob"}


def test_a_handler_that_does_not_read_the_user_loads_nothing():
    api = BoltAPI()
    auth = _AsyncOnlyAuth(secret=SECRET)

    @api.get("/ping", auth=[auth])
    async def ping(request: Request):
        await asyncio.sleep(0)
        return {"ok": True}

    with TestClient(api) as client:
        assert client.get("/ping", headers=_headers()).json() == {"ok": True}

    assert auth.calls == 0


@pytest.mark.django_db(transaction=True)
def test_the_session_user_of_django_loads_before_the_handler():
    """The lazy user of ``AuthenticationMiddleware`` loads with Django's ``auser``, not on the loop."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([SessionMiddleware, AuthenticationMiddleware])])

    @api.post("/login")
    async def login(request: Request):
        await alogin(request, await User.objects.aget(username="session_reader"))
        return {"ok": True}

    @api.get("/me")
    async def me(request: Request):
        await asyncio.sleep(0)
        return {"username": request.user.username, "authenticated": request.user.is_authenticated}

    User.objects.create_user(username="session_reader", password="pw-for-tests")
    with TestClient(api) as client:
        assert client.post("/login").status_code == 200
        response = client.get("/me")

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "session_reader", "authenticated": True}
