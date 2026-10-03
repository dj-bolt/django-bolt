"""
Tests for session-based authentication and session data operations.

Tests Django session integration with Django-Bolt, including:
- Login/logout with Django's alogin/alogout
- Async session methods (aget, aset, apop, etc.)
- Session persistence across requests
"""

from __future__ import annotations

import pytest
from django.contrib.auth import alogin, alogout
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.middleware import SessionMiddleware
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request
from django_bolt.middleware import DjangoMiddleware
from django_bolt.testing import TestClient


def create_test_user():
    """Create a test user inside a test."""
    return User.objects.create_user(
        username="sessionuser",
        email="session@test.com",
        password="testpass123",
    )


@pytest.fixture
def session_api():
    """Create API with session middleware enabled."""
    api = BoltAPI(
        django_middleware=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ]
    )

    @api.post("/login")
    async def login(request: Request, username: str = "", password: str = ""):
        """Login endpoint using Django's alogin."""
        user = await User.objects.filter(username=username).afirst()
        if user and user.check_password(password):
            await alogin(request, user)
            return {"status": "ok", "username": user.username}
        return {"status": "error", "message": "Invalid credentials"}

    @api.post("/logout")
    async def logout(request: Request):
        """Logout endpoint using Django's alogout."""
        await alogout(request)
        return {"status": "ok"}

    @api.get("/me")
    async def me(request: Request):
        """Get current user info."""
        user = await request.auser()
        if user.is_authenticated:
            return {
                "authenticated": True,
                "username": user.username,
                "user_id": user.id,
            }
        return {"authenticated": False}

    @api.post("/session/set")
    async def session_set(request: Request, key: str, value: str):
        """Set a session value using async aset."""
        await request.session.aset(key, value)
        return {"status": "ok", "key": key, "value": value}

    @api.get("/session/get")
    async def session_get(request: Request, key: str, default: str = ""):
        """Get a session value using async aget."""
        value = await request.session.aget(key, default)
        return {"key": key, "value": value}

    @api.post("/session/pop")
    async def session_pop(request: Request, key: str):
        """Pop a session value using async apop."""
        value = await request.session.apop(key, None)
        return {"key": key, "value": value}

    @api.get("/session/keys")
    async def session_keys(request: Request):
        """Get all session keys."""
        keys = await request.session.akeys()
        return {"keys": list(keys)}

    @api.post("/session/counter")
    async def session_counter(request: Request):
        """Increment a counter in session."""
        count = await request.session.aget("counter", 0)
        await request.session.aset("counter", count + 1)
        return {"counter": count + 1}

    @api.get("/session/info")
    async def session_info(request: Request):
        """Get session info."""
        return {
            "session_key": request.session.session_key,
        }

    return api


