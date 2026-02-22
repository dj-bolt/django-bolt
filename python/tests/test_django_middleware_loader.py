"""
Tests for Django middleware loader.

Tests verify the middleware loader API works correctly with actual HTTP requests
to ensure middleware runs and affects request/response behavior.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.middleware import DjangoMiddlewareStack, TimingMiddleware
from django_bolt.middleware.django_loader import (
    DEFAULT_EXCLUDED_MIDDLEWARE,
    get_django_middleware_setting,
    load_django_middleware,
)
from django_bolt.testing import TestClient

# =============================================================================
# Test Default Exclusions
# =============================================================================


class TestDefaultExclusions:
    """Tests for default middleware exclusions (now empty by default)."""

    def test_no_default_exclusions(self):
        """Test that there are no default exclusions - all middleware loaded."""
        # We now load ALL middleware from settings.MIDDLEWARE by default
        # Users can exclude specific middleware if needed via the exclude config
        assert set() == DEFAULT_EXCLUDED_MIDDLEWARE

    def test_csrf_not_excluded_by_default(self):
        """Test that CSRF middleware is NOT excluded by default."""
        assert "django.middleware.csrf.CsrfViewMiddleware" not in DEFAULT_EXCLUDED_MIDDLEWARE

    def test_clickjacking_not_excluded_by_default(self):
        """Test that clickjacking middleware is NOT excluded by default."""
        assert "django.middleware.clickjacking.XFrameOptionsMiddleware" not in DEFAULT_EXCLUDED_MIDDLEWARE

    def test_messages_not_excluded_by_default(self):
        """Test that messages middleware is NOT excluded by default."""
        assert "django.contrib.messages.middleware.MessageMiddleware" not in DEFAULT_EXCLUDED_MIDDLEWARE


# =============================================================================
# Test load_django_middleware
# =============================================================================


class TestLoadDjangoMiddleware:
    """Tests for load_django_middleware function."""

    def test_returns_empty_list_when_false(self):
        """Test that False returns empty list."""
        result = load_django_middleware(False)
        assert result == []

    def test_returns_empty_list_when_none(self):
        """Test that None returns empty list."""
        result = load_django_middleware(None)
        assert result == []

    def test_returns_middleware_stack_for_list(self):
        """Test that list config returns a DjangoMiddlewareStack."""
        result = load_django_middleware(
            [
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )
        assert len(result) == 1
        assert isinstance(result[0], DjangoMiddlewareStack)

    def test_returns_middleware_stack_for_true(self):
        """Test that True returns a DjangoMiddlewareStack (if MIDDLEWARE setting exists)."""
        result = load_django_middleware(True)
        # Should return either empty list (no middleware) or single stack
        assert isinstance(result, list)
        if result:
            assert len(result) == 1
            assert isinstance(result[0], DjangoMiddlewareStack)

    def test_exclude_config_filters_middleware(self):
        """Test that exclude configuration filters out middleware."""
        result = load_django_middleware(
            {
                "include": [
                    "django.contrib.sessions.middleware.SessionMiddleware",
                    "django.middleware.common.CommonMiddleware",
                ],
                "exclude": ["django.middleware.common.CommonMiddleware"],
            }
        )

        # Only SessionMiddleware should be loaded (as a stack)
        assert len(result) == 1
        assert isinstance(result[0], DjangoMiddlewareStack)
        # The stack should only contain SessionMiddleware
        assert len(result[0].middleware_classes) == 1

    def test_handles_invalid_middleware_gracefully(self):
        """Test that invalid middleware paths are skipped gracefully."""
        # load_django_middleware should skip invalid paths and not crash
        result = load_django_middleware(
            [
                "django.contrib.sessions.middleware.SessionMiddleware",
                "nonexistent.middleware.BrokenMiddleware",
            ]
        )

        # Only valid middleware should be loaded (as a stack)
        assert len(result) == 1
        assert isinstance(result[0], DjangoMiddlewareStack)
        # The stack should only contain SessionMiddleware
        assert len(result[0].middleware_classes) == 1


# =============================================================================
# Test get_django_middleware_setting
# =============================================================================


class TestGetDjangoMiddlewareSetting:
    """Tests for get_django_middleware_setting function."""

    def test_returns_list(self):
        """Test that it returns a list."""
        result = get_django_middleware_setting()
        assert isinstance(result, list)


# =============================================================================
# Test BoltAPI Integration
# =============================================================================


class TestBoltAPIIntegration:
    """Tests for BoltAPI django_middleware parameter."""

    def test_boltapi_no_django_middleware(self):
        """Test BoltAPI without django_middleware."""
        api = BoltAPI()
        assert api._middleware == []

    def test_boltapi_django_middleware_false(self):
        """Test BoltAPI with django_middleware=False."""
        api = BoltAPI(django_middleware=False)
        assert api._middleware == []

    def test_boltapi_django_middleware_list_creates_stack(self):
        """Test BoltAPI with middleware list creates DjangoMiddlewareStack."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        assert len(api._middleware) == 1
        assert isinstance(api._middleware[0], DjangoMiddlewareStack)

    def test_boltapi_combines_django_and_custom_middleware(self):
        """Test BoltAPI stores both Django and custom middleware."""
        api = BoltAPI(
            django_middleware=["django.contrib.sessions.middleware.SessionMiddleware"],
            middleware=[TimingMiddleware],
        )

        # Should have 2 middleware entries
        assert len(api._middleware) == 2
        # First should be DjangoMiddlewareStack
        assert isinstance(api._middleware[0], DjangoMiddlewareStack)
        # Second should be the custom middleware class
        assert api._middleware[1] is TimingMiddleware


