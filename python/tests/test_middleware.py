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


class HeaderProbe(Middleware):
    """Mark each response that it handles."""

    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers["X-Probe"] = "ran"
        return response


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


_BAD_KEY = 'rate_limit key must be "ip", "user", "api_key" or the name of a request header, got {}'


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

        # With no argument, the limit is 100 per second per address. The burst is two times the limit.
        @rate_limit()
        async def default_limit():
            return {"status": "ok"}

        assert default_limit.__bolt_middleware__ == [{"type": "rate_limit", "rps": 100, "burst": 200, "key": "ip"}]

    @pytest.mark.parametrize(
        ("kwargs", "error", "message"),
        [
            ({"rps": 0.5}, TypeError, "rate_limit rps must be an int, got float 0.5"),
            ({"rps": "10"}, TypeError, "rate_limit rps must be an int, got str '10'"),
            ({"rps": True}, TypeError, "rate_limit rps must be an int, got bool True"),
            ({"rps": 0}, ValueError, "rate_limit rps must be 1 or more, got 0"),
            ({"rps": -5}, ValueError, "rate_limit rps must be 1 or more, got -5"),
            ({"rps": 10, "burst": 2.5}, TypeError, "rate_limit burst must be an int, got float 2.5"),
            ({"rps": 10, "burst": 0}, ValueError, "rate_limit burst must be 1 or more, got 0"),
            ({"key": 5}, TypeError, "rate_limit key must be a str, got int 5"),
            ({"key": ""}, ValueError, "rate_limit key must not be empty"),
            ({"key": " "}, ValueError, _BAD_KEY.format("' '")),
            ({"key": "x\ny"}, ValueError, _BAD_KEY.format("'x\\ny'")),
            ({"key": "X-Tenant\n"}, ValueError, _BAD_KEY.format("'X-Tenant\\n'")),
            ({"key": "X-A B"}, ValueError, _BAD_KEY.format("'X-A B'")),
            ({"key": " ip"}, ValueError, _BAD_KEY.format("' ip'")),
            ({"key": "X-Tenant:"}, ValueError, _BAD_KEY.format("'X-Tenant:'")),
            ({"key": "X-Ténant"}, ValueError, _BAD_KEY.format("'X-Ténant'")),
        ],
        ids=[
            "rps-float",
            "rps-str",
            "rps-bool",
            "rps-zero",
            "rps-negative",
            "burst-float",
            "burst-zero",
            "key-int",
            "key-empty",
            "key-space",
            "key-line-break",
            "key-line-break-at-end",
            "key-space-inside",
            "key-space-at-start",
            "key-colon",
            "key-not-ascii",
        ],
    )
    def test_rate_limit_decorator_rejects_a_bad_value(self, kwargs, error, message):
        """A bad value raises at once. Before, Rust used the default limit for it, with no error."""
        with pytest.raises(error, match=f"^{re.escape(message)}$"):
            rate_limit(**kwargs)

    @pytest.mark.parametrize(
        ("inner", "outer"),
        [
            (rate_limit(rps=100), rate_limit(rps=1, burst=1)),
            (rate_limit(rps=100), middleware(type="rate_limit", rps=1)),
            (middleware(type="rate_limit", rps=100), rate_limit(rps=1)),
            (middleware({"type": "rate_limit", "rps": 100}), middleware({"type": "rate_limit", "rps": 1})),
        ],
        ids=["two-decorators", "keywords-over-decorator", "decorator-over-keywords", "two-dicts"],
    )
    def test_a_route_takes_one_rate_limit(self, inner, outer):
        """Before, the route kept the limit of the bottom decorator and dropped the other, with no error."""

        async def route():
            return {}

        with pytest.raises(ValueError, match="already has a @rate_limit"):
            outer(inner(route))

    @pytest.mark.parametrize("level", ["api", "router"])
    def test_a_rate_limit_dict_with_a_bad_value_stops_the_registration(self, level):
        """A rate limit dict that does not come from @rate_limit gets the checks of @rate_limit."""
        entry = {"type": "rate_limit", "rps": 0.5, "burst": 1, "key": "ip"}

        with pytest.raises(TypeError, match=r"^rate_limit rps must be an int, got float 0\.5$"):
            if level == "api":
                BoltAPI(middleware=[entry])
            else:
                Router(prefix="/rl", middleware=[entry])

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


