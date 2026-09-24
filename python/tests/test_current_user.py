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
from django.contrib.auth.models import User
from django.db import connection
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, CurrentUser, Depends, Request, get_current_user
from django_bolt.auth import JWTAuthentication
from django_bolt.middleware import DjangoMiddlewareStack
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
