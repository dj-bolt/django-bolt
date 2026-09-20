"""Regression tests for request-affine Django middleware execution."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import Annotated

import pytest
from asgiref.sync import async_to_sync, markcoroutinefunction
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.utils.decorators import async_only_middleware
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Depends, Request
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