class _NoCall:
    """A class that takes get_response, with instances that are not callable."""

    def __init__(self, get_response):
        self.get_response = get_response


class _DjangoStyle:
    """A plain class of the Django style. It has no base class of Bolt."""

    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request):
        request.state["seen"] = type(self).__name__
        return await self.get_response(request)


class _InheritsCall(_DjangoStyle):
    """A class that gets __call__ from its base class."""


class _ProcessRequestOnly(Middleware):
    async def process_request(self, request):
        request.state["seen"] = type(self).__name__
        return await self.get_response(request)


class _BaseProcessRequestOnly(BaseMiddleware):
    async def process_request(self, request):
        request.state["seen"] = type(self).__name__
        return await self.get_response(request)


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

    @pytest.mark.parametrize("level", ["api", "router", "route"])
    @pytest.mark.parametrize("entry", [int, str, dict, _NoCall], ids=["int", "str", "dict", "class-with-no-call"])
    def test_each_level_rejects_a_class_whose_instances_are_not_callable(self, level, entry):
        """The chain calls the instance with the request. Such a class gave a 500 for each request."""

        def build():
            if level == "api":
                BoltAPI(middleware=[entry])
            elif level == "router":
                Router(middleware=[entry])
            else:
                middleware(entry)(lambda: None)

        with pytest.raises(TypeError) as raised:
            build()

        assert str(raised.value) == (
            f"{level} middleware class {entry.__name__} has no __call__. "
            "A middleware class takes get_response in __init__, and Bolt calls its instance with the request."
        )

    @pytest.mark.parametrize(
        "entry",
        [_DjangoStyle, _ProcessRequestOnly, _BaseProcessRequestOnly, _InheritsCall],
        ids=["django-style", "middleware-subclass", "base-middleware-subclass", "inherited-call"],
    )
    @pytest.mark.parametrize("level", ["api", "router", "route"])
    def test_each_level_runs_a_class_whose_instances_are_callable(self, level, entry):
        api = BoltAPI(middleware=[entry] if level == "api" else None)
        router = Router(middleware=[entry] if level == "router" else None)

        @router.get("/entry")
        @middleware(*([entry] if level == "route" else []))
        async def handler(request):
            return {"seen": request.state.get("seen")}

        api.include_router(router)

        with TestClient(api) as client:
            response = client.get("/entry")

        assert response.status_code == 200
        assert response.json() == {"seen": entry.__name__}

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

    @pytest.mark.parametrize(
        ("build", "error", "message"),
        [
            (
                lambda: middleware(cors={"origins": ["https://a.test"]}),
                TypeError,
                "@middleware() does not take the keyword argument 'cors'. Use @cors(...).",
            ),
            (
                lambda: middleware(skip=["auth"]),
                TypeError,
                "@middleware() does not take the keyword argument 'skip'. Use @skip_middleware(...).",
            ),
            (
                lambda: middleware(rate_limit={"rps": 1}),
                TypeError,
                "@middleware() does not take the keyword argument 'rate_limit'. Use @rate_limit(...).",
            ),
            (
                lambda: middleware(origins=["https://a.test"]),
                TypeError,
                "@middleware() does not take the keyword argument 'origins'. "
                'Give middleware classes as positional arguments. A dict config needs type="cors" or type="rate_limit".',
            ),
            (
                lambda: middleware(type="bogus"),
                ValueError,
                "route middleware dict has the unknown type 'bogus'. "
                'The known types are "cors" (as @cors(...)) and "rate_limit" (as @rate_limit(...)).',
            ),
            (
                lambda: middleware({"type": "bogus"})(_plain_function),
                ValueError,
                "route middleware dict has the unknown type 'bogus'. "
                'The known types are "cors" (as @cors(...)) and "rate_limit" (as @rate_limit(...)).',
            ),
            (
                lambda: middleware({"origins": ["https://a.test"]})(_plain_function),
                ValueError,
                "route middleware dict has no 'type'. "
                'The known types are "cors" (as @cors(...)) and "rate_limit" (as @rate_limit(...)).',
            ),
            (
                lambda: BoltAPI(middleware=[{"type": "Cors", "origins": ["https://a.test"]}]),
                ValueError,
                "api middleware dict has the unknown type 'Cors'. "
                'The known types are "cors" (as @cors(...)) and "rate_limit" (as @rate_limit(...)).',
            ),
            (
                lambda: Router(middleware=[{"rps": 1}]),
                ValueError,
                "router middleware dict has no 'type'. "
                'The known types are "cors" (as @cors(...)) and "rate_limit" (as @rate_limit(...)).',
            ),
        ],
        ids=[
            "kwarg-cors",
            "kwarg-skip",
            "kwarg-rate-limit",
            "kwarg-other",
            "kwarg-type-unknown",
            "dict-type-unknown",
            "dict-no-type",
            "api-dict-type-case",
            "router-dict-no-type",
        ],
    )
    def test_a_form_that_no_middleware_reads_raises(self, build, error, message):
        # Before, Bolt kept the entry and nothing read it: the route had no such middleware.
        with pytest.raises(error) as raised:
            build()

        assert str(raised.value) == message

    @pytest.mark.parametrize(
        ("config", "error", "message"),
        [
            ({"type": "cors"}, ValueError, "@middleware(type='cors') requires an 'origins' or 'origin_regexes'"),
            ({"type": "cors", "credentials": True}, ValueError, "requires an 'origins' or 'origin_regexes'"),
            (
                {"type": "cors", "origins": 5},
                TypeError,
                "@middleware(type='cors') origins must be a str or a list of str, not int",
            ),
            (
                {"type": "cors", "origin_regexes": [""]},
                ValueError,
                "@middleware(type='cors') origin_regexes holds an empty pattern, which matches every origin.",
            ),
            (
                {"type": "cors", "origins": ["https://a.test"], "methods": "GET"},
                TypeError,
                "@middleware(type='cors') methods must be a list of str, not str 'GET'",
            ),
            (
                {"type": "cors", "origins": ["https://a.test"], "expose_headers": "X-A"},
                TypeError,
                "@middleware(type='cors') expose_headers must be a list of str, not str 'X-A'",
            ),
            (
                {"type": "cors", "origins": ["https://a.test"], "credentials": "yes"},
                TypeError,
                "@middleware(type='cors') credentials must be a bool, not str 'yes'",
            ),
            (
                {"type": "cors", "origins": ["https://a.test"], "max_age": -1},
                ValueError,
                "@middleware(type='cors') max_age must be from 0 to 4294967295, got -1",
            ),
            ({"type": "rate_limit", "rps": 0}, ValueError, "rate_limit rps must be 1 or more, got 0"),
            ({"type": "rate_limit", "burst": 1.5}, TypeError, "rate_limit burst must be an int, got float 1.5"),
            (
                {"type": "rate_limit", "key": "x forwarded for"},
                ValueError,
                'rate_limit key must be "ip", "user", "api_key" or the name of a request header, '
                "got 'x forwarded for'",
            ),
        ],
        ids=[
            "cors-no-origins",
            "cors-only-credentials",
            "cors-origins-int",
            "cors-empty-pattern",
            "cors-methods-str",
            "cors-expose-headers-str",
            "cors-credentials-str",
            "cors-max-age-negative",
            "rate-limit-rps-zero",
            "rate-limit-burst-float",
            "rate-limit-key-space",
        ],
    )
    @pytest.mark.parametrize("form", ["keywords", "dict"])
    def test_the_dict_form_of_a_route_refuses_what_the_decorator_refuses(self, form, config, error, message):
        # Before, @middleware(type="cors") took a config with no origins, and Rust read only the types.
        with pytest.raises(error, match=re.escape(message)):
            if form == "keywords":
                middleware(**config)
            else:
                middleware(config)(lambda: None)

    @pytest.mark.parametrize("level", ["api", "router"])
    @pytest.mark.parametrize(
        ("config", "error", "message"),
        [
            (
                {"type": "rate_limit", "rps": 5, "key": "x forwarded for"},
                ValueError,
                'rate_limit key must be "ip", "user", "api_key" or the name of a request header, '
                "got 'x forwarded for'",
            ),
            (
                {"type": "cors", "origins": ["https://a.test"], "methods": "GET"},
                TypeError,
                "{level} cors dict methods must be a list of str, not str 'GET'",
            ),
            (
                {"type": "cors", "origin_regexes": ""},
                ValueError,
                "{level} cors dict origin_regexes holds an empty pattern, which matches every origin.",
            ),
        ],
        ids=["rate-limit-key", "cors-methods", "cors-empty-pattern"],
    )
    def test_a_dict_of_an_api_or_a_router_gets_the_checks_of_the_decorator(self, level, config, error, message):
        # Before, a rate limit key with a space passed, and each caller had the same bucket.
        with pytest.raises(error, match=re.escape(message.format(level=level))):
            if level == "api":
                BoltAPI(middleware=[config])
            else:
                Router(middleware=[config])

    def test_a_cors_dict_of_an_api_or_a_router_needs_no_origins(self):
        """Such a config keeps the allow list of the outer config. Only a route config needs origins."""
        api = BoltAPI(middleware=[{"type": "cors", "origins": ["https://api.test"]}])
        router = Router(middleware=[{"type": "cors", "credentials": True}])

        @router.get("/inner")
        async def inner():
            return {}

        api.include_router(router)

        with TestClient(api, use_http_layer=True, read_django_settings=False) as client:
            response = client.get("/inner", headers={"Origin": "https://api.test"})

        assert response.headers.get("Access-Control-Allow-Origin") == "https://api.test"
        assert response.headers.get("Access-Control-Allow-Credentials") == "true"

    def test_the_keyword_form_gives_the_route_the_config_of_the_decorator(self):
        api = BoltAPI()

        @api.get("/keywords")
        @middleware(type="cors", origins="https://a.test", credentials=True)
        async def keywords():
            return {}

        @api.get("/limited")
        @middleware(type="rate_limit", rps=1, burst=1, key="X-Client")
        async def limited():
            return {}

        with TestClient(api, use_http_layer=True, read_django_settings=False) as client:
            allowed = client.get("/keywords", headers={"Origin": "https://a.test"})
            other = client.get("/keywords", headers={"Origin": "https://b.test"})
            statuses = [client.get("/limited", headers={"X-Client": "one"}).status_code for _ in range(2)]

        assert allowed.headers.get("Access-Control-Allow-Origin") == "https://a.test"
        assert other.headers.get("Access-Control-Allow-Origin") is None
        assert statuses == [200, 429]

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

    @pytest.mark.parametrize(
        "names",
        [
            (HeaderProbe,),
            ("headerprobe",),
            ("HEADERPROBE",),
            (f"{__name__}.headerprobe",),
            (HeaderProbe, "CORS", "Rate_Limit"),
        ],
        ids=["class", "lower-case-name", "upper-case-name", "dotted-path-case", "class-with-rust-names"],
    )
    def test_skip_middleware_takes_a_class_and_ignores_the_letter_case_of_a_name(self, names):
        api = BoltAPI(middleware=[HeaderProbe])

        @api.get("/skipped")
        @skip_middleware(*names)
        @cors(origins=["https://example.com"])
        @rate_limit(rps=1, burst=1)
        async def skipped():
            return {}

        @api.get("/plain")
        async def plain():
            return {}

        with TestClient(api) as client:
            responses = [client.get("/skipped", headers={"Origin": "https://example.com"}) for _ in range(3)]
            assert client.get("/plain").headers["x-probe"] == "ran"

        assert all("x-probe" not in response.headers for response in responses)
        if len(names) > 1:
            # A class in the names must not hide the names of the Rust middleware.
            assert [response.status_code for response in responses] == [200, 200, 200]
            assert all("access-control-allow-origin" not in response.headers for response in responses)

    @pytest.mark.parametrize("name", [42, None, CustomTestMiddleware(_plain_function), _plain_function])
    def test_skip_middleware_rejects_a_name_that_is_not_a_str_or_a_class(self, name):
        with pytest.raises(
            TypeError, match=r"@skip_middleware\(\) takes the name of a middleware or a middleware class"
        ):
            skip_middleware("cors", name)

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


