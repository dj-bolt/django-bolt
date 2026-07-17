"""Subprocess integration tests for asymmetric (RS256) JWT auth.

Complements the in-process ``test_jwt_asymmetric.py`` suite: this proves the
whole path over a real ``runbolt`` server — a PEM public key configured in
Python survives startup wiring into the Rust decoding key built at
registration, and provider-style tokens (including ones with non-string JOSE
header extension parameters, as Clerk and Auth0 emit) verify on real HTTP
requests. It also proves invalid key material aborts server startup instead
of silently serving the route unauthenticated.
"""

from __future__ import annotations

import time

import jwt
import pytest

from .apps import app_module, app_source
from .apps.jwt_rs256_provider import PRIVATE_KEY_PEM

pytestmark = pytest.mark.server_integration


def make_token(sub: str, *, headers=None, exp_offset: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "iat": now, "exp": now + exp_offset},
        PRIVATE_KEY_PEM,
        algorithm="RS256",
        headers=headers,
    )


def test_rs256_over_real_server(make_server_project):
    """A valid RS256 token authenticates against a server configured with
    only the PEM public key; missing and tampered tokens are rejected."""
    project = make_server_project(api_module=app_module("jwt_rs256_provider"))

    with project.start() as server:
        token = make_token("provider-user")
        ok = server.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["user_id"] == "provider-user"

        missing = server.get("/whoami")
        assert missing.status_code == 401, missing.text
        # RFC 6750: 401 carries a Bearer challenge.
        assert missing.headers.get("www-authenticate") == "Bearer"

        header, payload, signature = token.split(".")
        tampered = f"{header}.{payload}.{signature[:-2]}" + ("AA" if not signature.endswith("AA") else "BB")
        bad = server.get("/whoami", headers={"Authorization": f"Bearer {tampered}"})
        assert bad.status_code == 401, bad.text


def test_rs256_non_string_header_extra_over_real_server(make_server_project):
    """The Clerk repro end to end: a numeric JOSE header parameter must not
    break verification of an otherwise valid RS256 token."""
    project = make_server_project(api_module=app_module("jwt_rs256_provider"))

    with project.start() as server:
        token = make_token("provider-user", headers={"oiat": 1784025590})
        ok = server.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["user_id"] == "provider-user"


def test_invalid_public_key_aborts_startup(make_server_project):
    """A JWT backend whose public key is not a valid PEM must abort server
    startup, not boot with the route silently serving unauthenticated."""
    project = make_server_project(api_source=app_source("jwt_rs256_badkey"))

    with pytest.raises(AssertionError) as excinfo, project.start(timeout=15):
        pass
    assert "Invalid RSA public key PEM" in str(excinfo.value)
