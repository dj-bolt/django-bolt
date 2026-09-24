"""
Tests for request.user eager-loading functionality using HTTP client testing.

Tests the eager-loading, caching, and extensibility of request.user
for different authentication backends via actual HTTP requests.
"""

from __future__ import annotations

import asyncio
import time

import jwt
import pytest
from django.contrib.auth.models import User

from django_bolt import BoltAPI
from django_bolt.auth import (
    APIKeyAuthentication,
    IsAuthenticated,
    JWTAuthentication,
)
from django_bolt.exceptions import HTTPException
from django_bolt.testing import TestClient


def create_jwt_token(user_id="testuser", **kwargs):
    """Helper to create JWT tokens with user_id in sub claim."""
    payload = {
        "sub": user_id,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        **kwargs,
    }
    return jwt.encode(payload, "test-secret", algorithm="HS256")


@pytest.fixture(scope="module")
def jwt_api():
    """Create API with JWT authentication."""
    api = BoltAPI()

    @api.get("/me", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
    async def get_me(request):
        """Get current user from request.user."""
        user = request.user
        # If user didn't load from database, return 401 like unauthenticated request
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return {
            "user_loaded": True,
            "user_id": user.id,
            "username": user.username,
        }

    @api.get("/profile")
    async def get_profile(request):
        """Public endpoint - request.user should not be loaded."""
        user = request.user
        return {"is_authenticated": bool(user)}

    return api


@pytest.fixture(scope="module")
def api_key_api():
    """Create API with API key authentication."""
    api = BoltAPI()

    @api.get(
        "/api/data",
        auth=[APIKeyAuthentication(api_keys={"valid-key-123"})],
        guards=[IsAuthenticated()],
    )
    async def get_api_data(request):
        """API endpoint that requires API key."""
        user = request.user
        # APIKeyAuthentication doesn't provide user mapping by default
        return {
            "data": "sensitive",
            "user_loaded": bool(user),
        }

    return api


@pytest.fixture(scope="module")
def custom_auth_api():
    """Create API with custom auth backend that implements get_user."""

    class CustomAPIKeyAuth(APIKeyAuthentication):
        """Custom API key that maps keys to users."""

        async def get_user(self, user_id: str, auth_context: dict):
            """Map API key identifier to actual user."""
            if user_id == "apikey:admin-key":
                try:
                    return await User.objects.aget(username="admin")
                except User.DoesNotExist:
                    return None
            return None

    api = BoltAPI()

    @api.get(
        "/admin/data",
        auth=[CustomAPIKeyAuth(api_keys={"admin-key"})],
        guards=[IsAuthenticated()],
    )
    async def get_admin_data(request):
        """Admin endpoint with custom user mapping."""
        # Guard ensures user loaded, so safe to access
        return {
            "message": "admin access granted",
        }

    return api


class TestJWTUserLoading:
    """Test request.user loading with JWT authentication."""

    @pytest.mark.django_db(transaction=True)
    def test_auser_returns_jwt_user_without_django_middleware(self):
        """Both getters return the JWT user when Django middleware is absent.

        ``auser()`` loads the user one time. ``request.user`` shares that result.
        """
        api = BoltAPI(django_middleware=[])

        @api.get("/async-me", auth=[JWTAuthentication(secret="test-secret")], guards=[IsAuthenticated()])
        async def get_async_me(request):
            user = await request.auser()
            again = await request.auser()
            return {
                "username": user.username,
                "user_id": user.pk,
                "same_user": user == request.user and again is user,
            }

        user = User.objects.create(username="async_jwt_user")
        token = create_jwt_token(user_id=str(user.pk))
        with TestClient(api) as client:
            response = client.get("/async-me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200, response.text
        assert response.json() == {"username": user.username, "user_id": user.pk, "same_user": True}

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_auser_calls_share_one_query(self):
        """Two ``auser()`` calls of one request that run at the same time load the user one time."""
        queries = []

        class CountingJWT(JWTAuthentication):
            def get_user_sync(self, user_id):
                queries.append(user_id)
                return User.objects.get(pk=user_id)

        api = BoltAPI(django_middleware=[])

        @api.get("/me", auth=[CountingJWT(secret="test-secret")])
        async def me(request):
            first, second = await asyncio.gather(request.auser(), request.auser())
            return {"same": first is second, "shared": first is object.__getattribute__(request.user, "_wrapped")}

        user = User.objects.create(username="gather_user")
        token = create_jwt_token(user_id=str(user.pk))
        with TestClient(api) as client:
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200, response.text
        assert response.json() == {"same": True, "shared": True}
        assert len(queries) == 1

    @pytest.mark.django_db(transaction=True)
    def test_sync_read_during_auser_keeps_one_user(self):
        """A sync read while ``auser()`` waits loads the user first. ``auser()`` must keep that user.

        Otherwise ``request.user`` changes to a second object in the middle of
        the request, and a write to the first object is lost.
        """
        api = BoltAPI(django_middleware=[])

        @api.get("/me", auth=[JWTAuthentication(secret="test-secret")])
        async def me(request):
            pending = asyncio.ensure_future(request.auser())
            await asyncio.sleep(0)
            request.user.marker = "kept"
            first = object.__getattribute__(request.user, "_wrapped")
            loaded = await pending
            return {
                "auser_is_first": loaded is first,
                "user_is_first": object.__getattribute__(request.user, "_wrapped") is first,
                "marker": getattr(request.user, "marker", None),
            }

        user = User.objects.create(username="race_user")
        token = create_jwt_token(user_id=str(user.pk))
        with TestClient(api) as client:
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200, response.text
        assert response.json() == {"auser_is_first": True, "user_is_first": True, "marker": "kept"}

    @pytest.mark.django_db(transaction=True)
    def test_repr_of_the_lazy_user_hides_the_claims(self):
        """``repr(request.user)`` can reach a debug page or a log. It must not show the token claims.

        It must not load the user either.
        """
        api = BoltAPI(django_middleware=[])

        @api.get("/me", auth=[JWTAuthentication(secret="test-secret")])
        def me(request):
            before = repr(request.user)
            return {"before": before, "after": repr(request.user) if request.user.username else ""}

        user = User.objects.create(username="repr_user")
        token = create_jwt_token(user_id=str(user.pk), email="private@example.com", role="owner")
        with TestClient(api) as client:
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert "private@example.com" not in body["before"]
        assert "owner" not in body["before"]
        assert body["before"] == f"<LazyUser: user_id={str(user.pk)!r}, not loaded>"
        assert "repr_user" in body["after"]

    def test_auser_returns_anonymous_without_authentication(self):
        """The async getter still returns an anonymous user without authentication."""
        api = BoltAPI(django_middleware=[])

        @api.get("/async-public")
        async def get_async_public(request):
            user = await request.auser()
            return {"is_anonymous": user.is_anonymous, "is_authenticated": user.is_authenticated}

        with TestClient(api) as client:
            response = client.get("/async-public")

        assert response.status_code == 200, response.text
        assert response.json() == {"is_anonymous": True, "is_authenticated": False}

    @pytest.mark.django_db(transaction=True)  # Use real database transaction
    def test_authenticated_request_has_user(self, jwt_api):
        """Test that authenticated request with existing user returns 200."""
        # Create user in database with explicit commit
        user = User.objects.create(username="testuser")

        with TestClient(jwt_api) as client:
            # Use actual user ID from database
            token = create_jwt_token(user_id=str(user.id))
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

            print(f"DEBUG: status_code={response.status_code}, body={response.text}")
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
            data = response.json()
            assert data["user_loaded"] is True
            assert data["user_id"] == user.id
            assert data["username"] == "testuser"

    @pytest.mark.django_db
    def test_valid_token_for_nonexistent_user_returns_401(self, jwt_api):
        """Test that valid token for non-existent user returns 401."""
        with TestClient(jwt_api) as client:
            # Token is valid but user_id doesn't exist in DB
            token = create_jwt_token(user_id="999999")
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

            # Should return 401 because user not found (not 500 error)
            assert response.status_code == 401, f"Expected 401, got {response.status_code}: {response.text}"

    def test_unauthenticated_request_rejected(self, jwt_api):
        """Test that unauthenticated request is rejected by guard."""
        with TestClient(jwt_api) as client:
            response = client.get("/me")
            assert response.status_code == 401

    def test_invalid_token_rejected(self, jwt_api):
        """Test that invalid token is rejected."""
        with TestClient(jwt_api) as client:
            # Invalid token (wrong secret)
            token = jwt.encode(
                {
                    "sub": "999",
                    "exp": int(time.time()) + 3600,
                },
                "wrong-secret",
                algorithm="HS256",
            )
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401

    def test_expired_token_rejected(self, jwt_api):
        """Test that expired token is rejected."""
        with TestClient(jwt_api) as client:
            token = jwt.encode(
                {
                    "sub": "999",
                    "exp": int(time.time()) - 3600,  # Expired
                },
                "test-secret",
                algorithm="HS256",
            )
            response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401

    def test_public_endpoint_no_user(self, jwt_api):
        """Test that public endpoint without auth has no user."""
        with TestClient(jwt_api) as client:
            response = client.get("/profile")
            assert response.status_code == 200
            data = response.json()
            assert data["is_authenticated"] is False

    def test_invalid_token_syntax_rejected(self, jwt_api):
        """Test that malformed JWT is rejected."""
        with TestClient(jwt_api) as client:
            response = client.get("/me", headers={"Authorization": "Bearer invalid.token.here"})
            # Should be 401 for invalid token
            assert response.status_code == 401


class TestAPIKeyUserLoading:
    """Test request.user loading with API key authentication."""

    def test_valid_api_key_no_user_mapping(self, api_key_api):
        """Test that valid API key works but user is not loaded (no custom mapping)."""
        with TestClient(api_key_api) as client:
            response = client.get("/api/data", headers={"X-API-Key": "valid-key-123"})

            # Should succeed because key is valid
            assert response.status_code == 200
            data = response.json()
            # But user should not be loaded (APIKeyAuthentication doesn't provide get_user by default)
            assert data["user_loaded"] is False

    def test_invalid_api_key_rejected(self, api_key_api):
        """Test that invalid API key is rejected."""
        with TestClient(api_key_api) as client:
            response = client.get("/api/data", headers={"X-API-Key": "invalid-key"})
            assert response.status_code == 401

    def test_missing_api_key_rejected(self, api_key_api):
        """Test that missing API key is rejected."""
        with TestClient(api_key_api) as client:
            response = client.get("/api/data")
            assert response.status_code == 401


class TestCustomAuthBackendUserLoading:
    """Test request.user with custom auth backend that provides get_user."""

    @pytest.mark.django_db(transaction=True)
    def test_custom_backend_maps_key_to_user(self, custom_auth_api):
        """Test that custom backend properly maps API key to user."""
        # Create admin user with explicit commit
        User.objects.create(username="admin")

        with TestClient(custom_auth_api) as client:
            response = client.get("/admin/data", headers={"X-API-Key": "admin-key"})

            assert response.status_code == 200
            data = response.json()
            assert data["message"] == "admin access granted"

    @pytest.mark.django_db
    def test_custom_backend_returns_none_for_unknown_key(self, custom_auth_api):
        """Test that custom backend returns None for unknown key."""
        with TestClient(custom_auth_api) as client:
            response = client.get("/admin/data", headers={"X-API-Key": "unknown-key"})

            # Should still be rejected (key not in valid set)
            assert response.status_code == 401


class TestRequestUserSyncHandlers:
    """Test request.user in synchronous handlers."""

    @pytest.mark.django_db(transaction=True)
    def test_sync_handler_with_jwt(self):
        """Test that request.user works in sync handlers."""
        # Create test user
        user = User.objects.create(username="syncuser")

        api = BoltAPI()

        @api.get(
            "/sync-me",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
        def sync_get_me(request):
            """Synchronous handler."""
            user = request.user
            return {
                "user_loaded": user is not None,
            }

        with TestClient(api) as client:
            token = create_jwt_token(user_id=str(user.id))
            response = client.get("/sync-me", headers={"Authorization": f"Bearer {token}"})

            assert response.status_code == 200
            assert response.json()["user_loaded"] is True

    def test_sync_handler_unauthenticated(self):
        """Test that sync handler rejects unauthenticated requests."""
        api = BoltAPI()

        @api.get(
            "/sync-protected",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
        def sync_protected(request):
            return {"data": "protected"}

        with TestClient(api) as client:
            response = client.get("/sync-protected")
            assert response.status_code == 401


class TestSyncHandlerWithCustomBackend:
    """Test request.user in sync handlers with custom backends."""

    @pytest.mark.django_db(transaction=True)
    def test_sync_handler_with_custom_backend_should_load_user(self):
        """A sync handler reads request.user through the backend's get_user_sync."""

        class CustomSyncKeyAuth(APIKeyAuthentication):
            """Custom API key that maps keys to users (sync get_user_sync)."""

            def get_user_sync(self, user_id: str):
                """Map API key identifier to actual user."""
                if user_id == "apikey:test-key":
                    try:
                        return User.objects.get(username="testuser")
                    except User.DoesNotExist:
                        return None
                return None

        api = BoltAPI()

        @api.get(
            "/sync-custom",
            auth=[CustomSyncKeyAuth(api_keys={"test-key"})],
            guards=[IsAuthenticated()],
        )
        def sync_custom_handler(request):
            """Sync handler with custom backend."""
            user = request.user
            return {
                "user_loaded": user is not None,
                "username": user.username if user else None,
            }

        # Create test user
        User.objects.create(username="testuser")

        with TestClient(api) as client:
            response = client.get("/sync-custom", headers={"X-API-Key": "test-key"})

            assert response.status_code == 200
            data = response.json()
            assert data["user_loaded"] is True, "User should be loaded from custom backend in sync handler"
            assert data["username"] == "testuser"

    @pytest.mark.django_db(transaction=True)
    def test_async_handler_with_custom_backend_works(self):
        """Test that async handler with custom backend works."""

        class CustomAsyncKeyAuth(APIKeyAuthentication):
            """Custom API key that maps keys to users (async get_user)."""

            async def get_user(self, user_id: str, auth_context: dict):
                """Map API key identifier to actual user."""
                if user_id == "apikey:async-key":
                    try:
                        return await User.objects.aget(username="asyncuser")
                    except User.DoesNotExist:
                        return None
                return None

        api = BoltAPI()

        @api.get(
            "/async-custom",
            auth=[CustomAsyncKeyAuth(api_keys={"async-key"})],
            guards=[IsAuthenticated()],
        )
        async def async_custom_handler(request):
            """Async handler with custom backend."""
            user = await request.auser()
            return {
                "user_loaded": user is not None,
                "username": user.username if user else None,
            }

        @api.get(
            "/async-custom-sync-read",
            auth=[CustomAsyncKeyAuth(api_keys={"async-key"})],
            guards=[IsAuthenticated()],
        )
        async def async_custom_sync_read(request):
            """Sync access to request.user cannot run an async-only get_user."""
            try:
                return {"username": request.user.username}
            except RuntimeError as exc:
                return {"error": str(exc)}

        # Create test user
        User.objects.create(username="asyncuser")

        with TestClient(api) as client:
            response = client.get("/async-custom", headers={"X-API-Key": "async-key"})

            assert response.status_code == 200, f"Response: {response.text}"
            data = response.json()
            assert data["user_loaded"] is True
            assert data["username"] == "asyncuser"

            response = client.get("/async-custom-sync-read", headers={"X-API-Key": "async-key"})
            assert response.status_code == 200, f"Response: {response.text}"
            assert "await request.auser()" in response.json()["error"]


class TestRequestUserGuardBehavior:
    """Test request.user interaction with guards."""

    def test_guard_rejects_before_handler_runs(self):
        """Test that guards reject request before handler executes."""
        api = BoltAPI()

        handler_called = {"called": False}

        @api.get(
            "/guarded",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
        async def guarded_handler(request):
            handler_called["called"] = True
            return {"status": "ok"}

        with TestClient(api) as client:
            # Request without auth should be rejected
            response = client.get("/guarded")
            assert response.status_code == 401
            # Handler should not have been called
            assert handler_called["called"] is False

    @pytest.mark.django_db(transaction=True)
    def test_successful_auth_allows_handler(self):
        """Test that successful auth allows handler to execute."""
        # Create test user
        user = User.objects.create(username="user123")

        api = BoltAPI()

        handler_called = {"called": False}

        @api.get(
            "/allowed",
            auth=[JWTAuthentication(secret="test-secret")],
            guards=[IsAuthenticated()],
        )
        async def allowed_handler(request):
            handler_called["called"] = True
            user = request.user
            return {"status": "ok", "user_loaded": user is not None}

        with TestClient(api) as client:
            token = create_jwt_token(user_id=str(user.id))
            response = client.get("/allowed", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert handler_called["called"] is True
            assert response.json()["user_loaded"] is True