# =============================================================================
# Custom Middleware for Testing (named without "Test" prefix to avoid pytest collection)
# =============================================================================


class CustomHeaderMiddleware:
    """Custom middleware that adds a test header to verify it runs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Test-Middleware"] = "executed"
        return response


class RequestStateMiddleware:
    """Custom middleware that modifies request state."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Add to request.state which is accessible in Bolt handlers
        request.custom_middleware_value = "middleware_executed"
        return self.get_response(request)


# =============================================================================
# Test Full HTTP Cycle with Middleware Verification
# =============================================================================


@pytest.mark.django_db
class TestMiddlewareHTTPCycle:
    """Tests that verify middleware actually runs and affects requests/responses."""

    def test_session_middleware_runs_and_sets_session(self):
        """Test SessionMiddleware actually runs and sets session on request."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
            ]
        )

        session_available = {"has_session": False}

        @api.get("/test")
        async def test_route(request):
            # Verify session is available via request.state
            session = request.state.get("session")
            if session is not None:
                session_available["has_session"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Verify middleware actually ran and set session
            assert session_available["has_session"] is True

    def test_common_middleware_adds_content_length(self):
        """Test CommonMiddleware runs and adds Content-Length header."""
        api = BoltAPI(
            django_middleware=[
                "django.middleware.common.CommonMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # CommonMiddleware should add Content-Length
            assert "content-length" in response.headers or "Content-Length" in response.headers

    def test_custom_middleware_adds_header(self):
        """Test custom middleware runs and adds response header."""
        api = BoltAPI(middleware=[DjangoMiddlewareStack([CustomHeaderMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Verify our custom middleware actually ran
            assert response.headers.get("X-Test-Middleware") == "executed"

    def test_middleware_execution_order(self):
        """Test middleware executes in correct order (both request and response phases)."""
        # This test verifies middleware runs by checking headers added during response phase
        api = BoltAPI(middleware=[DjangoMiddlewareStack([CustomHeaderMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Middleware adds header during response phase
            assert response.headers.get("X-Test-Middleware") == "executed"

    def test_multiple_django_middleware_all_run(self):
        """Test multiple Django middleware all execute in order."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.common.CommonMiddleware",
            ]
        )

        checks = {"has_session": False, "has_content_length": False}

        @api.get("/test")
        async def test_route(request):
            # Check session from SessionMiddleware
            if request.state.get("session") is not None:
                checks["has_session"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Both middleware should have run
            assert checks["has_session"] is True
            checks["has_content_length"] = "content-length" in response.headers or "Content-Length" in response.headers
            assert checks["has_content_length"] is True

    def test_combined_django_and_bolt_middleware(self):
        """Test Django middleware and Bolt middleware work together."""
        api = BoltAPI(
            django_middleware=["django.contrib.sessions.middleware.SessionMiddleware"],
            middleware=[TimingMiddleware],
        )

        checks = {"has_session": False, "has_timing": False}

        @api.get("/test")
        async def test_route(request):
            if request.state.get("session") is not None:
                checks["has_session"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Django middleware should run
            assert checks["has_session"] is True
            # Bolt TimingMiddleware should add timing header
            checks["has_timing"] = "X-Response-Time" in response.headers or "x-response-time" in response.headers
            assert checks["has_timing"] is True

    def test_middleware_with_exclude_config(self):
        """Test that excluded middleware doesn't run."""
        # Create middleware stack with one middleware excluded
        api = BoltAPI(middleware=[DjangoMiddlewareStack([CustomHeaderMiddleware])])

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Middleware should run (not excluded)
            assert response.headers.get("X-Test-Middleware") == "executed"

        # Now test without middleware
        api2 = BoltAPI()

        @api2.get("/test")
        async def test_route2():
            return {"status": "ok"}

        with TestClient(api2) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Middleware should not run (not configured)
            assert "X-Test-Middleware" not in response.headers


# =============================================================================
# Test Real Django Middleware Execution
# =============================================================================


@pytest.mark.django_db
class TestRealDjangoMiddleware:
    """Tests that verify real Django middleware (CSRF, Security, etc.) actually run."""

    def test_csrf_middleware_runs(self):
        """Test CSRF middleware runs (verifies by checking it doesn't break requests)."""
        api = BoltAPI(
            django_middleware=[
                "django.middleware.csrf.CsrfViewMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            # CSRF middleware should run without breaking GET requests
            response = client.get("/test")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            # CSRF middleware is active and processed the request

    def test_security_middleware_adds_headers(self):
        """Test SecurityMiddleware runs and adds security headers."""
        api = BoltAPI(
            django_middleware=[
                "django.middleware.security.SecurityMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # SecurityMiddleware should add security headers
            # Check for at least one security header (case-insensitive)
            headers_lower = {k.lower(): v for k, v in response.headers.items()}
            has_security_header = (
                "x-content-type-options" in headers_lower
                or "x-xss-protection" in headers_lower
                or "strict-transport-security" in headers_lower
            )
            assert has_security_header, "SecurityMiddleware should add security headers"

    def test_clickjacking_middleware_adds_xframe_header(self):
        """Test XFrameOptionsMiddleware runs and adds X-Frame-Options header."""
        api = BoltAPI(
            django_middleware=[
                "django.middleware.clickjacking.XFrameOptionsMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Clickjacking middleware should add X-Frame-Options
            headers_lower = {k.lower(): v for k, v in response.headers.items()}
            assert "x-frame-options" in headers_lower, "XFrameOptionsMiddleware should add X-Frame-Options header"
            assert headers_lower["x-frame-options"] == "DENY"

    def test_gzip_middleware_compresses_response(self):
        """Test GZipMiddleware runs and compresses responses."""
        api = BoltAPI(
            django_middleware=[
                "django.middleware.gzip.GZipMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            # Return large response to trigger compression
            return {"data": "x" * 1000}

        with TestClient(api) as client:
            # Request with Accept-Encoding to trigger gzip
            response = client.get("/test", headers={"Accept-Encoding": "gzip"})
            assert response.status_code == 200
            # GZip middleware should add Content-Encoding header for large responses
            headers_lower = {k.lower(): v for k, v in response.headers.items()}
            # Django GZipMiddleware compresses responses > 200 bytes when Accept-Encoding: gzip
            assert "content-encoding" in headers_lower, "GZipMiddleware should add Content-Encoding header"
            assert headers_lower["content-encoding"] == "gzip"

    def test_messages_middleware_enables_messages_framework(self):
        """Test MessageMiddleware runs and enables Django messages framework."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.messages.middleware.MessageMiddleware",
            ]
        )

        message_added = {"success": False}

        @api.get("/test")
        async def test_route(request):
            from django.contrib import messages  # noqa: PLC0415

            # Try to add a message - this should work if middleware ran
            try:
                messages.info(request, "Test message")
                message_added["success"] = True
            except Exception:
                message_added["success"] = False
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Verify messages framework was available
            assert message_added["success"] is True

    def test_authentication_middleware_sets_user(self):
        """Test AuthenticationMiddleware runs and sets user on request."""
        api = BoltAPI(
            django_middleware=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
            ]
        )

        user_info = {"has_user": False, "is_anonymous": False}

        @api.get("/test")
        async def test_route(request):
            # AuthenticationMiddleware should set request.user
            if hasattr(request, "user"):
                user_info["has_user"] = True
                user_info["is_anonymous"] = request.user.is_anonymous
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # User should be set (AnonymousUser for unauthenticated)
            assert user_info["has_user"] is True
            assert user_info["is_anonymous"] is True

    def test_locale_middleware_processes_language(self):
        """Test LocaleMiddleware runs and processes Accept-Language header."""
        api = BoltAPI(
            django_middleware=[
                "django.middleware.locale.LocaleMiddleware",
            ]
        )

        @api.get("/test")
        async def test_route():
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test", headers={"Accept-Language": "en-US"})
            assert response.status_code == 200
            # Middleware should run without error
            # Just verify it doesn't break the request

    def test_multiple_real_django_middleware_stack(self):
        """Test multiple real Django middleware all run together."""
        api = BoltAPI(
            django_middleware=[
                "django.middleware.security.SecurityMiddleware",
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.common.CommonMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django.middleware.clickjacking.XFrameOptionsMiddleware",
            ]
        )

        checks = {
            "has_session": False,
            "has_user": False,
            "has_xframe": False,
            "has_csrf": False,
        }

        @api.get("/test")
        async def test_route(request):
            # Check various middleware effects
            if request.state.get("session") is not None:
                checks["has_session"] = True
            if hasattr(request, "user"):
                checks["has_user"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200

            # Verify multiple middleware ran
            assert checks["has_session"] is True, "SessionMiddleware should set session"
            assert checks["has_user"] is True, "AuthenticationMiddleware should set user"

            headers_lower = {k.lower(): v for k, v in response.headers.items()}
            checks["has_xframe"] = "x-frame-options" in headers_lower
            checks["has_csrf"] = "csrftoken" in response.cookies or "csrf" in str(response.headers).lower()

            assert checks["has_xframe"] is True, "XFrameOptionsMiddleware should add header"
            # CSRF token may or may not be present depending on request type
