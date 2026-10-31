"""
Test middleware functionality in Django-Bolt

Note: CORS and rate limiting are handled in Rust for performance.
The @cors() and @rate_limit() decorators attach metadata that Rust parses.
Python middleware classes (TimingMiddleware, etc.) run for custom logic only.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Annotated

import jwt
import msgspec
import pytest
from django.test import override_settings

from django_bolt import BoltAPI, Router
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.exceptions import HTTPException
from django_bolt.middleware import (
    BaseMiddleware,
    DjangoMiddlewareStack,
    ErrorHandlerMiddleware,
    LoggingMiddleware,
    Middleware,
    TimingMiddleware,
    cors,
    middleware,
    rate_limit,
    skip_middleware,
)
from django_bolt.middleware.middleware import FunctionMiddlewareSpec, normalize_middleware_specs
from django_bolt.params import Cookie, Header
from django_bolt.responses import HTML, JSON, PlainText
from django_bolt.testing import TestClient


# Test models
class ItemModel(msgspec.Struct):
    id: int
    name: str


# Custom test middleware
class CustomTestMiddleware(Middleware):
    call_count = 0

    async def process_request(self, request):
        type(self).call_count += 1
        return await self.get_response(request)


def _wrap_argument_injectors(api: BoltAPI, wrap) -> None:
    """Give each route that registers after this call the injector ``wrap(injector)``.

    The handler executor takes the injector when the route registers. Thus a
    later change to ``meta["injector"]`` has no effect.
    """
    compile_injector = api._compile_argument_injector
    api._compile_argument_injector = lambda meta: wrap(compile_injector(meta))


def _counting(injector, calls: dict):
    """Return an injector that counts its calls in ``calls["count"]``."""

    def counting_injector(request):
        calls["count"] += 1
        return injector(request)

    return counting_injector


class TestMiddlewareDecorators:
    """Test middleware decorator functionality"""

    def test_rate_limit_decorator(self):
        """Test rate limit decorator attaches metadata"""
        api = BoltAPI()

        @api.get("/limited")
        @rate_limit(rps=50, burst=100, key="ip")
        async def limited_endpoint():
            return {"status": "ok"}

        # Check that middleware metadata was attached
        handler = api._handlers[0]
        assert hasattr(handler, "__bolt_middleware__")
        middleware = handler.__bolt_middleware__
        assert len(middleware) > 0
        assert middleware[0]["type"] == "rate_limit"
        assert middleware[0]["rps"] == 50
        assert middleware[0]["burst"] == 100
        assert middleware[0]["key"] == "ip"

    def test_cors_decorator(self):
        """Test CORS decorator attaches metadata"""
        api = BoltAPI()

        @api.get("/cors-test")
        @cors(origins=["http://localhost:3000"], credentials=True, max_age=7200)
        async def cors_endpoint():
            return {"status": "ok"}

        handler = api._handlers[0]
        assert hasattr(handler, "__bolt_middleware__")
        middleware = handler.__bolt_middleware__
        assert len(middleware) > 0
        assert middleware[0]["type"] == "cors"
        assert middleware[0]["origins"] == ["http://localhost:3000"]
        assert middleware[0]["credentials"]
        assert middleware[0]["max_age"] == 7200

    def test_auth_via_guards(self):
        """Test authentication via guards parameter"""
        api = BoltAPI()

        @api.get(
            "/protected",
            auth=[JWTAuthentication(secret="test-secret", algorithms=["HS256", "HS384"])],
            guards=[IsAuthenticated()],
        )
        async def protected_endpoint():
            return {"status": "ok"}

        # Check that middleware metadata was compiled
        handler_id = 0
        assert handler_id in api._handler_middleware
        meta = api._handler_middleware[handler_id]
        assert "auth_backends" in meta
        assert len(meta["auth_backends"]) > 0
        assert meta["auth_backends"][0]["type"] == "jwt"
        assert meta["auth_backends"][0]["secret"] == "test-secret"
        assert meta["auth_backends"][0]["algorithms"] == ["HS256", "HS384"]

    def test_skip_middleware_decorator(self):
        """Test skip middleware decorator"""
        api = BoltAPI()

        @api.get("/no-middleware")
        @skip_middleware("cors", "rate_limit")
        async def no_middleware_endpoint():
            return {"status": "ok"}

        handler = api._handlers[0]
        assert hasattr(handler, "__bolt_skip_middleware__")
        skip = handler.__bolt_skip_middleware__
        assert "cors" in skip
        assert "rate_limit" in skip

    def test_multiple_middleware(self):
        """Test multiple middleware decorators on same route"""
        api = BoltAPI()

        @api.post("/secure", auth=[APIKeyAuthentication(api_keys={"key1", "key2"})], guards=[IsAuthenticated()])
        @rate_limit(rps=10)
        @cors(origins=["https://app.example.com"])
        async def secure_endpoint(data: dict):
            return {"received": data}

        handler = api._handlers[0]
        middleware = handler.__bolt_middleware__
        assert len(middleware) == 2  # rate_limit and cors

        # Check they're all there
        types = [m["type"] for m in middleware]
        assert "rate_limit" in types
        assert "cors" in types

        # Check auth is in metadata
        meta = api._handler_middleware[0]
        assert "auth_backends" in meta
        assert len(meta["auth_backends"]) > 0


class TestGlobalMiddleware:
    """Test global middleware configuration"""

    def test_global_middleware_rejects_instances(self):
        """Global middleware must be passed as classes, not instances."""

        class NoOpMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            async def __call__(self, request):
                return await self.get_response(request)

        no_op_instance = NoOpMiddleware(lambda request: request)

        with pytest.raises(TypeError, match="Pass middleware classes instead"):
            BoltAPI(middleware=[no_op_instance])

    def test_global_middleware_accepts_class_and_wrapper(self):
        """Class and wrapper middleware forms should remain supported."""

        class NoOpMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            async def __call__(self, request):
                return await self.get_response(request)

        stack = DjangoMiddlewareStack([NoOpMiddleware])
        api = BoltAPI(middleware=[NoOpMiddleware, stack])

        assert api._middleware[0] is NoOpMiddleware
        assert api._middleware[1] is stack


class _CallableInstance:
    """A callable object that is not a function."""

    def __call__(self, request, call_next): ...


async def _plain_function(request, call_next): ...


class TestMiddlewareEntryValidation:
    """Registration rejects a middleware entry of the wrong kind, and the message names the level."""

    @pytest.mark.parametrize("level", ["api", "router"])
    @pytest.mark.parametrize(
        ("entry", "message"),
        [
            (
                _plain_function,
                "{level} middleware does not accept middleware instances (function). "
                "Pass middleware classes instead, e.g. middleware=[MyMiddleware].",
            ),
            (
                _CallableInstance(),
                "{level} middleware does not accept middleware instances (_CallableInstance). "
                "Pass middleware classes instead, e.g. middleware=[MyMiddleware].",
            ),
            (
                CustomTestMiddleware(_plain_function),
                "{level} middleware does not accept middleware instances (CustomTestMiddleware). "
                "Pass middleware classes instead, e.g. middleware=[MyMiddleware].",
            ),
            (
                FunctionMiddlewareSpec(_plain_function),
                "{level} middleware does not support function-style middleware. "
                "Use middleware classes or DjangoMiddleware/DjangoMiddlewareStack wrappers.",
            ),
            (
                42,
                "Unsupported {level} middleware entry: 42. "
                "Expected middleware class, DjangoMiddleware wrapper, dict config, "
                "or function middleware (route-level only).",
            ),
        ],
        ids=["function", "callable-object", "middleware-instance", "function-spec", "not-callable"],
    )
    def test_global_and_router_levels_reject_the_entry(self, level, entry, message):
        def build():
            if level == "api":
                BoltAPI(middleware=[entry])
            else:
                Router(middleware=[entry])

        with pytest.raises(TypeError) as raised:
            build()

        assert str(raised.value) == message.format(level=level)

    def test_each_kind_of_entry_stays_in_the_list_in_order(self):
        """A kind of entry that the function accepts does not stop the loop."""
        stack = DjangoMiddlewareStack([CustomTestMiddleware])
        spec = FunctionMiddlewareSpec(_plain_function)
        entry = {"type": "rate_limit", "rps": 1, "burst": 2, "key": "ip"}

        normalized = normalize_middleware_specs(
            [spec, CustomTestMiddleware, stack, entry, _plain_function, CustomTestMiddleware],
            context="route",
            allow_function_middleware=True,
        )

        assert normalized[:4] == [spec, CustomTestMiddleware, stack, entry]
        assert normalized[4] == FunctionMiddlewareSpec(_plain_function)
        assert normalized[5] is CustomTestMiddleware

    def test_route_level_rejects_a_callable_object(self):
        with pytest.raises(TypeError, match=r"^route middleware does not accept middleware instances"):
            middleware(_CallableInstance(), CustomTestMiddleware)(lambda: None)

    def test_route_level_rejects_a_single_callable_object_with_the_route_name(self):
        with pytest.raises(
            TypeError, match=r"^route middleware does not accept middleware instances \(_CallableInstance\)"
        ):
            middleware(_CallableInstance())

    def test_route_level_wraps_a_function_a_bound_method_and_a_builtin(self):
        class Holder:
            async def method_middleware(self, request, call_next): ...

        specs = normalize_middleware_specs(
            [_plain_function, Holder().method_middleware, len], context="route", allow_function_middleware=True
        )

        assert [type(spec) for spec in specs] == [FunctionMiddlewareSpec] * 3
        assert [spec.func for spec in specs][::2] == [_plain_function, len]

    def test_a_bound_method_runs_as_route_middleware(self):
        events = []

        class Holder:
            async def method_middleware(self, request, call_next):
                events.append("method")
                return await call_next(request)

        api = BoltAPI()

        @api.get("/method")
        @middleware(Holder().method_middleware)
        async def method_route():
            return {"ok": True}

        with TestClient(api) as client:
            assert client.get("/method").json() == {"ok": True}

        assert events == ["method"]

    def test_one_decorator_takes_a_function_and_a_class_in_order(self):
        events = []

        async def function_middleware(request, call_next):
            events.append("function")
            return await call_next(request)

        class ClassMiddleware(Middleware):
            async def process_request(self, request):
                events.append("class")
                return await self.get_response(request)

        api = BoltAPI()

        @api.get("/mixed")
        @middleware(function_middleware, ClassMiddleware)
        async def mixed_route():
            return {"ok": True}

        with TestClient(api) as client:
            assert client.get("/mixed").json() == {"ok": True}

        assert events == ["function", "class"]

    def test_dict_entries_pass_through_unchanged(self):
        entry = {"type": "rate_limit", "rps": 1, "burst": 2, "key": "ip"}

        assert normalize_middleware_specs([entry, CustomTestMiddleware], context="api") == [entry, CustomTestMiddleware]
        assert normalize_middleware_specs(None, context="api") == []
        assert normalize_middleware_specs([], context="api") == []


class TestRouterMiddlewareContract:
    """Test router middleware contract validation."""

    def test_router_middleware_rejects_instances(self):
        class NoOpMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            async def __call__(self, request):
                return await self.get_response(request)

        no_op_instance = NoOpMiddleware(lambda request: request)
        with pytest.raises(TypeError, match="Pass middleware classes instead"):
            Router(prefix="/api", middleware=[no_op_instance])


class TestMiddlewareOrderingAndExecution:
    """Behavior tests for Global -> Router -> Route -> Handler ordering."""

    def test_router_and_route_middleware_execute_in_strict_order(self):
        events = []

        class GlobalMiddleware(Middleware):
            async def process_request(self, request):
                events.append("global:before")
                response = await self.get_response(request)
                events.append("global:after")
                return response

        class ParentRouterMiddleware(Middleware):
            async def process_request(self, request):
                events.append("parent-router:before")
                response = await self.get_response(request)
                events.append("parent-router:after")
                return response

        class ChildRouterMiddleware(Middleware):
            async def process_request(self, request):
                events.append("child-router:before")
                response = await self.get_response(request)
                events.append("child-router:after")
                return response

        class RouteMiddleware(Middleware):
            async def process_request(self, request):
                events.append("route:before")
                response = await self.get_response(request)
                events.append("route:after")
                return response

        api = BoltAPI(middleware=[GlobalMiddleware])
        parent_router = Router(prefix="/parent", middleware=[ParentRouterMiddleware])
        child_router = Router(prefix="/child", middleware=[ChildRouterMiddleware])

        @child_router.get("/items")
        @middleware(RouteMiddleware)
        async def list_items():
            events.append("handler")
            return {"ok": True}

        parent_router.include_router(child_router)
        api.include_router(parent_router)

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/parent/child/items")
            assert response.status_code == 200
            assert response.json() == {"ok": True}

        assert events == [
            "global:before",
            "parent-router:before",
            "child-router:before",
            "route:before",
            "handler",
            "route:after",
            "child-router:after",
            "parent-router:after",
            "global:after",
        ]

    def test_router_dict_middleware_is_compiled_for_rust_metadata(self):
        api = BoltAPI()
        router = Router(
            prefix="/rl",
            middleware=[
                {
                    "type": "rate_limit",
                    "rps": 7,
                    "burst": 14,
                    "key": "ip",
                }
            ],
        )

        @router.get("/items")
        async def list_items():
            return {"ok": True}

        api.include_router(router)
        middleware_meta = api._handler_middleware[0]
        assert "middleware" in middleware_meta
        assert middleware_meta["middleware"][0]["type"] == "rate_limit"

    def test_function_style_middleware_decorator_executes(self):
        events = []

        @middleware
        async def function_mw(request, call_next):
            events.append("function:before")
            response = await call_next(request)
            response.headers["X-Function-Middleware"] = "true"
            events.append("function:after")
            return response

        api = BoltAPI()

        @api.get("/fn")
        @function_mw
        async def fn_handler():
            events.append("handler")
            return {"ok": True}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/fn")
            assert response.status_code == 200
            assert response.headers.get("X-Function-Middleware") == "true"
            assert response.json() == {"ok": True}

        assert events == ["function:before", "handler", "function:after"]


class TestMiddlewareMetadata:
    """Test middleware metadata compilation"""

    def test_middleware_metadata_compilation(self):
        """Test that middleware metadata is compiled correctly"""
        api = BoltAPI()

        @api.get("/test")
        @cors(origins=["*"])
        @rate_limit(rps=100)
        async def test_endpoint():
            return {"status": "ok"}

        # Check handler middleware metadata
        assert len(api._handler_middleware) > 0
        handler_id = 0
        meta = api._handler_middleware[handler_id]

        assert "middleware" in meta
        assert len(meta["middleware"]) == 2  # CORS + rate_limit from decorators

        # Check types
        types = [m["type"] for m in meta["middleware"]]
        assert "cors" in types
        assert "rate_limit" in types

    def test_skip_middleware(self):
        """Test that skip_middleware removes middleware from compiled metadata"""
        api = BoltAPI()

        @api.get("/no-cors")
        @cors(origins=["*"])
        @rate_limit(rps=100)
        @skip_middleware("cors")
        async def no_cors_endpoint():
            return {"status": "ok"}

        handler_id = 0
        meta = api._handler_middleware[handler_id]

        # skip_middleware("cors") removes cors from the middleware list
        assert len(meta["middleware"]) == 1
        assert meta["middleware"][0]["type"] == "rate_limit"
        assert "cors" in meta["skip"]

    def test_skip_all_skips_rate_limit_cors_and_compression(self):
        """@skip_middleware("*") skips each middleware that Rust runs for the route."""
        api = BoltAPI()

        @api.get("/skip-all")
        @skip_middleware("*")
        @cors(origins=["https://example.com"])
        @rate_limit(rps=1, burst=1)
        async def skip_all_endpoint():
            return {"data": "x" * 5000}

        with TestClient(api) as client:
            for _ in range(3):
                response = client.get("/skip-all", headers={"Origin": "https://example.com", "Accept-Encoding": "gzip"})
                assert response.status_code == 200
                assert "Access-Control-Allow-Origin" not in response.headers
                assert "Content-Encoding" not in response.headers

    def test_skip_all_skips_the_global_cors_config(self):
        """@skip_middleware("*") also skips the CORS config of the settings."""
        with override_settings(CORS_ALLOWED_ORIGINS=["https://example.com"]):
            api = BoltAPI()

            @api.get("/skip-all")
            @skip_middleware("*")
            async def skip_all_endpoint():
                return {}

            @api.get("/plain")
            async def plain_endpoint():
                return {}

            with TestClient(api) as client:
                skipped = client.get("/skip-all", headers={"Origin": "https://example.com"})
                plain = client.get("/plain", headers={"Origin": "https://example.com"})

        assert skipped.status_code == 200
        assert "Access-Control-Allow-Origin" not in skipped.headers
        assert plain.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    def test_skip_middleware_runtime_with_testclient(self):
        """Test that skip_middleware actually skips middleware at runtime using TestClient."""
        api = BoltAPI()

        # Route WITH @cors but also @skip_middleware("cors") - CORS should be skipped
        @api.get("/skip-cors")
        @cors(origins=["https://example.com"])
        @skip_middleware("cors")
        async def skip_cors_endpoint():
            return {"message": "no cors headers"}

        # Route WITH @cors but NO skip - CORS should work
        @api.get("/with-cors")
        @cors(origins=["https://example.com"])
        async def with_cors_endpoint():
            return {"message": "has cors headers"}

        with TestClient(api, use_http_layer=True) as client:
            # Request to endpoint with skipped CORS - should NOT have CORS headers
            response = client.get("/skip-cors", headers={"Origin": "https://example.com"})
            assert response.status_code == 200
            assert "Access-Control-Allow-Origin" not in response.headers

            # Request to endpoint with CORS - should have CORS headers
            response = client.get("/with-cors", headers={"Origin": "https://example.com"})
            assert response.status_code == 200
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    def test_rust_arg_prebinding_skips_injector_on_http_fast_path(self):
        """Simple path/query handlers should execute without calling Python injector."""
        api = BoltAPI()

        # If injector is called, this test must fail.
        def failing_injector(_request):
            raise AssertionError("Injector should be bypassed by Rust prebinding")

        _wrap_argument_injectors(api, lambda _injector: failing_injector)

        @api.get("/items/{item_id}")
        async def get_item(item_id: int, q: str):
            return {"item_id": item_id, "q": q}

        middleware_meta = api._handler_middleware[0]

        # Ensure route is eligible for Rust-side prebinding.
        assert "rust_arg_bindings" in middleware_meta
        assert len(middleware_meta["rust_arg_bindings"]) == 2

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/items/123?q=hello")
            assert response.status_code == 200
            data = response.json()
            assert data["item_id"] == 123
            assert data["q"] == "hello"

    def test_rust_arg_prebinding_skips_injector_for_header_cookie_fast_path(self):
        """Header/Cookie bindings should also bypass injector on no-middleware fast path."""
        api = BoltAPI()

        def failing_injector(_request):
            raise AssertionError("Injector should be bypassed by Rust prebinding")

        _wrap_argument_injectors(api, lambda _injector: failing_injector)

        @api.get("/auth/{item_id}")
        async def get_auth_data(
            item_id: int,
            token: Annotated[str, Header(alias="x-token")],
            session_id: Annotated[str, Cookie(alias="session")],
        ):
            return {"item_id": item_id, "token": token, "session_id": session_id}

        middleware_meta = api._handler_middleware[0]

        assert "rust_arg_bindings" in middleware_meta
        sources = {entry["source"] for entry in middleware_meta["rust_arg_bindings"]}
        assert sources == {"path", "header", "cookie"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/auth/7", headers={"x-token": "token-123", "Cookie": "session=sess-abc"})
            assert response.status_code == 200
            data = response.json()
            assert data["item_id"] == 7
            assert data["token"] == "token-123"
            assert data["session_id"] == "sess-abc"

    def test_rust_arg_prebinding_missing_value_falls_back_to_injector(self):
        """A missing required value makes Rust fall back to the injector, and the injector gives the 422."""
        api = BoltAPI()
        calls = {"count": 0}
        _wrap_argument_injectors(api, lambda injector: _counting(injector, calls))

        @api.get("/requires-header")
        async def requires_header(api_key: Annotated[str, Header(alias="x-api-key")]):
            return {"api_key": api_key}

        assert "rust_arg_bindings" in api._handler_middleware[0]

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/requires-header")
            assert response.status_code == 422
            assert response.json()["detail"] == "Missing required header: x-api-key"

        assert calls["count"] == 1

    def test_rust_arg_prebinding_keeps_injector_path_when_python_middleware_present(self):
        """Prebound args/kwargs are used even when Python middleware chain is active."""

        class PassThroughMiddleware(Middleware):
            call_count = 0

            async def process_request(self, request):
                PassThroughMiddleware.call_count += 1
                return await self.get_response(request)

        api = BoltAPI(middleware=[PassThroughMiddleware])
        calls = {"count": 0}
        _wrap_argument_injectors(api, lambda injector: _counting(injector, calls))

        @api.get("/items/{item_id}")
        async def get_item(item_id: int, q: str):
            return {"item_id": item_id, "q": q}

        assert "rust_arg_bindings" in api._handler_middleware[0]

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/items/42?q=hello")
            assert response.status_code == 200
            assert response.json() == {"item_id": 42, "q": "hello"}

        assert calls["count"] == 0
        assert PassThroughMiddleware.call_count == 1


class TestMiddlewareExecution:
    """Test middleware execution in the pipeline"""

    @pytest.mark.asyncio
    async def test_request_dispatch_with_middleware(self):
        """Test that dispatch works with middleware metadata"""
        api = BoltAPI()

        @api.get("/test")
        @cors(origins=["*"])
        async def test_endpoint(request: dict):
            # Access context
            context = request.get("context")
            return {"has_context": context is not None, "context_type": type(context).__name__ if context else None}

        # Create test request
        test_request = {
            "method": "GET",
            "path": "/test",
            "body": b"",
            "params": {},
            "query": {},
            "headers": {},
            "cookies": {},
            "context": None,  # Will be populated by middleware
        }

        # Get handler and handler_id
        handler_id = 0  # First registered handler
        handler = api._handlers[handler_id]

        # Dispatch with handler_id
        result = await api._dispatch(handler, test_request, handler_id)
        status, meta, body_kind, body = result

        assert status == 200
        assert body_kind == 0  # _BODY_BYTES
        data = json.loads(body)
        assert "has_context" in data

    @pytest.mark.asyncio
    async def test_custom_middleware_execution(self):
        """Test custom middleware execution"""
        CustomTestMiddleware.call_count = 0
        api = BoltAPI(middleware=[CustomTestMiddleware])

        @api.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/test")
            assert response.status_code == 200

        assert CustomTestMiddleware.call_count == 1

    @pytest.mark.asyncio
    async def test_response_model_with_middleware(self):
        """Test response model validation with middleware"""
        api = BoltAPI()

        @api.post("/items", response_model=ItemModel)
        @cors(origins=["*"])
        async def create_item(item: ItemModel) -> ItemModel:
            return item

        test_request = {
            "method": "POST",
            "path": "/items",
            "body": msgspec.json.encode({"id": 1, "name": "Test"}),
            "params": {},
            "query": {},
            "headers": {"content-type": "application/json"},
            "cookies": {},
            "context": None,
        }

        handler_id = 0  # First registered handler
        handler = api._handlers[handler_id]
        result = await api._dispatch(handler, test_request, handler_id)
        status, meta, body_kind, body = result

        assert status == 200
        assert body_kind == 0  # _BODY_BYTES
        data = msgspec.json.decode(body)
        assert data["id"] == 1
        assert data["name"] == "Test"


class TestAuthTokenGeneration:
    """Test JWT token generation for auth testing"""

    def test_generate_valid_jwt(self):
        """Generate a valid JWT token for testing"""
        secret = "test-secret"
        payload = {
            "sub": "user123",
            "exp": int(time.time()) + 3600,  # 1 hour from now
            "iat": int(time.time()),
            "custom_claim": "test_value",
        }

        token = jwt.encode(payload, secret, algorithm="HS256")
        assert token is not None

        # Verify we can decode it
        decoded = jwt.decode(token, secret, algorithms=["HS256"])
        assert decoded["sub"] == "user123"
        assert decoded["custom_claim"] == "test_value"

    def test_generate_expired_jwt(self):
        """Generate an expired JWT token for testing"""
        secret = "test-secret"
        payload = {
            "sub": "user123",
            "exp": int(time.time()) - 3600,  # 1 hour ago
            "iat": int(time.time()) - 7200,
        }

        token = jwt.encode(payload, secret, algorithm="HS256")

        # Should raise error when verifying
        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(token, secret, algorithms=["HS256"])

    def test_generate_invalid_signature_jwt(self):
        """Generate a JWT with wrong signature"""
        secret = "test-secret"
        wrong_secret = "wrong-secret"
        payload = {"sub": "user123", "exp": int(time.time()) + 3600}

        token = jwt.encode(payload, wrong_secret, algorithm="HS256")

        # Should raise error when verifying with correct secret
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, secret, algorithms=["HS256"])


class TestMiddlewareIntegration:
    """Integration tests for middleware with actual server"""

    def test_middleware_registration(self):
        """Test that middleware gets registered with Rust"""
        api = BoltAPI()

        @api.get("/middleware-test")
        @rate_limit(rps=100)
        @cors(origins=["http://localhost:3000"])
        async def test_endpoint():
            return {"status": "ok"}

        # Check routes are registered
        assert len(api._routes) == 1
        method, path, handler_id, handler = api._routes[0]
        assert method == "GET"
        assert path == "/middleware-test"

        # Check middleware metadata
        assert handler_id in api._handler_middleware
        meta = api._handler_middleware[handler_id]
        assert len(meta["middleware"]) == 2

        types = [m["type"] for m in meta["middleware"]]
        assert "rate_limit" in types
        assert "cors" in types

    def test_preflight_route(self):
        """Test OPTIONS preflight handling"""
        api = BoltAPI()

        @api.get("/api/data")
        @cors(
            origins=["http://localhost:3000", "https://app.example.com"],
            methods=["GET", "POST", "PUT"],
            headers=["Content-Type", "Authorization", "X-Custom"],
            credentials=True,
            max_age=7200,
        )
        async def get_data():
            return {"data": [1, 2, 3]}

        # The preflight will be handled by Rust middleware
        # Here we just verify the metadata is correct
        meta = api._handler_middleware[0]
        cors_config = next(m for m in meta["middleware"] if m["type"] == "cors")

        assert cors_config["origins"] == ["http://localhost:3000", "https://app.example.com"]
        assert cors_config["methods"] == ["GET", "POST", "PUT"]
        assert cors_config["headers"] == ["Content-Type", "Authorization", "X-Custom"]
        assert cors_config["credentials"]
        assert cors_config["max_age"] == 7200


class ContentTypeRecorder(Middleware):
    """Record the Content-Type that Python middleware reads from a response."""

    seen: list = []

    async def process_request(self, request):
        response = await self.get_response(request)
        content_types = [value for key, value in response.headers.items() if key.lower() == "content-type"]
        type(self).seen.append(content_types)
        return response


class CsvContentType(Middleware):
    """Set a Content-Type with another letter case than Bolt uses."""

    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers["Content-Type"] = "text/csv"
        return response


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/html", "text/html; charset=utf-8"),
        ("/plain", "text/plain; charset=utf-8"),
        ("/bytes", "application/octet-stream"),
        ("/str", "text/plain; charset=utf-8"),
        ("/json", "application/json"),
        ("/json-with-header", "application/json"),
        ("/plain-with-cookie", "text/plain; charset=utf-8"),
        ("/empty", None),
    ],
)
def test_python_middleware_reads_the_content_type_of_a_built_in_response(path, content_type):
    """Python middleware reads the Content-Type that the client gets."""
    ContentTypeRecorder.seen = []
    api = BoltAPI(middleware=[ContentTypeRecorder])

    @api.get("/html")
    async def html():
        return HTML("<p>x</p>")

    @api.get("/plain")
    def plain():
        return PlainText("x")

    @api.get("/bytes")
    async def raw_bytes() -> bytes:
        return b"raw"

    @api.get("/str")
    async def text() -> str:
        return "hello"

    @api.get("/json")
    async def json_body() -> dict:
        return {"a": 1}

    @api.get("/json-with-header")
    async def json_with_header():
        return JSON({"a": 1}, headers={"x-extra": "1"})

    @api.get("/plain-with-cookie")
    async def plain_with_cookie():
        return PlainText("x").set_cookie("flavor", "oat")

    @api.get("/empty", status_code=204)
    async def empty():
        return None

    with TestClient(api) as client:
        response = client.get(path)

    assert response.headers.get("content-type") == content_type
    assert ContentTypeRecorder.seen == [[content_type] if content_type else []]


class XmlContentType(Middleware):
    """Read the Content-Type in one letter case, and set it in another."""

    seen: list = []

    async def process_request(self, request):
        response = await self.get_response(request)
        type(self).seen.append(response.headers.get("CONTENT-TYPE"))
        response.headers["content-type"] = "application/xml"
        return response


def test_python_middleware_headers_ignore_the_letter_case_of_a_name():
    """The last write of a header wins, whatever letter case each write uses."""
    XmlContentType.seen = []
    # The first middleware runs last on the response.
    api = BoltAPI(middleware=[XmlContentType, CsvContentType])

    @api.get("/html")
    async def html():
        return HTML("<p>x</p>")

    with TestClient(api) as client:
        response = client.get("/html")

    assert XmlContentType.seen == ["text/csv"]
    assert response.headers.get_list("content-type") == ["application/xml"]


class DropContentType(Middleware):
    """Delete the Content-Type, or set it to an empty value."""

    async def process_request(self, request):
        response = await self.get_response(request)
        if request.query.get("how") == "empty":
            response.headers["Content-Type"] = ""
        else:
            del response.headers["Content-Type"]
        return response


@pytest.mark.parametrize("how", ["delete", "empty"])
@pytest.mark.parametrize(
    "django_middleware",
    [None, ["django.middleware.clickjacking.XFrameOptionsMiddleware"]],
    ids=["python", "python+django"],
)
def test_python_middleware_removes_the_content_type(how, django_middleware):
    """A Content-Type that middleware deletes or empties is not sent."""
    api = BoltAPI(middleware=[DropContentType], django_middleware=django_middleware)

    @api.get("/html")
    async def html():
        return HTML("<p>x</p>")

    with TestClient(api) as client:
        response = client.get("/html", params={"how": how})

    assert response.status_code == 200
    assert "content-type" not in response.headers
    assert response.text == "<p>x</p>"


def test_python_middleware_replaces_the_content_type_in_any_letter_case():
    """A Content-Type that middleware sets wins over the one of the response."""
    api = BoltAPI(middleware=[CsvContentType])

    @api.get("/html")
    async def html():
        return HTML("<p>x</p>")

    with TestClient(api) as client:
        response = client.get("/html")

    assert response.headers.get_list("content-type") == ["text/csv"]
    assert response.text == "<p>x</p>"


class StateTag(BaseMiddleware):
    """Add a header to each response that this middleware handles."""

    exclude_paths = ["/static/*", "/api/**", "/exact", "/v1.0/ping"]
    exclude_methods = ["post"]

    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers["X-Handled"] = "yes"
        return response


@pytest.mark.parametrize(
    ("method", "path", "handled"),
    [
        ("GET", "/static/app.css", False),
        ("GET", "/static/css/app.css", True),
        ("GET", "/api/a/b/c", False),
        ("GET", "/exact", False),
        ("GET", "/exactly", True),
        ("GET", "/prefix/exact", True),
        ("GET", "/v1.0/ping", False),
        ("GET", "/v1x0/ping", True),
        ("GET", "/other", True),
        ("POST", "/other", False),
        ("PUT", "/other", True),
    ],
)
def test_base_middleware_skips_the_excluded_paths_and_methods(method, path, handled):
    """A request that matches an exclusion goes to the next layer. Other requests run the middleware."""
    api = BoltAPI(middleware=[StateTag])
    for route in ("/static/app.css", "/static/css/app.css", "/api/a/b/c", "/exact", "/exactly", "/prefix/exact"):
        api.get(route)(lambda: {"ok": True})
    for route in ("/v1.0/ping", "/v1x0/ping", "/other"):
        api.get(route)(lambda: {"ok": True})
    api.post("/other")(lambda: {"ok": True})
    api.put("/other")(lambda: {"ok": True})

    with TestClient(api) as client:
        response = client.request(method, path)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert ("x-handled" in response.headers) is handled


class TimingState(msgspec.Struct):
    request_id: str
    start_time: float


def test_timing_middleware_sets_the_request_id_and_the_response_time():
    """The request ID is the same in request.state and in the header. The ID is new for each request."""
    api = BoltAPI(middleware=[TimingMiddleware])

    @api.get("/timing")
    async def timing(request) -> TimingState:
        await asyncio.sleep(0.05)
        return TimingState(request_id=request.state["request_id"], start_time=request.state["start_time"])

    with TestClient(api) as client:
        first = client.get("/timing")
        second = client.get("/timing")

    for response in (first, second):
        assert response.status_code == 200
        request_id = response.headers["x-request-id"]
        assert response.json()["request_id"] == request_id
        assert str(uuid.UUID(request_id, version=4)) == request_id
        assert re.fullmatch(r"\d+\.\d{4}s", response.headers["x-response-time"])
        assert 0.05 <= float(response.headers["x-response-time"].removesuffix("s")) < 1
        assert response.json()["start_time"] > 0
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


class RecordingLogging(LoggingMiddleware):
    """A LoggingMiddleware that writes to a list, with all options on."""

    records: list = []

    def __init__(self, get_response):
        logger = logging.getLogger("test_middleware.recording_logging")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers = [_ListHandler(type(self).records)]
        super().__init__(get_response, logger=logger, log_body=True, log_headers=True, log_level=logging.WARNING)


class _ListHandler(logging.Handler):
    def __init__(self, records: list):
        super().__init__()
        self._records = records

    def emit(self, record):
        self._records.append(record)


def test_logging_middleware_logs_the_request_and_the_response():
    """The middleware logs one request line and one response line at the level that the caller sets."""
    RecordingLogging.records = []
    api = BoltAPI(middleware=[RecordingLogging])

    @api.post("/echo")
    async def echo(request):
        await asyncio.sleep(0.05)
        return {"size": len(request.body)}

    @api.get("/health")
    async def health():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.post("/echo?page=2", content=b"12345", headers={"X-Probe": "1"})
        client.get("/health")

    assert response.json() == {"size": 5}
    records = RecordingLogging.records
    assert [record.levelno for record in records] == [logging.WARNING, logging.WARNING]
    request_line, response_line = (record.getMessage() for record in records)
    assert request_line.startswith("Request: ")
    assert "'method': 'POST'" in request_line
    assert "'path': '/echo'" in request_line
    assert "'query': {'page': '2'}" in request_line
    assert "'body_size': 5" in request_line
    assert "'headers': {" in request_line
    assert "'x-probe': '1'" in request_line
    logged = re.fullmatch(r"Response: 200 for POST /echo \((\d+\.\d{4})s\)", response_line)
    assert logged
    assert 0.05 <= float(logged.group(1)) < 1


def test_logging_middleware_logs_less_by_default(caplog):
    """By default the middleware logs no headers and no body. It logs at INFO, and it skips /health."""
    api = BoltAPI(middleware=[LoggingMiddleware])

    @api.post("/echo")
    async def echo(request):
        return {"size": len(request.body)}

    @api.get("/health")
    async def health():
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="django_bolt.requests"), TestClient(api) as client:
        client.post("/echo", content=b"12345", headers={"X-Probe": "1"})
        client.get("/health")

    records = [record for record in caplog.records if record.name == "django_bolt.requests"]
    assert [record.levelno for record in records] == [logging.INFO, logging.INFO]
    request_line = records[0].getMessage()
    assert "'path': '/echo'" in request_line
    assert "query" not in request_line
    assert "body_size" not in request_line
    assert "headers" not in request_line
    assert "/health" not in request_line + records[1].getMessage()


class DebugErrorHandler(ErrorHandlerMiddleware):
    def __init__(self, get_response):
        super().__init__(get_response, debug=True)


@pytest.mark.parametrize("debug", [False, True], ids=["production", "debug"])
def test_error_handler_middleware_answers_500_and_logs_an_unhandled_exception(debug, caplog):
    """The 500 detail hides the error unless debug is on. An HTTPException keeps its status and is not logged."""
    api = BoltAPI(middleware=[DebugErrorHandler if debug else ErrorHandlerMiddleware])

    @api.get("/boom")
    async def boom():
        raise ValueError("secret detail")

    @api.get("/teapot")
    async def teapot():
        raise HTTPException(418, "teapot")

    with caplog.at_level(logging.ERROR, logger="django_bolt.errors"), TestClient(api) as client:
        crash = client.get("/boom")
        http_error = client.get("/teapot")

    assert crash.status_code == 500
    if debug:
        assert "Traceback (most recent call last)" in crash.json()["detail"]
        assert "ValueError: secret detail" in crash.json()["detail"]
    else:
        assert crash.json() == {"detail": "Internal Server Error"}
    assert http_error.status_code == 418
    assert http_error.json() == {"detail": "teapot"}
    messages = [record.getMessage() for record in caplog.records if record.name == "django_bolt.errors"]
    assert messages == ["Unhandled exception: secret detail"]


if __name__ == "__main__":
    # Run basic tests
    print("Testing middleware decorators...")
    test_decorators = TestMiddlewareDecorators()
    test_decorators.test_rate_limit_decorator()
    test_decorators.test_cors_decorator()
    test_decorators.test_auth_via_guards()
    test_decorators.test_skip_middleware_decorator()
    test_decorators.test_multiple_middleware()
    print("✓ Decorator tests passed")

    print("\nTesting global middleware...")
    test_global = TestGlobalMiddleware()
    test_global.test_global_middleware_rejects_instances()
    test_global.test_global_middleware_accepts_class_and_wrapper()
    print("✓ Global middleware tests passed")

    print("\nTesting middleware metadata...")
    test_meta = TestMiddlewareMetadata()
    test_meta.test_middleware_metadata_compilation()
    test_meta.test_skip_middleware()
    print("✓ Metadata tests passed")

    print("\nTesting JWT generation...")
    test_auth = TestAuthTokenGeneration()
    test_auth.test_generate_valid_jwt()
    test_auth.test_generate_expired_jwt()
    test_auth.test_generate_invalid_signature_jwt()
    print("✓ JWT tests passed")

    print("\nTesting middleware integration...")
    test_integration = TestMiddlewareIntegration()
    test_integration.test_middleware_registration()
    test_integration.test_preflight_route()
    print("✓ Integration tests passed")

    print("\nRunning async tests...")
    test_exec = TestMiddlewareExecution()
    asyncio.run(test_exec.test_request_dispatch_with_middleware())
    asyncio.run(test_exec.test_custom_middleware_execution())
    asyncio.run(test_exec.test_response_model_with_middleware())
    print("✓ Async execution tests passed")

    print("\n✅ All middleware tests passed!")