class TestSessionLogin:
    """Tests for session-based login/logout."""

    @pytest.mark.django_db(transaction=True)
    def test_login_success(self, session_api):
        """Test successful login sets session and authenticates user."""
        create_test_user()

        with TestClient(session_api) as client:
            # Login
            response = client.post(
                "/login",
                params={"username": "sessionuser", "password": "testpass123"},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            assert response.json()["username"] == "sessionuser"

            # Check we're authenticated
            response = client.get("/me")
            assert response.status_code == 200
            data = response.json()
            assert data["authenticated"] is True
            assert data["username"] == "sessionuser"

    @pytest.mark.django_db(transaction=True)
    def test_login_invalid_credentials(self, session_api):
        """Test login with invalid credentials fails."""
        create_test_user()

        with TestClient(session_api) as client:
            response = client.post(
                "/login",
                params={"username": "sessionuser", "password": "wrongpass"},
            )
            assert response.status_code == 200
            assert response.json()["status"] == "error"

            # Should not be authenticated
            response = client.get("/me")
            assert response.json()["authenticated"] is False

    @pytest.mark.django_db(transaction=True)
    def test_logout(self, session_api):
        """Test logout clears session authentication."""
        create_test_user()

        with TestClient(session_api) as client:
            # Login first
            client.post(
                "/login",
                params={"username": "sessionuser", "password": "testpass123"},
            )

            # Verify logged in
            response = client.get("/me")
            assert response.json()["authenticated"] is True

            # Logout
            response = client.post("/logout")
            assert response.status_code == 200

            # Verify logged out
            response = client.get("/me")
            assert response.json()["authenticated"] is False

    @pytest.mark.django_db(transaction=True)
    def test_unauthenticated_user(self, session_api):
        """Test unauthenticated request returns anonymous user."""
        with TestClient(session_api) as client:
            response = client.get("/me")
            assert response.status_code == 200
            assert response.json()["authenticated"] is False


class TestSessionData:
    """Tests for session data operations."""

    @pytest.mark.django_db(transaction=True)
    def test_session_set_and_get(self, session_api):
        """Test setting and getting session values."""
        with TestClient(session_api) as client:
            # Set a value
            response = client.post(
                "/session/set",
                params={"key": "theme", "value": "dark"},
            )
            assert response.status_code == 200
            assert response.json()["value"] == "dark"

            # Get the value back
            response = client.get("/session/get", params={"key": "theme"})
            assert response.status_code == 200
            assert response.json()["value"] == "dark"

    @pytest.mark.django_db(transaction=True)
    def test_session_get_default(self, session_api):
        """Test getting non-existent key returns default."""
        with TestClient(session_api) as client:
            response = client.get(
                "/session/get",
                params={"key": "nonexistent", "default": "fallback"},
            )
            assert response.status_code == 200
            assert response.json()["value"] == "fallback"

    @pytest.mark.django_db(transaction=True)
    def test_session_pop(self, session_api):
        """Test popping a session value removes it."""
        with TestClient(session_api) as client:
            # Set a value
            client.post("/session/set", params={"key": "temp", "value": "data"})

            # Pop it
            response = client.post("/session/pop", params={"key": "temp"})
            assert response.status_code == 200
            assert response.json()["value"] == "data"

            # Should be gone now
            response = client.get("/session/get", params={"key": "temp"})
            assert response.json()["value"] == ""

    @pytest.mark.django_db(transaction=True)
    def test_session_counter(self, session_api):
        """Test incrementing a counter in session."""
        with TestClient(session_api) as client:
            # Increment multiple times
            response = client.post("/session/counter")
            assert response.json()["counter"] == 1

            response = client.post("/session/counter")
            assert response.json()["counter"] == 2

            response = client.post("/session/counter")
            assert response.json()["counter"] == 3

    @pytest.mark.django_db(transaction=True)
    def test_session_persistence(self, session_api):
        """Test session data persists across requests."""
        with TestClient(session_api) as client:
            # Set multiple values
            client.post("/session/set", params={"key": "name", "value": "Alice"})
            client.post("/session/set", params={"key": "role", "value": "admin"})

            # Get them back in separate requests
            response = client.get("/session/get", params={"key": "name"})
            assert response.json()["value"] == "Alice"

            response = client.get("/session/get", params={"key": "role"})
            assert response.json()["value"] == "admin"

    @pytest.mark.django_db(transaction=True)
    def test_session_keys(self, session_api):
        """Test getting all session keys."""
        with TestClient(session_api) as client:
            # Set some values
            client.post("/session/set", params={"key": "a", "value": "1"})
            client.post("/session/set", params={"key": "b", "value": "2"})

            # Get keys
            response = client.get("/session/keys")
            keys = response.json()["keys"]
            assert "a" in keys
            assert "b" in keys


class _PassThroughMiddleware(MiddlewareMixin):
    def process_request(self, request):
        pass


class TestSessionWithAuth:
    """Tests for session data combined with authentication."""

    @pytest.mark.django_db(transaction=True)
    def test_auser_of_a_session_user_behind_chained_wrappers(self):
        """A wrapper after ``AuthenticationMiddleware`` must keep Django's async ``auser``.

        Each wrapper copies the Django request attributes again. The session
        user that the first copy took is then already the user of the request.
        """
        api = BoltAPI(
            middleware=[
                DjangoMiddleware(SessionMiddleware),
                DjangoMiddleware(AuthenticationMiddleware),
                DjangoMiddleware(_PassThroughMiddleware),
            ]
        )

        @api.post("/login")
        async def login(request: Request, username: str = "", password: str = ""):
            user = await User.objects.aget(username=username)
            await alogin(request, user)
            return {"status": "ok"}

        @api.get("/me")
        async def me(request: Request):
            user = await request.auser()
            return {"authenticated": user.is_authenticated, "username": user.username}

        create_test_user()
        with TestClient(api) as client:
            response = client.post("/login", params={"username": "sessionuser", "password": "testpass123"})
            assert response.status_code == 200, response.text
            response = client.get("/me")

        assert response.status_code == 200, response.text
        assert response.json() == {"authenticated": True, "username": "sessionuser"}

    @pytest.mark.django_db(transaction=True)
    def test_session_data_with_login(self, session_api):
        """Test session data persists through login."""
        create_test_user()

        with TestClient(session_api) as client:
            # Set session data before login
            client.post("/session/set", params={"key": "prefs", "value": "saved"})

            # Login
            client.post(
                "/login",
                params={"username": "sessionuser", "password": "testpass123"},
            )

            # Session data should still be there
            response = client.get("/session/get", params={"key": "prefs"})
            assert response.json()["value"] == "saved"

            # And we should be authenticated
            response = client.get("/me")
            assert response.json()["authenticated"] is True

    @pytest.mark.django_db(transaction=True)
    def test_session_data_after_logout(self, session_api):
        """Test session data behavior after logout."""
        create_test_user()

        with TestClient(session_api) as client:
            # Login and set data
            client.post(
                "/login",
                params={"username": "sessionuser", "password": "testpass123"},
            )
            client.post("/session/set", params={"key": "user_data", "value": "test"})

            # Logout (this flushes the session in Django)
            client.post("/logout")

            # Session data should be cleared after logout
            response = client.get("/session/get", params={"key": "user_data"})
            # After logout, session is flushed, so data is gone
            assert response.json()["value"] == ""


@pytest.fixture
def decorator_api():
    """Create API with Django decorators."""
    api = BoltAPI(
        django_middleware=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ]
    )

    @api.get("/public")
    async def public_view(request: Request):
        """Public endpoint - no decorator."""
        return {"status": "public"}

    @api.get("/protected")
    @login_required
    async def protected_view(request: Request):
        """Protected by @login_required."""
        user = await request.auser()
        return {"status": "protected", "username": user.username}

    @api.get("/admin-only")
    @permission_required("auth.view_user", raise_exception=True)
    async def admin_view(request: Request):
        """Protected by @permission_required."""
        return {"status": "admin"}

    @api.post("/login")
    async def login_view(request: Request, username: str = "", password: str = ""):
        """Login endpoint."""
        user = await User.objects.filter(username=username).afirst()
        if user and user.check_password(password):
            await alogin(request, user)
            return {"status": "ok"}
        return {"status": "error"}

    return api


