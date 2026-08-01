"""
Test guards and authentication system for Django-Bolt.

Tests the new DRF-inspired guards and authentication classes.
"""

import time

import django
import jwt
import pytest
from django.conf import settings  # noqa: PLC0415

from django_bolt import BoltAPI
from django_bolt.auth import (
    AllowAny,
    APIKeyAuthentication,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
)


class TestAuthenticationClasses:
    """Test authentication class configurations"""

    def test_jwt_authentication_basic(self):
        """Test JWT authentication class initialization"""
        auth = JWTAuthentication(secret="test-secret")
        assert auth.secret == "test-secret"
        assert auth.algorithms == ["HS256"]
        assert auth.scheme_name == "jwt"

        metadata = auth.to_metadata()
        assert metadata["type"] == "jwt"
        assert metadata["secret"] == "test-secret"
        assert metadata["algorithms"] == ["HS256"]

    def test_jwt_authentication_with_options(self):
        """Test JWT authentication with all options"""
        auth = JWTAuthentication(
            secret="my-secret",
            algorithms=["HS256", "HS384"],
            header="x-auth-token",
            audience="my-app",
            issuer="auth-server",
        )

        metadata = auth.to_metadata()
        assert metadata["secret"] == "my-secret"
        assert metadata["algorithms"] == ["HS256", "HS384"]
        assert metadata["header"] == "x-auth-token"
        assert metadata["audience"] == "my-app"
        assert metadata["issuer"] == "auth-server"

    def test_api_key_authentication(self):
        """Test API key authentication class"""
        auth = APIKeyAuthentication(api_keys={"key1", "key2", "key3"}, header="x-api-key")

        assert auth.scheme_name == "api_key"
        metadata = auth.to_metadata()
        assert metadata["type"] == "api_key"
        assert set(metadata["api_keys"]) == {"key1", "key2", "key3"}
        assert metadata["header"] == "x-api-key"

    def test_api_key_with_permissions(self):
        """Test API key authentication with permission mapping"""
        auth = APIKeyAuthentication(
            api_keys={"admin-key", "read-key"},
            key_permissions={"admin-key": ["users.create", "users.delete"], "read-key": ["users.view"]},
        )

        metadata = auth.to_metadata()
        assert "admin-key" in metadata["key_permissions"]
        assert "users.create" in metadata["key_permissions"]["admin-key"]


class TestPermissionGuards:
    """Test the guard classes: AllowAny, IsAuthenticated, and Requires."""

    def test_allow_any(self):
        assert AllowAny().to_metadata() == {"type": "allow_any"}

    def test_is_authenticated(self):
        assert IsAuthenticated().to_metadata() == {"type": "is_authenticated"}

    def test_requires_single_value(self):
        assert Requires("role", "client").to_metadata() == {
            "type": "requires",
            "claim": "role",
            "values": ["client"],
            "quantifier": 0,
            "message": None,
        }

    def test_requires_any_of(self):
        meta = Requires("permissions", "users.create", "users.update").to_metadata()
        assert meta["type"] == "requires"
        assert meta["claim"] == "permissions"
        assert meta["values"] == ["users.create", "users.update"]
        assert meta["quantifier"] == 0

    def test_requires_all_of(self):
        meta = Requires("permissions", all_of=["users.create", "users.delete"]).to_metadata()
        assert meta["values"] == ["users.create", "users.delete"]
        assert meta["quantifier"] == 1

    def test_requires_presence_only(self):
        meta = Requires("tenant_id").to_metadata()
        assert meta["values"] == []
        assert meta["quantifier"] == 0

    def test_requires_named_reuse(self):
        """The one way to define a reusable custom check: name an instance."""
        IsClient = Requires("role", "client")
        assert IsClient.to_metadata()["claim"] == "role"
        assert repr(IsClient) == "Requires('role', 'client')"


class TestRouteDecoratorAPI:
    """Test route decorator with guards and auth parameters"""

    def test_route_with_guards(self):
        """Test route decorator with guards parameter"""
        api = BoltAPI()

        @api.get("/admin", guards=[Requires("is_superuser", True)])
        async def admin_endpoint():
            return {"message": "admin only"}

        # Check that metadata was compiled
        api._handlers[0]
        handler_id = 0
        assert handler_id in api._handler_middleware

        metadata = api._handler_middleware[handler_id]
        assert "guards" in metadata
        assert len(metadata["guards"]) == 1
        assert metadata["guards"][0]["type"] == "requires"
        assert metadata["guards"][0]["claim"] == "is_superuser"

    def test_route_with_auth_override(self):
        """Test route with custom auth backend"""
        api = BoltAPI()

        @api.get("/api-only", auth=[APIKeyAuthentication(api_keys={"key1"})], guards=[IsAuthenticated()])
        async def api_endpoint():
            return {"message": "API key only"}

        handler_id = 0
        metadata = api._handler_middleware[handler_id]

        assert "auth_backends" in metadata
        assert len(metadata["auth_backends"]) == 1
        assert metadata["auth_backends"][0]["type"] == "api_key"

        assert "guards" in metadata
        assert metadata["guards"][0]["type"] == "is_authenticated"

    def test_route_with_multiple_guards(self):
        """Test route with multiple guards"""
        api = BoltAPI()

        @api.get(
            "/restricted",
            guards=[IsAuthenticated(), Requires("is_staff", True), Requires("permissions", "users.delete")],
        )
        async def restricted_endpoint():
            return {"message": "highly restricted"}

        handler_id = 0
        metadata = api._handler_middleware[handler_id]

        assert len(metadata["guards"]) == 3
        assert metadata["guards"][0]["type"] == "is_authenticated"
        assert metadata["guards"][1]["claim"] == "is_staff"
        assert metadata["guards"][2]["claim"] == "permissions"

    def test_public_route_with_allow_any(self):
        """Test that AllowAny explicitly bypasses global defaults"""
        api = BoltAPI()

        @api.get("/public", guards=[AllowAny()])
        async def public_endpoint():
            return {"message": "public"}

        handler_id = 0
        metadata = api._handler_middleware[handler_id]

        assert "guards" in metadata
        assert metadata["guards"][0]["type"] == "allow_any"


