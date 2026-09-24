"""A warning at registration for a sync ``request.user`` read that raises at request time.

In an async handler on a route with Django middleware, a sync read of
``request.user`` raises ``RuntimeError``. Bolt finds such a read in the source
of the handler when the route is registered, so the developer sees it at
startup and not on the first request.
"""

from __future__ import annotations

import inspect
import warnings

import pytest
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request, Router
from django_bolt.concurrency import sync_to_thread
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack, middleware


class _NoOpMiddleware(MiddlewareMixin):
    def process_request(self, request):
        pass


def _register(api: BoltAPI, handler, path: str = "/x") -> list[warnings.WarningMessage]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        api.get(path)(handler)
    return [w for w in caught if "request.user" in str(w.message)]


def _make_reads_user():
    # A new function per test: @middleware adds route middleware to the function itself.
    async def reads_user(request: Request):
        return {"username": request.user.username}

    return reads_user


def test_warns_for_a_sync_user_read_in_an_async_handler_behind_django_middleware():
    reads_user = _make_reads_user()
    caught = _register(BoltAPI(middleware=[DjangoMiddlewareStack([_NoOpMiddleware])]), reads_user)

    assert len(caught) == 1
    warning = caught[0]
    assert issubclass(warning.category, RuntimeWarning)
    assert "await request.auser()" in str(warning.message)
    assert "reads_user" in str(warning.message)
    # The warning points at the line of the read.
    _, start = inspect.getsourcelines(reads_user)
    assert warning.filename == inspect.getsourcefile(reads_user)
    assert warning.lineno == start + 1


def test_warns_for_django_middleware_on_the_route_or_router():
    api = BoltAPI()
    caught = _register(api, middleware(DjangoMiddleware(_NoOpMiddleware))(_make_reads_user()))
    assert len(caught) == 1

    router = Router(prefix="/r", middleware=[DjangoMiddleware(_NoOpMiddleware)])
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        router.get("/x")(_make_reads_user())
        api.include_router(router)
    assert [w for w in recorded if "request.user" in str(w.message)]


async def _awaits_user(request: Request):
    user = await request.auser()
    return {"username": user.username}


async def _reads_user_on_the_lane(request: Request):
    return {"username": await sync_to_thread(lambda: request.user.username)}


async def _sets_user(request: Request):
    request.user = None
    return {}


def _sync_reads_user(request: Request):
    return {"username": request.user.username}


@pytest.mark.parametrize(
    "handler",
    [_awaits_user, _reads_user_on_the_lane, _sets_user, _sync_reads_user],
    ids=["auser", "sync_to_thread", "assignment", "sync_handler"],
)
def test_no_warning_for_reads_that_work(handler):
    assert _register(BoltAPI(middleware=[DjangoMiddlewareStack([_NoOpMiddleware])]), handler) == []


def test_no_warning_without_django_middleware():
    assert _register(BoltAPI(), _make_reads_user()) == []
