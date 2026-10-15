"""@skip_middleware for the middleware that runs in Python.

A name selects a middleware class, a function middleware or a Django
middleware class. "*" selects each Bolt middleware, but no Django middleware.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, Router
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack, middleware, rate_limit, skip_middleware
from django_bolt.testing import TestClient


def _header_middleware(header: str) -> type:
    """Make a Bolt middleware class that sets ``header`` on the response."""

    class HeaderMiddleware:
        def __init__(self, get_response):
            self.get_response = get_response

        async def __call__(self, request):
            response = await self.get_response(request)
            response.headers[header] = "1"
            return response

    return HeaderMiddleware


GlobalMiddleware = _header_middleware("X-Global")
GlobalMiddleware.__name__ = GlobalMiddleware.__qualname__ = "GlobalMiddleware"
RouterMiddleware = _header_middleware("X-Router")
RouterMiddleware.__name__ = RouterMiddleware.__qualname__ = "RouterMiddleware"


class CsrfLikeMiddleware:
    """A Django middleware that sets a header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Django-A"] = "1"
        return response


class OtherDjangoMiddleware:
    """A second Django middleware that sets a header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Django-B"] = "1"
        return response


async def function_middleware(request, call_next):
    response = await call_next(request)
    response.headers["X-Function"] = "1"
    return response


def _headers(response) -> set[str]:
    return {
        name for name in ("X-Global", "X-Router", "X-Function", "X-Django-A", "X-Django-B") if name in response.headers
    }


def test_a_named_global_middleware_is_skipped_only_on_its_route():
    api = BoltAPI(middleware=[GlobalMiddleware])

    @api.get("/skipped")
    @skip_middleware("GlobalMiddleware")
    async def skipped():
        return {}

    @api.get("/plain")
    async def plain():
        return {}

    with TestClient(api) as client:
        assert _headers(client.get("/skipped")) == set()
        assert _headers(client.get("/plain")) == {"X-Global"}
        # The shared chain of the API still runs after the route chain is built.
        assert _headers(client.get("/skipped")) == set()


def test_a_dotted_path_selects_a_middleware():
    api = BoltAPI(middleware=[GlobalMiddleware])

    @api.get("/skipped")
    @skip_middleware(f"{GlobalMiddleware.__module__}.GlobalMiddleware")
    async def skipped():
        return {}

    with TestClient(api) as client:
        assert _headers(client.get("/skipped")) == set()


@pytest.mark.django_db
def test_skip_all_skips_bolt_middleware_and_keeps_django_middleware():
    api = BoltAPI(middleware=[GlobalMiddleware, DjangoMiddleware(CsrfLikeMiddleware)])
    router = Router(middleware=[RouterMiddleware])

    @router.get("/all")
    @skip_middleware("*")
    @middleware(function_middleware)
    async def skip_all():
        return {}

    @router.get("/none")
    @middleware(function_middleware)
    async def skip_none():
        return {}

    api.include_router(router)

    with TestClient(api) as client:
        assert _headers(client.get("/all")) == {"X-Django-A"}
        assert _headers(client.get("/none")) == {"X-Global", "X-Router", "X-Function", "X-Django-A"}


@pytest.mark.django_db
def test_a_name_selects_a_django_middleware_and_a_class_of_a_stack():
    api = BoltAPI(middleware=[DjangoMiddlewareStack([CsrfLikeMiddleware, OtherDjangoMiddleware])])

    @api.get("/one")
    @skip_middleware("CsrfLikeMiddleware")
    async def skip_one():
        return {}

    @api.get("/both")
    @skip_middleware("CsrfLikeMiddleware", "OtherDjangoMiddleware")
    async def skip_both():
        return {}

    @api.get("/plain")
    async def plain():
        return {}

    with TestClient(api) as client:
        assert _headers(client.get("/one")) == {"X-Django-B"}
        assert _headers(client.get("/both")) == set()
        assert _headers(client.get("/plain")) == {"X-Django-A", "X-Django-B"}


def test_a_name_selects_a_function_middleware_and_a_router_middleware():
    api = BoltAPI()
    router = Router(middleware=[RouterMiddleware])

    @router.get("/skipped")
    @skip_middleware("function_middleware", "RouterMiddleware")
    @middleware(function_middleware)
    async def skipped():
        return {}

    @router.get("/plain")
    @middleware(function_middleware)
    async def plain():
        return {}

    api.include_router(router)

    with TestClient(api) as client:
        assert _headers(client.get("/skipped")) == set()
        assert _headers(client.get("/plain")) == {"X-Router", "X-Function"}


def test_skip_all_skips_the_rust_middleware_of_the_route_too():
    """ "*" skips each Bolt middleware: the Python ones and the rate limit that Rust runs."""
    api = BoltAPI(middleware=[GlobalMiddleware])

    @api.get("/all")
    @skip_middleware("*")
    @rate_limit(rps=1, burst=1)
    async def skip_all():
        return {}

    with TestClient(api) as client:
        first = client.get("/all")
        second = client.get("/all")

    assert _headers(first) == set()
    assert second.status_code == 200


@pytest.mark.parametrize("first", ["/one", "/both"])
def test_routes_that_share_a_handler_keep_their_own_global_chain(first):
    """Each route gets the chain of its own skip list, in either request order."""
    api = BoltAPI(middleware=[GlobalMiddleware, RouterMiddleware])

    async def handler():
        return {}

    # The skip list is an attribute of the function, so it can grow between two registrations.
    api.get("/one")(skip_middleware("GlobalMiddleware")(handler))
    api.get("/both")(skip_middleware("RouterMiddleware")(handler))

    with TestClient(api) as client:
        client.get(first)
        assert _headers(client.get("/one")) == {"X-Router"}
        assert _headers(client.get("/both")) == set()
