"""The current user as a dependency: ``Depends(get_current_user)`` and ``CurrentUser``.

The dependency loads the user as ``await request.auser()`` does: with the
loader of the backend, on the lane of a request with Django middleware, and
into the cache of ``request.user``.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Annotated

import jwt
import pytest
from django.contrib.auth import alogin
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, CurrentUser, Depends, OptionalCurrentUser, Request, get_current_user
from django_bolt.auth import JWTAuthentication
from django_bolt.concurrency import in_lane_mode
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack
from django_bolt.testing import TestClient

SECRET = "current-user-test-secret-longer-than-32-characters"

_local = threading.local()


class _TenantMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _local.tenant = request.path.rsplit("/", 1)[-1]


class _TenantAuth(JWTAuthentication):
    """A backend with a custom sync loader that reads the tenant of the lane."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queries = 0

    def get_user_sync(self, user_id):
        self.queries += 1
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return SimpleNamespace(username="bob", tenant=getattr(_local, "tenant", "<unset>"), is_authenticated=True)


def _headers(user_id: str = "1") -> dict[str, str]:
    token = jwt.encode({"sub": user_id, "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handler_is_async", [True, False], ids=["async", "sync"])
def test_current_user_loads_with_the_backend_on_the_lane(handler_is_async):
    """The dependency uses the loader of the backend, on the lane, and fills ``request.user``."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware])])
    auth = _TenantAuth(secret=SECRET)

    if handler_is_async:

        @api.get("/tenant/{tenant}", auth=[auth])
        async def endpoint(tenant: str, request: Request, user: CurrentUser):
            return {"tenant": user.tenant, "cached": request.user.tenant}
    else:

        @api.get("/tenant/{tenant}", auth=[auth])
        def endpoint(tenant: str, request: Request, user: CurrentUser):
            return {"tenant": user.tenant, "cached": request.user.tenant}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers=_headers())

    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": "acme", "cached": "acme"}
    assert auth.queries == 1


@pytest.mark.django_db(transaction=True)
def test_get_current_user_returns_none_without_a_user():
    """No authentication, or a user ID with no row, gives ``None``."""
    api = BoltAPI()
    auth = JWTAuthentication(secret=SECRET)

    @api.get("/me", auth=[auth])
    async def me(user: Annotated[User | None, Depends(get_current_user)]):
        return {"username": user.username if user is not None else None}

    real = User.objects.create(username="real_user")
    with TestClient(api) as client:
        assert client.get("/me").json() == {"username": None}
        assert client.get("/me", headers=_headers(str(real.pk + 1000))).json() == {"username": None}
        assert client.get("/me", headers=_headers(str(real.pk))).json() == {"username": "real_user"}


@pytest.mark.django_db(transaction=True)
def test_current_user_in_a_sync_handler_keeps_lane_dispatch():
    """A sync handler loads its user with a sync dependency, so the request runs on the lane with no asyncio."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware])])
    auth = _TenantAuth(secret=SECRET)

    @api.get("/tenant/{tenant}", auth=[auth])
    def endpoint(tenant: str, user: CurrentUser):
        return {"tenant": user.tenant, "lane_mode": in_lane_mode()}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers=_headers())

    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": "acme", "lane_mode": True}
    assert auth.queries == 1


@pytest.mark.parametrize("handler_is_async", [True, False], ids=["async", "sync"])
def test_current_user_rejects_a_request_with_no_user(handler_is_async):
    """``CurrentUser`` answers 401 with no authenticated user. ``OptionalCurrentUser`` gives ``None``."""
    api = BoltAPI()

    if handler_is_async:

        @api.get("/required")
        async def required(user: CurrentUser):
            return {"username": user.username}

        @api.get("/optional")
        async def optional(user: OptionalCurrentUser):
            return {"user": None if user is None else user.username}
    else:

        @api.get("/required")
        def required(user: CurrentUser):
            return {"username": user.username}

        @api.get("/optional")
        def optional(user: OptionalCurrentUser):
            return {"user": None if user is None else user.username}

    with TestClient(api) as client:
        assert client.get("/required").status_code == 401
        response = client.get("/optional")
        assert response.status_code == 200, response.text
        assert response.json() == {"user": None}


def _sync_dependency(request: Request) -> str:
    return getattr(_local, "tenant", "<unset>")


@pytest.mark.django_db(transaction=True)
def test_a_sync_handler_with_sync_dependencies_keeps_lane_dispatch():
    """Sync dependencies of a sync handler do not need asyncio, so lane dispatch stays on."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware])])

    @api.get("/tenant/{tenant}")
    def endpoint(tenant: str, dep: Annotated[str, Depends(_sync_dependency)]):
        return {"dep": dep, "lane_mode": in_lane_mode()}

    with TestClient(api) as client:
        response = client.get("/tenant/acme")

    assert response.status_code == 200, response.text
    assert response.json() == {"dep": "acme", "lane_mode": True}


class _PassThrough:
    """A plain Bolt middleware. It makes the request async, so a sync handler runs on its lane."""

    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request):
        return await self.get_response(request)


def test_sync_dependencies_of_a_blocking_sync_handler_run_on_its_thread():
    """In an async request, the sync handler runs on the lane. Its sync dependencies must run there too.

    They can read the thread-local state of the Django middleware, as the handler does.
    """
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware]), _PassThrough])

    @api.get("/tenant/{tenant}")
    def endpoint(tenant: str, dep: Annotated[str, Depends(_sync_dependency)]):
        return {"dep": dep, "handler": getattr(_local, "tenant", "<unset>")}

    with TestClient(api) as client:
        response = client.get("/tenant/acme")

    assert response.status_code == 200, response.text
    assert response.json() == {"dep": "acme", "handler": "acme"}


@pytest.mark.django_db(transaction=True)
def test_current_user_of_a_session_in_a_sync_handler_of_an_async_request():
    """The sync form of ``CurrentUser`` reads the lazy session user of Django on the lane, not on the loop."""
    api = BoltAPI(
        middleware=[DjangoMiddleware(SessionMiddleware), DjangoMiddleware(AuthenticationMiddleware), _PassThrough]
    )

    @api.post("/login")
    async def login(request: Request):
        await alogin(request, await User.objects.aget(username="session_current"))
        return {"ok": True}

    @api.get("/me")
    def me(user: CurrentUser):
        return {"username": user.username}

    User.objects.create_user(username="session_current", password="pw-for-tests")
    with TestClient(api) as client:
        assert client.post("/login").status_code == 200
        response = client.get("/me")

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "session_current"}
