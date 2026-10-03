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
from asgiref.sync import async_to_sync, markcoroutinefunction, sync_to_async
from django.contrib.auth import alogin
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import User
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection
from django.db.backends.signals import connection_created
from django.http import HttpResponse
from django.template import engines
from django.utils.decorators import async_only_middleware
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Depends, Request, Router
from django_bolt.auth import JWTAuthentication
from django_bolt.concurrency import in_lane_mode, run_in_orm_executor, run_orm_blocking, sync_to_thread
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack, middleware
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
        loaded_on = request.user.loaded_on
        response = await self.get_response(request)
        response.headers["X-Middleware-Loaded-On"] = loaded_on
        return response


def _database_auth_token() -> str:
    return jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, _TEST_JWT_SECRET, algorithm="HS256")


@pytest.mark.django_db(transaction=True)
def test_a_sync_user_read_in_async_middleware_waits_for_the_lane():
    """A sync read on the event loop runs the query on the lane of the request.

    A query on another thread would miss the thread-local state of the Django
    middleware, and the handler on the lane would then use that user.
    """
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_NoOpDjangoMiddleware]), _ReadUserMiddleware])

    @api.get("/x", auth=[_DatabaseAuth(secret=_TEST_JWT_SECRET)])
    def endpoint(request: Request):
        return {"handler_on": threading.current_thread().name, "loaded_on": request.user.loaded_on}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/x", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert response.headers["x-middleware-loaded-on"] == body["handler_on"]
    assert body["loaded_on"] == body["handler_on"]


@pytest.mark.django_db(transaction=True)
def test_lazy_user_forced_on_a_lane_loads_on_that_lane():
    """A lazy user that a lane forces must not leave the lane for the ORM pool.

    This guards an invariant of ``run_orm_blocking``, not a change of this
    fix. The test is green on the code before the loader change too.
    """
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


class _ShieldCallOnlyMiddleware(MiddlewareMixin):
    """An async-only mixin subclass with no hooks. Its plain ``__call__`` returns a Future."""

    sync_capable = False
    async_capable = True

    def __call__(self, request):
        return asyncio.shield(self._respond(request))

    async def _respond(self, request):
        response = await super().__call__(request)
        response["X-Shielded"] = "yes"
        return response


class _AsyncOnlyHeaderMiddleware:
    """A plain async-only middleware that awaits ``get_response``."""

    sync_capable = False
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request):
        await asyncio.sleep(0)
        response = await self.get_response(request)
        response["X-Async-Only"] = "yes"
        return response


def _stack_with_async_only_api(middleware_classes: list) -> BoltAPI:
    api = BoltAPI(middleware=[DjangoMiddlewareStack(middleware_classes)])

    @api.get("/sync/{tenant}")
    def sync_route(tenant: str):
        return {"expected": tenant, "actual": _read_tenant()}

    @api.get("/async/{tenant}")
    async def async_route(tenant: str):
        await asyncio.sleep(0)
        return {"expected": tenant, "actual": await sync_to_thread(_read_tenant)}

    return api


