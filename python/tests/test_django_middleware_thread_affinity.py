"""Regression tests for request-affine Django middleware execution."""

from __future__ import annotations

import asyncio
import threading

import pytest
from django.contrib.auth.models import User
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request
from django_bolt.concurrency import run_in_orm_executor, sync_to_thread
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack
from django_bolt.testing import AsyncTestClient


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
