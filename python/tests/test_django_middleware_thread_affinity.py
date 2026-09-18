"""Regression tests for request-affine Django middleware execution."""

from __future__ import annotations

import asyncio
import threading

import pytest

from django_bolt import BoltAPI, Request
from django_bolt.concurrency import run_in_orm_executor
from django_bolt.middleware import DjangoMiddlewareStack
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