class ReshapeResponse(Middleware):
    """Replace the headers with a plain dict, set a bytearray body, or read a missing header."""

    async def process_request(self, request):
        response = await self.get_response(request)
        how = request.query.get("how")
        if how == "plain-dict":
            response.headers = {"Content-Type": "text/x-plain", "X-Plain": "1"}
        elif how == "bytearray":
            response.body = bytearray(b"hello")
        elif how == "missing-header":
            response.headers["x-default"] = response.headers.get("X-Nope", "fallback")
        return response


@pytest.mark.parametrize(
    ("how", "content_type", "extra_header", "body"),
    [
        ("plain-dict", "text/x-plain", ("x-plain", "1"), "<p>x</p>"),
        ("bytearray", "text/html; charset=utf-8", None, "hello"),
        ("missing-header", "text/html; charset=utf-8", ("x-default", "fallback"), "<p>x</p>"),
    ],
)
def test_python_middleware_reshapes_the_response(how, content_type, extra_header, body):
    """The client gets the headers and the body that middleware sets, in any container type."""
    api = BoltAPI(middleware=[ReshapeResponse])

    @api.get("/html")
    async def html():
        return HTML("<p>x</p>")

    with TestClient(api) as client:
        response = client.get("/html", params={"how": how})

    assert response.status_code == 200
    assert response.headers.get_list("content-type") == [content_type]
    if extra_header:
        assert response.headers.get(extra_header[0]) == extra_header[1]
    assert response.text == body


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


