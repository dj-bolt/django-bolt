"""
Tests for CORS handling on 404 routes and POST-only routes.

These tests verify fixes for:
1. CORS preflight on non-existent routes returns 204 (not 404)
2. CORS preflight on POST-only routes finds CORS config correctly
3. CORS headers are present on 404 responses for actual requests

These tests would FAIL without the fixes in handler.rs.
"""

import time

import jwt
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import IsAdminUser, IsAuthenticated, JWTAuthentication
from django_bolt.middleware import cors
from django_bolt.testing import TestClient


class TestCorsPreflightOn404Routes:
    """
    Test CORS preflight handling for non-existent routes.

    BUG FIXED: Previously, OPTIONS preflight to non-existent routes returned 404,
    which browsers reject. Now returns 204 with CORS headers.
    """

    def test_preflight_to_nonexistent_route_returns_204(self):
        """
        Test that OPTIONS preflight to a non-existent route returns 204.

        This test would FAIL without the fix because:
        - Before: OPTIONS /nonexistent -> 404 Not Found
        - After:  OPTIONS /nonexistent -> 204 No Content with CORS headers

        Browsers reject preflight responses with non-2xx status codes.
        """
        api = BoltAPI()

        @api.get("/existing-route")
        @cors(origins=["https://example.com"])
        async def existing_endpoint():
            return {"message": "exists"}

        global_origins = ["https://example.com", "https://trusted.com"]

        with TestClient(api, use_http_layer=True, cors_allowed_origins=global_origins) as client:
            response = client.options(
                "/api/nonexistent-route",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "GET",
                },
            )

            # CRITICAL: Must be 204, not 404
            assert response.status_code == 204, (
                f"Preflight to non-existent route should return 204, got {response.status_code}"
            )

            # CRITICAL: Must have CORS headers
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com", (
                f"Missing or wrong Access-Control-Allow-Origin header: {response.headers}"
            )

    def test_preflight_to_nonexistent_route_has_cors_headers(self):
        """
        Test that preflight to non-existent route includes all CORS headers.
        """
        api = BoltAPI()

        @api.get("/existing")
        async def existing():
            return {"message": "exists"}

        global_origins = ["https://example.com"]

        with TestClient(
            api,
            use_http_layer=True,
            cors_allowed_origins=global_origins,
        ) as client:
            response = client.options(
                "/does/not/exist",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type, Authorization",
                },
            )

            assert response.status_code == 204

            # Check required CORS headers are present
            assert "Access-Control-Allow-Origin" in response.headers
            assert "Access-Control-Allow-Methods" in response.headers
            assert "Access-Control-Allow-Headers" in response.headers
            # Note: credentials is false by default when using global origins without @cors decorator

    def test_actual_request_to_nonexistent_route_returns_404_with_cors(self):
        """
        Test that actual GET/POST to non-existent route returns 404 WITH CORS headers.

        This allows the browser to read the error response.
        """
        api = BoltAPI()

        @api.get("/existing")
        async def existing():
            return {"message": "exists"}

        global_origins = ["https://example.com"]

        with TestClient(api, use_http_layer=True, cors_allowed_origins=global_origins) as client:
            response = client.get(
                "/api/nonexistent",
                headers={"Origin": "https://example.com"},
            )

            # Should be 404
            assert response.status_code == 404

            # CRITICAL: Must have CORS headers so browser can read error
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com", (
                f"404 response missing CORS headers: {response.headers}"
            )


