"""Subprocess-based integration test for RS256 JWT auth.

Regression test for https://github.com/dj-bolt/django-bolt/issues/261:
JWTAuthentication with an RSA public key and algorithms=["RS256"] rejected
every token, valid or not, because the Rust decode path always built an
HMAC decoding key regardless of the configured algorithm. This proves the
fix over a real runbolt server rather than just unit-testing the Rust
change in isolation.
"""

from __future__ import annotations

import time

import jwt
import pytest

from .apps import app_module
from .apps.jwt_rs256_user import PRIVATE_KEY_PEM

pytestmark = pytest.mark.server_integration


def make_token(sub: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": sub, "iat": now, "exp": now + 3600}, PRIVATE_KEY_PEM, algorithm="RS256")


def test_rs256_jwt_over_real_server(make_server_project):
    """A validly-signed RS256 token must authenticate over a real server."""
    project = app_module("jwt_rs256_user")
    token = make_token("rs256-user")

    with make_server_project(api_module=project).start(startup_path="/app-health") as server:
        ok = server.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["user_id"] == "rs256-user"

        missing = server.get("/whoami")
        assert missing.status_code == 401, missing.text

        tampered = server.get("/whoami", headers={"Authorization": f"Bearer {token}x"})
        assert tampered.status_code == 401, tampered.text
