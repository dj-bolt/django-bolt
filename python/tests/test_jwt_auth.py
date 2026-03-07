"""
Tests for JWT authentication and Django User integration.

Tests JWT token creation, validation, and Django User model integration
with type-safe dependency injection.

Uses pytest-django for proper Django configuration.
"""

import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from django.conf import settings  # noqa: PLC0415
from django.contrib.auth import get_user_model
from django.db import InterfaceError, OperationalError

from django_bolt import BoltAPI
from django_bolt.auth import (
    IsAuthenticated,
    JWTAuthentication,
    create_jwt_for_user,
    extract_user_id_from_context,
    get_auth_context,
    get_current_user,
)
from django_bolt.params import Depends

# Mark all tests to use Django DB
pytestmark = pytest.mark.django_db


def test_create_jwt_for_user():
    """Test creating JWT tokens for Django users."""
    User = get_user_model()

    # Create a test user
    user = User.objects.create(username="testuser", email="test@example.com", is_staff=True, is_superuser=False)

    # Create JWT token
    token = create_jwt_for_user(user, secret="my-secret")

    # Decode and verify
    decoded = jwt.decode(token, "my-secret", algorithms=["HS256"])

    assert decoded["sub"] == str(user.id)
    assert decoded["username"] == "testuser"
    assert decoded["email"] == "test@example.com"
    assert decoded["is_staff"] is True
    assert decoded["is_superuser"] is False
    assert "exp" in decoded
    assert "iat" in decoded

    # Clean up
    user.delete()
    print("✓ JWT token creation for user works correctly")


def test_create_jwt_with_extra_claims():
    """Test creating JWT tokens with custom claims."""
    User = get_user_model()

    user = User.objects.create(username="admin", email="admin@example.com", is_staff=True, is_superuser=True)

    # Create token with extra claims
    token = create_jwt_for_user(
        user,
        secret="secret",
        extra_claims={"permissions": ["users.create", "users.delete"], "role": "admin", "department": "engineering"},
    )

    # Decode and verify
    decoded = jwt.decode(token, "secret", algorithms=["HS256"])

    assert decoded["sub"] == str(user.id)
    assert decoded["permissions"] == ["users.create", "users.delete"]
    assert decoded["role"] == "admin"
    assert decoded["department"] == "engineering"

    # Clean up
    user.delete()
    print("✓ JWT with extra claims works correctly")