class TestDjangoDecorators:
    """Tests for Django's login_required and permission decorators."""

    @pytest.mark.django_db(transaction=True)
    def test_login_required_redirects_anonymous(self, decorator_api):
        """Test @login_required redirects unauthenticated users."""
        with TestClient(decorator_api) as client:
            response = client.get("/protected", follow_redirects=False)
            # Django's login_required returns 302 redirect to login page
            assert response.status_code == 302
            assert "/accounts/login/" in response.headers.get("location", "")

    @pytest.mark.django_db(transaction=True)
    def test_login_required_allows_authenticated(self, decorator_api):
        """Test @login_required allows authenticated users."""
        create_test_user()

        with TestClient(decorator_api) as client:
            # Login first
            client.post("/login", params={"username": "sessionuser", "password": "testpass123"})

            # Now access protected view
            response = client.get("/protected")
            assert response.status_code == 200
            assert response.json()["status"] == "protected"
            assert response.json()["username"] == "sessionuser"

    @pytest.mark.django_db(transaction=True)
    def test_permission_required_denies_without_permission(self, decorator_api):
        """Test @permission_required denies users without permission."""
        create_test_user()

        with TestClient(decorator_api) as client:
            # Login as regular user (no permissions)
            client.post("/login", params={"username": "sessionuser", "password": "testpass123"})

            # Try to access admin view
            response = client.get("/admin-only")
            # Should get 403 Forbidden
            assert response.status_code == 403

    @pytest.mark.django_db(transaction=True)
    def test_permission_required_allows_with_permission(self, decorator_api):
        """Test @permission_required allows users with permission."""
        # Create user with permission
        user = create_test_user()
        content_type = ContentType.objects.get_for_model(User)
        permission = Permission.objects.get(
            codename="view_user",
            content_type=content_type,
        )
        user.user_permissions.add(permission)

        with TestClient(decorator_api) as client:
            # Login as user with permission
            client.post("/login", params={"username": "sessionuser", "password": "testpass123"})

            # Access admin view
            response = client.get("/admin-only")
            assert response.status_code == 200
            assert response.json()["status"] == "admin"

    @pytest.mark.django_db(transaction=True)
    def test_public_view_works(self, decorator_api):
        """Test public view works without authentication."""
        with TestClient(decorator_api) as client:
            response = client.get("/public")
            assert response.status_code == 200
            assert response.json()["status"] == "public"
