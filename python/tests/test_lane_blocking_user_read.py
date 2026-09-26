"""A sync ``request.user`` read on the event loop waits for the request lane.

Each case must load the user on the lane, with the thread-local state of the
Django middleware, and must not deadlock. Each request has a timeout, so a
deadlock fails the test instead of hanging the run.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from asgiref.sync import async_to_sync
from django.template import engines
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request
from django_bolt.auth import JWTAuthentication
from django_bolt.concurrency import sync_to_thread
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack
from django_bolt.testing import AsyncTestClient, TestClient

SECRET = "lane-blocking-user-read-secret-longer-than-32-chars"
_local = threading.local()


class _TenantMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _local.tenant = request.path.rsplit("/", 1)[-1]


class _CallOnlyMiddleware:
    """A sync ``__call__`` middleware. On the lane, it waits for the loop in ``async_to_sync``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


class _Auth(JWTAuthentication):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.threads: list[int] = []

    def get_user_sync(self, user_id):
        self.threads.append(threading.get_ident())
        return SimpleNamespace(username="bob", tenant=getattr(_local, "tenant", "<unset>"))


def _headers() -> dict[str, str]:
    token = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _lane_ident() -> int:
    return threading.get_ident()


def _get(api: BoltAPI, path: str):
    with TestClient(api, timeout=10) as client:
        return client.get(path, headers=_headers())


@pytest.mark.parametrize(
    "middleware",
    [
        pytest.param(lambda: [DjangoMiddlewareStack([_TenantMiddleware])], id="stack_hooks"),
        pytest.param(lambda: [DjangoMiddlewareStack([_TenantMiddleware, _CallOnlyMiddleware])], id="stack_call_chain"),
        pytest.param(lambda: [DjangoMiddleware(_TenantMiddleware), DjangoMiddleware(_CallOnlyMiddleware)], id="single"),
    ],
)
def test_a_sync_read_in_an_async_handler_loads_on_the_lane(middleware):
    api = BoltAPI(middleware=middleware())
    auth = _Auth(secret=SECRET)

    @api.get("/tenant/{tenant}", auth=[auth])
    async def endpoint(tenant: str, request: Request):
        await asyncio.sleep(0)
        user_tenant = request.user.tenant
        return {"tenant": user_tenant, "lane": await sync_to_thread(_lane_ident)}

    response = _get(api, "/tenant/acme")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant"] == "acme"
    assert auth.threads == [body["lane"]]


def test_a_sync_read_while_the_lane_runs_other_work():
    """The read waits behind the sync work of the same request on the lane."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware])])

    def slow() -> str:
        time.sleep(0.05)
        return "slow"

    @api.get("/tenant/{tenant}", auth=[_Auth(secret=SECRET)])
    async def endpoint(tenant: str, request: Request):
        pending = asyncio.ensure_future(sync_to_thread(slow))
        await asyncio.sleep(0.01)
        user_tenant = request.user.tenant
        return {"tenant": user_tenant, "slow": await pending}

    response = _get(api, "/tenant/acme")
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": "acme", "slow": "slow"}


def test_a_sync_read_under_async_to_sync_in_a_lane_handler():
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware])])

    @api.get("/tenant/{tenant}", auth=[_Auth(secret=SECRET)])
    def endpoint(tenant: str, request: Request):
        async def read():
            return request.user.tenant

        return {"tenant": async_to_sync(read)()}

    response = _get(api, "/tenant/acme")
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": "acme"}


def test_a_template_reads_the_user_in_an_async_handler():
    template = engines["django"].from_string("{{ user.tenant }}")
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware])])

    @api.get("/tenant/{tenant}", auth=[_Auth(secret=SECRET)])
    async def endpoint(tenant: str, request: Request):
        return {"page": template.render({}, request)}

    response = _get(api, "/tenant/acme")
    assert response.status_code == 200, response.text
    assert response.json() == {"page": "acme"}


@pytest.mark.asyncio
async def test_concurrent_requests_on_the_shared_loop():
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware, _CallOnlyMiddleware])])

    @api.get("/tenant/{tenant}", auth=[_Auth(secret=SECRET)])
    async def endpoint(tenant: str, request: Request):
        await asyncio.sleep(0)
        return {"tenant": request.user.tenant}

    async with AsyncTestClient(api) as client:
        tenants = [f"t{i}" for i in range(20)]
        responses = await asyncio.wait_for(
            asyncio.gather(*[client.get(f"/tenant/{t}", headers=_headers()) for t in tenants]), timeout=20
        )
    assert [r.json() for r in responses] == [{"tenant": t} for t in tenants]


def test_a_sync_read_whose_loader_uses_async_to_sync_on_the_lane():
    """The loader runs on the lane while the loop waits for it.

    An ``async_to_sync`` inside the loader must not schedule its coroutine on
    that loop: the loop cannot run it, and the request would deadlock.
    """
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMiddleware])])

    async def resolve_name():
        await asyncio.sleep(0)
        return "bob"

    class _BridgeAuth(JWTAuthentication):
        def get_user_sync(self, user_id):
            # The loader itself runs on the lane and sees its thread-local state.
            tenant = getattr(_local, "tenant", "<unset>")
            return SimpleNamespace(username=async_to_sync(resolve_name)(), tenant=tenant)

    @api.get("/tenant/{tenant}", auth=[_BridgeAuth(secret=SECRET)])
    async def endpoint(tenant: str, request: Request):
        await asyncio.sleep(0)
        user = request.user
        return {"tenant": user.tenant, "username": user.username}

    response = _get(api, "/tenant/acme")
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": "acme", "username": "bob"}
