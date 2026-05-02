"""
Subprocess-based integration tests for token revocation.

Spins up a real ``runbolt`` server, hits it with httpx, and verifies that
revoking a JTI causes the auth pipeline to reject subsequent requests.
Complements the in-process TestClient suite (`test_revocation_e2e.py`):

- TestClient suite covers fine-grained wiring (sync vs async dispatch,
  multi-backend isolation, handler call counts) — fast, in-process.
- This suite validates the same behavior over real HTTP, including
  startup wiring (auth backend metadata flowing through the dispatch
  pipeline) and TCP-level request handling.
"""

from __future__ import annotations

import time

import jwt
import pytest

pytestmark = pytest.mark.server_integration


_SECRET = "revocation-server-integration-secret"


def _make_api_body() -> str:
    """API body shipped to the runbolt subprocess.

    Defines a JWT-protected route + a logout endpoint that revokes the
    token's JTI via an in-memory store. The login endpoint issues
    tokens with random JTIs and exposes ``exp`` so the test client can
    reuse them.
    """
    return f"""
        import time
        import uuid

        import jwt

        from django_bolt.auth import (
            InMemoryRevocation,
            IsAuthenticated,
            JWTAuthentication,
        )

        SECRET = {_SECRET!r}
        revocation = InMemoryRevocation()
        auth = JWTAuthentication(secret=SECRET, revocation_store=revocation)


        @api.post("/login")
        async def login():
            jti = uuid.uuid4().hex
            now = int(time.time())
            token = jwt.encode(
                {{
                    "sub": "1",
                    "jti": jti,
                    "iat": now,
                    "exp": now + 3600,
                    "is_staff": False,
                    "is_superuser": False,
                    "username": "subject",
                }},
                SECRET,
                algorithm="HS256",
            )
            return {{"token": token, "jti": jti}}


        @api.get("/protected", auth=[auth], guards=[IsAuthenticated()])
        async def protected(request):
            return {{"ok": True, "user_id": request["context"].get("user_id")}}


        @api.post("/logout", auth=[auth], guards=[IsAuthenticated()])
        async def logout(request):
            claims = request["context"]["auth_claims"]
            await revocation.revoke(claims["jti"], exp=claims.get("exp"))
            return {{"status": "logged_out"}}
    """


def test_revoked_token_is_rejected_by_real_server(make_server_project):
    """Headline integration: real runbolt, real TCP, real revocation flow.

    1. POST /login → 200, returns a JWT.
    2. GET /protected with that token → 200.
    3. POST /logout with that token → 200, server revokes the JTI.
    4. GET /protected with the same token → 401.

    Verifies the auth metadata wiring (backends → middleware_meta →
    `meta["_revocation_handlers"]` → dispatch hook) is set up at server
    startup and that the dispatch hook actually rejects revoked tokens.
    """
    project = make_server_project(project_api_body=_make_api_body())

    with project.start() as server:
        login = server.request("POST", "/login")
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        first = server.get("/protected", headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["ok"] is True

        logout = server.request("POST", "/logout", headers=headers)
        assert logout.status_code == 200, logout.text

        second = server.get("/protected", headers=headers)
        assert second.status_code == 401, second.text


def test_token_without_jti_rejected_by_real_server(make_server_project):
    """When ``revocation_store=`` is configured, ``require_jti`` is
    auto-enabled — a token without a ``jti`` claim must be rejected
    even on the very first request."""
    project = make_server_project(project_api_body=_make_api_body())

    with project.start() as server:
        # Issue a token without a jti claim — bypasses the /login helper.
        now = int(time.time())
        token_without_jti = jwt.encode(
            {
                "sub": "1",
                "iat": now,
                "exp": now + 3600,
                "is_staff": False,
                "is_superuser": False,
                "username": "subject",
            },
            _SECRET,
            algorithm="HS256",
        )

        response = server.get(
            "/protected",
            headers={"Authorization": f"Bearer {token_without_jti}"},
        )

    assert response.status_code == 401, response.text


def test_unrevoked_token_works_across_many_requests(make_server_project):
    """A token that was never revoked must keep working across many
    requests — locks in that the dispatch hook doesn't accidentally
    invalidate valid tokens (e.g., via a stale per-request side
    effect)."""
    project = make_server_project(project_api_body=_make_api_body())

    with project.start() as server:
        token = server.request("POST", "/login").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        for i in range(10):
            response = server.get("/protected", headers=headers)
            assert response.status_code == 200, f"request {i}: {response.text}"
