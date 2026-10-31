from __future__ import annotations

import time
from typing import ClassVar

import jwt
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils.module_loading import import_string

from django_bolt import BoltAPI
from django_bolt.auth import InMemoryRevocation, JWTAuthViews
from django_bolt.testing import TestClient

pytestmark = pytest.mark.django_db


class User:
    pk = "view-user"
    is_active = True


def test_ready_made_cookie_lifecycle_routes():
    api = BoltAPI()
    store = InMemoryRevocation()

    async def validate(request, credentials):
        if credentials.username == "alice" and credentials.password == "correct":
            return User()
        return None

    JWTAuthViews(
        store=store,
        secret="a-secure-test-secret-that-is-long-enough",
        secure_cookies=False,
        credential_validator=validate,
    ).register(api)

    with TestClient(api) as client:
        denied = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
        assert denied.status_code == 401

        login = client.post("/auth/login", json={"username": "alice", "password": "correct"})
        assert login.status_code == 200
        assert client.cookies.get("access_token")
        assert client.cookies.get("refresh_token")

        headers = {"Sec-Fetch-Site": "none"}
        refreshed = client.post("/auth/refresh", headers=headers)
        assert refreshed.status_code == 200

        logout_all = client.post("/auth/logout-all", headers=headers)
        assert logout_all.status_code == 200
        assert client.cookies.get("access_token") is None
        assert client.cookies.get("refresh_token") is None


def test_auth_views_are_documented_in_openapi():
    api = BoltAPI()
    JWTAuthViews(store=InMemoryRevocation(), secret="a-secure-test-secret-that-is-long-enough").register(api)
    api._register_openapi_routes()

    with TestClient(api) as client:
        schema = client.get("/docs/openapi.json").json()
    assert set(schema["paths"]) >= {
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
        "/auth/logout-all",
    }


SECRET = "a-secure-test-secret-that-is-long-enough"


def _views_api(**options) -> BoltAPI:
    api = BoltAPI()

    async def validate(request, credentials):
        return User() if credentials.password == "correct" else None

    JWTAuthViews(store=InMemoryRevocation(), secret=SECRET, credential_validator=validate, **options).register(api)
    return api


LOGIN = {"username": "alice", "password": "correct"}
NO_CSRF_ORIGIN = {"Sec-Fetch-Site": "none"}


@pytest.mark.parametrize("secure_cookies", [True, False], ids=["secure", "not-secure"])
def test_cookies_are_secure_unless_the_project_turns_it_off(secure_cookies):
    options = {} if secure_cookies else {"secure_cookies": False}

    with TestClient(_views_api(**options)) as client:
        login = client.post("/auth/login", json=LOGIN)

    cookies = login.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for cookie in cookies:
        attributes = cookie.split("; ")
        assert ("Secure" in attributes) is secure_cookies
        assert "HttpOnly" in attributes


def test_views_use_the_given_key_algorithm_and_prefix():
    """Login signs with the secret, algorithm and ``kid`` of the views. The refresh route accepts these tokens."""
    api = _views_api(algorithm="HS512", kid="key-1", prefix="/api/auth-X/", secure_cookies=False)

    with TestClient(api) as client:
        login = client.post("/api/auth-X/login", json=LOGIN)
        assert login.status_code == 200
        access = client.cookies.get("access_token")
        refresh = client.cookies.get("refresh_token")
        for token in (access, refresh):
            assert jwt.get_unverified_header(token)["kid"] == "key-1"
            assert jwt.decode(token, SECRET, algorithms=["HS512"])["sub"] == "view-user"
        # Only the new prefix has routes.
        assert client.post("/auth/login", json=LOGIN).status_code == 404

        refreshed = client.post("/api/auth-X/refresh", headers=NO_CSRF_ORIGIN)
        assert refreshed.status_code == 200, refreshed.text
        assert client.cookies.get("refresh_token") != refresh
        assert client.post("/api/auth-X/logout", headers=NO_CSRF_ORIGIN).status_code == 200


def test_refresh_cookie_without_a_jti_is_rejected():
    """A refresh token that cannot be revoked must not reach the lifecycle routes."""
    now = int(time.time())
    claims = {"sub": "view-user", "typ": "refresh", "fam": "family-1", "ver": 0, "iat": now, "exp": now + 600}
    token = jwt.encode(claims, SECRET, algorithm="HS256")

    with TestClient(_views_api()) as client:
        for path in ("/auth/refresh", "/auth/logout", "/auth/logout-all"):
            response = client.post(path, headers={"Cookie": f"refresh_token={token}", **NO_CSRF_ORIGIN})
            assert response.status_code == 401, path

        # The same token with a jti passes the check.
        claims["jti"] = "jti-1"
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        response = client.post("/auth/logout-all", headers={"Cookie": f"refresh_token={token}", **NO_CSRF_ORIGIN})
        assert response.status_code == 200, response.text


class RecordingBackend(ModelBackend):
    """Django auth backend that keeps the request that each login attempt passes."""

    requests: ClassVar[list] = []

    async def aauthenticate(self, request, username=None, password=None, **kwargs):
        type(self).requests.append(request)
        return await super().aauthenticate(request, username=username, password=password, **kwargs)


@pytest.mark.django_db(transaction=True)
def test_default_login_checks_the_django_user(settings):
    """With no ``credential_validator``, login uses Django ``authenticate`` and the user table."""
    backend_path = "tests.test_jwt_auth_views.RecordingBackend"
    settings.AUTHENTICATION_BACKENDS = [backend_path]
    # Django imports the backend by its path. Read the list from the class that Django uses.
    recorded_requests = import_string(backend_path).requests
    recorded_requests.clear()
    get_user_model().objects.create_user("carol", password="right-password")
    get_user_model().objects.create_user("dave", password="right-password", is_active=False)

    api = BoltAPI()
    JWTAuthViews(
        store=InMemoryRevocation(),
        secret="a-secure-test-secret-that-is-long-enough",
        secure_cookies=False,
    ).register(api)

    with TestClient(api) as client:
        wrong_password = client.post("/auth/login", json={"username": "carol", "password": "wrong"})
        unknown_user = client.post("/auth/login", json={"username": "nobody", "password": "right-password"})
        inactive_user = client.post("/auth/login", json={"username": "dave", "password": "right-password"})
        assert [wrong_password.status_code, unknown_user.status_code, inactive_user.status_code] == [401] * 3
        assert not client.cookies.get("access_token")

        recorded_requests.clear()
        login = client.post("/auth/login", json={"username": "carol", "password": "right-password"})

        assert login.status_code == 200
        assert client.cookies.get("access_token")
        assert client.cookies.get("refresh_token")
    # The login passes the request of the client to Django, for backends that read it.
    assert len(recorded_requests) == 1
    assert recorded_requests[0] is not None
