"""
Test middleware functionality in Django-Bolt

Note: CORS and rate limiting are handled in Rust for performance.
The @cors() and @rate_limit() decorators attach metadata that Rust parses.
Python middleware classes (TimingMiddleware, etc.) run for custom logic only.
"""

import asyncio
import json
import time
from typing import Annotated

import jwt
import msgspec
import pytest

from django_bolt import BoltAPI, Router
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.middleware import DjangoMiddlewareStack, Middleware, cors, middleware, rate_limit, skip_middleware
from django_bolt.params import Cookie, Header
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

        @api.get("/items/{item_id}")
        async def get_item(item_id: int, q: str):
            return {"item_id": item_id, "q": q}

        handler_id = 0
        meta = api._handler_meta[handler_id]
        middleware_meta = api._handler_middleware[handler_id]

        # Ensure route is eligible for Rust-side prebinding.
        assert "rust_arg_bindings" in middleware_meta
        assert len(middleware_meta["rust_arg_bindings"]) == 2

        # If injector is called, this test must fail.
        def failing_injector(_request):
            raise AssertionError("Injector should be bypassed by Rust prebinding")

        meta["injector"] = failing_injector
        meta["injector_is_async"] = False

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/items/123?q=hello")
            assert response.status_code == 200
            data = response.json()
            assert data["item_id"] == 123
            assert data["q"] == "hello"

    def test_rust_arg_prebinding_skips_injector_for_header_cookie_fast_path(self):
        """Header/Cookie bindings should also bypass injector on no-middleware fast path."""
        api = BoltAPI()

        @api.get("/auth/{item_id}")
        async def get_auth_data(
            item_id: int,
            token: Annotated[str, Header(alias="x-token")],
            session_id: Annotated[str, Cookie(alias="session")],
        ):
            return {"item_id": item_id, "token": token, "session_id": session_id}

        handler_id = 0
        meta = api._handler_meta[handler_id]
        middleware_meta = api._handler_middleware[handler_id]

        assert "rust_arg_bindings" in middleware_meta
        sources = {entry["source"] for entry in middleware_meta["rust_arg_bindings"]}
        assert sources == {"path", "header", "cookie"}

        def failing_injector(_request):
            raise AssertionError("Injector should be bypassed by Rust prebinding")

        meta["injector"] = failing_injector
        meta["injector_is_async"] = False

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/auth/7", headers={"x-token": "token-123"}, cookies={"session": "sess-abc"})
            assert response.status_code == 200
            data = response.json()
            assert data["item_id"] == 7
            assert data["token"] == "token-123"
            assert data["session_id"] == "sess-abc"

    def test_rust_arg_prebinding_missing_value_falls_back_to_injector(self):
        """Missing required prebound values now short-circuit with 422 in Rust."""
        api = BoltAPI()

        @api.get("/requires-header")
        async def requires_header(api_key: Annotated[str, Header(alias="x-api-key")]):
            return {"api_key": api_key}

        handler_id = 0
        meta = api._handler_meta[handler_id]
        middleware_meta = api._handler_middleware[handler_id]

        assert "rust_arg_bindings" in middleware_meta
        original_injector = meta["injector"]
        calls = {"count": 0}

        def counting_injector(request):
            calls["count"] += 1
            return original_injector(request)

        meta["injector"] = counting_injector
        meta["injector_is_async"] = False

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/requires-header")
            assert response.status_code == 422
            assert response.json()["detail"] == "Missing required header: x-api-key"

        assert calls["count"] == 0

    def test_rust_arg_prebinding_keeps_injector_path_when_python_middleware_present(self):
        """Prebound args/kwargs are used even when Python middleware chain is active."""

        class PassThroughMiddleware(Middleware):
            call_count = 0

            async def process_request(self, request):
                PassThroughMiddleware.call_count += 1
                return await self.get_response(request)

        api = BoltAPI(middleware=[PassThroughMiddleware])

        @api.get("/items/{item_id}")
        async def get_item(item_id: int, q: str):
            return {"item_id": item_id, "q": q}

        handler_id = 0
        meta = api._handler_meta[handler_id]
        middleware_meta = api._handler_middleware[handler_id]

        assert "rust_arg_bindings" in middleware_meta
        calls = {"count": 0}

        def counting_injector(request):
            calls["count"] += 1
            return ([request["params"]["item_id"], request["query"]["q"]], {})

        meta["injector"] = counting_injector
        meta["injector_is_async"] = False

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