class TestCorsPreflightOnPostOnlyRoutes:
    """
    Test CORS preflight handling for POST-only routes.

    BUG FIXED: Previously, preflight only looked for GET routes to find CORS config.
    Now checks all HTTP methods (GET, POST, PUT, PATCH, DELETE).
    """

    def test_preflight_to_post_only_route_has_cors_headers(self):
        """
        Test that OPTIONS preflight to POST-only route returns CORS headers.

        This test would FAIL without the fix because:
        - Before: Only looked for GET route to find CORS config
        - After:  Checks GET, POST, PUT, PATCH, DELETE in order

        For a POST-only route, the old code would not find CORS config.
        """
        api = BoltAPI()

        # POST-only route - no GET equivalent
        @api.post("/post-only")
        @cors(origins=["https://example.com"])
        async def post_only():
            return {"message": "POST only endpoint"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.options(
                "/post-only",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )

            # Should be 204
            assert response.status_code == 204, (
                f"Preflight to POST-only route should return 204, got {response.status_code}"
            )

            # CRITICAL: Must have CORS headers from route-level config
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com", (
                f"POST-only route preflight missing CORS headers: {response.headers}"
            )

            # Should have methods including POST
            methods = response.headers.get("Access-Control-Allow-Methods", "")
            assert "POST" in methods, f"POST should be in allowed methods: {methods}"

    def test_preflight_to_post_only_with_global_cors(self):
        """
        Test that POST-only route uses global CORS config when no decorator.
        """
        api = BoltAPI()

        # POST-only route without @cors decorator
        @api.post("/post-global-cors")
        async def post_global_cors():
            return {"message": "POST only endpoint"}

        global_origins = ["https://example.com"]

        with TestClient(api, use_http_layer=True, cors_allowed_origins=global_origins) as client:
            response = client.options(
                "/post-global-cors",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                },
            )

            # Should be 204
            assert response.status_code == 204

            # Should use global CORS config
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    def test_preflight_to_put_only_route(self):
        """
        Test that PUT-only routes also get CORS headers.
        """
        api = BoltAPI()

        @api.put("/put-only")
        @cors(origins=["https://example.com"])
        async def put_only():
            return {"message": "PUT only endpoint"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.options(
                "/put-only",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "PUT",
                },
            )

            assert response.status_code == 204
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    def test_preflight_to_delete_only_route(self):
        """
        Test that DELETE-only routes also get CORS headers.
        """
        api = BoltAPI()

        @api.delete("/delete-only")
        @cors(origins=["https://example.com"])
        async def delete_only():
            return {"message": "DELETE only endpoint"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.options(
                "/delete-only",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "DELETE",
                },
            )

            assert response.status_code == 204
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    def test_preflight_to_patch_only_route(self):
        """
        Test that PATCH-only routes also get CORS headers.
        """
        api = BoltAPI()

        @api.patch("/patch-only")
        @cors(origins=["https://example.com"])
        async def patch_only():
            return {"message": "PATCH only endpoint"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.options(
                "/patch-only",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "PATCH",
                },
            )

            assert response.status_code == 204
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    def test_actual_post_request_works_after_preflight(self):
        """
        Test that actual POST request works after successful preflight.
        """
        api = BoltAPI()

        @api.post("/post-only")
        @cors(origins=["https://example.com"])
        async def post_only():
            return {"message": "POST only endpoint"}

        with TestClient(api, use_http_layer=True) as client:
            # First, preflight
            preflight = client.options(
                "/post-only",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )
            assert preflight.status_code == 204

            # Then, actual POST
            response = client.post(
                "/post-only",
                headers={
                    "Origin": "https://example.com",
                    "Content-Type": "application/json",
                },
                json={"test": "data"},
            )

            assert response.status_code == 200
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"