@pytest.mark.parametrize(
    ("middleware_class", "header"),
    [(_ShieldCallOnlyMiddleware, "x-shielded"), (_AsyncOnlyHeaderMiddleware, "x-async-only")],
    ids=["mixin_future", "plain_async"],
)
def test_stack_awaits_an_async_only_middleware_that_calls_get_response(middleware_class, header):
    """A ``__call__``-only stack gives an async-only middleware an async ``get_response``."""
    api = _stack_with_async_only_api([middleware_class])

    with TestClient(api) as client:
        for path in ("/sync/acme", "/async/acme"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers[header] == "yes"


@pytest.mark.parametrize(
    ("middleware_class", "header"),
    [(_ShieldCallOnlyMiddleware, "x-shielded"), (_AsyncOnlyHeaderMiddleware, "x-async-only")],
    ids=["mixin_future", "plain_async"],
)
def test_mixed_stack_keeps_thread_affinity_through_an_async_only_middleware(middleware_class, header):
    """Hooks before an async-only layer and the handler after it share the request's lane."""
    api = _stack_with_async_only_api([_TenantMixinMiddleware, middleware_class])

    with TestClient(api) as client:
        for path in ("/sync/acme", "/async/acme"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers[header] == "yes"
            assert response.json() == {"expected": "acme", "actual": "acme"}


class _TenantDatabaseAuth(JWTAuthentication):
    """A backend whose user loader reads the tenant that a Django hook set on the lane."""

    queries = 0

    def get_user_sync(self, user_id):
        self.queries += 1
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return SimpleNamespace(username="bob", tenant=_read_tenant())


class _AwaitUserTenantMiddleware:
    """Async Python middleware that loads the user with ``await request.auser()``."""

    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request):
        # Read the tenant before the handler can force the user on the lane.
        tenant = (await request.auser()).tenant
        response = await self.get_response(request)
        response.headers["X-User-Tenant"] = tenant
        return response


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handler_is_async", [False, True], ids=["sync", "async"])
def test_auser_awaited_on_the_event_loop_loads_on_the_lane_of_its_request(handler_is_async):
    """The user query must see the thread-local state that the Django hook set on the lane.

    The handler then reads ``request.user`` with no second query.
    """
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware]), _AwaitUserTenantMiddleware])
    auth = _TenantDatabaseAuth(secret=_TEST_JWT_SECRET)

    if handler_is_async:

        @api.get("/tenant/{tenant}", auth=[auth])
        async def endpoint(tenant: str, request: Request):
            await asyncio.sleep(0)
            return {"expected": tenant, "handler_saw": request.user.tenant}
    else:

        @api.get("/tenant/{tenant}", auth=[auth])
        def endpoint(tenant: str, request: Request):
            return {"expected": tenant, "handler_saw": request.user.tenant}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    assert response.headers["x-user-tenant"] == "acme"
    assert response.json() == {"expected": "acme", "handler_saw": "acme"}
    assert auth.queries == 1


@pytest.mark.django_db(transaction=True)
def test_auser_awaits_an_async_get_user_on_the_thread_of_its_request():
    """``await request.auser()`` runs an async ``get_user`` as a coroutine of the request.

    The coroutine must not go to a pool thread with its own event loop. Its
    nested thread-sensitive work must reach the lane of the request.
    """

    class AsyncTenantAuth(JWTAuthentication):
        async def get_user(self, user_id, auth_context):
            await asyncio.sleep(0)
            tenant = await sync_to_async(_read_tenant, thread_sensitive=True)()
            return SimpleNamespace(tenant=tenant, loaded_on=threading.get_ident())

    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/tenant/{tenant}", auth=[AsyncTenantAuth(secret=_TEST_JWT_SECRET)])
    async def endpoint(tenant: str, request: Request):
        user = await request.auser()
        return {"tenant": user.tenant, "loaded_on": user.loaded_on, "handler_on": threading.get_ident()}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant"] == "acme"
    assert body["loaded_on"] == body["handler_on"]


def _load_tenant_user():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return SimpleNamespace(tenant=_read_tenant(), loaded_on=threading.get_ident())


class _AsyncOnlyTenantAuth(JWTAuthentication):
    """A backend with an async ``get_user`` only. Its query goes through one of two async bridges."""

    def __init__(self, orm_bridge: str, **kwargs):
        super().__init__(**kwargs)
        self.orm_bridge = orm_bridge

    async def get_user(self, user_id, auth_context):
        await asyncio.sleep(0)
        if self.orm_bridge == "async":
            return await run_in_orm_executor(_load_tenant_user)
        return await sync_to_async(_load_tenant_user, thread_sensitive=True)()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("orm_bridge", ["async", "asgiref"])
