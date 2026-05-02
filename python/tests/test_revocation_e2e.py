"""End-to-end tests for token revocation through the in-process pipeline.

Uses ``TestClient`` (in-process Rust/Actix dispatch). The subprocess-based
counterpart is in ``integration/test_revocation_server_integration.py``.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import jwt
import pytest
from django.contrib.auth.models import User

from django_bolt import BoltAPI
from django_bolt.auth import (
    APIKeyAuthentication,
    DjangoCacheRevocation,
    InMemoryRevocation,
    IsAuthenticated,
    JWTAuthentication,
)
from django_bolt.auth.jwt_utils import create_jwt_for_user
from django_bolt.testing import TestClient

SECRET = "revocation-integration-secret"


def _create_user():
    return User.objects.create_user(
        username="revoke-test",
        email="revoke@test.com",
        password="x",
    )


def _issue_token(user, *, expires_in: int = 3600, jti: str | None = None) -> tuple[str, dict]:
    """Issue a JWT and return (token, claims).

    ``jti=None`` auto-generates a UUID; pass an explicit string
    (including ``""``) to control the value precisely.
    """
    if jti is None:
        jti = uuid.uuid4().hex
    token = create_jwt_for_user(
        user,
        secret=SECRET,
        expires_in=expires_in,
        extra_claims={"jti": jti},
    )
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    return token, claims


@pytest.fixture
def revocation_store():
    return InMemoryRevocation()


@pytest.fixture
def api(revocation_store):
    """API with one protected endpoint and one logout endpoint that revokes."""
    api = BoltAPI()
    auth = JWTAuthentication(secret=SECRET, revocation_store=revocation_store)

    @api.get("/protected", auth=[auth], guards=[IsAuthenticated()])
    async def protected(request):
        return {"ok": True, "user_id": request["context"].get("user_id")}

    @api.post("/logout", auth=[auth], guards=[IsAuthenticated()])
    async def logout(request):
        claims = request["context"]["auth_claims"]
        await revocation_store.revoke(claims["jti"], exp=claims.get("exp"))
        return {"status": "logged_out"}

    return api


@pytest.mark.django_db
def test_revoked_token_is_rejected_after_logout(api):
    """Valid token → logout → same token now rejected."""
    user = _create_user()
    token, _ = _issue_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(api) as client:
        first = client.get("/protected", headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["ok"] is True

        logout = client.post("/logout", headers=headers)
        assert logout.status_code == 200, logout.text

        rejected = client.get("/protected", headers=headers)
        assert rejected.status_code == 401, rejected.text


@pytest.mark.django_db
def test_unrevoked_token_passes(api):
    user = _create_user()
    token, _ = _issue_token(user)

    with TestClient(api) as client:
        r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text


@pytest.mark.django_db
def test_revoking_one_token_does_not_affect_others(api, revocation_store):
    """Revocation is keyed on JTI — other tokens for the same user keep working."""
    user = _create_user()
    token_a, claims_a = _issue_token(user, jti="aaa")
    token_b, _ = _issue_token(user, jti="bbb")

    asyncio.run(revocation_store.revoke(claims_a["jti"], exp=claims_a.get("exp")))

    with TestClient(api) as client:
        r_a = client.get("/protected", headers={"Authorization": f"Bearer {token_a}"})
        r_b = client.get("/protected", headers={"Authorization": f"Bearer {token_b}"})

    assert r_a.status_code == 401, r_a.text
    assert r_b.status_code == 200, r_b.text


@pytest.mark.django_db
def test_token_without_jti_is_rejected_when_revocation_configured():
    """Configuring a revocation store auto-enables require_jti — tokens
    without a jti claim must be rejected."""
    api = BoltAPI()
    store = InMemoryRevocation()
    auth = JWTAuthentication(secret=SECRET, revocation_store=store)

    @api.get("/p", auth=[auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    user = _create_user()
    # create_jwt_for_user without `extra_claims={"jti": ...}` produces a
    # token with no jti claim — exactly what we want here.
    token = create_jwt_for_user(user, secret=SECRET, expires_in=3600)

    with TestClient(api) as client:
        r = client.get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


@pytest.mark.django_db
def test_revocation_works_with_django_cache_store():
    """Same flow with DjangoCacheRevocation (LocMemCache backend)."""
    api = BoltAPI()
    store = DjangoCacheRevocation(cache_alias="default")
    auth = JWTAuthentication(secret=SECRET, revocation_store=store)

    @api.get("/p", auth=[auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    @api.post("/logout", auth=[auth], guards=[IsAuthenticated()])
    async def logout(request):
        claims = request["context"]["auth_claims"]
        await store.revoke(claims["jti"], exp=claims.get("exp"))
        return {"ok": True}

    user = _create_user()
    token, _ = _issue_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(api) as client:
        assert client.get("/p", headers=headers).status_code == 200
        assert client.post("/logout", headers=headers).status_code == 200
        assert client.get("/p", headers=headers).status_code == 401


# ---------------------------------------------------------------------------
# Dispatch-path integration: sync vs async, multi-backend, no-revocation routes
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sync_handler_with_revocation_still_enforces():
    """Sync `def` handlers are forced off the sync-dispatch fast path
    when revocation is configured (the handler is async). This fails if
    that suppression isn't in place."""
    api = BoltAPI()
    store = InMemoryRevocation()
    auth = JWTAuthentication(secret=SECRET, revocation_store=store)

    @api.get("/sync", auth=[auth], guards=[IsAuthenticated()])
    def sync_handler(request):
        return {"ok": True}

    user = _create_user()
    token, claims = _issue_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    asyncio.run(store.revoke(claims["jti"], exp=claims.get("exp")))

    with TestClient(api) as client:
        r = client.get("/sync", headers=headers)
    assert r.status_code == 401, f"Revocation must apply to sync handlers too — got {r.status_code}: {r.text}"