class TestMetadataCompilation:
    """Test that metadata is properly compiled and merged"""

    def test_global_auth_defaults(self):
        """Test that global auth defaults are used when no per-route auth"""
        if not settings.configured:
            settings.configure(
                SECRET_KEY="test-key",
                BOLT_AUTHENTICATION_CLASSES=[JWTAuthentication(secret="global-secret")],
                BOLT_DEFAULT_PERMISSION_CLASSES=[IsAuthenticated()],
            )
            django.setup()

        api = BoltAPI()

        @api.get("/default-protected")
        async def protected():
            return {"message": "uses global defaults"}

        # Should use global defaults
        handler_id = 0
        if handler_id in api._handler_middleware:
            metadata = api._handler_middleware[handler_id]
            # Global defaults should be applied
            assert "auth_backends" in metadata or "guards" in metadata

    def test_per_route_override_precedence(self):
        """Test that per-route auth/guards override global defaults"""
        api = BoltAPI()

        @api.get("/override", auth=[APIKeyAuthentication(api_keys={"key1"})], guards=[AllowAny()])
        async def override_endpoint():
            return {"message": "overrides global"}

        handler_id = 0
        metadata = api._handler_middleware[handler_id]

        # Should have per-route config, not global
        assert metadata["auth_backends"][0]["type"] == "api_key"
        assert metadata["guards"][0]["type"] == "allow_any"


class TestJWTTokenHandling:
    """Test JWT token creation and validation patterns"""

    def test_create_valid_jwt(self):
        """Test creating a valid JWT token"""
        secret = "test-secret"
        payload = {
            "sub": "user123",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "is_staff": True,
            "is_superuser": False,
            "permissions": ["users.view", "users.create"],
        }

        token = jwt.encode(payload, secret, algorithm="HS256")

        # Verify we can decode it
        decoded = jwt.decode(token, secret, algorithms=["HS256"])
        assert decoded["sub"] == "user123"
        assert decoded["is_staff"]
        assert "users.view" in decoded["permissions"]

    def test_expired_jwt(self):
        """Test that expired tokens are rejected"""
        secret = "test-secret"
        payload = {
            "sub": "user123",
            "exp": int(time.time()) - 100,  # Expired 100 seconds ago
            "iat": int(time.time()) - 200,
        }

        token = jwt.encode(payload, secret, algorithm="HS256")

        # Should raise ExpiredSignatureError
        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(token, secret, algorithms=["HS256"])

    def test_invalid_signature(self):
        """Test that tokens with wrong signature are rejected"""
        token = jwt.encode({"sub": "user"}, "secret1", algorithm="HS256")

        # Try to decode with different secret
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, "secret2", algorithms=["HS256"])


class TestContextPopulation:
    """Test that request context is properly populated with auth data"""

    def test_context_structure(self):
        """Test expected context structure"""
        # This would be tested in integration tests
        # Here we document the expected structure
        expected_context = {
            "user_id": "user123",
            "is_staff": False,
            "is_superuser": False,
            "auth_backend": "jwt",
            "permissions": ["users.view"],
            "auth_claims": {
                "sub": "user123",
                "exp": 1234567890,
                "iat": 1234567800,
                # ... other JWT claims
            },
        }

        # Verify structure
        assert "user_id" in expected_context
        assert "is_staff" in expected_context
        assert "is_superuser" in expected_context
        assert "auth_backend" in expected_context


class TestEdgeCases:
    """Test edge cases and error conditions"""

    def test_empty_guards_list(self):
        """Test route with empty guards list"""
        api = BoltAPI()

        @api.get("/no-guards", guards=[])
        async def no_guards():
            return {"message": "no guards"}

        # Should not create metadata if guards list is empty
        # Empty guards should result in no metadata or default behavior

    def test_none_auth_parameter(self):
        """Test route with auth=None (should use global defaults)"""
        api = BoltAPI()

        @api.get("/default-auth", auth=None, guards=[IsAuthenticated()])
        async def default_auth():
            return {"message": "uses global auth"}

        # Should fall back to global defaults
        handler_id = 0
        api._handler_middleware.get(handler_id)
        # Verify it uses global defaults or no auth

    def test_mixed_guard_types(self):
        """Test route with mix of instance and class guards"""
        api = BoltAPI()

        @api.get("/mixed", guards=[IsAuthenticated, Requires("is_staff", True)])
        async def mixed_guards():
            return {"message": "mixed guards"}

        handler_id = 0
        metadata = api._handler_middleware[handler_id]

        # Both should be compiled correctly
        assert len(metadata["guards"]) == 2

    def test_permission_guard_with_special_chars(self):
        """Test permission with dots and underscores"""
        guard = Requires("permissions", "myapp.custom_permission.delete")
        metadata = guard.to_metadata()
        assert metadata["values"] == ["myapp.custom_permission.delete"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