def test_auser_sends_the_orm_work_of_an_async_get_user_to_the_lane(orm_bridge):
    """The query of an awaited ``get_user`` must run on the lane, with either async bridge."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/tenant/{tenant}", auth=[_AsyncOnlyTenantAuth(orm_bridge, secret=_TEST_JWT_SECRET)])
    async def endpoint(tenant: str, request: Request):
        user = await request.auser()
        lane = await sync_to_thread(threading.get_ident)
        return {"tenant": user.tenant, "loaded_on": user.loaded_on, "lane": lane}

    with TestClient(api, share_db_connection=False) as client:
        for tenant in ("acme", "beta"):
            response = client.get(f"/tenant/{tenant}", headers={"Authorization": f"Bearer {_database_auth_token()}"})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["tenant"] == tenant
            assert body["loaded_on"] == body["lane"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handler_is_async", [False, True], ids=["sync_handler", "async_handler"])
def test_sync_access_to_the_user_of_an_async_only_get_user_raises(handler_is_async):
    """Sync ``request.user`` cannot drive an async ``get_user``. Bolt raises and names ``auser``."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])
    auth = _AsyncOnlyTenantAuth("async", secret=_TEST_JWT_SECRET)

    def force_user(request):
        try:
            tenant = request.user.tenant
        except RuntimeError as exc:
            return {"error": str(exc), "lane_mode": in_lane_mode()}
        return {"error": None, "tenant": tenant}

    if handler_is_async:

        @api.get("/tenant/{tenant}", auth=[auth])
        async def endpoint(tenant: str, request: Request):
            return await sync_to_thread(force_user, request)
    else:

        @api.get("/tenant/{tenant}", auth=[auth])
        def endpoint(tenant: str, request: Request):
            return force_user(request)

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert "request.auser" in body["error"]
    assert body["lane_mode"] is (not handler_is_async)


@pytest.mark.django_db(transaction=True)
def test_sync_user_loader_with_an_async_bridge_completes_when_forced_on_the_event_loop():
    """A lazy user forced on the event loop blocks that loop until the query returns.

    A request with no Django middleware has no lane, so the query runs on the
    thread of the loop. A loader that calls ``async_to_sync`` there must get a
    loop of its own and complete. Its thread-sensitive work comes back to the
    thread that forced the user.
    """

    def load_user():
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return SimpleNamespace(loaded_on=threading.current_thread().name)

    async def async_user():
        await asyncio.sleep(0)
        return await sync_to_async(load_user, thread_sensitive=True)()

    class SyncBridgeAuth(JWTAuthentication):
        def get_user_sync(self, user_id):
            return async_to_sync(async_user)()

    api = BoltAPI()

    @api.get("/tenant/{tenant}", auth=[SyncBridgeAuth(secret=_TEST_JWT_SECRET)])
    async def endpoint(tenant: str, request: Request):
        return {"loaded_on": request.user.loaded_on, "forced_on": threading.current_thread().name}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["loaded_on"] == body["forced_on"]