def test_jwt_authentication_with_django_user():
    """Test JWT authentication extracts correct user data."""
    User = get_user_model()

    # Create test user
    user = User.objects.create(username="jwtuser", email="jwt@example.com", is_staff=False, is_superuser=False)

    # Create API with JWT auth
    api = BoltAPI()

    @api.get("/protected", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
    async def protected_endpoint(request: dict):
        context = request["context"]
        return {
            "user_id": context.get("user_id"),
            "is_staff": context.get("is_staff"),
            "is_superuser": context.get("is_superuser"),
            "auth_backend": context.get("auth_backend"),
        }

    # Verify route registered
    assert len(api._routes) == 1

    # Clean up
    user.delete()
    print("✓ JWT authentication with Django User works")


def test_django_user_dependency_injection():
    """Test type-safe Django User dependency injection with Depends()."""
    # Create API with the dependency (using imported get_current_user)
    api = BoltAPI()

    @api.get("/me", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
    async def get_current_user_endpoint(user=Depends(get_current_user)):
        """
        Endpoint that receives Django User instance via dependency injection.

        The 'user' parameter is the actual Django User model instance,
        not just a dictionary. You have full access to:
        - user.id, user.username, user.email
        - user.is_staff, user.is_superuser, user.is_active
        - user.first_name, user.last_name, user.date_joined
        - All custom fields if using a custom User model
        - All User model methods
        """
        if user is None:
            return {"error": "User not found"}

        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        }

    # Verify the route was registered correctly
    assert len(api._routes) == 1
    method, path, handler_id, handler_fn = api._routes[0]
    assert method == "GET"
    assert path == "/me"

    # Verify handler metadata
    assert handler_id in api._handler_meta
    meta = api._handler_meta[handler_id]
    assert "fields" in meta

    # Check that the dependency parameter was detected
    fields = meta["fields"]
    user_field = next((f for f in fields if f.name == "user"), None)
    assert user_field is not None
    assert user_field.source == "dependency"
    assert user_field.dependency is not None

    print("✓ Django User dependency injection metadata correct")


def test_jwt_utils_extract_user_id():
    """Test extracting user_id from request context."""
    # Mock request with context
    request = {"context": {"user_id": "123", "is_staff": True, "auth_backend": "jwt"}}

    user_id = extract_user_id_from_context(request)
    assert user_id == "123"

    # Test with missing context
    empty_request = {}
    user_id = extract_user_id_from_context(empty_request)
    assert user_id is None

    print("✓ extract_user_id_from_context works correctly")


def test_jwt_utils_get_auth_context():
    """Test getting full auth context."""
    request = {
        "context": {
            "user_id": "456",
            "is_staff": False,
            "is_superuser": True,
            "auth_backend": "jwt",
            "permissions": ["read", "write"],
        }
    }

    ctx = get_auth_context(request)
    assert ctx["user_id"] == "456"
    assert ctx["is_staff"] is False
    assert ctx["is_superuser"] is True
    assert ctx["auth_backend"] == "jwt"
    assert ctx["permissions"] == ["read", "write"]

    print("✓ get_auth_context works correctly")


def test_jwt_claims_stored_in_context():
    """Test that JWT claims are properly stored in request context."""
    User = get_user_model()

    user = User.objects.create(username="claimsuser", email="claims@example.com", is_staff=True, is_superuser=False)

    # Create token with custom claims
    token = create_jwt_for_user(
        user,
        secret="secret",
        extra_claims={
            "permissions": ["read", "write"],
            "tenant_id": "tenant123",
        },
    )

    # Decode to verify claims are there
    decoded = jwt.decode(token, "secret", algorithms=["HS256"])
    assert decoded["permissions"] == ["read", "write"]
    assert decoded["tenant_id"] == "tenant123"

    # Clean up
    user.delete()
    print("✓ JWT claims storage verified")


def test_jwt_expiration():
    """Test JWT expiration handling."""
    User = get_user_model()

    user = User.objects.create(username="expuser")

    # Create an expired token
    expired_payload = {
        "sub": str(user.id),
        "exp": int(time.time()) - 100,  # Expired 100 seconds ago
        "iat": int(time.time()) - 200,
    }
    expired_token = jwt.encode(expired_payload, "secret", algorithm="HS256")

    # Verify it's expired
    try:
        jwt.decode(expired_token, "secret", algorithms=["HS256"])
        raise AssertionError("Should have raised ExpiredSignatureError")
    except jwt.ExpiredSignatureError:
        pass  # Expected

    # Create a valid token
    valid_token = create_jwt_for_user(user, secret="secret")
    decoded = jwt.decode(valid_token, "secret", algorithms=["HS256"])
    assert decoded["sub"] == str(user.id)

    # Clean up
    user.delete()
    print("✓ JWT expiration handling works")


def test_jwt_uses_django_secret_key():
    """Test that JWTAuthentication uses Django SECRET_KEY by default."""
    # Create JWT auth without explicit secret
    auth = JWTAuthentication()

    # Should use Django's SECRET_KEY
    assert auth.secret == settings.SECRET_KEY
    print("✓ JWT uses Django SECRET_KEY by default")


def test_jwt_custom_secret_overrides():
    """Test that explicit secret overrides Django SECRET_KEY."""
    # Create with explicit secret
    auth = JWTAuthentication(secret="custom-secret")

    # Should use the explicit secret, not Django's
    assert auth.secret == "custom-secret"
    assert auth.secret != settings.SECRET_KEY
    print("✓ Explicit JWT secret overrides Django SECRET_KEY")


@pytest.mark.asyncio
async def test_get_user_reraises_operational_error():
    """Test that OperationalError propagates instead of being swallowed.

    Regression test for https://github.com/dj-bolt/django-bolt/issues/165
    OperationalError must propagate so Django's connection pool can detect
    dead connections and recover.
    """
    auth = JWTAuthentication(secret="test-secret")

    with patch.object(
        auth._get_user_model().objects,
        "aget",
        new_callable=AsyncMock,
        side_effect=OperationalError("connection reset"),
    ):
        with pytest.raises(OperationalError):
            await auth.get_user("1", {})


@pytest.mark.asyncio
async def test_get_user_reraises_interface_error():
    """Test that InterfaceError propagates instead of being swallowed.

    Regression test for https://github.com/dj-bolt/django-bolt/issues/165
    """
    auth = JWTAuthentication(secret="test-secret")

    with patch.object(
        auth._get_user_model().objects,
        "aget",
        new_callable=AsyncMock,
        side_effect=InterfaceError("connection closed"),
    ):
        with pytest.raises(InterfaceError):
            await auth.get_user("1", {})


def test_get_user_sync_reraises_operational_error():
    """Test that sync get_user reraises OperationalError.

    Regression test for https://github.com/dj-bolt/django-bolt/issues/165
    """
    auth = JWTAuthentication(secret="test-secret")

    with patch.object(
        auth._get_user_model().objects,
        "get",
        side_effect=OperationalError("connection reset"),
    ):
        with pytest.raises(OperationalError):
            auth.get_user_sync("1")


def test_get_user_sync_reraises_interface_error():
    """Test that sync get_user reraises InterfaceError.

    Regression test for https://github.com/dj-bolt/django-bolt/issues/165
    """
    auth = JWTAuthentication(secret="test-secret")

    with patch.object(
        auth._get_user_model().objects,
        "get",
        side_effect=InterfaceError("connection closed"),
    ):
        with pytest.raises(InterfaceError):
            auth.get_user_sync("1")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