class TagMiddleware(Middleware):
    """Add the header ``x-api``."""

    header = "x-api"

    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers[self.header] = "tagged"
        return response


class RouterTagMiddleware(TagMiddleware):
    header = "x-router"


class BuildCounting(Middleware):
    """Count the middleware instances that Bolt builds, by class name."""

    built: list[str] = []

    def __init__(self, get_response):
        super().__init__(get_response)
        BuildCounting.built.append(type(self).__name__)

    async def process_request(self, request):
        return await self.get_response(request)


class GlobalCounting(BuildCounting):
    pass


class SkippedCounting(BuildCounting):
    pass


class RouteCounting(BuildCounting):
    pass


def test_bolt_builds_each_middleware_chain_one_time():
    """The chain of the API, the route chain and the own global chain of a route are cached."""
    BuildCounting.built = []
    api = BoltAPI(middleware=[GlobalCounting, SkippedCounting])

    @api.get("/shared")
    async def shared():
        return {"ok": True}

    @api.get("/own")
    @middleware(RouteCounting)
    @skip_middleware("SkippedCounting")
    async def own():
        return {"ok": True}

    with TestClient(api) as client:
        for _ in range(3):
            assert client.get("/shared").status_code == 200
            assert client.get("/own").status_code == 200

    # The chain of the API, then the own global chain of /own and its route chain.
    assert sorted(BuildCounting.built) == ["GlobalCounting", "GlobalCounting", "RouteCounting", "SkippedCounting"]


