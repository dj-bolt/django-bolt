"""
Tests for Django middleware integration with Django-Bolt.

Tests use TestClient for full HTTP cycle testing, verifying that middleware
actually runs and modifies requests/responses through the complete pipeline.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from functools import partial
from types import SimpleNamespace
from typing import Annotated

import msgspec
import pytest
from django.conf import settings
from django.contrib.auth import alogin, alogout
from django.contrib.auth.decorators import login_not_required, login_required
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.middleware.common import CommonMiddleware
from django.middleware.csrf import _does_token_match
from django.template import RequestContext, Template
from django.utils.deprecation import MiddlewareMixin
from django.utils.functional import SimpleLazyObject
from django.views.decorators.csrf import csrf_exempt

from django_bolt import BoltAPI, Request, Router
from django_bolt.auth import IsAuthenticated, JWTAuthentication, create_jwt_for_user
from django_bolt.concurrency import run_orm_blocking
from django_bolt.middleware import (
    DjangoMiddleware,
    DjangoMiddlewareStack,
    Middleware,
    TimingMiddleware,
    skip_middleware,
)
from django_bolt.middleware.django_adapter import _is_django_builtin_middleware, _route_sync_force
from django_bolt.middleware_response import MiddlewareResponse
from django_bolt.param_functions import Cookie, Header
from django_bolt.responses import HTML, JSON, FileResponse, PlainText, Redirect, Response, StreamingResponse
from django_bolt.testing import TestClient


# Define at module level to avoid issues with `from __future__ import annotations`
# Named without "Test" prefix to avoid pytest collection
class SampleRequestBody(msgspec.Struct):
    """Request body for body parsing tests."""

    name: str
    value: int


# =============================================================================
# Test DjangoMiddleware Adapter Creation
# =============================================================================


class TestDjangoMiddlewareAdapter:
    """Tests for the DjangoMiddleware adapter class."""

    def test_django_middleware_creation(self):
        """Test creating DjangoMiddleware wrapper."""
        middleware = DjangoMiddleware(SessionMiddleware)
        assert middleware.middleware_class == SessionMiddleware

    def test_django_middleware_from_string(self):
        """Test creating DjangoMiddleware from import path."""
        middleware = DjangoMiddleware("django.contrib.sessions.middleware.SessionMiddleware")
        assert middleware.middleware_class == SessionMiddleware

    def test_django_middleware_repr(self):
        """Test string representation."""
        middleware = DjangoMiddleware(SessionMiddleware)
        assert repr(middleware) == "DjangoMiddleware(SessionMiddleware)"

    def test_django_middleware_rejects_a_function(self):
        """A function is not a class or a path. The wrapper stops it at configuration."""
        with pytest.raises(TypeError) as raised:
            DjangoMiddleware(lambda request: request)

        assert str(raised.value) == (
            "DjangoMiddleware must be configured with a middleware class before being used in a chain. "
            "Use DjangoMiddleware(SomeMiddlewareClass) to create a wrapper."
        )

    def test_django_middleware_refuses_a_request_before_the_chain_is_built(self):
        """A wrapper that no chain has built has no middleware instance. It raises, and it does not run."""
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(DjangoMiddleware(SessionMiddleware)._call(None))

        assert str(raised.value) == (
            "DjangoMiddleware was not properly initialized. "
            "The middleware chain must be built before processing requests."
        )


# =============================================================================
# Test DjangoMiddlewareStack
# =============================================================================


class TestDjangoMiddlewareStack:
    """Tests for DjangoMiddlewareStack."""

    def test_middleware_stack_creation(self):
        """Test creating DjangoMiddlewareStack."""
        stack = DjangoMiddlewareStack([SessionMiddleware, CommonMiddleware])
        assert len(stack.middleware_classes) == 2

    def test_middleware_stack_repr(self):
        """Test string representation of stack."""
        stack = DjangoMiddlewareStack([SessionMiddleware, CommonMiddleware])
        assert repr(stack) == "DjangoMiddlewareStack([SessionMiddleware, CommonMiddleware])"


# =============================================================================
# Test Full HTTP Cycle - Session Middleware
# =============================================================================


@pytest.mark.django_db
class TestSessionMiddlewareHTTPCycle:
    """Tests for SessionMiddleware through full HTTP cycle."""

    def test_session_middleware_basic(self):
        """Test SessionMiddleware runs through HTTP cycle."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    def test_session_middleware_with_session_access(self):
        """Test SessionMiddleware sets session attribute on request."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        session_accessed = {"accessed": False}

        @api.get("/test")
        async def test_route(request):
            # Django session should be available via request.state["session"]
            session = request.state.get("session")
            if session is not None:
                session_accessed["accessed"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Session should have been accessible
            assert session_accessed["accessed"] is True


# =============================================================================
# Test Full HTTP Cycle - Authentication Middleware
# =============================================================================


@pytest.mark.django_db
class TestAuthMiddlewareHTTPCycle:
    """Tests for AuthenticationMiddleware through full HTTP cycle."""

    def test_auth_middleware_sets_user(self):
        """Test AuthenticationMiddleware sets user on request."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        user_set = {"has_user": False}

        @api.get("/test")
        async def test_route(request):
            # Django auth middleware sets request.user
            if hasattr(request, "user") and request.user is not None:
                user_set["has_user"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # User should have been set (AnonymousUser for unauthenticated)
            assert user_set["has_user"] is True


# =============================================================================
# Test Bolt route auth vs Django AuthenticationMiddleware precedence
# =============================================================================


def _make_jwt(user) -> str:
    return create_jwt_for_user(user, secret="test-secret")


@pytest.mark.django_db(transaction=True)
class TestBoltAuthUserPrecedence:
    """Bolt route-level auth (JWT) must not be clobbered by Django's
    session-based AuthenticationMiddleware, which sets AnonymousUser for
    requests that carry no Django session."""

    def test_jwt_user_survives_authentication_middleware(self):
        """Regression: request.user was AnonymousUser and the backend's custom
        get_user never ran when AuthenticationMiddleware was installed."""
        get_user_calls: list[str] = []

        class TrackingJWTAuth(JWTAuthentication):
            async def get_user(self, user_id, auth_context):
                get_user_calls.append(user_id)
                return await User.objects.aget(pk=user_id)

        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        @api.get("/jwt-me", auth=[TrackingJWTAuth(secret="test-secret")], guards=[IsAuthenticated()])
        async def jwt_me(request):
            user = request.user
            auser = await request.auser()
            return {
                "username": getattr(user, "username", None),
                "is_anonymous": bool(getattr(user, "is_anonymous", True)),
                "auser_username": getattr(auser, "username", None),
            }

        user = User.objects.create(username="jwt_user")

        with TestClient(api) as client:
            response = client.get("/jwt-me", headers={"Authorization": f"Bearer {_make_jwt(user)}"})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["is_anonymous"] is False
            assert data["username"] == "jwt_user"
            assert data["auser_username"] == "jwt_user"
            assert get_user_calls == [str(user.pk)]

    def test_jwt_user_survives_middleware_that_evaluates_anonymous_user(self):
        """A middleware peeking at request.user (forcing the lazy session
        lookup to AnonymousUser) must still not clobber the Bolt user."""

        peeked = {}

        class PeekingMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            def __call__(self, request):
                # Forces evaluation of AuthenticationMiddleware's lazy user
                peeked["is_authenticated"] = request.user.is_authenticated
                return self.get_response(request)

        api = BoltAPI(
            middleware=[DjangoMiddlewareStack([SessionMiddleware, AuthenticationMiddleware, PeekingMiddleware])]
        )

        @api.get("/jwt-peeked", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
        async def jwt_peeked(request):
            # An async handler behind Django middleware loads its user with auser().
            user = await request.auser()
            return {"username": getattr(user, "username", None)}

        user = User.objects.create(username="peeked_user")

        with TestClient(api) as client:
            response = client.get("/jwt-peeked", headers={"Authorization": f"Bearer {_make_jwt(user)}"})
            assert response.status_code == 200, response.text
            assert response.json()["username"] == "peeked_user"
            # Proves the middleware ran and evaluated the session-based user
            assert peeked == {"is_authenticated": False}

    def test_jwt_user_survives_peeking_middleware_in_a_sync_handler(self):
        """The sync ``request.user`` of a lane request keeps the Bolt user too."""

        peeked = {}

        class PeekingMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            def __call__(self, request):
                peeked["is_authenticated"] = request.user.is_authenticated
                return self.get_response(request)

        api = BoltAPI(
            middleware=[DjangoMiddlewareStack([SessionMiddleware, AuthenticationMiddleware, PeekingMiddleware])]
        )

        @api.get("/jwt-peeked", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
        def jwt_peeked(request):
            return {"username": getattr(request.user, "username", None)}

        user = User.objects.create(username="peeked_sync_user")

        with TestClient(api) as client:
            response = client.get("/jwt-peeked", headers={"Authorization": f"Bearer {_make_jwt(user)}"})
            assert response.status_code == 200, response.text
            assert response.json()["username"] == "peeked_sync_user"
            assert peeked == {"is_authenticated": False}

    def test_middleware_that_authenticates_user_still_wins(self):
        """A middleware that explicitly sets an authenticated user
        (login/impersonation) overrides the Bolt-auth user."""

        class ImpersonationMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            def __call__(self, request):
                request.user = User.objects.get(username="impersonated")
                return self.get_response(request)

        api = BoltAPI(middleware=[DjangoMiddlewareStack([ImpersonationMiddleware])])

        @api.get("/impersonated", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
        async def impersonated_route(request):
            return {"username": request.user.username}

        jwt_user = User.objects.create(username="original_jwt_user")
        User.objects.create(username="impersonated")

        with TestClient(api) as client:
            response = client.get("/impersonated", headers={"Authorization": f"Bearer {_make_jwt(jwt_user)}"})
            assert response.status_code == 200, response.text
            assert response.json()["username"] == "impersonated"

    def test_lazy_user_that_nobody_read_is_not_loaded(self):
        """Django sets a lazy user. The Bolt user wins, so the lazy user does no query."""
        loads: list[str] = []

        class LazyAnonymousMiddleware(MiddlewareMixin):
            def process_request(self, request):
                def load():
                    loads.append("loaded")
                    return AnonymousUser()

                request.user = SimpleLazyObject(load)

        api = BoltAPI(middleware=[DjangoMiddlewareStack([LazyAnonymousMiddleware])])

        @api.get("/lazy", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
        async def lazy_route(request):
            user = await request.auser()
            return {"username": user.username}

        user = User.objects.create(username="lazy_user")

        with TestClient(api) as client:
            response = client.get("/lazy", headers={"Authorization": f"Bearer {_make_jwt(user)}"})

        assert response.status_code == 200, response.text
        assert response.json() == {"username": "lazy_user"}
        assert loads == []

    def test_user_that_has_no_is_authenticated_never_replaces_the_bolt_user(self):
        """A value that is not a user object does not count as an authenticated user."""

        class NotAUserMiddleware(MiddlewareMixin):
            def process_request(self, request):
                request.user = SimpleNamespace(username="not-a-user")

        api = BoltAPI(middleware=[DjangoMiddlewareStack([NotAUserMiddleware])])

        @api.get("/not-a-user", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
        async def not_a_user_route(request):
            return {"username": request.user.username}

        user = User.objects.create(username="real_user")

        with TestClient(api) as client:
            response = client.get("/not-a-user", headers={"Authorization": f"Bearer {_make_jwt(user)}"})

        assert response.status_code == 200, response.text
        assert response.json() == {"username": "real_user"}


# =============================================================================
# Test Full HTTP Cycle - Custom Middleware
# =============================================================================


class HeaderAddingMiddleware:
    """Custom Django middleware that adds a header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Custom-Header"] = "test-value"
        return response


class ShortCircuitMiddleware:
    """Django middleware that returns early."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/blocked":
            return HttpResponse("Blocked by middleware", status=403)
        return self.get_response(request)


@pytest.mark.django_db
class TestCustomMiddlewareHTTPCycle:
    """Tests for custom Django middleware through full HTTP cycle."""

    def test_custom_middleware_adds_header(self):
        """Test custom middleware that adds response header."""
        api = BoltAPI(middleware=[DjangoMiddlewareStack([HeaderAddingMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            assert response.headers.get("X-Custom-Header") == "test-value"

    def test_middleware_short_circuit(self):
        """Test middleware can return early without calling handler."""
        api = BoltAPI(middleware=[DjangoMiddlewareStack([ShortCircuitMiddleware])])

        handler_called = {"called": False}

        @api.get("/blocked")
        async def blocked_route():
            handler_called["called"] = True
            return {"status": "ok"}

        @api.get("/allowed")
        async def allowed_route():
            return {"status": "allowed"}

        with TestClient(api) as client:
            # Blocked path should be short-circuited
            response = client.get("/blocked")
            assert response.status_code == 403
            assert b"Blocked by middleware" in response.content
            assert handler_called["called"] is False

            # Allowed path should work
            response = client.get("/allowed")
            assert response.status_code == 200
            assert response.json() == {"status": "allowed"}


class TenantHookMiddleware(MiddlewareMixin):
    """Hook middleware that sets a custom request attribute, as django-tenants does."""

    def process_request(self, request):
        request.tenant = "acme"


class TenantCallMiddleware:
    """__call__ middleware that sets a custom request attribute."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tenant = "acme"
        return self.get_response(request)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "middleware",
    [
        lambda: DjangoMiddlewareStack([TenantHookMiddleware]),
        lambda: DjangoMiddlewareStack([TenantCallMiddleware]),
        lambda: DjangoMiddlewareStack([TenantHookMiddleware, HeaderAddingMiddleware]),
        lambda: DjangoMiddleware(TenantHookMiddleware),
    ],
    ids=["hook_stack", "call_stack", "mixed_stack", "single_wrapper"],
)
def test_custom_request_attribute_reaches_async_handler(middleware):
    """An async handler reads a custom middleware attribute from request.state."""
    api = BoltAPI(middleware=[middleware()])

    @api.get("/tenant")
    async def tenant(request: Request):
        return {"tenant": request.state.get("tenant")}

    with TestClient(api) as client:
        assert client.get("/tenant").json() == {"tenant": "acme"}


class _ReadBodyMiddleware(MiddlewareMixin):
    """Read the body in process_request, as a logging or signing middleware does."""

    def process_request(self, request):
        request.read_result = request.read()
        request.body_result = request.body


@pytest.mark.parametrize(
    "middleware",
    [
        lambda: DjangoMiddleware(_ReadBodyMiddleware),
        lambda: DjangoMiddlewareStack([_ReadBodyMiddleware]),
    ],
    ids=["DjangoMiddleware", "DjangoMiddlewareStack"],
)
class TestMiddlewareReadsBody:
    """A middleware reads the body of a request with no body as b""."""

    def test_get_without_body_reads_empty_bytes(self, middleware):
        api = BoltAPI(middleware=[middleware()])

        @api.get("/read")
        async def read(request: Request):
            return {
                "read": request.state["read_result"].decode(),
                "body": request.state["body_result"].decode(),
            }

        with TestClient(api) as client:
            response = client.get("/read")
            assert response.status_code == 200
            assert response.json() == {"read": "", "body": ""}

    def test_post_with_body_reads_the_body(self, middleware):
        api = BoltAPI(middleware=[middleware()])

        @api.post("/read")
        async def read(request: Request):
            return {
                "read": request.state["read_result"].decode(),
                "body": request.state["body_result"].decode(),
            }

        with TestClient(api) as client:
            response = client.post("/read", content=b"payload")
            assert response.status_code == 200
            assert response.json() == {"read": "payload", "body": "payload"}


# =============================================================================
# Test Full HTTP Cycle - Middleware Chaining
# =============================================================================


class Order1Middleware:
    """First middleware in chain - adds header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Order-1"] = "first"
        return response


class Order2Middleware:
    """Second middleware in chain - adds header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Order-2"] = "second"
        return response


@pytest.mark.django_db
class TestMiddlewareChainingHTTPCycle:
    """Tests for middleware chaining through full HTTP cycle."""

    def test_multiple_middlewares_all_run(self):
        """Test that multiple middlewares in chain all execute."""
        api = BoltAPI(middleware=[DjangoMiddlewareStack([Order1Middleware, Order2Middleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Both middlewares should have added their headers
            assert response.headers.get("X-Order-1") == "first"
            assert response.headers.get("X-Order-2") == "second"


# =============================================================================
# Test Full HTTP Cycle - Error Handling
# =============================================================================


class ExceptionCatchingMiddleware:
    """Middleware that catches and handles exceptions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except ValueError as e:
            return HttpResponse(f"Caught error: {e}", status=400)


@pytest.mark.django_db
class TestErrorHandlingHTTPCycle:
    """Tests for error handling in middleware through HTTP cycle."""

    def test_middleware_catches_exception(self):
        """Test that middleware can catch and handle exceptions."""
        api = BoltAPI(middleware=[DjangoMiddlewareStack([ExceptionCatchingMiddleware])])

        @api.get("/error")
        async def error_route():
            raise ValueError("test error")

        with TestClient(api, raise_server_exceptions=False) as client:
            response = client.get("/error")
            assert response.status_code == 400
            assert b"Caught error: test error" in response.content


# =============================================================================
# Test Full HTTP Cycle - Mixed Bolt and Django Middleware
# =============================================================================


@pytest.mark.django_db
class TestMixedMiddlewareHTTPCycle:
    """Tests for mixing Bolt native and Django middleware."""

    def test_bolt_and_django_middleware_together(self):
        """Test Bolt middleware and Django middleware work together."""
        api = BoltAPI(
            django_middleware=["django.contrib.sessions.middleware.SessionMiddleware"],
            middleware=[TimingMiddleware],
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # TimingMiddleware should have added its header (X-Response-Time)
            assert "X-Response-Time" in response.headers or "x-response-time" in response.headers


# =============================================================================
# Test Request/Response Conversion (Unit Tests)
# =============================================================================


@pytest.mark.django_db
class TestMessagesFramework:
    """Test Django messages framework works with Django-Bolt middleware."""

    def test_messages_framework_accessible_via_request(self):
        """
        Test that Django's messages framework works through the middleware stack.

        This test verifies:
        1. MessageMiddleware sets request._messages on Django request
        2. _messages is synced to Bolt request.state["_messages"]
        3. request._messages is accessible via __getattr__ (reads from state)
        4. Messages added via django.contrib.messages are actually stored

        This test WILL FAIL if:
        - _sync_request_attributes doesn't sync _messages
        - __getattr__ doesn't read from state dict
        - MessageMiddleware isn't working
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.messages.middleware.MessageMiddleware",
            ]
        )

        captured = {"messages_storage": None, "message_count": 0}

        @api.get("/test")
        async def test_route(request):
            from django.contrib import messages  # noqa: PLC0415

            # Add messages - this requires _messages to be set by MessageMiddleware
            messages.info(request, "Test info message")
            messages.success(request, "Test success message")

            # Access _messages directly - this uses __getattr__ to read from state
            # If this fails, the messages framework isn't working
            messages_storage = request._messages
            captured["messages_storage"] = messages_storage
            captured["message_count"] = len(messages_storage)

            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200

            # Verify messages were stored - this is the actual test
            # If _messages wasn't synced, this will be None or 0
            assert captured["messages_storage"] is not None, (
                "request._messages was not accessible - __getattr__ or _sync_request_attributes broken"
            )
            assert captured["message_count"] == 2, (
                f"Expected 2 messages, got {captured['message_count']} - MessageMiddleware not working"
            )


# =============================================================================
# Test Middleware Categorization (Django built-in vs Third-party vs __call__-only)
# =============================================================================


class HookBasedThirdPartyMiddleware:
    """
    Third-party middleware with process_request/process_response hooks.

    This simulates a third-party middleware that might do blocking I/O in hooks.
    Should be routed through sync_to_async for safety.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def process_request(self, request):
        """Hook that runs before view - could do blocking I/O."""
        request.thirdparty_hook_ran = True
        return None  # Continue processing

    def process_response(self, request, response):
        """Hook that runs after view - could do blocking I/O."""
        response["X-ThirdParty-Hook"] = "processed"
        return response

    def __call__(self, request):
        # MiddlewareMixin pattern - hooks are called by __call__
        response = self.process_request(request)
        if response is not None:
            return response
        response = self.get_response(request)
        return self.process_response(request, response)


class CallOnlyMiddleware:
    """
    Middleware that only overrides __call__ (no hooks).

    This is the slowest path - requires wrapping in sync_to_async chain.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Set attribute before
        request.call_only_before = True
        response = self.get_response(request)
        # Add header after
        response["X-Call-Only"] = "processed"
        return response


class HookShortCircuitMiddleware:
    """Third-party middleware that short-circuits via process_request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def process_request(self, request):
        """Short-circuit if special header is present."""
        if request.META.get("HTTP_X_SHORTCIRCUIT"):
            return HttpResponse("Short-circuited by hook", status=418)
        return None

    def __call__(self, request):
        response = self.process_request(request)
        if response is not None:
            return response
        return self.get_response(request)


@pytest.mark.django_db
class TestMiddlewareCategorization:
    """Tests for middleware categorization into fast/safe/slow paths."""

    def test_django_builtin_uses_fast_path(self):
        """
        Test that Django safe built-in middleware uses fast path (direct calls).

        Only middleware that doesn't do blocking I/O (like CommonMiddleware)
        uses the fast path. SessionMiddleware, AuthenticationMiddleware, and
        MessageMiddleware are now routed through sync_to_async because they
        can perform blocking database I/O (e.g., session.save()).
        """
        # Verify classification - CommonMiddleware is safe (no blocking I/O)
        assert _is_django_builtin_middleware(CommonMiddleware) is True
        # SessionMiddleware is NOT safe (can do blocking I/O in process_response)
        assert _is_django_builtin_middleware(SessionMiddleware) is False

        # Test through HTTP cycle
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.common.CommonMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route(request):
            return {"session_exists": request.state.get("session") is not None}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200

    def test_thirdparty_hook_middleware_uses_safe_path(self):
        """
        Test that third-party middleware with hooks uses safe path (sync_to_async).

        Third-party middleware might do blocking I/O in hooks, so we wrap
        them in sync_to_async(thread_sensitive=True) for safety.
        """
        # Verify classification - third-party should NOT be Django built-in
        assert _is_django_builtin_middleware(HookBasedThirdPartyMiddleware) is False

        # Test through HTTP cycle
        api = BoltAPI(middleware=[DjangoMiddlewareStack([HookBasedThirdPartyMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Verify the hook ran and added header
            assert response.headers.get("X-ThirdParty-Hook") == "processed"

    def test_call_only_middleware_uses_chain_path(self):
        """
        Test that __call__-only middleware uses chain path.

        Middleware without hooks must be chained and run via sync_to_async.
        """
        api = BoltAPI(middleware=[DjangoMiddlewareStack([CallOnlyMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            assert response.headers.get("X-Call-Only") == "processed"

    def test_mixed_middleware_types(self):
        """
        Test mixing Django built-in, third-party hooks, and __call__-only.

        All three types should work together correctly.
        """
        api = BoltAPI(
            middleware=[
                DjangoMiddlewareStack(
                    [
                        SessionMiddleware,  # Django built-in (fast path)
                        HookBasedThirdPartyMiddleware,  # Third-party hooks (safe path)
                        CallOnlyMiddleware,  # __call__-only (chain path)
                    ]
                )
            ]
        )

        results = {}

        @api.get("/test")
        async def test_route(request):
            results["session"] = request.state.get("session") is not None
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # All middleware should have run
            assert results["session"] is True
            assert response.headers.get("X-ThirdParty-Hook") == "processed"
            assert response.headers.get("X-Call-Only") == "processed"

    def test_thirdparty_hook_short_circuit(self):
        """
        Test that third-party middleware can short-circuit via process_request.

        Even though third-party hooks go through sync_to_async, they should
        still be able to return early responses.
        """
        api = BoltAPI(middleware=[DjangoMiddlewareStack([HookShortCircuitMiddleware])])

        handler_called = {"called": False}

        @api.get("/test")
        async def test_route():
            handler_called["called"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            # Without header - should pass through
            response = client.get("/test")
            assert response.status_code == 200
            assert handler_called["called"] is True

            # Reset
            handler_called["called"] = False

            # With header - should short-circuit
            response = client.get("/test", headers={"X-Shortcircuit": "yes"})
            assert response.status_code == 418
            assert b"Short-circuited by hook" in response.content
            assert handler_called["called"] is False


@pytest.mark.django_db
class TestDjangoMiddlewareHookBehavior:
    """Behavior-focused tests for hook execution order and short-circuit semantics."""

    def test_process_request_short_circuit_still_runs_process_response(self):
        class RequestShortCircuitMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            def process_request(self, request):
                return HttpResponse("blocked early", status=418)

            def process_response(self, request, response):
                response["X-Response-Hook"] = "applied"
                return response

            def __call__(self, request):
                response = self.process_request(request)
                if response is None:
                    response = self.get_response(request)
                return self.process_response(request, response)

        api = BoltAPI(middleware=[DjangoMiddlewareStack([RequestShortCircuitMiddleware])])

        handler_called = {"called": False}

        @api.get("/test")
        async def test_route():
            handler_called["called"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 418
            assert response.headers.get("X-Response-Hook") == "applied"
            assert handler_called["called"] is False

    def test_process_view_short_circuit_still_runs_process_response(self):
        class ViewShortCircuitMiddleware:
            def __init__(self, get_response):
                self.get_response = get_response

            def process_view(self, request, view_func, view_args, view_kwargs):
                return HttpResponse("blocked in process_view", status=409)

            def process_response(self, request, response):
                response["X-View-Response-Hook"] = "applied"
                return response

            def __call__(self, request):
                return self.get_response(request)

        api = BoltAPI(middleware=[DjangoMiddlewareStack([ViewShortCircuitMiddleware])])

        handler_called = {"called": False}

        @api.get("/test")
        async def test_route():
            handler_called["called"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 409
            assert response.headers.get("X-View-Response-Hook") == "applied"
            assert handler_called["called"] is False

    def test_process_view_only_middleware_invocation_and_short_circuit(self):
        class ProcessViewOnlyMiddleware:
            call_count = 0

            def __init__(self, get_response):
                self.get_response = get_response

            def process_view(self, request, view_func, view_args, view_kwargs):
                type(self).call_count += 1
                if request.META.get("HTTP_X_STOP"):
                    return HttpResponse("stopped by process_view", status=406)
                return None

            def __call__(self, request):
                return self.get_response(request)

        api = BoltAPI(middleware=[DjangoMiddlewareStack([ProcessViewOnlyMiddleware])])

        handler_called = {"count": 0}

        @api.get("/test")
        async def test_route():
            handler_called["count"] += 1
            return {"status": "ok"}

        ProcessViewOnlyMiddleware.call_count = 0
        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

            response = client.get("/test", headers={"X-Stop": "1"})
            assert response.status_code == 406
            assert b"stopped by process_view" in response.content

        assert ProcessViewOnlyMiddleware.call_count == 2
        assert handler_called["count"] == 1

    def test_mixed_hook_and_call_only_stack_preserves_declared_order(self):
        events = []

        class HookA:
            def __init__(self, get_response):
                self.get_response = get_response

            def process_request(self, request):
                events.append("a:request")
                return None

            def process_view(self, request, view_func, view_args, view_kwargs):
                events.append("a:view")
                return None

            def process_response(self, request, response):
                events.append("a:response")
                return response

            def __call__(self, request):
                return self.get_response(request)

        class CallOnly:
            def __init__(self, get_response):
                self.get_response = get_response

            def __call__(self, request):
                events.append("call:before")
                response = self.get_response(request)
                events.append("call:after")
                return response

        class HookB:
            def __init__(self, get_response):
                self.get_response = get_response

            def process_request(self, request):
                events.append("b:request")
                return None

            def process_view(self, request, view_func, view_args, view_kwargs):
                events.append("b:view")
                return None

            def process_response(self, request, response):
                events.append("b:response")
                return response

            def __call__(self, request):
                return self.get_response(request)

        api = BoltAPI(middleware=[DjangoMiddlewareStack([HookA, CallOnly, HookB])])

        @api.get("/ordered")
        async def ordered_route():
            events.append("handler")
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/ordered")
            assert response.status_code == 200

        assert events == [
            "a:request",
            "call:before",
            "b:request",
            "a:view",
            "b:view",
            "handler",
            "b:response",
            "call:after",
            "a:response",
        ]


class TestMiddlewareCategorizationUnit:
    """Unit tests for middleware categorization helper functions."""

    def test_is_django_builtin_middleware_sessions(self):
        """Test SessionMiddleware is NOT recognized as Django safe built-in.

        SessionMiddleware can do blocking database I/O in process_response
        when saving the session, so it needs sync_to_async wrapping.
        """
        assert _is_django_builtin_middleware(SessionMiddleware) is False

    def test_is_django_builtin_middleware_common(self):
        """Test CommonMiddleware is recognized as Django safe built-in."""
        assert _is_django_builtin_middleware(CommonMiddleware) is True

    def test_is_django_builtin_middleware_auth(self):
        """Test AuthenticationMiddleware is NOT recognized as Django safe built-in.

        AuthenticationMiddleware can do blocking database I/O when loading
        the user, so it needs sync_to_async wrapping.
        """
        assert _is_django_builtin_middleware(AuthenticationMiddleware) is False

    def test_is_django_builtin_middleware_messages(self):
        """Test MessageMiddleware is NOT recognized as Django safe built-in.

        MessageMiddleware can do blocking database I/O when storing messages,
        so it needs sync_to_async wrapping.
        """
        assert _is_django_builtin_middleware(MessageMiddleware) is False

    def test_is_django_builtin_middleware_thirdparty(self):
        """Test third-party middleware is NOT recognized as Django built-in."""
        assert _is_django_builtin_middleware(HookBasedThirdPartyMiddleware) is False
        assert _is_django_builtin_middleware(CallOnlyMiddleware) is False
        assert _is_django_builtin_middleware(HeaderAddingMiddleware) is False

    def test_stack_categorizes_middleware_correctly(self):
        """Test DjangoMiddlewareStack correctly categorizes middleware.

        SessionMiddleware is now categorized as third-party (needs sync_to_async)
        because it can do blocking database I/O.
        """
        stack = DjangoMiddlewareStack(
            [
                SessionMiddleware,  # Now third-party (can do blocking I/O)
                HookBasedThirdPartyMiddleware,  # Third-party with hooks
                CallOnlyMiddleware,  # __call__-only
            ]
        )

        # Trigger categorization by creating instances
        def dummy(r):
            pass

        stack._create_middleware_instance(dummy)

        # Verify categorization - SessionMiddleware is now in thirdparty
        assert len(stack._django_hook_middleware) == 0  # No Django safe middleware
        assert len(stack._thirdparty_hook_middleware) == 2  # SessionMiddleware + HookBasedThirdPartyMiddleware
        assert stack._compatibility_chain is not None
        assert stack._call_middleware_chain is None

    def test_stack_no_call_only_middleware(self):
        """Test stack without __call__-only middleware has no chain."""
        stack = DjangoMiddlewareStack(
            [
                SessionMiddleware,  # Django built-in with hooks
                HookBasedThirdPartyMiddleware,  # Third-party with hooks
            ]
        )

        def dummy(r):
            pass

        stack._create_middleware_instance(dummy)

        # Verify no chain created (fast path)
        assert stack._call_middleware_chain is None
        assert stack._compatibility_chain is None


class TestRequestConversion:
    """Unit tests for request conversion - using real middleware through HTTP cycle."""

    def test_query_params_available(self):
        """Test query params are available in handler."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        received_params = {}

        @api.get("/test")
        async def test_route(request, page: int = 1, limit: int = 10):
            received_params["page"] = page
            received_params["limit"] = limit
            return {"page": page, "limit": limit}

        with TestClient(api) as client:
            response = client.get("/test?page=5&limit=20")
            assert response.status_code == 200
            assert received_params["page"] == 5
            assert received_params["limit"] == 20

    def test_cookies_available(self):
        """Test cookies are available in handler."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route(request):
            return {"cookies": dict(request.cookies)}

        with TestClient(api) as client:
            response = client.get("/test", headers={"Cookie": "test_cookie=value"})
            assert response.status_code == 200
            assert response.json() == {"cookies": {"test_cookie": "value"}}

    def test_headers_available(self):
        """Test headers are available in handler."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        received_header = {"value": None}

        @api.get("/test")
        async def test_route(request):
            received_header["value"] = request.headers.get("x-custom-header")
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test", headers={"X-Custom-Header": "test-value"})
            assert response.status_code == 200
            assert received_header["value"] == "test-value"

    def test_body_available(self):
        """Test body is available in handler."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        @api.post("/test")
        async def test_route(body: SampleRequestBody):
            return {"received_name": body.name, "received_value": body.value}

        with TestClient(api) as client:
            response = client.post("/test", json={"name": "test", "value": 42})
            assert response.status_code == 200
            assert response.json() == {"received_name": "test", "received_value": 42}


class _DjangoRequestRecorder(MiddlewareMixin):
    """Record what a Django middleware reads from the request that Bolt converted."""

    seen: dict = {}

    def process_request(self, request):
        try:
            request.POST["added"] = "1"
            post_is_mutable = True
        except AttributeError:
            post_is_mutable = False
        type(self).seen = {
            "method": request.method,
            "path": request.path,
            "path_info": request.path_info,
            "get_tags": request.GET.getlist("tag"),
            "get_x": request.GET.get("x"),
            "get_is_mutable": request.GET._mutable,
            "post": dict(request.POST.lists()),
            "post_is_mutable": post_is_mutable,
            "read": request.read() if request.body else b"",
            "body": request.body,
        }


@pytest.mark.parametrize(
    "layout",
    [lambda: DjangoMiddleware(_DjangoRequestRecorder), lambda: DjangoMiddlewareStack([_DjangoRequestRecorder])],
    ids=["wrapper", "stack"],
)
class TestDjangoSideRequest:
    """A Django middleware sees a request that a Django server would build from the same HTTP request."""

    def _send(self, layout, method, path, **kwargs):
        _DjangoRequestRecorder.seen = {}
        api = BoltAPI(middleware=[layout()])

        @getattr(api, method.lower())(path.split("?")[0])
        async def route(tag: list[str] | None = None):
            # The handler declares a list parameter, so Bolt keeps each value of a repeated key.
            return {"ok": True}

        with TestClient(api) as client:
            assert client.request(method, path, **kwargs).status_code == 200
        return _DjangoRequestRecorder.seen

    def test_repeated_query_key_is_a_list_and_the_query_can_change(self, layout):
        seen = self._send(layout, "GET", "/query?tag=a&tag=b&x=1")

        assert seen["method"] == "GET"
        assert seen["path"] == seen["path_info"] == "/query"
        assert seen["get_tags"] == ["a", "b"]
        assert seen["get_x"] == "1"
        assert seen["get_is_mutable"] is True
        assert seen["post"] == {}
        assert seen["read"] == seen["body"] == b""

    def test_request_with_no_query_has_an_empty_query_dict(self, layout):
        seen = self._send(layout, "GET", "/plain")

        assert seen["get_tags"] == []
        assert seen["get_x"] is None

    def test_form_body_is_parsed_into_an_immutable_post(self, layout):
        seen = self._send(
            layout,
            "POST",
            "/form",
            content=b"a=1&a=2&b=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        assert seen["post"] == {"a": ["1", "2"], "b": ["x"]}
        assert seen["post_is_mutable"] is False
        assert seen["read"] == seen["body"] == b"a=1&a=2&b=x"

    @pytest.mark.parametrize("content_type", ["application/json", None], ids=["json", "no-content-type"])
    def test_other_body_is_not_parsed_as_a_form(self, layout, content_type):
        headers = {"Content-Type": content_type} if content_type else {}
        seen = self._send(layout, "POST", "/raw", content=b'{"a": "1"}', headers=headers)

        assert seen["post"] == {}
        assert seen["read"] == seen["body"] == b'{"a": "1"}'


# =============================================================================
# Test Django Security Features (login_required, PermissionDenied)
# =============================================================================


@pytest.mark.django_db
class TestLoginRequiredBehavior:
    """Tests for Django's login_required decorator behavior."""

    def test_login_required_redirects_anonymous_user(self):
        """
        Test that @login_required redirects unauthenticated users.

        When an anonymous user tries to access a protected view,
        Django should redirect to LOGIN_URL (default: /accounts/login/).
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        handler_called = {"called": False}

        @api.get("/protected")
        @login_required
        async def protected_route(request):
            handler_called["called"] = True
            return {"status": "secret data"}

        with TestClient(api, raise_server_exceptions=False) as client:
            client.get("/protected")
            # Should redirect to login page (302) or we get redirected (200 to login form)
            # TestClient follows redirects by default, so we check the handler wasn't called
            assert handler_called["called"] is False

    def test_request_has_django_methods(self):
        """
        Test that Bolt request has Django-compatible methods.

        These methods are needed for Django decorators like @login_required
        to work correctly.
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        captured = {"get_full_path": None, "build_absolute_uri": None}

        @api.get("/test")
        async def test_route(request):
            captured["get_full_path"] = request.get_full_path()
            captured["build_absolute_uri"] = request.build_absolute_uri()
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test?page=1&limit=10")
            assert response.status_code == 200
            # Verify Django-compatible methods exist and work
            assert "page=" in captured["get_full_path"]
            assert "limit=" in captured["get_full_path"]
            assert captured["build_absolute_uri"].startswith("http")


@pytest.mark.django_db
class TestPermissionDenied:
    """Tests for Django PermissionDenied exception handling."""

    def test_permission_denied_returns_403(self):
        """
        Test that raising PermissionDenied returns 403 response.

        When a view raises PermissionDenied, Django should return
        a 403 Forbidden response.
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        @api.get("/admin-only")
        async def admin_only_route(request):
            # Anonymous users are not authenticated
            if not request.user.is_authenticated:
                raise PermissionDenied("Authentication required")
            return {"status": "admin data"}

        with TestClient(api, raise_server_exceptions=False) as client:
            response = client.get("/admin-only")
            assert response.status_code == 403

    def test_authenticated_user_check(self):
        """
        Test checking user.is_authenticated in handler.

        This verifies Django's AuthenticationMiddleware sets request.user
        to AnonymousUser for unauthenticated requests.
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        user_info = {"is_authenticated": None, "is_anonymous": None}

        @api.get("/check-user")
        async def check_user_route(request):
            user_info["is_authenticated"] = request.user.is_authenticated
            user_info["is_anonymous"] = request.user.is_anonymous
            return {"authenticated": request.user.is_authenticated}

        with TestClient(api) as client:
            response = client.get("/check-user")
            assert response.status_code == 200
            # Anonymous user should not be authenticated
            assert user_info["is_authenticated"] is False
            assert user_info["is_anonymous"] is True


# =============================================================================
# Test CSRF Middleware (process_view support)
# =============================================================================


@pytest.mark.django_db
class TestCSRFMiddleware:
    """Tests for Django's CSRF middleware integration.

    CSRF middleware uses process_view (not process_request) to check tokens.
    This verifies the process_view hook is properly called.
    """

    def test_csrf_get_request_allowed(self):
        """
        Test that GET requests work without CSRF token.

        CSRF protection only applies to "unsafe" methods (POST, PUT, DELETE, etc.).
        GET requests should always be allowed through.
        """

        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    def test_csrf_post_without_token_rejected(self):
        """
        Test that POST requests without CSRF token are rejected.

        When CsrfViewMiddleware is enabled and a POST request doesn't include
        a valid CSRF token, it should return 403 Forbidden.
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ]
        )

        handler_called = {"called": False}

        @api.post("/submit")
        async def submit_route():
            handler_called["called"] = True
            return {"status": "submitted"}

        with TestClient(api, raise_server_exceptions=False) as client:
            response = client.post("/submit", json={"data": "test"})
            # Without CSRF token, should be rejected
            assert response.status_code == 403
            assert handler_called["called"] is False

    def test_csrf_exempt_endpoint_allowed(self):
        """
        Test that @csrf_exempt decorated endpoints allow POST without token.

        Django's @csrf_exempt decorator should bypass CSRF validation.
        The middleware gets the handler as the view in process_view, and
        reads its csrf_exempt attribute, as with a Django view.

        Note: Django's @csrf_exempt wraps the function to expect a `request`
        parameter (Django view signature), so the handler must accept it.
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ]
        )

        handler_called = {"called": False}

        @api.post("/api/webhook")
        @csrf_exempt
        async def webhook_route(request):
            # Django's @csrf_exempt wrapper passes request as first arg
            handler_called["called"] = True
            return {"status": "received"}

        with TestClient(api) as client:
            response = client.post("/api/webhook", json={"event": "test"})
            # csrf_exempt should allow through without token
            assert response.status_code == 200
            assert handler_called["called"] is True
            assert response.json() == {"status": "received"}

    def test_csrf_head_request_allowed(self):
        """
        Test that HEAD requests work without CSRF token (safe method).
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ]
        )

        @api.head("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.head("/test")
            assert response.status_code == 200

    def test_csrf_options_request_allowed(self):
        """
        Test that OPTIONS requests work without CSRF token (safe method).
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ]
        )

        @api.options("/test")
        async def test_route():
            return {"methods": ["GET", "POST"]}

        with TestClient(api) as client:
            response = client.options("/test")
            assert response.status_code == 200

    def test_csrf_template_token_matches_cookie(self):
        """
        Test that template-rendered csrf_token matches middleware cookie secret.

        This guards the middleware bridge contract: handlers/templates must see the
        same META dict that CSRF middleware uses for validation/cookie updates.
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
            ]
        )

        @api.get("/form")
        async def form_route(request):
            template = Template("<form method='post'>{% csrf_token %}<input name='value' value='x'></form>")
            return HTML(template.render(RequestContext(request, {})))

        @api.post("/submit")
        async def submit_route():
            return {"status": "submitted"}

        with TestClient(api, raise_server_exceptions=False) as client:
            get_response = client.get("/form")
            assert get_response.status_code == 200

            cookie_token = client.cookies.get("csrftoken")
            assert cookie_token is not None

            match = re.search(
                r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)['\"]",
                get_response.text,
            )
            assert match is not None
            form_token = match.group(1)

            assert _does_token_match(form_token, cookie_token)

            post_response = client.post("/submit", data={"csrfmiddlewaretoken": form_token})
            assert post_response.status_code == 200
            assert post_response.json() == {"status": "submitted"}


# =============================================================================
# Test process_view through the single wrapper and the stack
# =============================================================================

# A CSRF secret has 32 letters or digits. The cookie and the header carry it.
_CSRF_SECRET = "abcdefghijklmnopqrstuvwxyz012345"


class _BlockInViewMiddleware(MiddlewareMixin):
    """Return a response from process_view, before the handler."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        return HttpResponse("blocked in process_view", status=418)

    def process_response(self, request, response):
        response["X-Response-Hook"] = "ran"
        return response


class _MarkInViewMiddleware:
    """A plain middleware class. Its process_view returns None."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-View-Hook"] = "ran" if getattr(request, "_view_hook_ran", False) else "skipped"
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        request._view_hook_ran = True


class _TagInViewMiddleware(MiddlewareMixin):
    """Set a request attribute in process_view."""

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.view_tag = "set in process_view"


class _CallOnlyMiddleware:
    """A middleware with no hooks. In a stack with a hook middleware, the stack uses its compatibility chain."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


_LOGIN_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
]


def _add_get_route(api: BoltAPI, kind: str, path: str, view, decorator=None) -> None:
    """Add GET ``path``, which returns ``view(request)``. ``decorator`` goes on the handler."""
    if kind == "sync":

        def handler(request):
            return view(request)

    else:

        async def handler(request):
            return view(request)

    api.get(path)(decorator(handler) if decorator else handler)


def _add_submit_route(api: BoltAPI, kind: str, calls: list) -> None:
    """Add POST /submit. A sync handler runs on a lane, an async handler on the loop."""
    if kind == "sync":

        @api.post("/submit")
        def submit():
            calls.append(kind)
            return {"status": "submitted"}

    else:

        @api.post("/submit")
        async def submit():
            calls.append(kind)
            return {"status": "submitted"}


@pytest.mark.parametrize("kind", ["sync", "async"])
class TestProcessView:
    """DjangoMiddleware and DjangoMiddlewareStack run process_view, as Django's handler does."""

    def test_csrf_rejects_a_post_with_no_token(self, kind):
        api = BoltAPI(middleware=[DjangoMiddleware("django.middleware.csrf.CsrfViewMiddleware")])
        calls = []
        _add_submit_route(api, kind, calls)

        with TestClient(api) as client:
            response = client.post("/submit", json={"data": "test"})

        assert response.status_code == 403
        assert calls == []

    def test_csrf_accepts_a_post_with_a_valid_token(self, kind):
        api = BoltAPI(middleware=[DjangoMiddleware("django.middleware.csrf.CsrfViewMiddleware")])
        calls = []
        _add_submit_route(api, kind, calls)

        with TestClient(api) as client:
            response = client.post(
                "/submit",
                json={"data": "test"},
                headers={"X-CSRFToken": _CSRF_SECRET, "Cookie": f"csrftoken={_CSRF_SECRET}"},
            )

        assert response.status_code == 200
        assert response.json() == {"status": "submitted"}
        assert calls == [kind]

    def test_csrf_exempt_route_accepts_a_post_with_no_token(self, kind):
        api = BoltAPI(middleware=[DjangoMiddleware("django.middleware.csrf.CsrfViewMiddleware")])

        if kind == "sync":

            @api.post("/webhook")
            @csrf_exempt
            def webhook(request):
                return {"status": "received"}

        else:

            @api.post("/webhook")
            @csrf_exempt
            async def webhook(request):
                return {"status": "received"}

        with TestClient(api) as client:
            response = client.post("/webhook", json={"event": "test"})

        assert response.status_code == 200
        assert response.json() == {"status": "received"}

    def test_process_view_response_stops_the_request(self, kind):
        api = BoltAPI(middleware=[DjangoMiddleware(_BlockInViewMiddleware)])
        calls = []
        _add_submit_route(api, kind, calls)

        with TestClient(api) as client:
            response = client.post("/submit", json={"data": "test"})

        assert response.status_code == 418
        assert response.content == b"blocked in process_view"
        assert response.headers["x-response-hook"] == "ran"
        assert calls == []

    def test_process_view_that_returns_none_lets_the_handler_run(self, kind):
        api = BoltAPI(middleware=[DjangoMiddleware(_MarkInViewMiddleware)])
        calls = []
        _add_submit_route(api, kind, calls)

        with TestClient(api) as client:
            response = client.post("/submit", json={"data": "test"})

        assert response.status_code == 200
        assert response.headers["x-view-hook"] == "ran"
        assert calls == [kind]

    @pytest.mark.django_db
    @pytest.mark.parametrize("layout", ["wrapper", "stack"])
    def test_login_not_required_route_skips_the_login_check(self, kind, layout):
        if layout == "wrapper":
            api = BoltAPI(middleware=[DjangoMiddleware(path) for path in _LOGIN_MIDDLEWARE])
        else:
            api = BoltAPI(django_middleware=_LOGIN_MIDDLEWARE)
        _add_get_route(api, kind, "/public", lambda _request: {"public": True}, decorator=login_not_required)
        _add_get_route(api, kind, "/private", lambda _request: {"public": False})

        with TestClient(api) as client:
            public = client.get("/public", follow_redirects=False)
            private = client.get("/private", follow_redirects=False)

        assert public.status_code == 200
        assert public.json() == {"public": True}
        assert private.status_code == 302
        assert private.headers["location"].startswith(settings.LOGIN_URL)

    @pytest.mark.parametrize(
        "middleware",
        [
            pytest.param(DjangoMiddleware(_TagInViewMiddleware), id="wrapper"),
            pytest.param(DjangoMiddlewareStack([_TagInViewMiddleware]), id="stack"),
            pytest.param(DjangoMiddlewareStack([_TagInViewMiddleware, _CallOnlyMiddleware]), id="mixed-stack"),
        ],
    )
    def test_handler_gets_the_attributes_that_process_view_sets(self, kind, middleware):
        api = BoltAPI(middleware=[middleware])
        _add_get_route(api, kind, "/tag", lambda request: {"tag": request.state.get("view_tag")})

        with TestClient(api) as client:
            response = client.get("/tag")

        assert response.status_code == 200
        assert response.json() == {"tag": "set in process_view"}


class _RequestHook(MiddlewareMixin):
    """A hook middleware with process_request only. It has no process_view."""

    def process_request(self, request):
        return None


class _ViewRecorder(MiddlewareMixin):
    """Record what the stack gives to process_view and to process_response."""

    calls: list = []

    def process_view(self, request, view_func, view_args, view_kwargs):
        type(self).calls.append(("view", view_func, view_args, view_kwargs))

    def process_response(self, request, response):
        type(self).calls.append(("response", request.path))
        return response


def _async_short_circuit(get_response):
    """A middleware factory with no flags. It returns an async function that does not call get_response."""

    async def short_circuit(request):
        return HttpResponse(f"async answer for {request.path}", status=418)

    return short_circuit


class TestCompatibilityChain:
    """A stack with hook and __call__ middleware together keeps each hook in the order of the stack."""

    def test_process_view_of_each_entered_hook_runs_with_the_route_handler(self):
        _ViewRecorder.calls = []
        stack = DjangoMiddlewareStack([_RequestHook, _ViewRecorder, _CallOnlyMiddleware])
        api = BoltAPI(middleware=[stack])

        @api.get("/items/{item_id}")
        @csrf_exempt
        async def read_item(item_id: int):
            return {"item_id": item_id}

        with TestClient(api) as client:
            response = client.get("/items/7")

        assert response.json() == {"item_id": 7}
        view_call, response_call = _ViewRecorder.calls
        _, view_func, view_args, view_kwargs = view_call
        assert view_func.__name__ == "read_item"
        assert view_func.csrf_exempt is True
        assert view_args == ()
        assert view_kwargs == {}
        assert response_call == ("response", "/items/7")

    def test_async_function_from_a_middleware_factory_answers_the_request(self):
        stack = DjangoMiddlewareStack([_RequestHook, _async_short_circuit])
        api = BoltAPI(middleware=[stack])

        @api.get("/hello")
        async def hello():
            return {"reached": True}

        with TestClient(api) as client:
            response = client.get("/hello")

        assert response.status_code == 418
        assert response.text == "async answer for /hello"


class _HookCallsGetResponse(MiddlewareMixin):
    """A hook middleware that calls get_response from a hook. A stack runs no get_response for a hook."""

    def process_request(self, request):
        return self.get_response(request)


def test_hook_that_calls_get_response_gets_a_clear_error(caplog):
    """The stack gives a hook middleware a get_response that refuses the call. The client gets a 500."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_HookCallsGetResponse])])

    @api.get("/hook")
    async def hook_route():
        return {}

    with caplog.at_level(logging.ERROR), TestClient(api) as client:
        response = client.get("/hook")

    assert response.status_code == 500
    assert any(
        "RuntimeError: Hook-based middleware should not call get_response" in record.getMessage()
        for record in caplog.records
    )


class _ResponseOnlyHook:
    """A plain class with process_response only. It has no __call__."""

    def __init__(self, get_response):
        self.get_response = get_response

    def process_response(self, request, response):
        response["X-Response-Only"] = "ran"
        return response


class _MarkerCallOnly:
    """A __call__ middleware that sets an attribute on the Django request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.outer_marker = "set by the stack"
        return self.get_response(request)


class _MarkerReader(MiddlewareMixin):
    """Record the marker that an outer middleware set on the Django request."""

    seen: list = []

    def process_request(self, request):
        type(self).seen.append(getattr(request, "outer_marker", None))


class TestStackShapes:
    """A stack takes middleware of any shape, and a wrapper inside it shares the Django request."""

    def test_class_with_process_response_only_runs_its_hook(self):
        api = BoltAPI(middleware=[DjangoMiddlewareStack([_ResponseOnlyHook])])

        @api.get("/response-only")
        async def response_only():
            return {"ok": True}

        with TestClient(api) as client:
            response = client.get("/response-only")

        assert response.json() == {"ok": True}
        assert response.headers["x-response-only"] == "ran"

    def test_empty_stack_passes_the_request_to_the_handler(self):
        api = BoltAPI(middleware=[DjangoMiddlewareStack([])])

        @api.get("/empty-stack")
        async def empty_stack():
            return {"ok": True}

        with TestClient(api) as client:
            assert client.get("/empty-stack").json() == {"ok": True}

    @pytest.mark.parametrize(
        "outer_classes",
        [[_MarkerCallOnly], [_RequestHook, _MarkerCallOnly]],
        ids=["call-only", "mixed"],
    )
    def test_wrapper_after_a_stack_gets_the_django_request_of_the_stack(self, outer_classes):
        _MarkerReader.seen = []
        api = BoltAPI(middleware=[DjangoMiddlewareStack(outer_classes), DjangoMiddleware(_MarkerReader)])

        @api.get("/shared")
        async def shared():
            return {"ok": True}

        with TestClient(api) as client:
            assert client.get("/shared").json() == {"ok": True}

        assert _MarkerReader.seen == ["set by the stack"]


class _HeaderOnResponseHook(MiddlewareMixin):
    def process_response(self, request, response):
        response["X-Django"] = "ran"
        return response


class _HeaderOnResponseCall:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Call"] = "ran"
        return response


@pytest.mark.parametrize(
    ("layout", "headers"),
    [
        (lambda: DjangoMiddleware(_HeaderOnResponseHook), {"x-django": "ran"}),
        (lambda: DjangoMiddlewareStack([_HeaderOnResponseHook]), {"x-django": "ran"}),
        (lambda: DjangoMiddlewareStack([_HeaderOnResponseCall]), {"x-call": "ran"}),
        (
            lambda: DjangoMiddlewareStack([_HeaderOnResponseHook, _HeaderOnResponseCall]),
            {"x-django": "ran", "x-call": "ran"},
        ),
    ],
    ids=["wrapper", "stack", "call-only-stack", "mixed-stack"],
)
class TestFileAndStreamThroughDjangoMiddleware:
    """A file or a stream keeps its body and its headers behind Django middleware. The middleware adds its headers."""

    def test_file_response(self, layout, headers, tmp_path):
        path = tmp_path / "data.txt"
        path.write_bytes(b"file-body-" * 10)
        api = BoltAPI(middleware=[layout()])

        @api.get("/file")
        async def file_route():
            return FileResponse(str(path), filename="data.txt")

        with TestClient(api) as client:
            response = client.get("/file")

        assert response.status_code == 200
        assert response.content == b"file-body-" * 10
        assert response.headers["content-disposition"] == 'attachment; filename="data.txt"'
        assert response.headers["content-length"] == "100"
        assert {name: response.headers[name] for name in headers} == headers

    def test_streaming_response(self, layout, headers):
        api = BoltAPI(middleware=[layout()])

        @api.get("/stream")
        async def stream_route():
            async def chunks():
                yield b"one-"
                yield b"two"

            return StreamingResponse(chunks(), media_type="text/plain", headers={"X-Own": "stream"})

        with TestClient(api) as client:
            response = client.get("/stream", headers={"Accept-Encoding": "identity"})

        assert response.status_code == 200
        assert response.content == b"one-two"
        assert response.headers["content-type"] == "text/plain"
        assert response.headers["x-own"] == "stream"
        assert {name: response.headers[name] for name in headers} == headers


class _StateAttributesHook(MiddlewareMixin):
    """Set the request attributes that Django middleware commonly sets."""

    def process_request(self, request):
        request.session = {"key": "value"}
        request._messages = "message storage"
        request.csrf_processing_done = True
        request.csrf_cookie_needs_reset = True
        request.tenant = "acme"
        request._private = "stays on the Django request"


class _UserAndAuserHook(MiddlewareMixin):
    """Set a user and an auser, as Django's AuthenticationMiddleware does."""

    def process_request(self, request):
        async def auser():
            return SimpleNamespace(username="from auser")

        request.user = SimpleNamespace(is_authenticated=True, username="django_user")
        request.auser = auser


class _AuserOnlyHook(MiddlewareMixin):
    """Set an auser and no user."""

    def process_request(self, request):
        async def auser():
            return "auser only"

        request.auser = auser


@pytest.mark.parametrize(
    "layout",
    [lambda hook: DjangoMiddleware(hook), lambda hook: DjangoMiddlewareStack([hook])],
    ids=["wrapper", "stack"],
)
class TestDjangoAttributesReachTheHandler:
    """The handler reads the attributes that Django middleware set, from request.state and request.user."""

    def test_common_attributes_are_in_the_state(self, layout):
        api = BoltAPI(middleware=[layout(_StateAttributesHook)])

        @api.get("/state")
        async def state_route(request):
            state = request.state
            return {
                "session": state.get("session"),
                "messages": state.get("_messages"),
                "csrf_processing_done": state.get("csrf_processing_done"),
                "csrf_cookie_needs_reset": state.get("csrf_cookie_needs_reset"),
                "tenant": state.get("tenant"),
                "private": state.get("_private"),
            }

        with TestClient(api) as client:
            response = client.get("/state")

        assert response.json() == {
            "session": {"key": "value"},
            "messages": "message storage",
            "csrf_processing_done": True,
            "csrf_cookie_needs_reset": True,
            "tenant": "acme",
            "private": None,
        }

    def test_user_and_auser_of_django_reach_the_handler(self, layout):
        api = BoltAPI(middleware=[layout(_UserAndAuserHook)])

        @api.get("/user")
        async def user_route(request):
            auser = await request.auser()
            return {"user": request.user.username, "auser": auser.username}

        with TestClient(api) as client:
            response = client.get("/user")

        assert response.json() == {"user": "django_user", "auser": "from auser"}

    def test_auser_without_a_user_reaches_the_handler(self, layout):
        api = BoltAPI(middleware=[layout(_AuserOnlyHook)])

        @api.get("/auser")
        async def auser_route(request):
            return {"auser": await request.auser()}

        with TestClient(api) as client:
            response = client.get("/auser")

        assert response.json() == {"auser": "auser only"}


class _FirstUserHook(MiddlewareMixin):
    """Set an authenticated user and an auser that names itself."""

    def process_request(self, request):
        async def auser():
            return SimpleNamespace(username="from first auser")

        request.user = SimpleNamespace(is_authenticated=True, username="first")
        request.auser = auser


class _AnonymousUserHook(MiddlewareMixin):
    """Set an anonymous user and an auser that names itself."""

    def process_request(self, request):
        async def auser():
            return SimpleNamespace(username="from second auser")

        request.user = SimpleNamespace(is_authenticated=False, username="second")
        request.auser = auser


def test_auser_of_a_user_that_a_later_middleware_does_not_replace_follows_that_user():
    """A later anonymous user does not replace the user. Its auser does not replace the auser either."""
    api = BoltAPI(middleware=[DjangoMiddleware(_FirstUserHook), DjangoMiddleware(_AnonymousUserHook)])

    @api.get("/kept")
    async def kept_route(request):
        auser = await request.auser()
        return {"user": request.user.username, "auser": auser.username}

    with TestClient(api) as client:
        response = client.get("/kept")

    assert response.json() == {"user": "first", "auser": "first"}


@pytest.mark.parametrize(
    "layout",
    [lambda: DjangoMiddleware(CommonMiddleware), lambda: DjangoMiddlewareStack([CommonMiddleware])],
    ids=["wrapper", "stack"],
)
def test_file_keeps_its_length_behind_a_middleware_that_sets_content_length(layout, tmp_path):
    """CommonMiddleware sets Content-Length from the empty content of the Django response. Bolt sends the real length."""
    path = tmp_path / "data.txt"
    path.write_bytes(b"file-body-" * 10)
    api = BoltAPI(middleware=[layout()])

    @api.get("/file")
    async def file_route():
        return FileResponse(str(path))

    with TestClient(api) as client:
        response = client.get("/file")

    assert response.status_code == 200
    assert response.content == b"file-body-" * 10
    assert response.headers["content-length"] == "100"


class _ViewArgsHook(MiddlewareMixin):
    """Record the view arguments and the thread state that process_view sees."""

    seen: list = []

    def process_view(self, request, view_func, view_args, view_kwargs):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        type(self).seen.append({"args": view_args, "kwargs": view_kwargs, "on_loop": on_loop})


class _BuiltinModuleViewArgsHook(_ViewArgsHook):
    """The same hook, in a module that Bolt treats as a Django built-in."""

    __module__ = "django.middleware.probe"


class _ConfiguredHeaderMiddleware(MiddlewareMixin):
    """A middleware that takes an option in its constructor."""

    def __init__(self, get_response, header_value="default"):
        super().__init__(get_response)
        self.header_value = header_value

    def process_response(self, request, response):
        response["X-Configured"] = self.header_value
        return response


def _async_factory(get_response):
    """A middleware factory with no flags. It returns an async function that calls get_response."""

    async def middleware(request):
        response = await get_response(request)
        response["X-Async-Factory"] = "ran"
        return response

    return middleware


class _AwaitableCall:
    """A middleware whose plain ``__call__`` returns a coroutine. It answers without get_response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self._answer(request)

    async def _answer(self, request):
        return HttpResponse("awaitable answer", status=418)


class _AsyncOnlyCall(_AwaitableCall):
    sync_capable = False
    async_capable = True


def _class_flags_factory(get_response):
    return _AwaitableCall(get_response)


_class_flags_factory.sync_capable = False
_class_flags_factory.async_capable = True


def _instance_flags_factory(get_response):
    return _AsyncOnlyCall(get_response)


@pytest.mark.parametrize("kind", ["sync", "async"])
class TestDjangoMiddlewareWrapper:
    """DjangoMiddleware(...) gives the middleware its options, its view arguments and the right thread."""

    @pytest.mark.parametrize("hook_class", [_ViewArgsHook, _BuiltinModuleViewArgsHook], ids=["third-party", "built-in"])
    @pytest.mark.parametrize("layout", ["wrapper", "stack", "mixed-stack"])
    def test_process_view_gets_an_empty_tuple_and_an_empty_dict(self, kind, hook_class, layout):
        hook_class.seen = []
        middleware = {
            "wrapper": lambda: DjangoMiddleware(hook_class),
            "stack": lambda: DjangoMiddlewareStack([hook_class]),
            "mixed-stack": lambda: DjangoMiddlewareStack([hook_class, _CallOnlyMiddleware]),
        }[layout]()
        api = BoltAPI(middleware=[middleware])
        _add_get_route(api, kind, "/view", lambda _request: {"ok": True})

        with TestClient(api) as client:
            assert client.get("/view").json() == {"ok": True}

        assert [(call["args"], call["kwargs"]) for call in hook_class.seen] == [((), {})]

    def test_a_third_party_process_view_runs_off_the_event_loop(self, kind):
        _ViewArgsHook.seen = []
        api = BoltAPI(middleware=[DjangoMiddleware(_ViewArgsHook)])
        _add_get_route(api, kind, "/view", lambda _request: {"ok": True})

        with TestClient(api) as client:
            assert client.get("/view").status_code == 200

        assert [call["on_loop"] for call in _ViewArgsHook.seen] == [False]

    def test_constructor_options_reach_the_middleware(self, kind):
        api = BoltAPI(middleware=[DjangoMiddleware(_ConfiguredHeaderMiddleware, header_value="custom")])
        _add_get_route(api, kind, "/option", lambda _request: {"ok": True})

        with TestClient(api) as client:
            response = client.get("/option")

        assert response.headers["x-configured"] == "custom"


class _RequestHookProbe(MiddlewareMixin):
    """Record whether process_request runs on the event loop."""

    seen: list = []

    def process_request(self, request):
        try:
            asyncio.get_running_loop()
            type(self).seen.append(True)
        except RuntimeError:
            type(self).seen.append(False)


class _BuiltinModuleRequestHookProbe(_RequestHookProbe):
    __module__ = "django.middleware.probe"


@pytest.mark.parametrize(
    ("hook_class", "on_loop"),
    [(_RequestHookProbe, False), (_BuiltinModuleRequestHookProbe, True)],
    ids=["third-party", "built-in"],
)
def test_mixed_stack_calls_a_built_in_hook_direct_and_a_third_party_hook_off_the_loop(hook_class, on_loop):
    """A hook of Django runs on the event loop. A third-party hook can block, so it runs on the request thread."""
    hook_class.seen = []
    api = BoltAPI(middleware=[DjangoMiddlewareStack([hook_class, _CallOnlyMiddleware])])

    @api.get("/probe")
    async def probe():
        return {"ok": True}

    with TestClient(api) as client:
        assert client.get("/probe").status_code == 200

    assert hook_class.seen == [on_loop]


class TestRouteSyncForce:
    """A lazy user that Django sets loads through run_orm_blocking, once."""

    def test_wraps_the_setup_one_time(self):
        def load_user():
            return "user"

        user = SimpleLazyObject(load_user)

        _route_sync_force(user)
        wrapped = user.__dict__["_setupfunc"]
        _route_sync_force(user)

        assert wrapped.func is run_orm_blocking
        assert wrapped.args == (load_user,)
        assert user.__dict__["_setupfunc"] is wrapped

    def test_wraps_a_setup_that_is_another_partial(self):
        load_user = partial(str, "user")
        user = SimpleLazyObject(load_user)

        _route_sync_force(user)

        assert user.__dict__["_setupfunc"].func is run_orm_blocking
        assert user.__dict__["_setupfunc"].args == (load_user,)

    def test_keeps_a_user_that_is_not_lazy_or_is_loaded(self):
        def load_user():
            return "user"

        loaded = SimpleLazyObject(load_user)
        assert str(loaded) == "user"

        _route_sync_force(loaded)
        _route_sync_force("plain user")

        assert loaded.__dict__["_setupfunc"] is load_user


class TestDjangoMiddlewareFactories:
    """A path string can name a factory function. The wrapper reads the flags of the factory and of its result."""

    def test_async_function_from_a_factory_awaits_get_response(self):
        api = BoltAPI(middleware=[DjangoMiddleware(f"{__name__}._async_factory")])

        @api.get("/factory")
        async def factory_route():
            return {"ok": True}

        with TestClient(api) as client:
            response = client.get("/factory")

        assert response.json() == {"ok": True}
        assert response.headers["x-async-factory"] == "ran"

    @pytest.mark.parametrize("factory", ["_class_flags_factory", "_instance_flags_factory"])
    def test_async_only_flag_of_the_factory_or_of_its_result_makes_the_wrapper_await(self, factory):
        api = BoltAPI(middleware=[DjangoMiddleware(f"{__name__}.{factory}")])

        @api.get("/factory")
        async def factory_route():
            return {"reached": True}

        with TestClient(api) as client:
            response = client.get("/factory")

        assert response.status_code == 418
        assert response.text == "awaitable answer"


# =============================================================================
# Test Multiple Set-Cookie Headers (Cookie Overwriting Bug Fix)
# =============================================================================


class MultipleCookieMiddleware:
    """
    Middleware that sets multiple cookies on the response.

    This middleware is used to test that multiple Set-Cookie headers are
    preserved and not overwritten. HTTP allows multiple Set-Cookie headers
    (one per cookie), but naive dict-based implementations would overwrite
    previous cookies since dict keys must be unique.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Set multiple cookies - each should result in a separate Set-Cookie header
        response.set_cookie("session_id", "abc123", httponly=True)
        response.set_cookie("user_pref", "dark_mode", max_age=86400)
        response.set_cookie("tracking_consent", "accepted", secure=True, samesite="Strict")
        return response


class TestMultipleCookieHeaders:
    """
    Tests for multiple Set-Cookie header support.

    HTTP allows multiple Set-Cookie headers because each cookie must be sent
    in its own header. This test verifies that middleware setting multiple
    cookies results in all cookies being present in the response.

    This test WILL FAIL if:
    - Set-Cookie headers are stored in a dict (causing overwrites)
    - Only the last Set-Cookie header survives
    - Cookies are merged incorrectly
    """

    def test_multiple_cookies_all_preserved(self):
        """
        Test that middleware setting multiple cookies preserves ALL cookies.

        This is the core test for the cookie overwriting bug fix.
        With the old dict-based implementation, only one cookie would survive
        because dict can't have duplicate keys.
        """
        api = BoltAPI(middleware=[DjangoMiddlewareStack([MultipleCookieMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200

            # Get all Set-Cookie headers
            # response.headers is a case-insensitive dict, but for multiple
            # headers with the same name, we need to check the raw headers
            set_cookie_headers = []
            for key, value in response.headers.multi_items():
                if key.lower() == "set-cookie":
                    set_cookie_headers.append(value)

            # CRITICAL: All THREE cookies must be present
            # If this fails with only 1 cookie, the old bug is back
            assert len(set_cookie_headers) >= 3, (
                f"Expected at least 3 Set-Cookie headers, got {len(set_cookie_headers)}. "
                f"This indicates cookies are being overwritten. "
                f"Headers: {set_cookie_headers}"
            )

            # Verify each specific cookie is present
            all_cookies = "\n".join(set_cookie_headers)

            assert "session_id=abc123" in all_cookies, (
                f"session_id cookie missing from response. Set-Cookie headers: {set_cookie_headers}"
            )
            assert "user_pref=dark_mode" in all_cookies, (
                f"user_pref cookie missing from response. Set-Cookie headers: {set_cookie_headers}"
            )
            assert "tracking_consent=accepted" in all_cookies, (
                f"tracking_consent cookie missing from response. Set-Cookie headers: {set_cookie_headers}"
            )

    def test_cookies_have_correct_attributes(self):
        """
        Test that cookie attributes (HttpOnly, Secure, SameSite) are preserved.

        This verifies that not only are all cookies present, but their
        attributes are correctly passed through.
        """
        api = BoltAPI(middleware=[DjangoMiddlewareStack([MultipleCookieMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200

            # Collect all Set-Cookie headers
            set_cookie_headers = []
            for key, value in response.headers.multi_items():
                if key.lower() == "set-cookie":
                    set_cookie_headers.append(value)

            # Check session_id has HttpOnly
            # Find the session_id cookie line
            session_cookie = next((c for c in set_cookie_headers if "session_id=" in c), None)
            assert session_cookie is not None, "session_id cookie not found"
            assert "httponly" in session_cookie.lower(), (
                f"session_id cookie missing HttpOnly attribute: {session_cookie}"
            )

            # Check tracking_consent has Secure and SameSite=Strict
            tracking_cookie = next((c for c in set_cookie_headers if "tracking_consent=" in c), None)
            assert tracking_cookie is not None, "tracking_consent cookie not found"
            assert "secure" in tracking_cookie.lower(), (
                f"tracking_consent cookie missing Secure attribute: {tracking_cookie}"
            )
            assert "samesite=strict" in tracking_cookie.lower(), (
                f"tracking_consent cookie missing SameSite=Strict: {tracking_cookie}"
            )

    def test_handler_can_also_set_cookies(self):
        """
        Test that handler can set cookies and they combine with middleware cookies.

        This verifies the complete cookie flow: middleware sets some cookies,
        handler sets additional cookies, and ALL of them appear in response.
        """
        api = BoltAPI(middleware=[DjangoMiddlewareStack([MultipleCookieMiddleware])])

        @api.get("/test")
        async def test_route():
            # Handler returns data, middleware will add cookies
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200

            # Count Set-Cookie headers
            cookie_count = sum(1 for key, _ in response.headers.multi_items() if key.lower() == "set-cookie")

            # Middleware sets 3 cookies
            assert cookie_count >= 3, f"Expected at least 3 cookies from middleware, got {cookie_count}"

    def test_no_cookies_when_middleware_doesnt_set_them(self):
        """
        Test that responses without cookies don't have spurious Set-Cookie headers.

        This is a sanity check to ensure we're not adding empty cookie headers.
        """
        api = BoltAPI(middleware=[DjangoMiddlewareStack([HeaderAddingMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200

            # Count Set-Cookie headers
            cookie_count = sum(1 for key, _ in response.headers.multi_items() if key.lower() == "set-cookie")

            # Should have no cookies (HeaderAddingMiddleware doesn't set cookies)
            assert cookie_count == 0, f"Expected no Set-Cookie headers, got {cookie_count}"


class CookieAttributesMiddleware:
    """Set cookies that use each attribute, and a cookie that uses none."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.set_cookie(
            "full", "1", max_age=3600, path="/app", domain="example.com", secure=True, httponly=True, samesite="Lax"
        )
        response.set_cookie("plain", "2")
        response.set_cookie("nopath", "4", path=None)
        response.set_cookie("dated", "3", expires=datetime.datetime(2031, 1, 2, 3, 4, 5, tzinfo=datetime.UTC))
        return response


@pytest.mark.parametrize(
    "layout",
    [
        lambda: DjangoMiddleware(CookieAttributesMiddleware),
        lambda: DjangoMiddlewareStack([CookieAttributesMiddleware]),
    ],
    ids=["wrapper", "stack"],
)
def test_django_cookie_attributes_reach_the_client(layout):
    """Each attribute of a Django cookie is in its Set-Cookie header. A cookie with no attribute gets only the path."""
    api = BoltAPI(middleware=[layout()])

    @api.get("/cookies")
    async def cookies_route():
        return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/cookies")

    set_cookies = {
        value.split("=", 1)[0]: value for name, value in response.headers.multi_items() if name.lower() == "set-cookie"
    }
    assert set(set_cookies) == {"full", "plain", "nopath", "dated"}
    full = set_cookies["full"].split("; ")
    assert full[0] == "full=1"
    assert {"HttpOnly", "SameSite=Lax", "Secure", "Path=/app", "Domain=example.com", "Max-Age=3600"} <= set(full)
    assert any(part.startswith("Expires=") for part in full)
    assert set_cookies["plain"] == "plain=2; Path=/"
    assert set_cookies["nopath"] == "nopath=4; Path=/"
    assert "Expires=Thu, 02 Jan 2031 03:04:05 GMT" in set_cookies["dated"].split("; ")
    assert "Path=/" in set_cookies["dated"].split("; ")


# =============================================================================
# Test Handler-Set Cookies Survive django_middleware
# =============================================================================


class TestHandlerCookiesWithDjangoMiddleware:
    """
    Cookies set by the handler (e.g. JSON(...).set_cookie(...)) must survive
    when django_middleware is configured. Previously they were silently
    dropped because _to_django_response() built a fresh HttpResponse without
    carrying _raw_cookies onto it, so Django middleware never saw them and
    _to_bolt_response() harvested an empty cookie jar.
    """

    def test_handler_cookies_preserved_through_locale_middleware(self):
        api = BoltAPI(
            django_middleware=[
                "django.middleware.locale.LocaleMiddleware",
                "django.middleware.common.CommonMiddleware",
            ]
        )

        @api.post("/login")
        async def login():
            return (
                JSON({"ok": True})
                .set_cookie(
                    name="access_token",
                    value="access-abc",
                    max_age=3600,
                    secure=True,
                    httponly=True,
                )
                .set_cookie(
                    name="refresh_token",
                    value="refresh-xyz",
                    max_age=86400,
                    secure=True,
                    httponly=True,
                )
            )

        with TestClient(api) as client:
            response = client.post("/login")
            assert response.status_code == 200

            set_cookie_headers = [value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"]
            joined = "\n".join(set_cookie_headers)

            assert len(set_cookie_headers) >= 2, (
                f"Expected at least 2 Set-Cookie headers from handler, got {len(set_cookie_headers)}: {set_cookie_headers}"
            )
            assert "access_token=access-abc" in joined, (
                f"access_token cookie missing. Set-Cookie headers: {set_cookie_headers}"
            )
            assert "refresh_token=refresh-xyz" in joined, (
                f"refresh_token cookie missing. Set-Cookie headers: {set_cookie_headers}"
            )

    def test_handler_cookies_preserved_with_single_django_middleware(self):
        api = BoltAPI(
            middleware=[DjangoMiddleware("django.middleware.common.CommonMiddleware")],
        )

        @api.post("/login")
        async def login():
            return JSON({"ok": True}).set_cookie(
                name="session",
                value="single-mw-cookie",
                httponly=True,
            )

        with TestClient(api) as client:
            response = client.post("/login")
            assert response.status_code == 200

            set_cookie_headers = [value for key, value in response.headers.multi_items() if key.lower() == "set-cookie"]
            joined = "\n".join(set_cookie_headers)

            assert "session=single-mw-cookie" in joined, (
                f"Handler-set cookie missing through DjangoMiddleware. Set-Cookie headers: {set_cookie_headers}"
            )


# =============================================================================
# Test auser Setter for Django alogin() Compatibility
# =============================================================================


@pytest.mark.django_db
class TestAuserSetter:
    """Tests for PyRequest.auser setter used by Django's alogin()."""

    def test_auser_setter_basic(self):
        """
        Test that request.auser can be set and retrieved.

        Django's alogin() sets request.auser to a coroutine function.
        This test verifies the setter works correctly.
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        @api.get("/test-auser-setter")
        async def test_auser_setter(request):
            # Create a custom async user loader
            async def custom_auser():
                return "custom_user_value"

            # Set auser (this is what Django's alogin() does)
            request.auser = custom_auser

            # Verify we can retrieve it
            retrieved_auser = request.auser
            assert retrieved_auser is custom_auser

            # Verify it returns expected value when awaited
            result = await retrieved_auser()
            return {"status": "ok", "auser_result": result}

        with TestClient(api) as client:
            response = client.get("/test-auser-setter")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["auser_result"] == "custom_user_value"

    def test_auser_setter_with_django_alogin(self):
        """
        Test that Django's alogin() can set request.auser without error.

        Before fix: AttributeError: attribute 'auser' is not writable
        After fix: alogin() works correctly
        """
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        @api.post("/test-alogin")
        async def test_alogin_handler(request):
            # Create or get a test user
            user, _ = await User.objects.aget_or_create(
                username="test_alogin_user",
                defaults={"email": "test@example.com"},
            )

            # This should NOT raise AttributeError anymore
            # Django's alogin() internally does: request.auser = auser
            await alogin(request, user)

            # Verify the user is logged in
            logged_in_user = await request.auser()
            return {
                "status": "ok",
                "user_id": user.id,
                "logged_in_as": logged_in_user.username if hasattr(logged_in_user, "username") else str(logged_in_user),
            }

        @api.post("/test-alogout")
        async def test_alogout_handler(request):
            # This also uses request.auser setter
            await alogout(request)
            return {"status": "ok", "logged_out": True}

        with TestClient(api) as client:
            # Test alogin
            response = client.post("/test-alogin")
            assert response.status_code == 200, f"alogin failed: {response.text}"
            data = response.json()
            assert data["status"] == "ok"
            assert data["logged_in_as"] == "test_alogin_user"

            # Test alogout (also uses request.auser setter)
            response = client.post("/test-alogout")
            assert response.status_code == 200, f"alogout failed: {response.text}"
            data = response.json()
            assert data["status"] == "ok"
            assert data["logged_out"] is True


# =============================================================================
# Test META is the Rust-built request.meta (REMOTE_ADDR, SERVER_NAME)
# =============================================================================


class RemoteAddrMiddleware:
    """Reads the keys that django-debug-toolbar, django-axes and django-ratelimit read."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Remote-Addr"] = request.META["REMOTE_ADDR"]
        response["X-Server-Name"] = request.META["SERVER_NAME"]
        return response


class TestDjangoMiddlewareMeta:
    def test_middleware_and_handler_see_remote_addr(self):
        api = BoltAPI(middleware=[DjangoMiddlewareStack([RemoteAddrMiddleware])])

        @api.get("/meta")
        async def meta_route(request):
            return {"remote_addr": request.META["REMOTE_ADDR"]}

        with TestClient(api) as client:
            response = client.get("/meta", headers={"host": "example.com:8443"})
            assert response.status_code == 200
            assert response.headers["X-Remote-Addr"] == "127.0.0.1"
            assert response.headers["X-Server-Name"] == "example.com"
            assert response.json() == {"remote_addr": "127.0.0.1"}


def _order_recording_middleware(name: str, calls: list[str], *, short_circuit: bool = False) -> type:
    class Recorder(MiddlewareMixin):
        def process_request(self, request):
            calls.append(f"{name}.request")
            if short_circuit:
                return HttpResponse("stopped", status=418)
            return None

        def process_response(self, request, response):
            calls.append(f"{name}.response")
            return response

    return Recorder


@pytest.mark.parametrize("handler_kind", ["async", "sync"])
def test_short_circuit_runs_process_response_of_entered_middleware_only(handler_kind):
    """The async flow (one hop for each phase) and the lane flow keep Django's hook order."""
    calls: list[str] = []
    stack = DjangoMiddlewareStack(
        [
            _order_recording_middleware("a", calls),
            _order_recording_middleware("b", calls, short_circuit=True),
            _order_recording_middleware("c", calls),
        ]
    )
    api = BoltAPI(middleware=[stack])

    if handler_kind == "async":

        @api.get("/x")
        async def x():
            calls.append("handler")
            return {"ok": True}
    else:

        @api.get("/x")
        def x():
            calls.append("handler")
            return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/x")

    assert response.status_code == 418
    assert calls == ["a.request", "b.request", "b.response", "a.response"]


@pytest.mark.parametrize("handler_kind", ["async", "sync"])
def test_full_pass_runs_hooks_in_declared_and_reverse_order(handler_kind):
    calls: list[str] = []
    stack = DjangoMiddlewareStack([_order_recording_middleware("a", calls), _order_recording_middleware("b", calls)])
    api = BoltAPI(middleware=[stack])

    if handler_kind == "async":

        @api.get("/x")
        async def x():
            calls.append("handler")
            return {"ok": True}
    else:

        @api.get("/x")
        def x():
            calls.append("handler")
            return {"ok": True}

    with TestClient(api) as client:
        assert client.get("/x").status_code == 200

    assert calls == ["a.request", "b.request", "handler", "b.response", "a.response"]


class RawValuesRecorder(MiddlewareMixin):
    """Record the cookie and header strings that Django middleware sees."""

    seen: dict = {}

    def process_request(self, request):
        RawValuesRecorder.seen = {
            "cookie": request.COOKIES.get("n"),
            "header": request.META.get("HTTP_X_COUNT"),
        }


class TestTypedParamsKeepRawStringsForDjango:
    """A typed cookie or header reaches Django middleware as its original string."""

    def test_django_middleware_sees_raw_cookie_and_header(self):
        api = BoltAPI(middleware=[DjangoMiddlewareStack([RawValuesRecorder])])

        @api.get("/typed")
        async def typed(count: Annotated[int, Cookie(alias="n")], x_count: Annotated[int, Header()]) -> dict:
            return {"count": count, "x_count": x_count}

        with TestClient(api) as client:
            response = client.get("/typed", headers={"X-Count": "0007", "Cookie": "n=0005"})

        assert response.status_code == 200, response.text
        assert response.json() == {"count": 5, "x_count": 7}
        assert RawValuesRecorder.seen == {"cookie": "0005", "header": "0007"}


class QueryRecorder(MiddlewareMixin):
    """Record the query that Django middleware sees."""

    seen: dict = {}

    def process_request(self, request):
        QueryRecorder.seen = {
            "get": request.GET.dict(),
            "query_string": request.META.get("QUERY_STRING"),
            "full_path": request.get_full_path(),
        }


class TestDjangoMiddlewareSeesTheQuery:
    """Django middleware gets the query, also when the handler does not read it."""

    def test_middleware_sees_the_query_that_the_handler_ignores(self):
        api = BoltAPI(middleware=[DjangoMiddlewareStack([QueryRecorder])])

        @api.get("/page")
        async def page() -> dict:
            return {"ok": True}

        with TestClient(api) as client:
            response = client.get("/page?next=%2Fa%3Fb%3D1&q=x+y")

        assert response.status_code == 200, response.text
        assert QueryRecorder.seen == {
            "get": {"next": "/a?b=1", "q": "x y"},
            "query_string": "next=%2Fa%3Fb%3D1&q=x+y",
            "full_path": "/page?next=%2Fa%3Fb%3D1&q=x+y",
        }


def _add_routes_of_each_response_type(api: BoltAPI) -> None:
    @api.get("/html")
    async def html():
        return HTML("<p>x</p>")

    @api.get("/plain")
    def plain():
        return PlainText("x")

    @api.get("/octet")
    async def octet():
        return Response(b"\x00\x01", media_type="application/octet-stream")

    @api.get("/bytes")
    async def raw_bytes() -> bytes:
        return b"raw"

    @api.get("/str")
    async def text() -> str:
        return "hello"

    @api.get("/json")
    async def json_body() -> dict:
        return {"a": 1}

    @api.get("/empty", status_code=204)
    async def empty():
        return None

    @api.get("/redirect")
    async def redirect():
        return Redirect("/target")


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/html", "text/html; charset=utf-8"),
        ("/plain", "text/plain; charset=utf-8"),
        ("/octet", "application/octet-stream"),
        ("/bytes", "application/octet-stream"),
        ("/str", "text/plain; charset=utf-8"),
        ("/json", "application/json"),
        ("/empty", None),
        ("/redirect", None),
    ],
)
def test_django_middleware_keeps_the_content_type_of_the_response(path, content_type):
    """A response gets the same Content-Type with and without Django middleware.

    A built-in response type keeps its content type in its type, not in its
    headers. A type with no content type keeps none.
    """
    plain_api = BoltAPI()
    _add_routes_of_each_response_type(plain_api)
    middleware_api = BoltAPI(django_middleware=["django.middleware.clickjacking.XFrameOptionsMiddleware"])
    _add_routes_of_each_response_type(middleware_api)

    with TestClient(plain_api) as client:
        expected = client.get(path, follow_redirects=False)
    with TestClient(middleware_api) as client:
        response = client.get(path, follow_redirects=False)

    assert response.headers.get("x-frame-options") == "DENY"
    assert expected.headers.get("content-type") == content_type
    assert response.headers.get("content-type") == content_type
    assert (response.status_code, response.content) == (expected.status_code, expected.content)


def test_django_middleware_keeps_a_304_without_a_content_type():
    """Django's 304 has no Content-Type, and Bolt adds none."""
    api = BoltAPI(django_middleware=["django.middleware.http.ConditionalGetMiddleware"])

    @api.get("/doc")
    async def doc() -> dict:
        return {"a": 1}

    with TestClient(api) as client:
        first = client.get("/doc")
        response = client.get("/doc", headers={"If-None-Match": first.headers["etag"]})

    assert first.headers["content-type"] == "application/json"
    assert response.status_code == 304
    assert "content-type" not in response.headers
    assert response.content == b""


class OwnHtmlResponse(Middleware):
    """Python middleware that answers with its own response, which has no headers."""

    response_type = "html"

    async def process_request(self, request):
        await self.get_response(request)
        return MiddlewareResponse(200, {}, b"<p>own</p>", type(self).response_type)


class OwnUnknownTypeResponse(OwnHtmlResponse):
    """The same, with a response type that Rust does not know."""

    response_type = "custom"


@pytest.mark.parametrize(
    ("middleware", "content_type"),
    [
        (OwnHtmlResponse, "text/html; charset=utf-8"),
        (OwnUnknownTypeResponse, "application/octet-stream"),
    ],
    ids=["html", "unknown-type"],
)
def test_django_middleware_keeps_the_content_type_of_a_middleware_response_type(middleware, content_type):
    """A MiddlewareResponse with no headers gets the content type that Rust gives its type."""
    responses = []
    for django_middleware in (None, ["django.middleware.clickjacking.XFrameOptionsMiddleware"]):
        api = BoltAPI(middleware=[middleware], django_middleware=django_middleware)

        @api.get("/page")
        async def page() -> dict:
            return {"a": 1}

        with TestClient(api) as client:
            responses.append(client.get("/page"))
    without_django, with_django = responses

    assert with_django.headers.get("x-frame-options") == "DENY"
    assert without_django.headers.get("content-type") == content_type
    assert with_django.headers.get("content-type") == content_type
    assert with_django.content == without_django.content == b"<p>own</p>"


class BytesLikeBody(Middleware):
    """Python middleware that sets a bytes-like body that is not bytes."""

    body_type = bytearray

    async def process_request(self, request):
        response = await self.get_response(request)
        response.body = type(self).body_type(b"abc")
        return response


class MemoryViewBody(BytesLikeBody):
    body_type = memoryview


@pytest.mark.parametrize("middleware", [BytesLikeBody, MemoryViewBody], ids=["bytearray", "memoryview"])
def test_django_middleware_keeps_a_bytes_like_body(middleware):
    """A bytearray or memoryview body reaches the client as its bytes, not as its repr."""
    api = BoltAPI(
        middleware=[middleware],
        django_middleware=["django.middleware.clickjacking.XFrameOptionsMiddleware"],
    )

    @api.get("/page")
    async def page() -> dict:
        return {"a": 1}

    with TestClient(api) as client:
        response = client.get("/page")

    assert response.headers.get("x-frame-options") == "DENY"
    assert response.content == b"abc"


@pytest.mark.parametrize(("path", "body"), [("/bytearray", b"abc"), ("/number", b"42")])
def test_django_middleware_keeps_the_body_of_a_response(path, body):
    """The bridge to Django middleware sends the rendered body, not its Python repr."""
    api = BoltAPI(django_middleware=["django.middleware.clickjacking.XFrameOptionsMiddleware"])

    @api.get("/bytearray")
    async def byte_array():
        return Response(bytearray(b"abc"), media_type="application/octet-stream")

    @api.get("/number")
    async def number():
        return Response(42, media_type="text/plain")

    with TestClient(api) as client:
        response = client.get(path)

    assert response.headers.get("x-frame-options") == "DENY"
    assert response.status_code == 200
    assert response.content == body


class _SeenByDjangoMiddleware(MiddlewareMixin):
    """Django middleware that marks each response that reaches its process_response."""

    def process_response(self, request, response):
        response["X-Seen"] = "yes"
        return response


def _answers_with(make_response) -> type[Middleware]:
    """Return a Bolt middleware whose process_request answers with ``make_response()``."""

    class AnswersWithResponseObject(Middleware):
        async def process_request(self, request):
            return make_response()

    return AnswersWithResponseObject


_BOLT_RESPONSE_CASES = {
    "html": (
        lambda: HTML("<p>x</p>", status_code=202, headers={"X-Own": "1"}),
        202,
        "text/html; charset=utf-8",
        b"<p>x</p>",
    ),
    "plain": (lambda: PlainText("hi", status_code=201), 201, "text/plain; charset=utf-8", b"hi"),
    "json": (lambda: JSON({"a": 1}, status_code=201), 201, "application/json", b'{"a":1}'),
    "media-type": (
        lambda: Response("<b>2</b>", media_type="application/xml", headers={"X-Own": "1"}),
        200,
        "application/xml",
        b"<b>2</b>",
    ),
    "content-type-header": (
        lambda: Response("<b>2</b>", media_type="application/xml", headers={"Content-Type": "text/xml"}),
        200,
        "text/xml",
        b"<b>2</b>",
    ),
    "redirect": (lambda: Redirect("/elsewhere", headers={"X-Own": "1"}), 307, None, b""),
}

_DJANGO_MIDDLEWARE_WRAPPERS = [
    pytest.param(lambda: DjangoMiddleware(_SeenByDjangoMiddleware), id="DjangoMiddleware"),
    pytest.param(lambda: DjangoMiddlewareStack([_SeenByDjangoMiddleware]), id="DjangoMiddlewareStack"),
]


@pytest.mark.parametrize("wrapper", _DJANGO_MIDDLEWARE_WRAPPERS)
@pytest.mark.parametrize("case", _BOLT_RESPONSE_CASES)
def test_django_middleware_keeps_a_bolt_response_of_a_middleware(wrapper, case):
    """A Bolt response object keeps its status, content type, body and headers behind Django middleware."""
    make_response, status_code, content_type, body = _BOLT_RESPONSE_CASES[case]
    api = BoltAPI(middleware=[wrapper(), _answers_with(make_response)])

    @api.get("/page")
    async def page() -> dict:
        return {"a": 1}

    with TestClient(api) as client:
        response = client.get("/page", follow_redirects=False)

    assert response.headers.get("x-seen") == "yes"
    assert response.status_code == status_code
    assert response.headers.get("content-type") == content_type
    assert response.content == body
    if case == "redirect":
        assert response.headers.get("location") == "/elsewhere"
    if case in {"html", "media-type", "redirect"}:
        assert response.headers.get("x-own") == "1"


@pytest.mark.parametrize("wrapper", _DJANGO_MIDDLEWARE_WRAPPERS)
def test_django_middleware_keeps_the_cookies_of_a_bolt_response(wrapper):
    """A cookie on a Bolt response object reaches the client behind Django middleware."""
    api = BoltAPI(
        middleware=[wrapper(), _answers_with(lambda: HTML("x").set_cookie("sid", "abc", httponly=True))],
    )

    @api.get("/page")
    async def page() -> dict:
        return {"a": 1}

    with TestClient(api) as client:
        response = client.get("/page")

    assert response.headers.get("x-seen") == "yes"
    assert response.headers.get("content-type") == "text/html; charset=utf-8"
    assert response.cookies.get("sid") == "abc"
    assert "httponly" in response.headers["set-cookie"].lower()


def _two_chunks():
    yield b"a"
    yield b"b"


@pytest.mark.parametrize("wrapper", _DJANGO_MIDDLEWARE_WRAPPERS)
def test_django_middleware_streams_a_streaming_response_of_a_middleware(wrapper):
    """A StreamingResponse of a middleware sends its chunks behind Django middleware.

    Before, the client got the repr of the generator as JSON.
    """
    api = BoltAPI(
        middleware=[wrapper(), _answers_with(lambda: StreamingResponse(_two_chunks(), media_type="text/plain"))],
    )

    @api.get("/page")
    async def page() -> dict:
        return {"a": 1}

    with TestClient(api) as client:
        response = client.get("/page")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/plain"
    assert response.content == b"ab"


class StrBody(Middleware):
    """Python middleware that sets a str body."""

    async def process_request(self, request):
        response = await self.get_response(request)
        response.body = "text \u00e9 body"
        return response


def test_django_middleware_keeps_a_str_body():
    """A str body reaches the client as its UTF-8 bytes."""
    api = BoltAPI(
        middleware=[StrBody],
        django_middleware=["django.middleware.clickjacking.XFrameOptionsMiddleware"],
    )

    @api.get("/page")
    async def page() -> dict:
        return {"a": 1}

    with TestClient(api) as client:
        response = client.get("/page")

    assert response.headers.get("x-frame-options") == "DENY"
    assert response.content == "text \u00e9 body".encode()


class _PassThrough(MiddlewareMixin):
    """A Django middleware with no hooks."""


class _TagResponse(MiddlewareMixin):
    def process_response(self, request, response):
        response["X-Tag"] = "tagged"
        return response


def test_routes_of_a_router_with_one_django_middleware_answer_their_own_bodies():
    """Each route chain gets its own copy of a wrapper that the routes share."""
    api = BoltAPI()
    router = Router(prefix="/r", middleware=[DjangoMiddleware(_TagResponse)])

    @router.get("/a")
    async def route_a():
        return {"route": "a"}

    @router.get("/b")
    async def route_b():
        return {"route": "b"}

    api.include_router(router)

    with TestClient(api) as client:
        for name in ("a", "b", "a", "b"):
            response = client.get(f"/r/{name}")
            assert response.json() == {"route": name}
            assert response.headers.get("x-tag") == "tagged"


def test_a_route_that_skips_a_django_middleware_keeps_it_for_the_other_routes():
    """The own global chain of a route does not change the wrappers of the API chain."""
    api = BoltAPI(middleware=[DjangoMiddleware(_PassThrough), DjangoMiddleware(_TagResponse)])

    @api.get("/normal")
    async def normal():
        return {"ok": True}

    @api.get("/skip")
    @skip_middleware("_TagResponse")
    async def skip():
        return {"ok": True}

    with TestClient(api) as client:
        assert client.get("/normal").headers.get("x-tag") == "tagged"
        assert "x-tag" not in client.get("/skip").headers
        assert client.get("/normal").headers.get("x-tag") == "tagged"
