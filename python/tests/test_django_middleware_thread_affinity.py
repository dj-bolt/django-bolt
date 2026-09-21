"""Regression tests for request-affine Django middleware execution."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from types import SimpleNamespace
from typing import Annotated

import jwt
import pytest
from asgiref.sync import async_to_sync, markcoroutinefunction
from django.contrib.auth.models import User
from django.db import connection
from django.db.backends.signals import connection_created
from django.http import HttpResponse
from django.utils.decorators import async_only_middleware
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Depends, Request
from django_bolt.auth import JWTAuthentication
from django_bolt.concurrency import in_lane_mode, run_in_orm_executor, run_orm_blocking, sync_to_thread
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack
from django_bolt.testing import AsyncTestClient, TestClient


@pytest.mark.django_db(transaction=True)
def test_django_hook_threadlocal_is_isolated_between_concurrent_async_requests():
    """A hook's thread-local state must stay with its async request."""
    local = threading.local()
    arrived = 0
    release_reads = asyncio.Event()

    class TenantLikeMiddleware:
        def __init__(self, get_response):
            self.get_response = get_response

        def process_request(self, request):
            local.tenant = request.path.rsplit("/", 1)[-1]

    api = BoltAPI(middleware=[DjangoMiddlewareStack([TenantLikeMiddleware])])

    @api.get("/tenant/{tenant}")
    async def tenant(tenant: str, request: Request):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            release_reads.set()
        await release_reads.wait()
        return {
            "expected": tenant,
            "actual": await run_in_orm_executor(lambda: local.tenant),
        }

    async def run_requests():
        async with AsyncTestClient(api) as client:
            return await asyncio.gather(client.get("/tenant/acme"), client.get("/tenant/beta"))

    responses = asyncio.run(run_requests())
    payloads = [response.json() for response in responses]

    assert [response.status_code for response in responses] == [200, 200]
    assert [{"expected": body["expected"], "actual": body["actual"]} for body in payloads] == [
        {"expected": "acme", "actual": "acme"},
        {"expected": "beta", "actual": "beta"},
    ]


_local = threading.local()


class _TenantMixinMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _local.tenant = request.path.rsplit("/", 1)[-1]


def _read_tenant() -> str:
    return getattr(_local, "tenant", "<unset>")


def _sync_orm_api(middleware: list) -> BoltAPI:
    api = BoltAPI(middleware=middleware)

    @api.get("/tenant/{tenant}")
    def tenant(tenant: str):
        User.objects.count()
        return {"expected": tenant, "actual": _read_tenant()}

    return api


def _sync_to_thread_api(middleware: list) -> BoltAPI:
    api = BoltAPI(middleware=middleware)

    @api.get("/tenant/{tenant}")
    async def tenant(tenant: str):
        await asyncio.sleep(0.01)
        return {"expected": tenant, "actual": await sync_to_thread(_read_tenant)}

    return api


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("build_api", [_sync_orm_api, _sync_to_thread_api])
@pytest.mark.parametrize(
    "middleware",
    [
        lambda: [DjangoMiddlewareStack([_TenantMixinMiddleware])],
        lambda: [DjangoMiddleware(_TenantMixinMiddleware)],
    ],
    ids=["stack", "single_wrapper"],
)
def test_threadlocal_follows_each_concurrent_request(build_api, middleware):
    """Each thread hand-off must use the thread that ran the Django middleware."""
    api = build_api(middleware())

    async def run_requests():
        async with AsyncTestClient(api) as client:
            return await asyncio.gather(*[client.get(f"/tenant/t{i}") for i in range(8)])

    payloads = [response.json() for response in asyncio.run(run_requests())]

    assert payloads == [{"expected": f"t{i}", "actual": f"t{i}"} for i in range(8)]


_lane_local = threading.local()


def _count_on_this_thread() -> int:
    _lane_local.count = getattr(_lane_local, "count", 0) + 1
    return _lane_local.count


def test_sync_handler_runs_the_complete_request_on_one_lane():
    """The middleware hook and the sync handler share one lane thread."""
    seen = {}

    class RecordThread(MiddlewareMixin):
        def process_request(self, request):
            seen["hook"] = threading.get_ident()

    api = BoltAPI(middleware=[DjangoMiddlewareStack([RecordThread])])

    @api.get("/lane")
    def lane():
        return {"on_lane": in_lane_mode(), "same_thread": seen["hook"] == threading.get_ident()}

    with TestClient(api) as client:
        assert client.get("/lane").json() == {"on_lane": True, "same_thread": True}


