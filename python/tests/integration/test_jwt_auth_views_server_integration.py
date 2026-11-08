"""JWTAuthViews over a real server with two worker processes and one shared store.

``TestClient`` runs one process. Here each request opens a new connection, so the
kernel can send it to either process. A store that one process keeps in its own
memory fails these tests.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration

LOGIN = {"username": "user", "password": "correct"}
NEW_CONNECTION = {"Connection": "close", "Sec-Fetch-Site": "same-origin"}


def _post(server, path, refresh=None, **kwargs):
    """Send a POST on a new connection with one refresh token, or no cookie."""
    server.client.cookies.clear()
    headers = dict(NEW_CONNECTION)
    if refresh is not None:
        headers["Cookie"] = f"refresh_token={refresh}"
    return server.request("POST", path, headers=headers, **kwargs)


def _login(server):
    response = _post(server, "/auth/login", json=LOGIN)
    assert response.status_code == 200, response.text
    return response.cookies["refresh_token"]


def _refresh(server, token):
    response = _post(server, "/auth/refresh", token)
    assert response.status_code == 200, response.text
    return response.cookies["refresh_token"]


def _check_session_lifecycle(server):
    pids = {server.get("/pid", headers={"Connection": "close"}).json()["pid"] for _ in range(20)}
    assert len(pids) == 2, pids

    # A used refresh token that comes back ends its family, also the token that replaced it.
    first = _login(server)
    second = _refresh(server, first)
    assert _post(server, "/auth/refresh", first).status_code == 401
    assert _post(server, "/auth/refresh", second).status_code == 401

    # Logout ends the session.
    logged_out = _login(server)
    assert _post(server, "/auth/logout", logged_out).status_code == 200
    assert _post(server, "/auth/refresh", logged_out).status_code == 401

    # Logout-all ends each older session. A new login works.
    old = _login(server)
    assert _post(server, "/auth/logout-all", old).status_code == 200
    assert _post(server, "/auth/refresh", old).status_code == 401
    _refresh(server, _login(server))


@pytest.mark.redis
def test_session_lifecycle_on_a_shared_redis_store(make_server_project, redis_cache_settings):
    project = make_server_project(api_module=app_module("jwt_auth_views"), settings_extra=redis_cache_settings)

    with project.start(processes=2) as server:
        _check_session_lifecycle(server)


def test_session_lifecycle_on_the_database_store(make_server_project):
    project = make_server_project(
        api_module=app_module("jwt_auth_views"),
        installed_apps=["django_bolt.auth.revocation_db"],
        settings_extra='REVOCATION_STORE = "database"\n',
    )
    project.manage("migrate")

    with project.start(processes=2) as server:
        _check_session_lifecycle(server)
