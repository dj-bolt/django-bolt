"""
Tests for Django admin integration that actually use a Django project.

These tests configure Django properly and validate admin via ASGI mounts.
"""

import pytest

from django_bolt.api import BoltAPI
from django_bolt.testing import TestClient


@pytest.mark.django_db(transaction=True)
def test_admin_root_redirect():
    """Test /admin/ returns content (redirect or login page) via TestClient."""
    from django_bolt.admin.admin_detection import should_enable_admin  # noqa: PLC0415

    if not should_enable_admin():
        pytest.skip("Django admin not enabled")

    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    @api.get("/test")
    async def test_route():
        return {"test": "ok"}

    # Check if admin mount was registered
    if not api._admin_routes_registered:
        pytest.skip("Admin mount was not registered")

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/admin/")

        print("\n[Admin Root Test]")
        print(f"Status: {response.status_code}")
        print(f"Headers: {dict(response.headers)}")
        print(f"Body length: {len(response.content)}")
        print(f"Body preview: {response.text[:300] if response.text else 'N/A'}")

        # Should return a valid response (redirect or login page)
        assert response.status_code in (200, 301, 302), f"Expected valid response, got {response.status_code}"

        # CRITICAL: Body should NOT be empty
        assert len(response.content) > 0, (
            f"Response body is EMPTY! Got {len(response.content)} bytes. Admin mount path is broken."
        )


@pytest.mark.django_db(transaction=True)
def test_admin_login_page():
    """Test /admin/login/ returns HTML page (not empty body) via TestClient."""
    from django_bolt.admin.admin_detection import should_enable_admin  # noqa: PLC0415

    if not should_enable_admin():
        pytest.skip("Django admin not enabled")

    api = BoltAPI()
    api._register_admin_routes("127.0.0.1", 8000)

    @api.get("/test")
    async def test_route():
        return {"test": "ok"}

    # Check if admin mount was registered
    if not api._admin_routes_registered:
        pytest.skip("Admin mount was not registered")

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/admin/login/")

        print("\n[Admin Login Test]")
        print(f"Status: {response.status_code}")
        print(f"Headers: {dict(response.headers)}")
        print(f"Body length: {len(response.content)}")
        print(f"Body preview: {response.text[:300]}")

        # Should return 200 OK
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # CRITICAL: Body should NOT be empty - THIS IS THE BUG
        assert len(response.content) > 0, (
            f"Admin login page body is EMPTY! Got {len(response.content)} bytes. Admin mount path is broken."
        )

        # Should be HTML
        content_type = response.headers.get("content-type", "")
        assert "html" in content_type.lower(), f"Expected HTML, got {content_type}"

        # Should contain login form
        body_text = response.text.lower()
        assert "login" in body_text or "django" in body_text, f"Expected login content, got: {body_text[:200]}"
