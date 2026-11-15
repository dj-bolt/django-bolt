"""Contextvar propagation across Bolt's bounded executor helpers.

Request-scoped state (e.g. a tenant-aware database router selecting the
database per request) lives in ContextVars. Executor hops must carry the
caller's context into the worker thread, or ORM evaluation runs without the
tenant/database selection established by the request.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from django_bolt import BoltAPI
from django_bolt.concurrency import run_in_orm_executor, sync_to_thread
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack
from django_bolt.testing import TestClient

_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant", default=None)


def test_run_in_orm_executor_propagates_contextvars():
    async def main():
        _tenant.set("acme")
        return await run_in_orm_executor(_tenant.get)

    assert asyncio.run(main()) == "acme"


class _PassMiddleware:
    """A Django middleware that changes nothing."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


def _describe_call(first: int, second: int, third: int = 0) -> dict:
    return {"arguments": [first, second, third], "thread": threading.current_thread().name}


@pytest.mark.parametrize(
    ("middleware", "pools"),
    [
        (list, True),
        (lambda: [DjangoMiddlewareStack([_PassMiddleware])], False),
        (lambda: [DjangoMiddleware(_PassMiddleware)], False),
    ],
    ids=["no-django-middleware", "middleware-stack", "single-middleware"],
)
def test_thread_helpers_pass_the_arguments_and_use_the_correct_thread(middleware, pools):
    """Each helper gives its arguments to the callable.

    With no Django middleware, ``sync_to_thread`` uses the default pool and
    ``run_in_orm_executor`` uses the ORM pool. Behind a Django middleware, the
    two calls run on the one thread of the request.
    """
    api = BoltAPI(middleware=middleware())

    @api.get("/calls")
    async def calls():
        return {
            "thread": await sync_to_thread(_describe_call, 1, 2, third=3),
            "positional": await sync_to_thread(_describe_call, 6, 7),
            "orm": await run_in_orm_executor(_describe_call, 4, 5),
            "handler_thread": threading.current_thread().name,
        }

    with TestClient(api) as client:
        payload = client.get("/calls").json()

    assert payload["thread"]["arguments"] == [1, 2, 3]
    assert payload["positional"]["arguments"] == [6, 7, 0]
    assert payload["orm"]["arguments"] == [4, 5, 0]
    if pools:
        assert payload["thread"]["thread"].startswith("bolt_default")
        assert payload["positional"]["thread"].startswith("bolt_default")
        assert payload["orm"]["thread"].startswith("bolt_orm")
    else:
        assert payload["thread"]["thread"] == payload["orm"]["thread"]
        assert payload["thread"]["thread"] != payload["handler_thread"]
        assert not payload["thread"]["thread"].startswith("bolt_")


def test_sync_to_thread_propagates_contextvars():
    def read_with_suffix(suffix: str) -> str:
        return f"{_tenant.get()}-{suffix}"

    async def main():
        _tenant.set("globex")
        positional = await sync_to_thread(_tenant.get)
        with_kwargs = await sync_to_thread(read_with_suffix, suffix="kw")
        return positional, with_kwargs

    assert asyncio.run(main()) == ("globex", "globex-kw")