@pytest.mark.django_db(transaction=True)
def test_a_sync_user_read_in_an_async_handler_loads_on_the_lane():
    """An async handler behind Django middleware must not load its user off the lane."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])
    auth = _TenantDatabaseAuth(secret=_TEST_JWT_SECRET)

    @api.get("/tenant/{tenant}", auth=[auth])
    async def endpoint(tenant: str, request: Request):
        return {"tenant": request.user.tenant, "again": (await request.auser()).tenant}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    assert response.json() == {"tenant": "acme", "again": "acme"}
    assert auth.queries == 1


@pytest.mark.django_db(transaction=True)
def test_a_sync_user_read_under_async_to_sync_on_a_lane_loads_on_the_lane():
    """``async_to_sync`` in a lane handler runs its coroutine on a loop away from the lane.

    The lane waits for that loop, so the query goes through the
    ``CurrentThreadExecutor`` of the lane.
    """
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])

    @api.get("/tenant/{tenant}", auth=[_TenantDatabaseAuth(secret=_TEST_JWT_SECRET)])
    def endpoint(tenant: str, request: Request):
        async def read_sync():
            return request.user.tenant

        async def read_async():
            return (await request.auser()).tenant

        return {"sync": async_to_sync(read_sync)(), "async": async_to_sync(read_async)()}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    assert response.json() == {"sync": "acme", "async": "acme"}


class _NeitherCapableMiddleware:
    sync_capable = False
    async_capable = False

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


@pytest.mark.parametrize(
    "wrap",
    [lambda: DjangoMiddlewareStack([_NeitherCapableMiddleware]), lambda: DjangoMiddleware(_NeitherCapableMiddleware)],
    ids=["stack", "single_wrapper"],
)
def test_a_middleware_with_no_capability_is_rejected(wrap):
    """Django rejects such a middleware at load. Bolt must not run it."""
    with pytest.raises(RuntimeError, match="sync_capable"):
        wrap()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("level", ["route", "router"])
@pytest.mark.parametrize(
    "wrap",
    [lambda: DjangoMiddleware(_TenantMixinMiddleware), lambda: DjangoMiddlewareStack([_TenantMixinMiddleware])],
    ids=["single", "stack"],
)
def test_sync_handler_behind_route_level_django_middleware_runs_on_the_lane(level, wrap):
    """Django middleware on a route or a router makes a sync handler blocking, as on the API.

    The handler has no ORM call of its own, so the handler analysis alone
    marks it non-blocking. It must still run on the lane of its request: its
    lazy user must load there and see the thread-local state of the hook.
    """
    api = BoltAPI()
    auth = _TenantDatabaseAuth(secret=_TEST_JWT_SECRET)

    def endpoint(tenant: str, request: Request):
        return {"expected": tenant, "actual": _read_tenant(), "user_tenant": request.user.tenant}

    if level == "route":
        api.get("/tenant/{tenant}", auth=[auth])(middleware(wrap())(endpoint))
    else:
        router = Router(prefix="/r", middleware=[wrap()])
        router.get("/tenant/{tenant}", auth=[auth])(endpoint)
        api.include_router(router)

    path = "/tenant/acme" if level == "route" else "/r/tenant/acme"
    with TestClient(api, share_db_connection=False) as client:
        response = client.get(path, headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    assert response.json() == {"expected": "acme", "actual": "acme", "user_tenant": "acme"}


class _SyncOnlyHeaderMiddleware:
    """A plain sync middleware. Django gives it a sync ``get_response``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Sync-Only"] = _read_tenant()
        return response


def _sync_only_header_factory(get_response):
    def middleware(request):
        response = get_response(request)
        response["X-Sync-Only"] = _read_tenant()
        return response

    return middleware


