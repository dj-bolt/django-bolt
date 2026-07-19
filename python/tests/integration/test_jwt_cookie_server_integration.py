"""Subprocess-based integration tests for cookie JWT auth and get_user overrides.

Complements the in-process TestClient suite (`test_jwt_cookie_auth.py`):

- TestClient suite covers extraction semantics exhaustively (wrong cookie
  name, expired/forged tokens, override resolution priority) — fast,
  in-process.
- This suite proves the startup wiring over a real server: the ``cookie=``
  backend config must survive runbolt startup (Python metadata → Rust
  ``AppState``) for extraction to work on real HTTP Cookie headers, and the
  ``get_user`` override must be registered at server startup for
  ``request.user`` to use it.
"""

from __future__ import annotations

import time

import jwt
import pytest

from .apps import app_module
from .apps.jwt_cookie_user import SECRET, USERNAME

pytestmark = pytest.mark.server_integration


def make_token(sub: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": sub, "iat": now, "exp": now + 3600}, SECRET, algorithm="HS256")


def test_cookie_jwt_over_real_server(make_server_project):
    """Cookie extraction against a real runbolt server.

    A valid token in the ``access_token`` cookie authenticates; no cookie is
    rejected; the Authorization header is NOT a fallback for a cookie-mode
    backend; and a dual backend list serves cookie and header clients alike.
    """
    project = make_server_project(api_module=app_module("jwt_cookie_user"))
    token = make_token("cookie-user")

    with project.start() as server:
        ok = server.get("/whoami", headers={"Cookie": f"access_token={token}"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["user_id"] == "cookie-user"

        missing = server.get("/whoami")
        assert missing.status_code == 401, missing.text

        header_only = server.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert header_only.status_code == 401, header_only.text

        via_cookie = server.get("/dual", headers={"Cookie": f"access_token={token}"})
        assert via_cookie.status_code == 200, via_cookie.text

        via_header = server.get("/dual", headers={"Authorization": f"Bearer {token}"})
        assert via_header.status_code == 200, via_header.text


def test_cookie_csrf_over_real_server(make_server_project):
    """Cookie CSRF uses the effective request origin on the real HTTP path."""
    project = make_server_project(api_module=app_module("jwt_cookie_user"))
    token = make_token("cookie-user")

    with project.start() as server:
        cookie = {"Cookie": f"access_token={token}"}
        http_origin = server.base_url
        https_origin = f"https://{server.host}:{server.port}"

        cross_site = server.request(
            "POST",
            "/write",
            headers={**cookie, "Sec-Fetch-Site": "cross-site"},
        )
        assert cross_site.status_code == 403, cross_site.text

        same_origin = server.request(
            "POST",
            "/write",
            headers={**cookie, "Sec-Fetch-Site": "same-origin"},
        )
        assert same_origin.status_code == 200, same_origin.text

        matching_origin = server.request(
            "POST",
            "/write",
            headers={**cookie, "Origin": http_origin},
        )
        assert matching_origin.status_code == 200, matching_origin.text

        wrong_scheme = server.request(
            "POST",
            "/write",
            headers={**cookie, "Origin": https_origin},
        )
        assert wrong_scheme.status_code == 403, wrong_scheme.text

        forwarded_scheme = server.request(
            "POST",
            "/write",
            headers={
                **cookie,
                "Origin": https_origin,
                "X-Forwarded-Proto": "https",
            },
        )
        assert forwarded_scheme.status_code == 200, forwarded_scheme.text

        no_origin_signal = server.request("POST", "/write", headers=cookie)
        assert no_origin_signal.status_code == 403, no_origin_signal.text

        safe_method = server.get("/whoami", headers=cookie)
        assert safe_method.status_code == 200, safe_method.text


def test_custom_get_user_over_real_server(make_server_project):
    """request.user loads via the subclass's get_user in a real server.

    The token's ``sub`` is a username, which the default pk-based query can
    never resolve — a 200 with the right username proves the backend
    registered at server startup resolved the override and used it.
    """
    project = make_server_project(api_module=app_module("jwt_cookie_user"))

    with project.start() as server:
        seeded = server.request("POST", "/seed")
        assert seeded.status_code == 200, seeded.text
        assert seeded.json()["username"] == USERNAME

        token = make_token(USERNAME)
        response = server.get("/me", headers={"Cookie": f"access_token={token}"})
        assert response.status_code == 200, response.text
        assert response.json()["username"] == USERNAME