def test_a_python_middleware_before_a_rust_middleware_runs():
    """A dict spec runs in Rust. The Python middleware before it in the list still runs."""
    rate_limit_spec = {"type": "rate_limit", "rps": 1, "burst": 1, "key": "ip"}
    api = BoltAPI(middleware=[TagMiddleware, rate_limit_spec])
    router = Router(prefix="/router", middleware=[RouterTagMiddleware])

    @api.get("/api")
    async def on_api():
        return {"ok": True}

    @router.get("/items")
    @rate_limit(rps=1, burst=1)
    async def on_router():
        return {"ok": True}

    api.include_router(router)

    with TestClient(api) as client:
        api_response = client.get("/api")
        api_statuses = [client.get("/api").status_code for _ in range(3)]
        router_response = client.get("/router/items")
        router_statuses = [client.get("/router/items").status_code for _ in range(3)]

    assert (api_response.status_code, api_response.headers.get("x-api")) == (200, "tagged")
    assert router_response.status_code == 200
    assert (router_response.headers.get("x-api"), router_response.headers.get("x-router")) == ("tagged", "tagged")
    # The Rust rate limit also runs. A burst of 1 refuses a request that comes
    # less than one second after the last one. A slow runner can refill the
    # bucket between two requests, so the check accepts one 429 of three.
    assert 429 in api_statuses, api_statuses
    assert 429 in router_statuses, router_statuses


