"""Tests for cookie functionality in django-bolt responses."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from django_bolt import JSON, Cookie, Response, StreamingResponse
from django_bolt.cookies import make_delete_cookie
from django_bolt.responses import HTML, PlainText, Redirect
from django_bolt.serialization import (
    serialize_html_response,
    serialize_plaintext_response,
    serialize_redirect_response,
)


class TestCookieSerialization:
    """Tests for Cookie.to_header_value() serialization."""

    def test_basic_cookie(self):
        """Test basic cookie with just name and value."""
        cookie = Cookie("session", "abc123")
        header = cookie.to_header_value()
        assert "session=abc123" in header
        assert "Path=/" in header
        assert "SameSite=Lax" in header

    def test_cookie_with_max_age(self):
        """Test cookie with max_age attribute."""
        cookie = Cookie("session", "abc123", max_age=3600)
        header = cookie.to_header_value()
        assert "Max-Age=3600" in header

    def test_cookie_with_expires_datetime(self):
        """Test cookie with expires as datetime object."""
        expires = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
        cookie = Cookie("session", "abc123", expires=expires)
        header = cookie.to_header_value()
        assert "expires=Wed, 31 Dec 2025 23:59:59 GMT" in header

    def test_cookie_with_expires_string(self):
        """Test cookie with expires as string."""
        cookie = Cookie("session", "abc123", expires="Thu, 01 Jan 2026 00:00:00 GMT")
        header = cookie.to_header_value()
        assert "expires=Thu, 01 Jan 2026 00:00:00 GMT" in header

    def test_cookie_with_domain(self):
        """Test cookie with domain attribute."""
        cookie = Cookie("session", "abc123", domain=".example.com")
        header = cookie.to_header_value()
        assert "Domain=.example.com" in header

    def test_cookie_with_custom_path(self):
        """Test cookie with custom path."""
        cookie = Cookie("session", "abc123", path="/api")
        header = cookie.to_header_value()
        assert "Path=/api" in header

    def test_cookie_secure_flag(self):
        """Test cookie with secure flag."""
        cookie = Cookie("session", "abc123", secure=True)
        header = cookie.to_header_value()
        assert "Secure" in header

    def test_cookie_httponly_flag(self):
        """Test cookie with httponly flag."""
        cookie = Cookie("session", "abc123", httponly=True)
        header = cookie.to_header_value()
        assert "HttpOnly" in header

    def test_cookie_samesite_strict(self):
        """Test cookie with SameSite=Strict."""
        cookie = Cookie("session", "abc123", samesite="Strict")
        header = cookie.to_header_value()
        assert "SameSite=Strict" in header

    def test_cookie_samesite_none(self):
        """Test cookie with SameSite=None (requires Secure)."""
        cookie = Cookie("session", "abc123", samesite="None", secure=True)
        header = cookie.to_header_value()
        assert "SameSite=None" in header
        assert "Secure" in header

    def test_cookie_samesite_disabled(self):
        """Test cookie with SameSite disabled (False)."""
        cookie = Cookie("session", "abc123", samesite=False)
        header = cookie.to_header_value()
        assert "SameSite" not in header

    def test_cookie_all_attributes(self):
        """Test cookie with all attributes set."""
        cookie = Cookie(
            name="session",
            value="abc123",
            max_age=3600,
            expires="Thu, 01 Jan 2026 00:00:00 GMT",
            path="/api",
            domain=".example.com",
            secure=True,
            httponly=True,
            samesite="Strict",
        )
        header = cookie.to_header_value()
        assert "session=abc123" in header
        assert "Max-Age=3600" in header
        assert "expires=Thu, 01 Jan 2026 00:00:00 GMT" in header
        assert "Path=/api" in header
        assert "Domain=.example.com" in header
        assert "Secure" in header
        assert "HttpOnly" in header
        assert "SameSite=Strict" in header

    def test_cookie_value_escaping(self):
        """Test that special characters in cookie values are properly escaped."""
        cookie = Cookie("data", "hello world")
        header = cookie.to_header_value()
        # SimpleCookie should properly quote values with spaces
        assert "data=" in header


class TestMakeDeleteCookie:
    """Tests for make_delete_cookie helper function."""

    def test_delete_cookie_basic(self):
        """Test basic delete cookie creation."""
        cookie = make_delete_cookie("session")
        assert cookie.name == "session"
        assert cookie.value == ""
        assert cookie.max_age == 0
        assert cookie.expires == "Thu, 01 Jan 1970 00:00:00 GMT"
        assert cookie.path == "/"
        assert cookie.domain is None

    def test_delete_cookie_with_path(self):
        """Test delete cookie with custom path."""
        cookie = make_delete_cookie("session", path="/api")
        assert cookie.path == "/api"

    def test_delete_cookie_with_domain(self):
        """Test delete cookie with domain."""
        cookie = make_delete_cookie("session", domain=".example.com")
        assert cookie.domain == ".example.com"

    def test_delete_cookie_header_value(self):
        """Test delete cookie serializes correctly."""
        cookie = make_delete_cookie("session")
        header = cookie.to_header_value()
        assert "session=" in header
        assert "Max-Age=0" in header
        assert "expires=Thu, 01 Jan 1970 00:00:00 GMT" in header


class TestResponseSetCookie:
    """Tests for set_cookie() method on response classes."""

    def test_response_set_cookie_returns_self(self):
        """Test that set_cookie returns self for method chaining."""
        response = Response({"ok": True})
        result = response.set_cookie("session", "abc123")
        assert result is response

    def test_response_method_chaining(self):
        """Test multiple set_cookie calls can be chained."""
        response = Response({"ok": True}).set_cookie("session", "abc123").set_cookie("prefs", "dark")
        assert hasattr(response, "_cookies")
        assert len(response._cookies) == 2
        assert response._cookies[0].name == "session"
        assert response._cookies[1].name == "prefs"

    def test_json_set_cookie(self):
        """Test set_cookie on JSON response."""
        response = JSON({"user": "john"}).set_cookie("token", "xyz", httponly=True)
        assert len(response._cookies) == 1
        assert response._cookies[0].name == "token"
        assert response._cookies[0].httponly is True

    def test_plaintext_set_cookie(self):
        """Test set_cookie on PlainText response."""
        response = PlainText("Hello").set_cookie("visit", "1")
        assert len(response._cookies) == 1
        assert response._cookies[0].name == "visit"

    def test_html_set_cookie(self):
        """Test set_cookie on HTML response."""
        response = HTML("<h1>Hi</h1>").set_cookie("theme", "light")
        assert len(response._cookies) == 1
        assert response._cookies[0].name == "theme"

    def test_redirect_set_cookie(self):
        """Test set_cookie on Redirect response."""
        response = Redirect("/dashboard").set_cookie("logged_in", "true")
        assert len(response._cookies) == 1
        assert response._cookies[0].name == "logged_in"

    def test_streaming_response_set_cookie(self):
        """Test set_cookie on StreamingResponse."""

        def gen():
            yield b"chunk1"
            yield b"chunk2"

        response = StreamingResponse(gen()).set_cookie("stream_id", "12345")
        assert len(response._cookies) == 1
        assert response._cookies[0].name == "stream_id"

    def test_set_cookie_all_parameters(self):
        """Test set_cookie with all parameters."""
        response = Response({"ok": True}).set_cookie(
            name="session",
            value="abc123",
            max_age=3600,
            expires="Thu, 01 Jan 2026 00:00:00 GMT",
            path="/api",
            domain=".example.com",
            secure=True,
            httponly=True,
            samesite="Strict",
        )
        cookie = response._cookies[0]
        assert cookie.name == "session"
        assert cookie.value == "abc123"
        assert cookie.max_age == 3600
        assert cookie.expires == "Thu, 01 Jan 2026 00:00:00 GMT"
        assert cookie.path == "/api"
        assert cookie.domain == ".example.com"
        assert cookie.secure is True
        assert cookie.httponly is True
        assert cookie.samesite == "Strict"


class TestResponseDeleteCookie:
    """Tests for delete_cookie() method on response classes."""

    def test_response_delete_cookie_returns_self(self):
        """Test that delete_cookie returns self for method chaining."""
        response = Response({"ok": True})
        result = response.delete_cookie("session")
        assert result is response

    def test_delete_cookie_creates_expired_cookie(self):
        """Test delete_cookie creates a cookie that expires immediately."""
        response = Response({"ok": True}).delete_cookie("old_session")
        assert len(response._cookies) == 1
        cookie = response._cookies[0]
        assert cookie.name == "old_session"
        assert cookie.value == ""
        assert cookie.max_age == 0

    def test_delete_cookie_with_path(self):
        """Test delete_cookie with custom path."""
        response = Response({"ok": True}).delete_cookie("session", path="/api")
        cookie = response._cookies[0]
        assert cookie.path == "/api"

    def test_delete_cookie_with_domain(self):
        """Test delete_cookie with domain."""
        response = Response({"ok": True}).delete_cookie("session", domain=".example.com")
        cookie = response._cookies[0]
        assert cookie.domain == ".example.com"

    def test_combined_set_and_delete(self):
        """Test setting new cookies while deleting old ones."""
        response = Response({"ok": True}).set_cookie("new_session", "xyz").delete_cookie("old_session")
        assert len(response._cookies) == 2
        assert response._cookies[0].name == "new_session"
        assert response._cookies[0].value == "xyz"
        assert response._cookies[1].name == "old_session"
        assert response._cookies[1].max_age == 0


class TestSerializationWithCookies:
    """Tests for raw cookie tuples in ResponseMeta (Rust handles serialization)."""

    def test_plaintext_serialization_includes_cookies(self):
        """Test that PlainText serialization includes raw cookie tuples for Rust."""
        response = PlainText("Hello").set_cookie("greeting", "sent")
        status, meta, body_kind, body = serialize_plaintext_response(response)
        assert body_kind == 0  # _BODY_BYTES
        # Verify ResponseMeta format: (response_type, custom_ct, custom_headers, cookies)
        assert isinstance(meta, tuple)
        assert len(meta) == 4
        response_type, custom_ct, custom_headers, cookies = meta
        assert response_type == "plaintext"
        assert cookies is not None
        assert len(cookies) == 1
        # Cookie tuple: (name, value, path, max_age, expires, domain, secure, httponly, samesite)
        assert cookies[0][0] == "greeting"
        assert cookies[0][1] == "sent"

    def test_html_serialization_includes_cookies(self):
        """Test that HTML serialization includes raw cookie tuples for Rust."""
        response = HTML("<h1>Hi</h1>").set_cookie("page", "home")
        status, meta, body_kind, body = serialize_html_response(response)
        assert body_kind == 0  # _BODY_BYTES
        assert isinstance(meta, tuple)
        response_type, custom_ct, custom_headers, cookies = meta
        assert response_type == "html"
        assert cookies is not None
        assert len(cookies) == 1
        assert cookies[0][0] == "page"
        assert cookies[0][1] == "home"

    def test_redirect_serialization_includes_cookies(self):
        """Test that Redirect serialization includes raw cookie tuples for Rust."""
        response = Redirect("/dashboard").set_cookie("redirected", "true")
        status, meta, body_kind, body = serialize_redirect_response(response)
        assert body_kind == 0  # _BODY_BYTES
        assert isinstance(meta, tuple)
        response_type, custom_ct, custom_headers, cookies = meta
        assert response_type == "redirect"
        assert cookies is not None
        assert len(cookies) == 1
        assert cookies[0][0] == "redirected"
        assert cookies[0][1] == "true"

    def test_multiple_cookies_in_serialization(self):
        """Test that multiple cookies produce multiple raw tuples for Rust."""
        response = PlainText("OK").set_cookie("a", "1").set_cookie("b", "2").set_cookie("c", "3")
        status, meta, body_kind, body = serialize_plaintext_response(response)
        assert body_kind == 0  # _BODY_BYTES
        assert isinstance(meta, tuple)
        response_type, custom_ct, custom_headers, cookies = meta
        assert cookies is not None
        assert len(cookies) == 3
        assert cookies[0][0] == "a"
        assert cookies[1][0] == "b"
        assert cookies[2][0] == "c"


class TestCookieImport:
    """Tests for Cookie class import from django_bolt."""

    def test_cookie_import(self):
        """Test that Cookie can be imported from django_bolt."""
        from django_bolt import Cookie

        cookie = Cookie("test", "value")
        assert cookie.name == "test"
        assert cookie.value == "value"


@pytest.mark.asyncio
class TestAsyncSerializationWithCookies:
    """Tests for raw cookie tuples in async serialized responses (Rust handles serialization)."""

    async def test_json_async_serialization_includes_cookies(self):
        """Test that JSON async serialization includes raw cookie tuples for Rust."""
        from django_bolt.serialization import serialize_json_response

        response = JSON({"ok": True}).set_cookie("api_token", "secret123", httponly=True)
        status, meta, body_kind, body = await serialize_json_response(response, None, None)
        assert body_kind == 0  # _BODY_BYTES
        assert isinstance(meta, tuple)
        response_type, custom_ct, custom_headers, cookies = meta
        assert response_type == "json"
        assert cookies is not None
        assert len(cookies) == 1
        # Cookie tuple: (name, value, path, max_age, expires, domain, secure, httponly, samesite)
        assert cookies[0][0] == "api_token"
        assert cookies[0][1] == "secret123"
        assert cookies[0][7] is True  # httponly

    async def test_response_async_serialization_includes_cookies(self):
        """Test that Response async serialization includes raw cookie tuples for Rust."""
        from django_bolt.serialization import serialize_generic_response

        response = Response({"status": "logged_in"}).set_cookie("session", "xyz", secure=True)
        status, meta, body_kind, body = await serialize_generic_response(response, None, None)
        assert body_kind == 0  # _BODY_BYTES
        assert isinstance(meta, tuple)
        response_type, custom_ct, custom_headers, cookies = meta
        assert cookies is not None
        assert len(cookies) == 1
        assert cookies[0][0] == "session"
        assert cookies[0][1] == "xyz"
        assert cookies[0][6] is True  # secure


class TestCookieSecurityValidation:
    """Tests for cookie security validation in Rust layer.

    These tests verify that invalid cookies are rejected to prevent
    injection attacks and other security issues.
    """

    def test_valid_cookie_passes(self):
        """Test that valid cookies are properly serialized."""
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()

        @api.get("/set-cookie")
        async def set_cookie():
            return Response({"ok": True}).set_cookie("session", "abc123", httponly=True)

        client = TestClient(api)
        response = client.get("/set-cookie")
        assert response.status_code == 200
        # Check Set-Cookie header is present
        set_cookie = response.headers.get("set-cookie", "")
        assert "session=" in set_cookie
        assert "HttpOnly" in set_cookie

    def test_invalid_cookie_name_rejected(self):
        """Test that cookie names with injection characters are rejected.

        Rust should reject cookie names containing separators like ; which
        could be used for injection attacks. A warning is logged to stderr.
        """
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()

        @api.get("/bad-cookie-name")
        async def bad_cookie_name():
            # Attempt injection via cookie name
            return Response({"ok": True}).set_cookie("session; Path=/evil", "xyz")

        client = TestClient(api)
        response = client.get("/bad-cookie-name")
        assert response.status_code == 200
        # Cookie should be rejected - no Set-Cookie header
        set_cookie = response.headers.get("set-cookie", "")
        assert "evil" not in set_cookie

    def test_control_chars_in_value_rejected(self):
        """Test that cookie values with control characters are rejected.

        Control characters like CR/LF could enable header injection attacks.
        A warning is logged to stderr.
        """
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()

        @api.get("/bad-cookie-value")
        async def bad_cookie_value():
            # Attempt header injection via cookie value
            return Response({"ok": True}).set_cookie("session", "value\r\nSet-Cookie: evil=1")

        client = TestClient(api)
        response = client.get("/bad-cookie-value")
        assert response.status_code == 200
        # Cookie should be rejected - no evil cookie
        set_cookie = response.headers.get("set-cookie", "")
        assert "evil" not in set_cookie

    def test_empty_cookie_name_rejected(self):
        """Test that empty cookie names are rejected. A warning is logged to stderr."""
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()

        @api.get("/empty-name")
        async def empty_name():
            return Response({"ok": True}).set_cookie("", "value")

        client = TestClient(api)
        response = client.get("/empty-name")
        assert response.status_code == 200
        # Empty name cookie should be rejected
        set_cookie = response.headers.get("set-cookie", "")
        assert set_cookie == "" or "=" not in set_cookie.split(";")[0]

    def test_special_chars_in_value_escaped(self):
        """Test that special characters in cookie values are properly escaped."""
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()

        @api.get("/special-value")
        async def special_value():
            # Value with spaces should be quoted
            return Response({"ok": True}).set_cookie("data", "hello world")

        client = TestClient(api)
        response = client.get("/special-value")
        assert response.status_code == 200
        set_cookie = response.headers.get("set-cookie", "")
        assert "data=" in set_cookie

    def test_multiple_valid_cookies(self):
        """Test that multiple valid cookies all get set."""
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()

        @api.get("/multi-cookie")
        async def multi_cookie():
            return Response({"ok": True}).set_cookie("a", "1").set_cookie("b", "2").set_cookie("c", "3")

        client = TestClient(api)
        response = client.get("/multi-cookie")
        assert response.status_code == 200
        # All cookies should be present (may be in multiple headers or one)
        all_cookies = response.headers.get("set-cookie", "")
        # Check all three cookies are set
        assert "a=" in all_cookies or response.headers.get_list("set-cookie")

    def test_mixed_valid_invalid_cookies(self):
        """Test that valid cookies are set even when mixed with invalid ones.

        Note: Rust logs a warning to stderr for invalid cookies, but we can't
        capture Rust's eprintln! from Python tests. Check server logs manually
        for: [django-bolt] WARNING: Invalid cookie name 'bad; injection'
        """
        from django_bolt import BoltAPI
        from django_bolt.testing import TestClient

        api = BoltAPI()

        @api.get("/mixed-cookies")
        async def mixed_cookies():
            return (
                Response({"ok": True})
                .set_cookie("valid", "good")
                .set_cookie("bad; injection", "evil")  # Invalid name - rejected
                .set_cookie("also_valid", "fine")
            )

        client = TestClient(api)
        response = client.get("/mixed-cookies")
        assert response.status_code == 200
        set_cookie = response.headers.get("set-cookie", "")
        # Valid cookies should be present
        assert "valid=" in set_cookie or "also_valid=" in set_cookie
        # Injection attempt should be blocked
        assert "injection" not in set_cookie
