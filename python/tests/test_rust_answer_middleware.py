"""The middleware of the API runs for a request that Rust answers (issue #865).

Rust answers a request with no route (404, 405) and rejects a request in
authentication or a guard (401, 403). Django runs each middleware for such a
request. Bolt now gives the answer of Rust to the Python and Django
middleware, as the response of the handler. A 429 stays in Rust.
"""

from __future__ import annotations

import threading
import time

import jwt
import pytest
from django.middleware.csrf import CsrfViewMiddleware
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication, Requires
from django_bolt.middleware import DjangoMiddleware, DjangoMiddlewareStack, Middleware, middleware, rate_limit
from django_bolt.responses import JSON
from django_bolt.testing import TestClient

_SECRET = "rust-answer-secret"
_hooks: list[tuple] = []


class RequestIdMiddleware:
    """A request-id middleware in the ``__call__`` style of Django."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Request-Id"] = "rid"
        return response


class HookRecorder(MiddlewareMixin):
    """Record each hook that Bolt calls, and add a header in process_response."""

    def process_request(self, request):
        _hooks.append(("process_request", request.path))

    def process_view(self, request, view_func, view_args, view_kwargs):
        _hooks.append(("process_view", request.path))

    def process_response(self, request, response):
        _hooks.append(("process_response", request.path, response.status_code))
        response["X-Hook"] = "seen"
        return response


_thread_state = threading.local()


class ThreadTenantMiddleware(MiddlewareMixin):
    """Keep the tenant in a thread-local, as django-tenants does."""

    def process_request(self, request):
        _thread_state.tenant = "acme"

    def process_response(self, request, response):
        response["X-Tenant"] = getattr(_thread_state, "tenant", "lost")
        _thread_state.tenant = None
        return response


class BoltRequestIdMiddleware(Middleware):
    """A Bolt middleware that adds a header to each response."""

    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers["X-Bolt-Request-Id"] = "bolt-rid"
        return response


class ClearStateMiddleware(Middleware):
    """A Bolt middleware that removes each value of request.state."""

    async def process_request(self, request):
        request.state.clear()
        return await self.get_response(request)


class MaintenanceMiddleware(Middleware):
    """A Bolt middleware that answers each request itself."""

    async def process_request(self, request):
        return JSON({"maintenance": True}, status_code=503)


_LAYOUTS = {
    "wrapper": lambda: [DjangoMiddleware(RequestIdMiddleware)],
    "stack": lambda: [DjangoMiddlewareStack([RequestIdMiddleware])],
    "bolt": lambda: [BoltRequestIdMiddleware],
}
_HEADER = {"wrapper": "x-request-id", "stack": "x-request-id", "bolt": "x-bolt-request-id"}


def _token(**claims) -> str:
    now = int(time.time())
    return jwt.encode({"sub": "user-1", "iat": now, "exp": now + 3600, **claims}, _SECRET, algorithm="HS256")


def _api(middleware_specs, kind: str = "async", **api_kwargs) -> BoltAPI:
    """An API with one open route and one route for each rejection of Rust."""
    api = BoltAPI(middleware=middleware_specs, **api_kwargs)
    key_auth = [APIKeyAuthentication(api_keys={"secret"}, header="x-api-key")]
    jwt_auth = [JWTAuthentication(secret=_SECRET)]

    if kind == "sync":

        @api.get("/open")
        def open_route():
            return {"ok": True}

        @api.get("/private", auth=key_auth, guards=[IsAuthenticated()])
        def private():
            return {"ok": True}

        @api.get("/admin", auth=jwt_auth, guards=[Requires("role", "admin")])
        def admin():
            return {"ok": True}

        @api.get("/limited")
        @rate_limit(rps=1, burst=1, key="ip")
        def limited():
            return {"ok": True}

    else:

        @api.get("/open")
        async def open_route():
            return {"ok": True}

        @api.get("/private", auth=key_auth, guards=[IsAuthenticated()])
        async def private():
            return {"ok": True}

        @api.get("/admin", auth=jwt_auth, guards=[Requires("role", "admin")])
        async def admin():
            return {"ok": True}

        @api.get("/limited")
        @rate_limit(rps=1, burst=1, key="ip")
        async def limited():
            return {"ok": True}

    return api


def _rust_answers(client: TestClient) -> list:
    """Send one request for each kind of answer of Rust that the middleware gets."""
    return [
        client.get("/no-route"),
        client.post("/open"),
        client.get("/private"),
        client.get("/admin", headers={"Authorization": f"Bearer {_token(role='user')}"}),
    ]


def _until_rate_limited(client: TestClient) -> list:
    """Send requests to /limited until one gets a 429. The bucket has one token."""
    responses = [client.get("/limited")]
    while responses[-1].status_code != 429 and len(responses) < 20:
        responses.append(client.get("/limited"))
    return responses


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_the_middleware_of_the_api_gets_each_answer_of_rust(layout, kind):
    """The client gets the answer of an API with no middleware, plus the header of the middleware."""
    with TestClient(_api([], kind)) as client:
        plain = _rust_answers(client)
    with TestClient(_api(_LAYOUTS[layout](), kind)) as client:
        answers = _rust_answers(client)

    assert [response.status_code for response in answers] == [404, 405, 401, 403]
    header = _HEADER[layout]
    for with_middleware, without_middleware in zip(answers, plain, strict=True):
        assert with_middleware.headers[header] == ("bolt-rid" if layout == "bolt" else "rid")
        assert header not in without_middleware.headers
        assert with_middleware.status_code == without_middleware.status_code
        assert with_middleware.content == without_middleware.content
        headers = dict(with_middleware.headers)
        del headers[header]
        assert headers == dict(without_middleware.headers)


def test_the_answer_of_a_path_with_no_route_runs_no_process_view():
    """Django finds no view for such a path, so it calls no process_view."""
    _hooks.clear()
    with TestClient(_api([DjangoMiddleware(HookRecorder)])) as client:
        response = client.get("/no-route")
        method_response = client.post("/open")

    assert response.status_code == 404
    assert response.headers["x-hook"] == "seen"
    assert method_response.status_code == 405
    assert _hooks == [
        ("process_request", "/no-route"),
        ("process_response", "/no-route", 404),
        ("process_request", "/open"),
        ("process_response", "/open", 405),
    ]


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("wrap", [DjangoMiddleware, lambda cls: DjangoMiddlewareStack([cls])], ids=["wrapper", "stack"])
def test_a_rejection_of_a_route_runs_no_process_view(kind, wrap):
    """No view runs for a rejection, and Bolt does not read its body."""
    _hooks.clear()
    with TestClient(_api([wrap(HookRecorder)], kind)) as client:
        response = client.get("/private")

    assert response.status_code == 401
    assert response.headers["x-hook"] == "seen"
    assert response.headers["www-authenticate"]
    assert _hooks == [
        ("process_request", "/private"),
        ("process_response", "/private", 401),
    ]


def test_the_csrf_middleware_does_not_replace_a_rejection_of_a_form_post():
    """CsrfViewMiddleware checks in process_view. A 401 must not become a CSRF 403."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([CsrfViewMiddleware])])

    @api.post(
        "/private", auth=[APIKeyAuthentication(api_keys={"secret"}, header="x-api-key")], guards=[IsAuthenticated()]
    )
    async def private():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.post("/private", data={"name": "value"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"]


def test_a_middleware_that_clears_the_state_does_not_run_the_handler():
    """The answer of Rust is not in request.state, so middleware cannot remove it."""
    calls = []
    api = BoltAPI()

    @api.get(
        "/private", auth=[APIKeyAuthentication(api_keys={"secret"}, header="x-api-key")], guards=[IsAuthenticated()]
    )
    @middleware(ClearStateMiddleware)
    async def private():
        calls.append("handler")
        return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/private")

    assert response.status_code == 401
    assert calls == []


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_a_429_does_not_reach_the_python_middleware(layout, kind):
    """The rate limit sheds load in Rust, before Python runs."""
    with TestClient(_api(_LAYOUTS[layout](), kind)) as client:
        responses = _until_rate_limited(client)

    header = _HEADER[layout]
    *allowed, limited = responses
    assert limited.status_code == 429
    assert header not in limited.headers
    assert [response.headers[header] for response in allowed] == ["bolt-rid" if layout == "bolt" else "rid"] * len(
        allowed
    )


def test_a_middleware_can_answer_a_request_with_no_route():
    with TestClient(_api([MaintenanceMiddleware])) as client:
        response = client.get("/no-route")

    assert response.status_code == 503
    assert response.json() == {"maintenance": True}


@pytest.mark.parametrize("layout", ["wrapper", "stack"])
def test_a_thread_local_middleware_keeps_its_thread_for_an_answer_of_rust(layout):
    """A sync flow runs on one request lane, so the thread-local value stays."""
    specs = (
        [DjangoMiddleware(ThreadTenantMiddleware)]
        if layout == "wrapper"
        else [DjangoMiddlewareStack([ThreadTenantMiddleware])]
    )
    with TestClient(_api(specs, "sync")) as client:
        answers = _rust_answers(client)

    assert [response.headers["x-tenant"] for response in answers] == ["acme"] * 4


def _formatted(error, request):
    return JSON({"problem": error.detail, "code": error.status}, status_code=error.status)


def test_the_middleware_gets_the_answer_of_the_error_formatter():
    with TestClient(_api([DjangoMiddleware(RequestIdMiddleware)], error_formatter=_formatted)) as client:
        response = client.get("/no-route")

    assert response.status_code == 404
    assert response.json() == {"problem": "Not Found", "code": 404}
    assert response.headers["x-request-id"] == "rid"


def test_the_route_middleware_gets_a_rejection_of_its_route():
    """A route with its own middleware and no middleware on the API."""
    api = BoltAPI()

    @api.get(
        "/private",
        auth=[APIKeyAuthentication(api_keys={"secret"}, header="x-api-key")],
        guards=[IsAuthenticated()],
    )
    @middleware(BoltRequestIdMiddleware)
    async def private(request: Request):
        return {"ok": True}

    with TestClient(api) as client:
        rejected = client.get("/private")
        allowed = client.get("/private", headers={"x-api-key": "secret"})
        no_route = client.get("/no-route")

    assert rejected.status_code == 401
    assert rejected.headers["x-bolt-request-id"] == "bolt-rid"
    assert allowed.status_code == 200
    assert allowed.headers["x-bolt-request-id"] == "bolt-rid"
    # The route middleware does not belong to the API, so a path with no route does not run it.
    assert no_route.status_code == 404
    assert "x-bolt-request-id" not in no_route.headers
