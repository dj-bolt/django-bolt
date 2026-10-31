"""
Test Django decorator compatibility with Django-Bolt.

Tests that Django decorators like @login_required return Django HttpResponse types
that are properly handled by the serialization layer.
"""

from __future__ import annotations

import pytest
from django.http import (
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
)

from django_bolt import BoltAPI
from django_bolt.testing import TestClient
from django_bolt.types import Request


@pytest.fixture(scope="module")
def api():
    """Create test API with Django decorator test routes."""
    api = BoltAPI()

    # Simulate @login_required behavior - returns HttpResponseRedirect when not authenticated
    @api.get("/login-required")
    async def login_required_endpoint(request: Request):
        """Simulates what @login_required does when user is not authenticated."""
        # Check if user is authenticated (simplified check)
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            # This is what @login_required returns
            return HttpResponseRedirect("/accounts/login/?next=/login-required")
        return {"user": "authenticated"}

    # Simulate CSRF failure - returns HttpResponseForbidden
    @api.post("/csrf-protected")
    async def csrf_protected_endpoint(request: Request):
        """Simulates what happens when CSRF validation fails."""
        # Check for CSRF token (simplified)
        csrf_token = request.headers.get("x-csrftoken")
        if not csrf_token:
            # This is what Django's CSRF middleware returns on failure
            return HttpResponseForbidden("CSRF verification failed.")
        return {"status": "ok"}

    # Generic Django HttpResponse
    @api.get("/django-response")
    async def django_response_endpoint():
        """Returns a generic Django HttpResponse."""
        response = HttpResponse(content="Hello from Django", status=200, content_type="text/plain")
        response["X-Custom-Header"] = "custom-value"
        return response

    # Django HttpResponse with custom status
    @api.get("/django-response-created")
    async def django_response_created():
        """Returns Django HttpResponse with 201 status."""
        return HttpResponse("Created", status=201)

    # Normal Bolt response for comparison
    @api.get("/normal-response")
    async def normal_response():
        """Normal dict response - should not go through Django path."""
        return {"status": "ok"}

    return api


@pytest.fixture(scope="module")
def client(api):
    """Create TestClient for the API."""
    with TestClient(api) as client:
        yield client


class TestLoginRequiredDecorator:
    """Test @login_required decorator compatibility."""

    def test_login_required_redirect_when_not_authenticated(self, client):
        """Test that login_required-style redirect is properly handled.

        When @login_required decorates a view and user is not authenticated,
        it returns HttpResponseRedirect to the login page.
        """
        response = client.get("/login-required", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers.get("location") == "/accounts/login/?next=/login-required"

    def test_login_required_redirect_location_header(self, client):
        """Test that redirect location header is properly set."""
        response = client.get("/login-required", follow_redirects=False)

        location = response.headers.get("location")
        assert location is not None
        assert "/accounts/login/" in location
        assert "next=" in location


class TestCSRFHandling:
    """Test CSRF-related Django response handling."""

    def test_csrf_failure_returns_403(self, client):
        """Test that CSRF failure response (403 Forbidden) is properly handled."""
        # POST without CSRF token
        response = client.post("/csrf-protected")

        assert response.status_code == 403
        assert b"CSRF" in response.content

    def test_csrf_success_with_token(self, client):
        """Test that request with CSRF token succeeds."""
        response = client.post("/csrf-protected", headers={"x-csrftoken": "valid-token"})

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestDjangoHttpResponse:
    """Test generic Django HttpResponse handling."""

    def test_django_http_response_content(self, client):
        """Test Django HttpResponse content is properly returned."""
        response = client.get("/django-response")

        assert response.status_code == 200
        assert response.content == b"Hello from Django"

    def test_django_http_response_headers(self, client):
        """Test Django HttpResponse headers are preserved."""
        response = client.get("/django-response")

        assert response.headers.get("x-custom-header") == "custom-value"
        assert "text/plain" in response.headers.get("content-type", "")

    def test_django_http_response_custom_status(self, client):
        """Test Django HttpResponse with custom status code."""
        response = client.get("/django-response-created")

        assert response.status_code == 201
        assert response.content == b"Created"


class TestNormalResponsesUnaffected:
    """Test that normal Bolt responses are not affected by Django handling."""

    def test_dict_response_works(self, client):
        """Test that dict responses still work normally."""
        response = client.get("/normal-response")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_dict_response_is_json(self, client):
        """Test that dict responses are JSON encoded."""
        response = client.get("/normal-response")

        assert "application/json" in response.headers.get("content-type", "")


class TestDjangoRedirect:
    """Test the headers and body of a Django HttpResponseRedirect."""

    def test_django_redirect_keeps_its_other_headers_and_sends_no_body(self):
        api = BoltAPI()

        @api.get("/redirect")
        async def redirect():
            response = HttpResponseRedirect("/target")
            response["X-Extra"] = "1"
            return response

        with TestClient(api) as client:
            response = client.get("/redirect", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/target"
        assert response.headers["x-extra"] == "1"
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert response.content == b""

    def test_django_redirect_without_a_content_type_sends_none(self):
        """A redirect has no default content type, so the response has no Content-Type header."""
        api = BoltAPI()

        @api.get("/redirect")
        async def redirect():
            response = HttpResponseRedirect("/target")
            del response["Content-Type"]
            return response

        with TestClient(api) as client:
            response = client.get("/redirect", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/target"
        assert "content-type" not in response.headers
