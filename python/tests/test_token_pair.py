"""Tests for the access + refresh token lifecycle (issue #239).

Exercises token-pair issuance, rotation modes, reuse detection, global
logout, and the absolute session cap end to end through a real
``TestClient`` server plus direct unit tests of the helpers.
"""

from __future__ import annotations

import time

import jwt
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import (
    IsAuthenticated,
    JWTAuthentication,
    TokenRotationError,
    create_token_pair,
    rotate_refresh_token,
)
from django_bolt.auth.revocation import InMemoryRevocation
from django_bolt.testing import TestClient

SECRET = "token-pair-secret"

pytestmark = pytest.mark.django_db


def decode(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=["HS256"])


class TestCreateTokenPair:
    def test_issues_access_and_refresh_with_expected_claims(self):
        pair = create_token_pair("user-42", secret=SECRET)

        assert pair.access_claims["typ"] == "access"
        assert pair.refresh_claims["typ"] == "refresh"
        assert pair.access_claims["sub"] == "user-42"
        assert pair.refresh_claims["sub"] == "user-42"
        # Refresh token has a jti and family; access token does not.
        assert "jti" in pair.refresh_claims
        assert "fam" in pair.refresh_claims
        assert "jti" not in pair.access_claims
        # Both carry the immutable origin-auth-time.
        assert pair.access_claims["oat"] == pair.refresh_claims["oat"]
        # Round-trips through the real secret.
        assert decode(pair.access_token)["typ"] == "access"

    def test_access_ttl_shorter_than_refresh_ttl(self):
        pair = create_token_pair("u", secret=SECRET, access_ttl=100, refresh_ttl=1000)
        assert pair.refresh_claims["exp"] - pair.access_claims["exp"] == 900

    def test_method_recorded_as_amr(self):
        pair = create_token_pair("u", secret=SECRET, method="otp")
        assert pair.access_claims["amr"] == ["otp"]

    def test_extra_claims_copied_into_both(self):
        pair = create_token_pair("u", secret=SECRET, claims={"role": "admin"})
        assert pair.access_claims["role"] == "admin"
        assert pair.refresh_claims["role"] == "admin"

    def test_accepts_django_user_pk(self):
        class FakeUser:
            pk = 7

        pair = create_token_pair(FakeUser(), secret=SECRET)
        assert pair.access_claims["sub"] == "7"


class TestRotation:
    @pytest.mark.asyncio
    async def test_full_rotation_issues_new_pair_and_revokes_old(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        old_jti = pair.refresh_claims["jti"]

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

        assert rotated.refresh_claims["jti"] != old_jti
        # Same family carried across the rotation.
        assert rotated.refresh_claims["fam"] == pair.refresh_claims["fam"]
        # Old token now revoked.
        assert await store.is_revoked(old_jti)

    @pytest.mark.asyncio
    async def test_mode_b_keeps_refresh_token(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, rotate=False)

        assert rotated.refresh_token == ""
        assert rotated.access_claims["typ"] == "access"
        # Original refresh token is untouched (not revoked).
        assert not await store.is_revoked(pair.refresh_claims["jti"])

    @pytest.mark.asyncio
    async def test_oat_preserved_across_rotation(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        original_oat = pair.refresh_claims["oat"]

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
        assert rotated.refresh_claims["oat"] == original_oat

    @pytest.mark.asyncio
    async def test_reuse_of_rotated_token_revokes_family(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        fam = pair.refresh_claims["fam"]

        # First rotation succeeds and revokes the original jti.
        await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

        # Replaying the now-revoked original token is reuse: it fails and
        # burns the whole family.
        with pytest.raises(TokenRotationError):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
        assert await store.is_family_revoked(fam)

    @pytest.mark.asyncio
    async def test_family_revoked_blocks_rotation(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        await store.revoke_family(pair.refresh_claims["fam"])

        with pytest.raises(TokenRotationError, match="family"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_missing_jti_rejected(self):
        store = InMemoryRevocation()
        with pytest.raises(TokenRotationError, match="jti"):
            await rotate_refresh_token({"sub": "u", "typ": "refresh"}, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_max_session_lifetime_enforced(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        # Backdate the origin so the session is older than the cap.
        pair.refresh_claims["oat"] = int(time.time()) - 10_000

        with pytest.raises(TokenRotationError, match="maximum lifetime"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, max_session_lifetime=3600)

    @pytest.mark.asyncio
    async def test_new_pair_carries_current_user_version(self):
        store = InMemoryRevocation()
        await store.bump_user_version("u")  # version now 1
        pair = create_token_pair("u", secret=SECRET)

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
        assert rotated.access_claims["ver"] == 1


class TestStorePrimitives:
    @pytest.mark.asyncio
    async def test_user_version_bump(self):
        store = InMemoryRevocation()
        assert await store.get_user_version("u") == 0
        assert await store.bump_user_version("u") == 1
        assert await store.bump_user_version("u") == 2
        assert await store.get_user_version("u") == 2

    @pytest.mark.asyncio
    async def test_family_revocation(self):
        store = InMemoryRevocation()
        assert not await store.is_family_revoked("fam-1")
        await store.revoke_family("fam-1")
        assert await store.is_family_revoked("fam-1")


class TestRefreshEndpointTypEnforcement:
    """The rotation endpoint rejects access tokens at the Rust layer, and
    normal routes reject refresh tokens — proving type separation end to
    end through a real server pipeline."""

    @pytest.fixture(scope="class")
    def client(self):
        api = BoltAPI()

        @api.get("/api", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
        async def api_route(request: dict):
            return {"user_id": request["context"]["user_id"]}

        @api.post(
            "/refresh",
            auth=[JWTAuthentication(secret=SECRET, token_type="refresh")],
            guards=[IsAuthenticated()],
        )
        async def refresh_route(request: dict):
            return {"typ": request["context"]["auth_claims"]["typ"]}

        with TestClient(api) as c:
            yield c

    def test_access_token_works_on_api_route(self, client):
        pair = create_token_pair("u", secret=SECRET)
        r = client.get("/api", headers={"Authorization": f"Bearer {pair.access_token}"})
        assert r.status_code == 200

    def test_refresh_token_rejected_on_api_route(self, client):
        pair = create_token_pair("u", secret=SECRET)
        r = client.get("/api", headers={"Authorization": f"Bearer {pair.refresh_token}"})
        assert r.status_code == 401

    def test_refresh_token_works_on_refresh_route(self, client):
        pair = create_token_pair("u", secret=SECRET)
        r = client.request("POST", "/refresh", headers={"Authorization": f"Bearer {pair.refresh_token}"})
        assert r.status_code == 200
        assert r.json()["typ"] == "refresh"

    def test_access_token_rejected_on_refresh_route(self, client):
        pair = create_token_pair("u", secret=SECRET)
        r = client.request("POST", "/refresh", headers={"Authorization": f"Bearer {pair.access_token}"})
        assert r.status_code == 401