def test_sync_requests_reuse_a_lane_thread():
    """A lane stays alive between requests, so its thread-local state persists."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/count")
    def count():
        return {"count": _count_on_this_thread()}

    with TestClient(api) as client:
        counts = [client.get("/count").json()["count"] for _ in range(3)]

    assert counts[-1] > 1


def test_async_requests_reuse_a_lane_thread():
    """The sync work of an async handler goes to a lane that later requests reuse."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/count")
    async def count():
        return {"count": await sync_to_thread(_count_on_this_thread)}

    with TestClient(api) as client:
        counts = [client.get("/count").json()["count"] for _ in range(3)]

    assert counts[-1] > 1


async def _async_dependency() -> str:
    await asyncio.sleep(0)
    return "dep"


@pytest.mark.django_db(transaction=True)
def test_sync_handler_with_an_await_in_its_flow_does_not_use_lane_dispatch():
    """A flow that can suspend stays on the event loop and keeps its thread-local state."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/tenant/{tenant}")
    def tenant(tenant: str, dep: Annotated[str, Depends(_async_dependency)]):
        User.objects.count()
        return {"expected": tenant, "actual": _read_tenant(), "dep": dep}

    with TestClient(api) as client:
        assert client.get("/tenant/acme").json() == {"expected": "acme", "actual": "acme", "dep": "dep"}


class _CallOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


def test_async_request_works_on_a_lane_that_served_a_sync_request():
    """Lane mode must end with the sync request. The next owner of the lane can suspend."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_CallOnlyMiddleware])])

    @api.get("/sync")
    def sync_route():
        return {"on_lane": in_lane_mode()}

    @api.get("/async")
    async def async_route():
        await asyncio.sleep(0.01)
        return {"lane_mode_in_sync_work": await sync_to_thread(in_lane_mode)}

    with TestClient(api) as client:
        assert client.get("/sync").json() == {"on_lane": True}
        response = client.get("/async")

    assert response.status_code == 200
    assert response.json() == {"lane_mode_in_sync_work": False}


_request_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_var", default="<unset>")


def test_context_variable_of_one_lane_request_does_not_reach_the_next():
    """Each complete request on a lane runs in its own context."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/var/{value}")
    def set_var(value: str):
        before = _request_var.get()
        _request_var.set(value)
        return {"before": before}

    with TestClient(api) as client:
        seen = [client.get(f"/var/v{i}").json()["before"] for i in range(3)]

    assert seen == ["<unset>", "<unset>", "<unset>"]


class _BlockingOrmInHookMiddleware(MiddlewareMixin):
    """A hook that runs blocking ORM work, as a forced ``request.user`` does."""

    def process_request(self, request):
        _local.tenant = request.path.rsplit("/", 1)[-1]
        request.seen_by_orm_call = run_orm_blocking(_read_tenant)


def test_blocking_orm_call_in_a_hook_of_an_async_request_stays_on_the_lane():
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_BlockingOrmInHookMiddleware])])

    @api.get("/tenant/{tenant}")
    async def tenant(tenant: str, request: Request):
        await asyncio.sleep(0)
        return {"expected": tenant, "actual": request.state["seen_by_orm_call"]}

    with TestClient(api) as client:
        assert client.get("/tenant/acme").json() == {"expected": "acme", "actual": "acme"}


def test_sync_handler_behind_single_wrappers_runs_on_one_lane():
    """Chained DjangoMiddleware wrappers use lane dispatch, as a stack does."""
    seen = {}

    class RecordThread(MiddlewareMixin):
        def process_request(self, request):
            seen["hook"] = threading.get_ident()

        def process_response(self, request, response):
            response["X-Hook-Thread"] = str(threading.get_ident())
            return response

    api = BoltAPI(middleware=[DjangoMiddleware(_TenantMixinMiddleware), DjangoMiddleware(RecordThread)])

    @api.get("/tenant/{tenant}")
    def tenant(tenant: str):
        return {
            "on_lane": in_lane_mode(),
            "same_thread": seen["hook"] == threading.get_ident(),
            "actual": _read_tenant(),
        }

    with TestClient(api) as client:
        response = client.get("/tenant/acme")

    assert response.json() == {"on_lane": True, "same_thread": True, "actual": "acme"}
    assert response.headers["X-Hook-Thread"] == str(seen["hook"])


def test_sync_handler_with_no_orm_call_runs_on_the_lane_of_an_async_flow():
    """A flow that cannot use lane dispatch still runs its sync handler on the request's lane."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/tenant/{tenant}")
    def tenant(tenant: str, dep: Annotated[str, Depends(_async_dependency)]):
        return {"expected": tenant, "actual": _read_tenant()}

    with TestClient(api) as client:
        assert client.get("/tenant/acme").json() == {"expected": "acme", "actual": "acme"}