class TestCorsOriginSchemeRequired:
    """
    Test that CORS origins must include scheme (http:// or https://).

    This is a documentation/configuration test, not a code fix test.
    """

    def test_origin_with_scheme_matches(self):
        """
        Test that origin WITH scheme matches correctly.

        Browser sends: Origin: https://example.com
        Config has:    https://example.com
        Result:        Match!
        """
        api = BoltAPI()

        @api.get("/test")
        @cors(origins=["https://example.com"])
        async def test_endpoint():
            return {"message": "test"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get(
                "/test",
                headers={"Origin": "https://example.com"},
            )

            assert response.status_code == 200
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"

    def test_origin_not_in_allowed_list_rejected(self):
        """
        Test that origin not in allowed list is rejected.
        """
        api = BoltAPI()

        @api.get("/test")
        @cors(origins=["https://example.com"])
        async def test_endpoint():
            return {"message": "test"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get(
                "/test",
                headers={"Origin": "https://evil.com"},
            )

            assert response.status_code == 200
            # Should NOT have CORS header for disallowed origin
            assert (
                "Access-Control-Allow-Origin" not in response.headers
                or response.headers.get("Access-Control-Allow-Origin") != "https://evil.com"
            )


class TestCorsOnErrorResponses:
    """
    Test CORS headers on error responses (401, 403, 429).

    BUG FIXED: Previously, error responses (401 Unauthorized, 403 Forbidden,
    429 Too Many Requests) did not include CORS headers, causing browsers to
    block JavaScript from reading the error message.
    """

    def test_401_unauthorized_has_cors_headers_with_global_cors(self):
        """
        Test that 401 Unauthorized response includes CORS headers.

        This test would FAIL without the fix because:
        - Before: 401 response had no CORS headers
        - After:  401 response includes Access-Control-Allow-Origin

        Without CORS headers, browsers block JS from reading the 401 error.
        """
        api = BoltAPI()

        @api.get(
            "/protected",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
        async def protected_endpoint():
            return {"message": "protected"}

        global_origins = ["https://example.com"]

        with TestClient(api, use_http_layer=True, cors_allowed_origins=global_origins) as client:
            # Request without auth token
            response = client.get(
                "/protected",
                headers={"Origin": "https://example.com"},
            )

            # Should be 401
            assert response.status_code == 401, f"Expected 401, got {response.status_code}"

            # CRITICAL: Must have CORS headers so browser can read error
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com", (
                f"401 response missing CORS headers: {dict(response.headers)}"
            )

    def test_403_forbidden_has_cors_headers_with_global_cors(self):
        """
        Test that 403 Forbidden response includes CORS headers.

        This test would FAIL without the fix because:
        - Before: 403 response had no CORS headers
        - After:  403 response includes Access-Control-Allow-Origin
        """
        api = BoltAPI()

        @api.get(
            "/admin-only",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAdminUser()],
        )
        async def admin_endpoint():
            return {"message": "admin only"}

        global_origins = ["https://example.com"]

        # Create a valid token but NOT an admin
        token = jwt.encode(
            {
                "sub": "regular-user",
                "exp": int(time.time()) + 3600,
                "is_superuser": False,
            },
            "test-secret",
            algorithm="HS256",
        )

        with TestClient(api, use_http_layer=True, cors_allowed_origins=global_origins) as client:
            response = client.get(
                "/admin-only",
                headers={
                    "Origin": "https://example.com",
                    "Authorization": f"Bearer {token}",
                },
            )

            # Should be 403 (authenticated but not admin)
            assert response.status_code == 403, f"Expected 403, got {response.status_code}"

            # CRITICAL: Must have CORS headers so browser can read error
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com", (
                f"403 response missing CORS headers: {dict(response.headers)}"
            )

    def test_401_has_cors_with_route_level_cors(self):
        """
        Test that 401 response uses route-level CORS config when available.
        """
        api = BoltAPI()

        @api.get(
            "/protected-with-cors",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
        @cors(origins=["https://route-level.com"])
        async def protected_with_cors():
            return {"message": "protected"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get(
                "/protected-with-cors",
                headers={"Origin": "https://route-level.com"},
            )

            assert response.status_code == 401

            # Should use route-level CORS config
            assert response.headers.get("Access-Control-Allow-Origin") == "https://route-level.com", (
                f"401 should use route-level CORS: {dict(response.headers)}"
            )

    def test_403_has_cors_with_route_level_cors(self):
        """
        Test that 403 response uses route-level CORS config when available.
        """
        api = BoltAPI()

        @api.get(
            "/admin-with-cors",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAdminUser()],
        )
        @cors(origins=["https://route-level.com"])
        async def admin_with_cors():
            return {"message": "admin"}

        token = jwt.encode(
            {
                "sub": "regular-user",
                "exp": int(time.time()) + 3600,
                "is_superuser": False,
            },
            "test-secret",
            algorithm="HS256",
        )

        with TestClient(api, use_http_layer=True) as client:
            response = client.get(
                "/admin-with-cors",
                headers={
                    "Origin": "https://route-level.com",
                    "Authorization": f"Bearer {token}",
                },
            )

            assert response.status_code == 403

            # Should use route-level CORS config
            assert response.headers.get("Access-Control-Allow-Origin") == "https://route-level.com", (
                f"403 should use route-level CORS: {dict(response.headers)}"
            )

    def test_disallowed_origin_no_cors_on_401(self):
        """
        Test that 401 response does NOT include CORS headers for disallowed origin.
        """
        api = BoltAPI()

        @api.get(
            "/protected",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
        async def protected():
            return {"message": "protected"}

        global_origins = ["https://allowed.com"]

        with TestClient(api, use_http_layer=True, cors_allowed_origins=global_origins) as client:
            response = client.get(
                "/protected",
                headers={"Origin": "https://evil.com"},
            )

            assert response.status_code == 401

            # Should NOT have CORS header for disallowed origin
            cors_header = response.headers.get("Access-Control-Allow-Origin")
            assert cors_header != "https://evil.com", f"Should not allow evil.com origin: {cors_header}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