def test_a_route_rate_limit_limits_a_router_route_with_no_api_limit():
    api = BoltAPI()
    router = Router(prefix="/router")

    @router.get("/items")
    @rate_limit(rps=1, burst=1)
    async def on_router():
        return {"ok": True}

    api.include_router(router)

    with TestClient(api) as client:
        statuses = [client.get("/router/items").status_code for _ in range(4)]

    assert statuses[0] == 200
    assert 429 in statuses[1:], statuses


_STRICT = {"type": "rate_limit", "rps": 1, "burst": 1}
_LOOSE = {"type": "rate_limit", "rps": 1000}


@pytest.mark.parametrize(
    ("api_limit", "router_limit", "route_limit", "limited"),
    [
        (_LOOSE, None, rate_limit(rps=1, burst=1), True),
        (_STRICT, None, rate_limit(rps=1000), False),
        (_LOOSE, _STRICT, None, True),
        (_STRICT, _LOOSE, None, False),
        (_STRICT, _STRICT, rate_limit(rps=1000), False),
        (_LOOSE, _LOOSE, rate_limit(rps=1, burst=1), True),
        (_STRICT, _STRICT, skip_middleware("rate_limit"), False),
    ],
    ids=[
        "route-over-api",
        "loose-route-over-api",
        "router-over-api",
        "loose-router-over-api",
        "loose-route-over-router",
        "route-over-router",
        "skip-over-all",
    ],
)
def test_the_most_specific_rate_limit_applies(api_limit, router_limit, route_limit, limited):
    """The order is route, router, API. Before, the API limit won over each other limit."""
    api = BoltAPI(middleware=[api_limit])
    router = Router(prefix="/router", middleware=[router_limit] if router_limit else None)

    async def items():
        return {"ok": True}

    router.get("/items")(route_limit(items) if route_limit else items)
    api.include_router(router)

    with TestClient(api) as client:
        statuses = [client.get("/router/items").status_code for _ in range(4)]

    assert statuses[0] == 200
    assert (429 in statuses[1:]) is limited, statuses


class WireTupleMiddleware(Middleware):
    """Answer with a ResponseWireV1 tuple, without the handler."""

    async def process_request(self, request):
        return (418, 0, 0, b'{"teapot":true}')


class ForbidWithJson(Middleware):
    """Answer with a response object, without the handler."""

    async def process_request(self, request):
        response = JSON({"detail": "forbidden"}, status_code=403, headers={"X-Reason": "blocked"})
        response.set_cookie("seen", "1")
        return response


class DictAnswer(Middleware):
    """Answer with a dict, as a handler can."""

    async def process_request(self, request):
        return {"from": "middleware"}


class TextAnswer(Middleware):
    """Answer with a PlainText response."""

    async def process_request(self, request):
        return PlainText("stop", status_code=429)


class MarkResponse(Middleware):
    """Change the response of the inner middleware."""

    async def process_request(self, request):
        response = await self.get_response(request)
        response.status_code = 401
        response.headers["X-Outer"] = "changed"
        return response


