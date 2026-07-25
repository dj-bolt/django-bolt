"""
Integration tests for guards and authentication with TestClient.

Tests the full request flow including Rust-side authentication and guard evaluation.
"""

import time

import django
import jwt
import pytest
from django.conf import settings  # noqa: PLC0415
from django.core.management import call_command  # noqa: PLC0415

from django_bolt import BoltAPI
from django_bolt.auth import (
    AllowAny,
    APIKeyAuthentication,
    IsAuthenticated,
    JWTAuthentication,
    Requires,
)
from django_bolt.testing import TestClient


def create_token(user_id="user123", is_staff=False, is_superuser=False, permissions=None):
    """Helper to create JWT tokens"""
    payload = {
        "sub": user_id,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "is_staff": is_staff,
        "is_superuser": is_superuser,
        "permissions": permissions or [],
    }
    return jwt.encode(payload, "test-secret", algorithm="HS256")


@pytest.fixture(scope="module")
def api():
    """Create test API with guards and authentication"""
    # Setup Django
    if not settings.configured:
        settings.configure(
            DEBUG=True,
            SECRET_KEY="test-secret-key-for-guards",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django_bolt",
            ],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            USE_TZ=True,
        )
        django.setup()
        # Run migrations to create tables
        call_command("migrate", "--run-syncdb", verbosity=0)

    api = BoltAPI()

    # Public endpoint with AllowAny
    @api.get("/public", guards=[AllowAny()])
    async def public_endpoint():
        return {"message": "public", "auth": "not required"}

    # Protected endpoint requiring authentication
    @api.get("/protected", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
    async def protected_endpoint(request: dict):
        context = request.get("context", {})
        return {
            "message": "protected",
            "user_id": context.get("user_id"),
            "is_staff": context.get("is_staff", False),
            "is_superuser": context.get("is_superuser", False),
        }

    # Admin-only endpoint
    @api.get("/admin", auth=[JWTAuthentication(secret="test-secret")], guards=[Requires("is_superuser", True)])
    async def admin_endpoint(request: dict):
        context = request["context"]
        return {
            "message": "admin area",
            "user_id": context["user_id"],
            "is_superuser": context["is_superuser"],
        }

    # Staff-only endpoint
    @api.get("/staff", auth=[JWTAuthentication(secret="test-secret")], guards=[Requires("is_staff", True)])
    async def staff_endpoint(request: dict):
        return {
            "message": "staff area",
            "user_id": request["context"]["user_id"],
        }

    # Permission-required endpoint
    @api.get("/delete-users", auth=[JWTAuthentication(secret="test-secret")], guards=[Requires("permissions", "users.delete")])
    async def delete_users_endpoint():
        return {"message": "deleting users"}

    # Multiple permissions (any)
    @api.get(
        "/moderate",
        auth=[JWTAuthentication(secret="test-secret")],
        guards=[Requires("permissions", "users.moderate", "posts.moderate")],
    )
    async def moderate_endpoint():
        return {"message": "moderating content"}

    # API key authentication
    @api.get(
        "/api-endpoint",
        auth=[APIKeyAuthentication(api_keys={"valid-key-123", "valid-key-456"})],
        guards=[IsAuthenticated()],
    )
    async def api_key_endpoint(request: dict):
        return {
            "message": "API key valid",
            "user_id": request["context"].get("user_id"),
            "backend": request["context"].get("auth_backend"),
        }

    # Full context inspection endpoint
    @api.get("/context", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
    async def context_endpoint(request: dict):
        context = request.get("context", {})
        return {
            "context_keys": list(context.keys()) if hasattr(context, "keys") else [],
            "user_id": context.get("user_id"),
            "is_staff": context.get("is_staff"),
            "is_superuser": context.get("is_superuser"),
            "auth_backend": context.get("auth_backend"),
            "has_claims": "auth_claims" in context,
            "has_permissions": "permissions" in context,
        }

    return api


@pytest.fixture(scope="module")
def client(api):
    """Create TestClient for the API"""
    with TestClient(api) as client:
        yield client


def test_public_endpoint(client):
    """Test public endpoint (AllowAny)"""
    response = client.get("/public")
    assert response.status_code == 200
    assert response.json()["auth"] == "not required"


def test_protected_endpoint_without_token(client):
    """Test protected endpoint without token (should fail with 401)"""
    response = client.get("/protected")
    assert response.status_code == 401


def test_protected_endpoint_with_valid_token(client):
    """Test protected endpoint with valid token"""
    token = create_token(user_id="user123")
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "protected"
    assert data["user_id"] == "user123"


def test_admin_endpoint_with_non_admin_token(client):
    """Test admin endpoint with non-admin token (should fail with 403)"""
    token = create_token(user_id="regular-user", is_superuser=False)
    response = client.get("/admin", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_admin_endpoint_with_admin_token(client):
    """Test admin endpoint with admin token"""
    token = create_token(user_id="admin-user", is_superuser=True)
    response = client.get("/admin", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "admin area"
    assert data["is_superuser"] is True


def test_staff_endpoint_with_non_staff_token(client):
    """Test staff endpoint with non-staff token"""
    token = create_token(user_id="regular-user", is_staff=False)
    response = client.get("/staff", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_staff_endpoint_with_staff_token(client):
    """Test staff endpoint with staff token"""
    token = create_token(user_id="staff-user", is_staff=True)
    response = client.get("/staff", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "staff area"


def test_permission_based_endpoint_without_permission(client):
    """Test permission-based endpoint without required permission"""
    token = create_token(user_id="user-no-perms", permissions=[])
    response = client.get("/delete-users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_permission_based_endpoint_with_permission(client):
    """Test permission-based endpoint with required permission"""
    token = create_token(user_id="user-with-perms", permissions=["users.delete"])
    response = client.get("/delete-users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["message"] == "deleting users"


def test_has_any_permission_with_one_match(client):
    """Any-of permissions: one matching permission passes"""
    token = create_token(user_id="moderator", permissions=["users.moderate"])
    response = client.get("/moderate", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["message"] == "moderating content"


def test_has_any_permission_without_match(client):
    """Any-of permissions: no matching permission is 403"""
    token = create_token(user_id="user", permissions=["other.permission"])
    response = client.get("/moderate", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_api_key_authentication_without_key(client):
    """Test API key authentication without key"""
    response = client.get("/api-endpoint")
    assert response.status_code == 401


def test_api_key_authentication_with_valid_key(client):
    """Test API key authentication with valid key"""
    response = client.get("/api-endpoint", headers={"X-API-Key": "valid-key-123"})
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "API key valid"


def test_api_key_authentication_with_invalid_key(client):
    """Test API key authentication with invalid key"""
    response = client.get("/api-endpoint", headers={"X-API-Key": "invalid-key"})
    assert response.status_code == 401


def test_context_population(client):
    """Test that context is properly populated"""
    token = create_token(user_id="context-user", is_staff=True, permissions=["test.permission"])
    response = client.get("/context", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "context-user"
    assert data["is_staff"] is True
    assert "context_keys" in data


def test_invalid_jwt_signature(client):
    """Test invalid JWT signature"""
    token = jwt.encode(
        {"sub": "user123", "exp": int(time.time()) + 3600},
        "wrong-secret",  # Different secret
        algorithm="HS256",
    )
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_expired_jwt_token(client):
    """Test expired JWT token"""
    token = jwt.encode(
        {"sub": "user123", "exp": int(time.time()) - 3600},  # Expired
        "test-secret",
        algorithm="HS256",
    )
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
