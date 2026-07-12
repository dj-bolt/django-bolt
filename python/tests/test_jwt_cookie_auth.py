"""
Tests for cookie-based JWT extraction, validated entirely in Rust.

JWTAuthentication(cookie="access_token") makes Rust extract the token from
the named cookie only (no header fallback) and validate the signature/exp/nbf
without touching Python.
"""

from __future__ import annotations

import time

import jwt
import pytest
from django.contrib.auth.models import User

from django_bolt import BoltAPI
from django_bolt.auth import InMemoryRevocation, IsAuthenticated, JWTAuthentication
from django_bolt.auth.user_loader import default_django_user_loader
from django_bolt.testing import TestClient

SECRET = "test-secret"


def create_token(user_id="user123", **extra):
    payload = {
        "sub": user_id,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        **extra,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@pytest.fixture(scope="module")
def cookie_api():
    """API using cookie-based JWT extraction."""
    api = BoltAPI()

    @api.get(
        "/whoami",
        auth=[JWTAuthentication(secret=SECRET, cookie="access_token")],
        guards=[IsAuthenticated()],
    )
    async def whoami(request):
        return {"user_id": request["context"]["user_id"]}

    return api


class TestJWTCookieAuth:
    """Cookie-based JWT extraction, validated in Rust."""

    def test_valid_token_in_cookie(self, cookie_api):
        token = create_token(user_id="cookie-user")
        with TestClient(cookie_api) as client:
            response = client.get("/whoami", headers={"Cookie": f"access_token={token}"})
            assert response.status_code == 200
            assert response.json()["user_id"] == "cookie-user"

    def test_valid_token_in_cookie_among_others(self, cookie_api):
        token = create_token(user_id="cookie-user")
        with TestClient(cookie_api) as client:
            response = client.get(
                "/whoami",
                headers={"Cookie": f"csrftoken=abc; access_token={token}; theme=dark"},
            )
            assert response.status_code == 200
            assert response.json()["user_id"] == "cookie-user"

    def test_missing_token_rejected(self, cookie_api):
        with TestClient(cookie_api) as client:
            response = client.get("/whoami")
            assert response.status_code == 401

    def test_wrong_cookie_name_rejected(self, cookie_api):
        token = create_token()
        with TestClient(cookie_api) as client:
            response = client.get("/whoami", headers={"Cookie": f"session={token}"})
            assert response.status_code == 401

    def test_invalid_token_in_cookie_rejected(self, cookie_api):
        bad = jwt.encode({"sub": "x", "exp": int(time.time()) + 3600}, "wrong-secret", algorithm="HS256")
        with TestClient(cookie_api) as client:
            response = client.get("/whoami", headers={"Cookie": f"access_token={bad}"})
            assert response.status_code == 401

    def test_expired_token_in_cookie_rejected(self, cookie_api):
        expired = jwt.encode({"sub": "x", "exp": int(time.time()) - 3600}, SECRET, algorithm="HS256")
        with TestClient(cookie_api) as client:
            response = client.get("/whoami", headers={"Cookie": f"access_token={expired}"})
            assert response.status_code == 401

    def test_authorization_header_ignored_in_cookie_mode(self, cookie_api):
        """With cookie mode on, the Authorization header is NOT a fallback."""
        token = create_token(user_id="header-user")
        with TestClient(cookie_api) as client:
            response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401

    def test_cookie_is_sole_source(self, cookie_api):
        cookie_token = create_token(user_id="from-cookie")
        header_token = create_token(user_id="from-header")
        with TestClient(cookie_api) as client:
            response = client.get(
                "/whoami",
                headers={
                    "Cookie": f"access_token={cookie_token}",
                    "Authorization": f"Bearer {header_token}",
                },
            )
            assert response.status_code == 200
            assert response.json()["user_id"] == "from-cookie"

    def test_dual_backends_serve_cookie_and_header(self):
        """Cookie backend + header backend on one route: each source works."""
        api = BoltAPI()

        @api.get(
            "/dual",
            auth=[
                JWTAuthentication(secret=SECRET, cookie="access_token"),
                JWTAuthentication(secret=SECRET),
            ],
            guards=[IsAuthenticated()],
        )
        async def dual(request):
            return {"user_id": request["context"]["user_id"]}

        with TestClient(api) as client:
            cookie_token = create_token(user_id="via-cookie")
            response = client.get("/dual", headers={"Cookie": f"access_token={cookie_token}"})
            assert response.status_code == 200
            assert response.json()["user_id"] == "via-cookie"

            header_token = create_token(user_id="via-header")
            response = client.get("/dual", headers={"Authorization": f"Bearer {header_token}"})
            assert response.status_code == 200
            assert response.json()["user_id"] == "via-header"

            response = client.get("/dual")
            assert response.status_code == 401

    def test_cookie_ignored_without_cookie_kwarg(self):
        """Default backend (no cookie=) must NOT read tokens from cookies."""
        api = BoltAPI()

        @api.get(
            "/header-only",
            auth=[JWTAuthentication(secret=SECRET)],
            guards=[IsAuthenticated()],
        )
        async def header_only(request):
            return {"ok": True}

        token = create_token()
        with TestClient(api) as client:
            response = client.get("/header-only", headers={"Cookie": f"access_token={token}"})
            assert response.status_code == 401

    @pytest.mark.django_db(transaction=True)
    def test_cookie_auth_with_custom_get_user(self):
        """Cookie extraction (Rust) composes with a custom user query (subclass)."""
        user = User.objects.create(username="combo")
        calls = []

        class CustomQueryJWT(JWTAuthentication):
            async def get_user(self, user_id, auth_context):
                calls.append(user_id)
                return await User.objects.only("id", "username").aget(pk=user_id)

            def get_user_sync(self, user_id):
                calls.append(user_id)
                return User.objects.only("id", "username").get(pk=user_id)

        api = BoltAPI()

        @api.get(
            "/combo-me",
            auth=[CustomQueryJWT(secret=SECRET, cookie="access_token")],
            guards=[IsAuthenticated()],
        )
        async def combo_me(request):
            u = request.user
            return {"username": u.username if u else None}

        token = create_token(user_id=str(user.id))
        with TestClient(api) as client:
            response = client.get("/combo-me", headers={"Cookie": f"access_token={token}"})
            assert response.status_code == 200
            assert response.json()["username"] == "combo"
            assert calls == [str(user.id)]


class TestGetUserOverrideResolution:
    """Overriding either get_user or get_user_sync alone must be honored."""

    @pytest.mark.django_db(transaction=True)
    def test_async_get_user_override_alone_is_used(self):
        """Overriding ONLY the async get_user must drive request.user.

        The token's sub is a username, which the default pk-based query can
        never resolve — so a 200 with the right username proves the override
        ran, and inheriting the default get_user_sync did not shadow it.
        """
        user = User.objects.create(username="asyncoverride")

        class UsernameJWT(JWTAuthentication):
            async def get_user(self, user_id, auth_context):
                return await User.objects.aget(username=user_id)

        api = BoltAPI()

        @api.get(
            "/async-override",
            auth=[UsernameJWT(secret=SECRET)],
            guards=[IsAuthenticated()],
        )
        async def me(request):
            u = request.user
            return {"username": u.username if u else None}

        token = create_token(user_id=user.username)
        with TestClient(api) as client:
            response = client.get("/async-override", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["username"] == "asyncoverride"

    @pytest.mark.django_db(transaction=True)
    def test_sync_get_user_sync_override_alone_is_used(self):
        """Overriding ONLY get_user_sync must drive request.user."""
        user = User.objects.create(username="syncoverride")

        class UsernameSyncJWT(JWTAuthentication):
            def get_user_sync(self, user_id):
                return User.objects.get(username=user_id)

        api = BoltAPI()

        @api.get(
            "/sync-override",
            auth=[UsernameSyncJWT(secret=SECRET)],
            guards=[IsAuthenticated()],
        )
        async def me(request):
            u = request.user
            return {"username": u.username if u else None}

        token = create_token(user_id=user.username)
        with TestClient(api) as client:
            response = client.get("/sync-override", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["username"] == "syncoverride"

    @pytest.mark.django_db(transaction=True)
    def test_override_honored_alongside_plain_backend_on_other_route(self):
        """A custom get_user must win on its own route even when a plain JWT
        backend (same scheme_name) is registered first on another route.

        Guards against global scheme-keyed loader registration, where the
        first "jwt" backend silently supplies user loading for all routes.
        """
        user = User.objects.create(username="perroute")

        class UsernameJWT(JWTAuthentication):
            async def get_user(self, user_id, auth_context):
                return await User.objects.aget(username=user_id)

        api = BoltAPI()

        # Plain backend registers "jwt" first.
        @api.get("/plain", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
        async def plain(request):
            return {"ok": True}

        @api.get("/custom-me", auth=[UsernameJWT(secret=SECRET)], guards=[IsAuthenticated()])
        async def custom_me(request):
            u = request.user
            return {"username": u.username if u else None}

        token = create_token(user_id=user.username)
        with TestClient(api) as client:
            response = client.get("/custom-me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["username"] == "perroute"

    @pytest.mark.django_db(transaction=True)
    def test_default_loader_returns_none_for_stale_user_id(self):
        """The fallback loader (unregistered schemes, e.g. session auth) must
        return None for a deleted user, like the framework backend defaults —
        not raise User.DoesNotExist into the handler."""
        assert default_django_user_loader("999999", None, False) is None

    @pytest.mark.django_db(transaction=True)
    def test_sync_handler_on_async_dispatch_path_loads_user(self):
        """A sync handler forced onto the async dispatch path (here via a
        revocation store) runs inline on the event loop when it has no
        detected ORM operations. request.user must still load — the loader
        has to route the query through the executor, not call the sync ORM
        on the event-loop thread (SynchronousOnlyOperation).
        """
        user = User.objects.create(username="syncloop")
        api = BoltAPI()

        @api.get(
            "/sync-loop-me",
            auth=[JWTAuthentication(secret=SECRET, revocation_store=InMemoryRevocation())],
            guards=[IsAuthenticated()],
        )
        def sync_loop_me(request):
            u = request.user
            return {"username": u.username if u else None}

        token = create_token(user_id=str(user.id), jti="sync-loop-jti")
        with TestClient(api) as client:
            response = client.get("/sync-loop-me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200, response.text
            assert response.json()["username"] == "syncloop"

    @pytest.mark.django_db(transaction=True)
    def test_default_backend_loads_by_pk(self):
        """Plain JWTAuthentication keeps the default pk-based query."""
        user = User.objects.create(username="plainuser")
        api = BoltAPI()

        @api.get(
            "/plain-me",
            auth=[JWTAuthentication(secret=SECRET)],
            guards=[IsAuthenticated()],
        )
        async def me(request):
            u = request.user
            return {"username": u.username if u else None}

        token = create_token(user_id=str(user.id))
        with TestClient(api) as client:
            response = client.get("/plain-me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["username"] == "plainuser"