@pytest.mark.parametrize(
    "middleware_class",
    [_SyncOnlyHeaderMiddleware, f"{__name__}._sync_only_header_factory"],
    ids=["class", "factory"],
)
def test_single_wrapper_gives_a_sync_only_middleware_a_sync_get_response(middleware_class):
    """A sync-only middleware must get a response from ``get_response``, not a coroutine.

    The async handler forces the event loop path. The middleware runs on the
    lane of the request, after the hook that set the tenant.
    """
    api = BoltAPI(middleware=[DjangoMiddleware(_TenantMixinMiddleware), DjangoMiddleware(middleware_class)])

    @api.get("/tenant/{tenant}")
    async def endpoint(tenant: str):
        await asyncio.sleep(0)
        return {"tenant": tenant}

    @api.get("/sync/{tenant}")
    def sync_endpoint(tenant: str):
        return {"tenant": tenant}

    with TestClient(api) as client:
        for path in ("/tenant/acme", "/sync/acme"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == {"tenant": "acme"}
            assert response.headers["x-sync-only"] == "acme"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("hop", ["to_thread", "sync_to_async_unsafe"])
def test_a_sync_user_read_on_another_thread_of_a_lane_request_raises(hop):
    """A thread with no event loop that is not the lane also misses the state of the lane.

    ``asyncio.to_thread`` copies the context of the request to such a thread.
    The read must raise and cache nothing, so ``auser()`` then loads on the lane.
    """
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])
    auth = _TenantDatabaseAuth(secret=_TEST_JWT_SECRET)

    def read_user(request):
        try:
            return request.user.tenant
        except RuntimeError as exc:
            return str(exc)

    @api.get("/tenant/{tenant}", auth=[auth])
    async def endpoint(tenant: str, request: Request):
        if hop == "to_thread":
            seen = await asyncio.to_thread(read_user, request)
        else:
            seen = await sync_to_async(read_user, thread_sensitive=False)(request)
        return {"seen": seen, "tenant": (await request.auser()).tenant}

    with TestClient(api, share_db_connection=False) as client:
        response = client.get("/tenant/acme", headers={"Authorization": f"Bearer {_database_auth_token()}"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert "await request.auser()" in body["seen"]
    assert body["tenant"] == "acme"
    assert auth.queries == 1


@pytest.mark.django_db(transaction=True)
def test_a_template_reads_the_user_in_an_async_handler():
    """The auth context processor gives a template ``request.user``.

    In an async handler behind Django middleware, the template reads the user
    that ``await request.auser()`` loaded. Without that, its read waits for
    the lane and loads the user there.
    """
    template = engines["django"].from_string("{{ user.tenant }}")
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TenantMixinMiddleware])])
    auth = _TenantDatabaseAuth(secret=_TEST_JWT_SECRET)

    @api.get("/loaded/{tenant}", auth=[auth])
    async def loaded(tenant: str, request: Request):
        await request.auser()
        return {"page": template.render({}, request)}

    @api.get("/lazy/{tenant}", auth=[auth])
    async def lazy(tenant: str, request: Request):
        return {"page": template.render({}, request)}

    headers = {"Authorization": f"Bearer {_database_auth_token()}"}
    with TestClient(api, share_db_connection=False) as client:
        assert client.get("/loaded/acme", headers=headers).json() == {"page": "acme"}
        assert client.get("/lazy/acme", headers=headers).json() == {"page": "acme"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("wrapper", ["stack", "single"])
def test_a_sync_read_of_the_session_user_in_an_async_handler_runs_on_the_lane(wrapper):
    """The lazy user of ``AuthenticationMiddleware`` loads with a sync read on the event loop.

    Django's lazy object would run the session query on the loop thread and
    raise ``SynchronousOnlyOperation``. Bolt routes the read through
    ``run_orm_blocking``, so it runs on the lane of the request.
    """
    if wrapper == "stack":
        middleware = [DjangoMiddlewareStack([SessionMiddleware, AuthenticationMiddleware])]
    else:
        middleware = [DjangoMiddleware(SessionMiddleware), DjangoMiddleware(AuthenticationMiddleware)]
    api = BoltAPI(middleware=middleware)

    @api.post("/login")
    async def login(request: Request):
        await alogin(request, await User.objects.aget(username="session_reader"))
        return {"ok": True}

    @api.get("/me")
    async def me(request: Request):
        await asyncio.sleep(0)
        return {
            "username": request.user.username,
            "authenticated": request.user.is_authenticated,
            # Django caches the sync and the async user apart. They are one user.
            "same": (await request.auser()) == request.user,
        }

    User.objects.create_user(username="session_reader", password="pw-for-tests")
    with TestClient(api) as client:
        assert client.post("/login").status_code == 200
        response = client.get("/me")

    assert response.status_code == 200, response.text
    assert response.json() == {"username": "session_reader", "authenticated": True, "same": True}
