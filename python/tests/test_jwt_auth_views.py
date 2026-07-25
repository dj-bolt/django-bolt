from __future__ import annotations

import pytest

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
