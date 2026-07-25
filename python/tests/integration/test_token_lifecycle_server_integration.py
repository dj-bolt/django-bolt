"""Real-server integration tests for access/refresh token rotation."""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_token_lifecycle_over_real_server(make_server_project):
    project = make_server_project(api_module=app_module("token_lifecycle"))

    with project.start() as server:
        login = server.request("POST", "/login")
        assert login.status_code == 200, login.text
        original = login.json()

        access_works = server.get(
            "/whoami",
            headers=bearer(original["access_token"]),
        )
        assert access_works.status_code == 200, access_works.text
        assert access_works.json()["user_id"] == "lifecycle-user"

        refresh_on_access_route = server.get(
            "/whoami",
            headers=bearer(original["refresh_token"]),
        )
        assert refresh_on_access_route.status_code == 401, refresh_on_access_route.text

        access_on_refresh_route = server.request(
            "POST",
            "/refresh",
            headers=bearer(original["access_token"]),
        )
        assert access_on_refresh_route.status_code == 401, access_on_refresh_route.text

        rotated_response = server.request(
            "POST",
            "/refresh",
            headers=bearer(original["refresh_token"]),
        )
        assert rotated_response.status_code == 200, rotated_response.text
        rotated = rotated_response.json()

        rotated_access_works = server.get(
            "/whoami",
            headers=bearer(rotated["access_token"]),
        )
        assert rotated_access_works.status_code == 200, rotated_access_works.text

        replay = server.request(
            "POST",
            "/refresh",
            headers=bearer(original["refresh_token"]),
        )
        assert replay.status_code == 401, replay.text

        burned_descendant = server.request(
            "POST",
            "/refresh",
            headers=bearer(rotated["refresh_token"]),
        )
        assert burned_descendant.status_code == 401, burned_descendant.text


def test_token_cookie_serialization_over_real_server(make_server_project):
    project = make_server_project(api_module=app_module("token_lifecycle"))

    with project.start() as server:
        response = server.request("POST", "/login-cookies")
        assert response.status_code == 200, response.text

        cookies = response.headers.get_list("set-cookie")
        access_cookie = next(value for value in cookies if value.startswith("access_token="))
        refresh_cookie = next(value for value in cookies if value.startswith("refresh_token="))

        assert "HttpOnly" in access_cookie
        assert "Path=/" in access_cookie
        assert "HttpOnly" in refresh_cookie
        assert "Path=/refresh" in refresh_cookie
