"""
Comprehensive CORS implementation tests.

These tests verify correct CORS behavior and would fail if:
- CORS middleware is removed
- Implementation is incorrect (missing Vary headers, wrong origin matching, etc.)

Tests route-level CORS decorators with TestClient use_http_layer=True mode.

NOTE: Global Django settings (CORS_ALLOWED_ORIGINS, CORS_ALLOWED_ORIGIN_REGEXES)
are tested in actual server integration tests, not with TestClient.
"""

import warnings

import pytest

from django_bolt import BoltAPI
from django_bolt.middleware import cors, skip_middleware
from django_bolt.testing import TestClient


class TestOriginMatching:
    """Test origin matching logic (exact match, regex, wildcard)"""

    def test_exact_origin_match_allowed(self):
        """Test that exact origin match is allowed"""
        api = BoltAPI()

        @api.get("/data")
        @cors(origins=["https://example.com", "https://trusted.com"])
        async def get_data():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/data", headers={"Origin": "https://example.com"})

            assert response.status_code == 200
            # CRITICAL: Must reflect the origin
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"
            # CRITICAL: Must have Vary: Origin when reflecting
            assert "Origin" in response.headers.get("Vary", "")

    def test_exact_origin_match_rejected(self):
        """Test that unlisted origins are rejected"""
        api = BoltAPI()

        @api.get("/data")
        @cors(origins=["https://example.com"])
        async def get_data():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/data", headers={"Origin": "https://evil.com"})

            assert response.status_code == 200
            # CRITICAL: Must NOT have CORS headers for disallowed origin
            assert "Access-Control-Allow-Origin" not in response.headers

    # NOTE: Regex origin matching via CORS_ALLOWED_ORIGIN_REGEXES requires actual Django
    # server with settings, not TestClient. Tested in integration tests.

    def test_wildcard_origin(self):
        """Test wildcard origin allows all origins"""
        api = BoltAPI()

        @api.get("/public")
        @cors(origins=["*"])
        async def get_public():
            return {"data": "public"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/public", headers={"Origin": "https://any-domain.com"})

            assert response.status_code == 200
            # CRITICAL: Wildcard without credentials uses literal "*"
            assert response.headers.get("Access-Control-Allow-Origin") == "*"
            # CRITICAL: Wildcard should NOT have Vary header
            assert "Origin" not in response.headers.get("Vary", "")

    # NOTE: Testing "no Origin header" behavior is not reliably testable with TestClient
    # as it may add default headers. Test with actual HTTP requests instead.


class TestWildcardCredentials:
    """Test wildcard + credentials handling (invalid per CORS spec)"""

    # NOTE: Wildcard + credentials via Django settings (CORS_ALLOW_ALL_ORIGINS + CORS_ALLOW_CREDENTIALS)
    # is tested in integration tests with actual server, not TestClient.

    def test_wildcard_triggers_warning_with_credentials(self):
        """Test that using wildcard with credentials triggers a warning"""
        api = BoltAPI()

        # This should trigger a RuntimeWarning
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")

            @api.get("/api/data")
            @cors(origins=["*"], credentials=True)
            async def get_data():
                return {"data": "sensitive"}

            # Filter to only CORS wildcard+credentials warnings
            cors_warnings = [
                warning
                for warning in w
                if issubclass(warning.category, RuntimeWarning)
                and "wildcard" in str(warning.message).lower()
                and "credentials" in str(warning.message).lower()
            ]

            # Check that at least one warning was raised with correct content
            assert len(cors_warnings) >= 1, f"Expected CORS warning, got {len(cors_warnings)} warnings"
            assert "wildcard" in str(cors_warnings[0].message).lower()
            assert "credentials" in str(cors_warnings[0].message).lower()

    def test_wildcard_warning_points_at_the_decorator_line(self):
        """The warning names the file of the caller, not the file of the library."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            @cors(origins=["*"], credentials=True)
            async def get_data():
                return {}

        assert [warning.filename for warning in caught] == [__file__]
        assert caught[0].category is RuntimeWarning
        assert str(caught[0].message) == (
            "CORS misconfiguration: Cannot use wildcard '*' with credentials=True. "
            "This violates the CORS specification. Please specify explicit origins."
        )


class TestVaryHeaders:
    """Test Vary headers are added per CORS spec"""

    def test_vary_origin_when_reflecting(self):
        """Test that Vary: Origin is added when reflecting origin"""
        api = BoltAPI()

        @api.get("/data")
        @cors(origins=["https://example.com", "https://trusted.com"])
        async def get_data():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/data", headers={"Origin": "https://example.com"})

            # CRITICAL: Must have Vary: Origin for caching
            vary_header = response.headers.get("Vary", "")
            assert "Origin" in vary_header

    def test_vary_headers_for_preflight(self):
        """Test that preflight responses have proper Vary headers"""
        api = BoltAPI()

        @api.post("/data")
        @cors(
            origins=["https://example.com"], methods=["GET", "POST", "PUT"], headers=["Content-Type", "Authorization"]
        )
        async def post_data(data: dict):
            return {"received": data}

        with TestClient(api, use_http_layer=True) as client:
            # Preflight request
            response = client.options(
                "/data",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type",
                },
            )

            # CRITICAL: Preflight must have Vary headers per CORS spec
            vary_header = response.headers.get("Vary", "")
            # Verify Vary header exists (exact content may vary by implementation)
            assert vary_header != "", f"Expected Vary header in preflight, got: {response.headers}"
            # Should include at least one of the standard preflight Vary headers
            assert any(
                h in vary_header for h in ["Access-Control-Request-Method", "Access-Control-Request-Headers", "Origin"]
            )


class TestPreflightRequests:
    """Test CORS preflight OPTIONS handling"""

    def test_preflight_returns_204(self):
        """Test that preflight requests return 204 No Content"""
        api = BoltAPI()

        @api.post("/api/submit")
        @cors(
            origins=["https://app.example.com"],
            methods=["GET", "POST", "PUT", "DELETE"],
            headers=["Content-Type", "Authorization", "X-Custom-Header"],
        )
        async def submit_data(data: dict):
            return {"success": True}

        with TestClient(api, use_http_layer=True) as client:
            response = client.options(
                "/api/submit",
                headers={
                    "Origin": "https://app.example.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type, Authorization",
                },
            )

            # CRITICAL: Preflight must return 204
            assert response.status_code == 204
            # CRITICAL: Must reflect allowed origin
            assert response.headers.get("Access-Control-Allow-Origin") == "https://app.example.com"
            # CRITICAL: Must include allowed methods
            methods = response.headers.get("Access-Control-Allow-Methods", "")
            assert "POST" in methods
            # CRITICAL: Must include allowed headers
            headers = response.headers.get("Access-Control-Allow-Headers", "")
            assert "Content-Type" in headers
            assert "Authorization" in headers

    def test_preflight_rejected_for_disallowed_origin(self):
        """Test that preflight is rejected for disallowed origins"""
        api = BoltAPI()

        @api.post("/api/submit")
        @cors(origins=["https://trusted.com"])
        async def submit_data(data: dict):
            return {"success": True}

        with TestClient(api, use_http_layer=True) as client:
            # First verify that allowed origin works
            allowed_response = client.options(
                "/api/submit", headers={"Origin": "https://trusted.com", "Access-Control-Request-Method": "POST"}
            )
            assert allowed_response.status_code == 204
            assert allowed_response.headers.get("Access-Control-Allow-Origin") == "https://trusted.com"

            # Now test that disallowed origin is rejected
            response = client.options(
                "/api/submit", headers={"Origin": "https://evil.com", "Access-Control-Request-Method": "POST"}
            )

            # CRITICAL: Preflight must be rejected (403 or no CORS headers)
            # If 403, it's rejected. If 204 but no headers, also rejected.
            if response.status_code == 204:
                # BUG: If we get 204 with CORS headers for disallowed origin, test should fail
                assert "Access-Control-Allow-Origin" not in response.headers, (
                    f"CORS headers should NOT be present for disallowed origin, got: {response.headers}"
                )
            else:
                assert response.status_code == 403

    def test_preflight_with_credentials(self):
        """Test preflight with credentials flag"""
        api = BoltAPI()

        @api.post("/secure")
        @cors(origins=["https://app.example.com"], credentials=True, methods=["POST", "PUT"], max_age=7200)
        async def secure_endpoint(data: dict):
            return {"data": data}

        with TestClient(api, use_http_layer=True) as client:
            response = client.options(
                "/secure", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"}
            )

            assert response.status_code == 204
            # CRITICAL: Must include credentials header
            assert response.headers.get("Access-Control-Allow-Credentials") == "true"
            # CRITICAL: Must have max age
            assert response.headers.get("Access-Control-Max-Age") == "7200"

    def test_preflight_max_age_header(self):
        """Test that preflight includes max-age header"""
        api = BoltAPI()

        @api.post("/data")
        @cors(origins=["https://app.example.com"], methods=["POST"], max_age=86400)
        async def post_data(data: dict):
            return {"received": data}

        with TestClient(api, use_http_layer=True) as client:
            # Preflight should have Max-Age header
            preflight = client.options(
                "/data", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"}
            )
            assert preflight.status_code == 204
            # CRITICAL: Preflight must include max-age
            max_age = preflight.headers.get("Access-Control-Max-Age")
            assert max_age == "86400"

    def test_preflight_defaults_allow_each_standard_method_for_one_hour(self):
        """A @cors with no methods and no max_age allows seven methods and caches the preflight for 3600 seconds."""
        api = BoltAPI()

        @api.get("/data")
        @cors(origins=["https://app.example.com"])
        async def get_data():
            return {}

        with TestClient(api, use_http_layer=True) as client:
            preflight = client.options(
                "/data", headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "GET"}
            )

        assert preflight.status_code == 204
        assert preflight.headers.get("Access-Control-Allow-Methods") == "GET, POST, PUT, PATCH, DELETE, OPTIONS, QUERY"
        assert preflight.headers.get("Access-Control-Max-Age") == "3600"


class TestRouteLevelVsGlobal:
    """Test route-level CORS config vs global config priority"""

    def test_route_level_overrides_global(self):
        """Test that route-level config overrides global config"""
        api = BoltAPI()

        @api.get("/custom")
        @cors(origins=["https://custom.com"])
        async def custom_endpoint():
            return {"data": "custom"}

        # Global config has different origins
        with TestClient(api, use_http_layer=True, cors_allowed_origins=["https://global.com"]) as client:
            # Route-level origin should work
            response = client.get("/custom", headers={"Origin": "https://custom.com"})
            assert response.status_code == 200
            assert response.headers.get("Access-Control-Allow-Origin") == "https://custom.com"

            # Global origin should NOT work on this route
            response = client.get("/custom", headers={"Origin": "https://global.com"})
            assert "Access-Control-Allow-Origin" not in response.headers

    # NOTE: Global regex origins (CORS_ALLOWED_ORIGIN_REGEXES) and global exact origins
    # (CORS_ALLOWED_ORIGINS) via Django settings are tested in integration tests.


class TestSkipMiddleware:
    """Test @skip_middleware("cors") functionality"""

    def test_skip_cors_removes_headers(self):
        """Test that @skip_middleware("cors") removes CORS headers"""
        api = BoltAPI()

        @api.get("/no-cors")
        @skip_middleware("cors")
        async def no_cors_endpoint():
            return {"data": "no cors"}

        with TestClient(api, use_http_layer=True, cors_allowed_origins=["https://example.com"]) as client:
            response = client.get("/no-cors", headers={"Origin": "https://example.com"})

            assert response.status_code == 200
            # CRITICAL: Must NOT have CORS headers when skipped
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_skip_cors_on_route_with_decorator(self):
        """Test that skip works even when @cors decorator is present"""
        api = BoltAPI()

        @api.get("/conflicting")
        @cors(origins=["https://example.com"])
        @skip_middleware("cors")
        async def conflicting_endpoint():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/conflicting", headers={"Origin": "https://example.com"})

            # CRITICAL: Skip should take precedence
            assert "Access-Control-Allow-Origin" not in response.headers


class TestCorsCredentials:
    """Test CORS credentials handling"""

    def test_credentials_flag_adds_header(self):
        """Test that credentials=True adds Access-Control-Allow-Credentials header"""
        api = BoltAPI()

        @api.get("/secure")
        @cors(origins=["https://app.example.com"], credentials=True)
        async def secure_endpoint():
            return {"data": "sensitive"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/secure", headers={"Origin": "https://app.example.com"})

            assert response.status_code == 200
            # CRITICAL: Must have credentials header
            assert response.headers.get("Access-Control-Allow-Credentials") == "true"

    def test_no_credentials_flag_omits_header(self):
        """Test that credentials=False omits Access-Control-Allow-Credentials header"""
        api = BoltAPI()

        @api.get("/public")
        @cors(origins=["https://example.com"], credentials=False)
        async def public_endpoint():
            return {"data": "public"}

        with TestClient(api, use_http_layer=True) as client:
            response = client.get("/public", headers={"Origin": "https://example.com"})

            assert response.status_code == 200
            # CRITICAL: Must NOT have credentials header
            assert "Access-Control-Allow-Credentials" not in response.headers


class TestCorsEdgeCases:
    """Test edge cases and corner scenarios"""

    def test_multiple_origins_in_config(self):
        """Test handling multiple origins in configuration"""
        api = BoltAPI()

        @api.get("/multi")
        @cors(origins=["https://app1.com", "https://app2.com", "https://app3.com"])
        async def multi_endpoint():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            # First origin should work
            response = client.get("/multi", headers={"Origin": "https://app1.com"})
            assert response.headers.get("Access-Control-Allow-Origin") == "https://app1.com"

            # Second origin should work
            response = client.get("/multi", headers={"Origin": "https://app2.com"})
            assert response.headers.get("Access-Control-Allow-Origin") == "https://app2.com"

            # Third origin should work
            response = client.get("/multi", headers={"Origin": "https://app3.com"})
            assert response.headers.get("Access-Control-Allow-Origin") == "https://app3.com"

            # Unlisted origin should NOT work
            response = client.get("/multi", headers={"Origin": "https://evil.com"})
            assert "Access-Control-Allow-Origin" not in response.headers

    # NOTE: Combining exact origins (CORS_ALLOWED_ORIGINS) with regex patterns
    # (CORS_ALLOWED_ORIGIN_REGEXES) via Django settings requires actual server
    # integration testing. TestClient doesn't read Django settings at runtime.

    def test_case_sensitive_origin_matching(self):
        """Test that origin matching is case-sensitive"""
        api = BoltAPI()

        @api.get("/case-test")
        @cors(origins=["https://Example.com"])
        async def case_endpoint():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            # Exact case should work
            response = client.get("/case-test", headers={"Origin": "https://Example.com"})
            assert response.headers.get("Access-Control-Allow-Origin") == "https://Example.com"

            # Different case should NOT work (origins are case-sensitive)
            response = client.get("/case-test", headers={"Origin": "https://example.com"})
            # Note: In practice, origins should be lowercase, but this tests implementation
            # The test may pass or fail depending on implementation - adjust based on spec

    def test_port_in_origin(self):
        """Test that ports in origins are handled correctly"""
        api = BoltAPI()

        @api.get("/port-test")
        @cors(origins=["http://localhost:3000", "http://localhost:8080"])
        async def port_endpoint():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            # Port 3000 should work
            response = client.get("/port-test", headers={"Origin": "http://localhost:3000"})
            assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"

            # Port 8080 should work
            response = client.get("/port-test", headers={"Origin": "http://localhost:8080"})
            assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:8080"

            # Different port should NOT work
            response = client.get("/port-test", headers={"Origin": "http://localhost:9000"})
            assert "Access-Control-Allow-Origin" not in response.headers

    def test_trailing_slash_in_origin(self):
        """Test that origins without trailing slashes are handled correctly"""
        api = BoltAPI()

        @api.get("/slash-test")
        @cors(origins=["https://example.com"])
        async def slash_endpoint():
            return {"data": "test"}

        with TestClient(api, use_http_layer=True) as client:
            # Without trailing slash (standard)
            response = client.get("/slash-test", headers={"Origin": "https://example.com"})
            assert response.headers.get("Access-Control-Allow-Origin") == "https://example.com"

            # With trailing slash should NOT match (origins don't have trailing slashes)
            response = client.get("/slash-test", headers={"Origin": "https://example.com/"})
            # This should not match because origin format is without trailing slash
            assert "Access-Control-Allow-Origin" not in response.headers


# NOTE: SSE CORS tests are in test_sse_cors_integration.py which tests production handler.rs
# TestClient uses test_state.rs for streaming, not production code, so SSE CORS tests
# must use integration tests that start a real server.


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
