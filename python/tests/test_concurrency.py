"""Contextvar propagation across Bolt's bounded executor helpers.

Request-scoped state (e.g. a tenant-aware database router selecting the
database per request) lives in ContextVars. Executor hops must carry the
caller's context into the worker thread, or ORM evaluation runs without the
tenant/database selection established by the request.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
import time
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_bolt import BoltAPI
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.concurrency import check_pool_environment, run_in_orm_executor, run_orm_blocking, sync_to_thread
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


class _KeyUsers(APIKeyAuthentication):
    """An API key backend with a sync ``get_user``, so ``request.user`` needs no database."""

    def get_user(self, user_id, auth_context):
        return SimpleNamespace(name=f"user of {user_id}")


@pytest.mark.parametrize(
    "middleware",
    [list, lambda: [DjangoMiddlewareStack([_PassMiddleware])]],
    ids=["no-django-middleware", "middleware-stack"],
)
def test_a_sync_read_of_request_user_keeps_the_running_loop(middleware):
    """An async handler can await after a sync read of ``request.user``.

    The read blocks the event loop, and Bolt hides the running loop from the
    thread for that time. The handler gets the loop back after the read.
    """
    api = BoltAPI(middleware=middleware())

    @api.get("/me", auth=[_KeyUsers(api_keys={"secret"})], guards=[IsAuthenticated()])
    async def me(request):
        name = request.user.name
        loop = asyncio.get_running_loop()
        await asyncio.sleep(0)
        return {"user": name, "same_loop": asyncio.get_running_loop() is loop}

    with TestClient(api) as client:
        response = client.get("/me", headers={"X-API-Key": "secret"})

    assert response.status_code == 200, response.text
    assert response.json() == {"user": "user of apikey:secret", "same_loop": True}


def test_a_sync_read_on_a_loop_can_run_a_loop_of_its_own():
    """A sync read can run a second event loop on the thread, and the outer task stays the current task.

    Python 3.14 keeps the current task for each thread. While the read hides
    the running loop, it must also hide the current task. Else the inner loop
    cannot start its task, and it waits with no end.
    """
    results: list[object] = []

    def blocking(fn):
        async def call():
            outer_task = asyncio.current_task()
            value = run_orm_blocking(fn)
            return value, asyncio.current_task() is outer_task

        return asyncio.run(call())

    def inner() -> int:
        return 1

    def outer() -> int:
        value, same_task = blocking(inner)
        return value + same_task

    def run() -> None:
        results.append(blocking(outer))

    # A thread with a time limit, so that a hang fails the test and does not stop the suite.
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "the nested event loop did not finish"
    assert results == [(2, True)]


def _slow_thread_name() -> str:
    time.sleep(0.05)
    return threading.current_thread().name


def _documented_pool_size(pool: str) -> int:
    """The thread count that the settings reference gives for a pool of this process."""
    variable = {"orm": "DJANGO_BOLT_ORM_THREADS", "default": "DJANGO_BOLT_EXECUTOR_THREADS"}[pool]
    if os.environ.get(variable, "").strip():
        return int(os.environ[variable])
    if pool == "default":
        return min(32, (os.cpu_count() or 1) + 4)
    return 1 if all("sqlite" in database["ENGINE"] for database in settings.DATABASES.values()) else 4


@pytest.mark.parametrize(
    ("pool", "helper", "thread_prefix"),
    [("orm", run_in_orm_executor, "bolt_orm"), ("default", sync_to_thread, "bolt_default")],
)
def test_each_pool_has_its_documented_thread_count(pool, helper, thread_prefix):
    """More blocking calls than threads at the same time use each thread of the pool, and no more."""
    size = _documented_pool_size(pool)

    async def main():
        return await asyncio.gather(*(helper(_slow_thread_name) for _ in range(size + 4)))

    names = set(asyncio.run(main()))

    assert len(names) == size
    assert all(name.startswith(thread_prefix) for name in names)


def test_sync_to_thread_propagates_contextvars():
    def read_with_suffix(suffix: str) -> str:
        return f"{_tenant.get()}-{suffix}"

    async def main():
        _tenant.set("globex")
        positional = await sync_to_thread(_tenant.get)
        with_kwargs = await sync_to_thread(read_with_suffix, suffix="kw")
        return positional, with_kwargs

    assert asyncio.run(main()) == ("globex", "globex-kw")


@pytest.mark.parametrize("variable", ["DJANGO_BOLT_EXECUTOR_THREADS", "DJANGO_BOLT_ORM_THREADS"])
def test_check_pool_environment_refuses_an_invalid_thread_count(monkeypatch, variable):
    """The server calls ``check_pool_environment`` at startup, so an invalid value stops startup.

    The pools read the variables at their first use. Without this check, the
    first request that needs the pool fails.
    """
    for name in ("DJANGO_BOLT_EXECUTOR_THREADS", "DJANGO_BOLT_ORM_THREADS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "0")

    with pytest.raises(ImproperlyConfigured, match=rf"^{variable} must be an int of 1 or more, got '0'\.$"):
        check_pool_environment()