def test_a_middleware_can_answer_with_a_wire_tuple():
    api = BoltAPI(middleware=[WireTupleMiddleware])

    @api.get("/tea")
    async def tea():
        return {"handler": True}

    with TestClient(api) as client:
        response = client.get("/tea")

    assert (response.status_code, response.json()) == (418, {"teapot": True})
    assert response.headers["content-type"] == "application/json"


def test_a_middleware_can_answer_with_a_response_object():
    api = BoltAPI(middleware=[ForbidWithJson])

    @api.get("/tea")
    async def tea():
        return {"handler": True}

    with TestClient(api) as client:
        response = client.get("/tea")

    assert (response.status_code, response.json()) == (403, {"detail": "forbidden"})
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-reason"] == "blocked"
    assert response.cookies["seen"] == "1"


@pytest.mark.parametrize(
    ("answer", "status", "content_type", "body"),
    [
        (DictAnswer, 201, "application/json", b'{"from":"middleware"}'),
        (TextAnswer, 429, "text/plain; charset=utf-8", b"stop"),
    ],
)
def test_a_middleware_can_answer_with_what_a_handler_can_return(answer, status, content_type, body):
    api = BoltAPI()

    # A dict gets the status of the route, as a dict from the handler does.
    @api.post("/tea", status_code=201)
    @middleware(answer)
    async def tea():
        return {"handler": True}

    with TestClient(api) as client:
        response = client.post("/tea")

    assert (response.status_code, response.content) == (status, body)
    assert response.headers["content-type"] == content_type


def test_an_outer_middleware_changes_the_response_object_of_an_inner_middleware():
    api = BoltAPI(middleware=[MarkResponse, ForbidWithJson])

    @api.get("/tea")
    async def tea():
        return {"handler": True}

    with TestClient(api) as client:
        response = client.get("/tea")

    assert (response.status_code, response.json()) == (401, {"detail": "forbidden"})
    assert response.headers["x-outer"] == "changed"
    assert response.headers["x-reason"] == "blocked"
    assert response.cookies["seen"] == "1"


class HeaderMapping(Middleware):
    """Use each mapping operation on the response headers, in other letter cases."""

    seen: dict = {}

    async def process_request(self, request):
        response = await self.get_response(request)
        headers = response.headers
        seen = type(self).seen
        seen["in"] = ("X-KEEP" in headers, "x-missing" in headers, 1 in headers)
        seen["getitem"] = headers["X-Keep"]
        try:
            headers["X-Missing"]
        except KeyError:
            seen["getitem_missing"] = "KeyError"
        seen["pop"] = headers.pop("X-POP")
        seen["pop_default"] = (headers.pop("X-Gone", "fallback"), headers.pop("X-Gone", None))
        try:
            headers.pop("X-Gone")
        except KeyError:
            seen["pop_missing"] = "KeyError"
        headers.update({"X-Upd-Dict": "1"})
        headers.update([("X-Upd-Pair", "2")])
        headers.update({"X-Keep": "updated"}, **{"X-Kw": "3"})
        headers |= {"X-Ior-Dict": "4"}
        headers |= [("X-Ior-Pair", "5")]
        return response


def test_python_middleware_response_headers_behave_as_a_case_insensitive_mapping():
    """Python middleware reads and edits the response headers like a dict, whatever the letter case."""
    HeaderMapping.seen = {}
    api = BoltAPI(middleware=[HeaderMapping])

    @api.get("/h")
    async def handler():
        return JSON({"ok": True}, headers={"X-Keep": "kept", "X-Pop": "popped"})

    with TestClient(api) as client:
        response = client.get("/h")

    assert HeaderMapping.seen == {
        "in": (True, False, False),
        "getitem": "kept",
        "getitem_missing": "KeyError",
        "pop": "popped",
        "pop_default": ("fallback", None),
        "pop_missing": "KeyError",
    }
    assert "x-pop" not in response.headers
    assert response.headers["x-keep"] == "updated"
    assert response.headers["x-upd-dict"] == "1"
    assert response.headers["x-upd-pair"] == "2"
    assert response.headers["x-kw"] == "3"
    assert response.headers["x-ior-dict"] == "4"
    assert response.headers["x-ior-pair"] == "5"


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