class _ForbidInCallMiddleware(MiddlewareMixin):
    def __call__(self, request):
        return HttpResponse("forbidden", status=403)


def test_single_wrapper_keeps_a_call_override_of_a_mixin_subclass():
    """Direct hook calls on a lane must not skip a custom ``__call__``."""
    api = BoltAPI(middleware=[DjangoMiddleware(_ForbidInCallMiddleware)])

    @api.get("/guarded")
    def guarded():
        return {"reached": True}

    with TestClient(api) as client:
        assert client.get("/guarded").status_code == 403


async def _read_tenant_in_orm_executor() -> str:
    return await run_in_orm_executor(_read_tenant)


def test_async_bridge_in_a_sync_handler_keeps_the_request_thread():
    """``async_to_sync`` starts a new thread. Its ORM work must return to the lane."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/tenant/{tenant}")
    def tenant(tenant: str):
        return {"actual": async_to_sync(_read_tenant_in_orm_executor)()}

    with TestClient(api) as client:
        assert client.get("/tenant/acme").json() == {"actual": "acme"}


class _ForbidInAcallMiddleware(MiddlewareMixin):
    async def __acall__(self, request):
        return HttpResponse("forbidden", status=403)


def test_single_wrapper_keeps_an_acall_override_of_a_mixin_subclass():
    api = BoltAPI(middleware=[DjangoMiddleware(_ForbidInAcallMiddleware)])

    @api.get("/guarded")
    def guarded():
        return {"reached": True}

    with TestClient(api) as client:
        assert client.get("/guarded").status_code == 403


class _DelegatingCallMiddleware(MiddlewareMixin):
    def __call__(self, request):
        return super().__call__(request)

    def process_response(self, request, response):
        response["X-Delegated"] = "yes"
        return response


def test_single_wrapper_runs_a_call_override_that_delegates_to_the_mixin():
    api = BoltAPI(middleware=[DjangoMiddleware(_DelegatingCallMiddleware)])

    @api.get("/sync")
    def sync_route():
        return {"ok": True}

    @api.get("/async")
    async def async_route():
        await asyncio.sleep(0)
        return {"ok": True}

    with TestClient(api) as client:
        for path in ("/sync", "/async"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["x-delegated"] == "yes"


class _AwaitingForbidMiddleware:
    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request):
        await asyncio.sleep(0)
        return HttpResponse("forbidden", status=403)


def test_stack_with_a_middleware_that_awaits_does_not_use_lane_dispatch():
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware, _AwaitingForbidMiddleware])])

    @api.get("/guarded")
    def guarded():
        return {"reached": True}

    with TestClient(api) as client:
        assert client.get("/guarded").status_code == 403


@async_only_middleware
def _awaiting_forbid_factory(get_response):
    async def middleware(request):
        await asyncio.sleep(0)
        return HttpResponse("forbidden", status=403)

    return middleware


def test_stack_with_an_async_only_factory_does_not_use_lane_dispatch():
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware, _awaiting_forbid_factory])])

    @api.get("/guarded")
    def guarded():
        return {"reached": True}

    with TestClient(api) as client:
        assert client.get("/guarded").status_code == 403


class _AsyncCallMixinMiddleware(MiddlewareMixin):
    async def __call__(self, request):
        await asyncio.sleep(0)
        return HttpResponse("forbidden", status=403)


def _async_call_mixin_factory(get_response):
    return _AsyncCallMixinMiddleware(get_response)


def test_single_wrapper_inspects_the_instance_that_a_factory_returns():
    api = BoltAPI(middleware=[DjangoMiddleware(f"{__name__}._async_call_mixin_factory")])

    @api.get("/guarded")
    def guarded():
        return {"reached": True}

    with TestClient(api) as client:
        assert client.get("/guarded").status_code == 403


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handler_is_async", [False, True], ids=["sync", "async"])
def test_test_client_exit_closes_the_connections_of_its_lanes(handler_is_async):
    """An open lane connection blocks the drop of the test database at teardown."""
    lane_connections = []

    def track(sender, connection, **kwargs):
        lane_connections.append(connection)

    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    if handler_is_async:

        @api.get("/users")
        async def users():
            await asyncio.sleep(0)
            return {"count": await sync_to_thread(User.objects.count)}
    else:

        @api.get("/users")
        def users():
            return {"count": User.objects.count()}

    connection_created.connect(track)
    try:
        with TestClient(api) as client:
            assert client.get("/users").json() == {"count": 0}
            assert lane_connections
    finally:
        connection_created.disconnect(track)

    assert [conn.connection for conn in lane_connections] == [None] * len(lane_connections)


@pytest.mark.django_db(transaction=True)
def test_async_test_client_exit_closes_the_connections_of_its_lanes():
    lane_connections = []

    def track(sender, connection, **kwargs):
        lane_connections.append(connection)

    api = _sync_to_thread_api([DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/users")
    async def users():
        return {"count": await sync_to_thread(User.objects.count)}

    async def run_request():
        async with AsyncTestClient(api) as client:
            return (await client.get("/users")).json()

    connection_created.connect(track)
    try:
        assert asyncio.run(run_request()) == {"count": 0}
    finally:
        connection_created.disconnect(track)

    assert lane_connections
    assert [conn.connection for conn in lane_connections] == [None] * len(lane_connections)


class _ShieldMiddleware(MiddlewareMixin):
    """An async-only mixin subclass with a plain ``def __call__`` that returns a Future."""

    sync_capable = False
    async_capable = True

    def __call__(self, request):
        return asyncio.shield(super().__call__(request))

    def process_response(self, request, response):
        response["X-Shielded"] = "yes"
        return response


def test_single_wrapper_awaits_an_async_only_call_override_on_the_event_loop():
    """A ``sync_capable=False`` middleware needs the loop, even with a sync ``__call__``."""
    api = BoltAPI(middleware=[DjangoMiddleware(_ShieldMiddleware)])

    @api.get("/x")
    async def endpoint():
        await asyncio.sleep(0)
        return {"ok": True}

    @api.get("/sync")
    def sync_endpoint():
        return {"ok": True}

    with TestClient(api) as client:
        for path in ("/x", "/sync"):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert response.json() == {"ok": True}
            assert response.headers["x-shielded"] == "yes"


_TEST_JWT_SECRET = "test-only-secret-longer-than-32-characters"


class _DatabaseAuth(JWTAuthentication):
    """A backend whose sync user loader runs a raw database query."""

    def get_user_sync(self, user_id):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return SimpleNamespace(username="bob", loaded_on=threading.current_thread().name)


class _NoOpDjangoMiddleware(MiddlewareMixin):
    def process_request(self, request):
        pass


class _ReadUserMiddleware:
    """Async Python middleware that forces the lazy user on the event loop."""

    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request):
        username = request.user.username
        response = await self.get_response(request)
        response.headers["X-Middleware-User"] = username
        return response


def _database_auth_token() -> str:
    return jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, _TEST_JWT_SECRET, algorithm="HS256")


@pytest.mark.django_db(transaction=True)
def test_async_middleware_forces_the_lazy_user_of_a_sync_handler_off_the_event_loop():
    """A sync handler behind Django middleware runs on a lane. Its lazy user can still be forced on the loop."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_NoOpDjangoMiddleware]), _ReadUserMiddleware])

    @api.get("/x", auth=[_DatabaseAuth(secret=_TEST_JWT_SECRET)])
    def endpoint(request: Request):
        return {"ok": True, "username": request.user.username}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/x", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "username": "bob"}
    assert response.headers["x-middleware-user"] == "bob"


@pytest.mark.django_db(transaction=True)
def test_lazy_user_forced_on_a_lane_loads_on_that_lane():
    """A lazy user that a lane forces must not leave the lane for the ORM pool."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_NoOpDjangoMiddleware])])

    @api.get("/x", auth=[_DatabaseAuth(secret=_TEST_JWT_SECRET)])
    def endpoint(request: Request):
        user = request.user
        return {"handler_thread": threading.current_thread().name, "loaded_on": user.loaded_on}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/x", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["loaded_on"] == body["handler_thread"]