@pytest.mark.django_db
def test_multi_backend_revocation_only_applies_to_matched_backend():
    """A route with both JWT (revocation) and API key (no revocation): an
    API-key request must not be checked against JWT's revocation store."""
    api = BoltAPI()
    jwt_store = InMemoryRevocation()
    jwt_auth = JWTAuthentication(secret=SECRET, revocation_store=jwt_store)
    apikey_auth = APIKeyAuthentication(api_keys={"valid-key"})

    @api.get("/p", auth=[jwt_auth, apikey_auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True, "backend": request["context"].get("auth_backend")}

    asyncio.run(jwt_store.revoke("any-jti", exp=int(time.time()) + 3600))

    with TestClient(api) as client:
        r = client.get("/p", headers={"X-API-Key": "valid-key"})

    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "api_key"


@pytest.mark.django_db
def test_custom_revoked_token_handler_without_store():
    """The `revoked_token_handler=` config path should work the same as
    `revocation_store=`."""
    revoked_set: set[str] = set()

    async def custom_handler(jti: str) -> bool:
        return jti in revoked_set

    api = BoltAPI()
    auth = JWTAuthentication(secret=SECRET, revoked_token_handler=custom_handler)

    @api.get("/p", auth=[auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    user = _create_user()
    token, claims = _issue_token(user)
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(api) as client:
        assert client.get("/p", headers=headers).status_code == 200
        revoked_set.add(claims["jti"])
        r = client.get("/p", headers=headers)
        assert r.status_code == 401, r.text


@pytest.mark.django_db
def test_re_issued_token_after_revocation_works():
    """Revocation is per-JTI, not per-user — a fresh token with a new JTI
    works even after the previous one was revoked."""
    api = BoltAPI()
    store = InMemoryRevocation()
    auth = JWTAuthentication(secret=SECRET, revocation_store=store)

    @api.get("/p", auth=[auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    user = _create_user()
    old_token, old_claims = _issue_token(user, jti="old")
    new_token, _ = _issue_token(user, jti="new")

    asyncio.run(store.revoke(old_claims["jti"], exp=old_claims.get("exp")))

    with TestClient(api) as client:
        old = client.get("/p", headers={"Authorization": f"Bearer {old_token}"})
        new = client.get("/p", headers={"Authorization": f"Bearer {new_token}"})

    assert old.status_code == 401, old.text
    assert new.status_code == 200, new.text


@pytest.mark.django_db
def test_revocation_handler_invoked_exactly_once_per_request():
    """Hook fires once per authenticated request — not zero, not twice."""
    call_log: list[str] = []

    async def counting_handler(jti: str) -> bool:
        call_log.append(jti)
        return False

    api = BoltAPI()
    auth = JWTAuthentication(secret=SECRET, revoked_token_handler=counting_handler)

    @api.get("/p", auth=[auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    user = _create_user()
    token, claims = _issue_token(user)

    with TestClient(api) as client:
        for _ in range(3):
            client.get("/p", headers={"Authorization": f"Bearer {token}"})

    assert call_log == [claims["jti"]] * 3, f"Expected the handler to fire exactly once per request, got {call_log}"


@pytest.mark.django_db
def test_route_without_revocation_unaffected():
    """Routes without a revocation store must dispatch normally — no
    crashes from the dispatch hook accessing route metadata."""
    api = BoltAPI()
    auth_no_revocation = JWTAuthentication(secret=SECRET)

    @api.get("/p", auth=[auth_no_revocation], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    user = _create_user()
    token, _ = _issue_token(user)

    with TestClient(api) as client:
        r = client.get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


@pytest.mark.django_db
def test_idempotent_re_revoke_same_jti():
    """Revoking the same JTI twice is a no-op."""
    user = _create_user()
    token, claims = _issue_token(user)

    api = BoltAPI()
    store = InMemoryRevocation()
    auth = JWTAuthentication(secret=SECRET, revocation_store=store)

    @api.get("/p", auth=[auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    asyncio.run(store.revoke(claims["jti"], exp=claims.get("exp")))
    asyncio.run(store.revoke(claims["jti"], exp=claims.get("exp")))

    with TestClient(api) as client:
        r = client.get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


@pytest.mark.django_db
def test_empty_jti_in_token_is_rejected():
    """An empty-string JTI is treated as missing — cannot identify the
    token in any store, so reject with 401."""
    api = BoltAPI()
    store = InMemoryRevocation()
    auth = JWTAuthentication(secret=SECRET, revocation_store=store)

    @api.get("/p", auth=[auth], guards=[IsAuthenticated()])
    async def p(request):
        return {"ok": True}

    user = _create_user()
    token, _ = _issue_token(user, jti="")

    with TestClient(api) as client:
        r = client.get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


@pytest.mark.django_db
def test_default_ttl_is_passed_through_to_cache_backend():
    """Per-instance default_ttl must reach `cache.set(timeout=)` when
    revoke() is called without an explicit `exp`."""
    api = BoltAPI()
    store = DjangoCacheRevocation(cache_alias="default", default_ttl=42)
    auth = JWTAuthentication(secret=SECRET, revocation_store=store)

    @api.post("/logout", auth=[auth], guards=[IsAuthenticated()])
    async def logout(request):
        await store.revoke(request["context"]["auth_claims"]["jti"])
        return {"ok": True}

    user = _create_user()
    token, _ = _issue_token(user)

    captured: dict[str, object] = {}
    real_cache = store.cache
    real_set = real_cache.set

    def spy_set(key, value, timeout=None, **kwargs):
        captured["timeout"] = timeout
        return real_set(key, value, timeout=timeout, **kwargs)

    real_cache.set = spy_set
    try:
        with TestClient(api) as client:
            r = client.post("/logout", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        assert captured.get("timeout") == 42, f"default_ttl=42 should have reached cache.set(), got {captured}"
    finally:
        real_cache.set = real_set
