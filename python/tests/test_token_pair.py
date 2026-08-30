"""Tests for the access + refresh token lifecycle (issue #239).

Exercises token-pair issuance, rotation modes, reuse detection, global
logout, and the absolute session cap end to end through a real
``TestClient`` server plus direct unit tests of the helpers.
"""

from __future__ import annotations

import asyncio
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
        # Both tokens carry a jti so a revocation store can identify them.
        # Only the refresh token has a family.
        assert pair.refresh_claims["jti"]
        assert pair.access_claims["jti"]
        assert pair.access_claims["jti"] != pair.refresh_claims["jti"]
        assert "fam" in pair.refresh_claims
        assert "fam" not in pair.access_claims
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

    def test_kid_added_to_both_token_headers(self):
        pair = create_token_pair("u", secret=SECRET, kid="signing-key-2026-07")

        assert jwt.get_unverified_header(pair.access_token)["kid"] == "signing-key-2026-07"
        assert jwt.get_unverified_header(pair.refresh_token)["kid"] == "signing-key-2026-07"

    def test_accepts_django_user_pk(self):
        class FakeUser:
            pk = 7

        pair = create_token_pair(FakeUser(), secret=SECRET)
        assert pair.access_claims["sub"] == "7"

    @pytest.mark.parametrize("reserved", ["sub", "iat", "oat", "ver", "typ", "exp", "jti", "fam"])
    def test_reserved_claims_cannot_be_overridden(self, reserved):
        """claims= must not overwrite lifecycle claims — e.g. ver=10**9 would
        make a token immune to global logout, oat would defeat the session cap."""
        with pytest.raises(ValueError, match=reserved):
            create_token_pair("u", secret=SECRET, claims={reserved: "attacker-controlled"})

    @pytest.mark.asyncio
    async def test_reserved_claims_rejected_at_rotation(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)

        with pytest.raises(ValueError, match="ver"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, claims={"ver": 10**9})

    @pytest.mark.asyncio
    async def test_reserved_claims_rejected_at_rotation_mode_b(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)

        with pytest.raises(ValueError, match="oat"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, rotate=False, claims={"oat": 0})


class TestRotation:
    @pytest.mark.asyncio
    async def test_rotation_adds_kid_to_new_tokens(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)

        rotated = await rotate_refresh_token(
            pair.refresh_claims,
            store=store,
            secret=SECRET,
            kid="replacement-key",
        )

        assert jwt.get_unverified_header(rotated.access_token)["kid"] == "replacement-key"
        assert jwt.get_unverified_header(rotated.refresh_token)["kid"] == "replacement-key"

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
    async def test_concurrent_rotation_only_mints_one_descendant(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)

        results = await asyncio.gather(
            rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET),
            rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET),
            return_exceptions=True,
        )

        successes = [result for result in results if not isinstance(result, Exception)]
        failures = [result for result in results if isinstance(result, TokenRotationError)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert await store.is_family_revoked(pair.refresh_claims["fam"])

    @pytest.mark.asyncio
    async def test_full_rotation_requires_family_revocation_support(self):
        class NoFamilyStore(InMemoryRevocation):
            async def is_family_revoked(self, fam):
                raise NotImplementedError

        pair = create_token_pair("u", secret=SECRET)

        with pytest.raises(TokenRotationError, match="families"):
            await rotate_refresh_token(pair.refresh_claims, store=NoFamilyStore(), secret=SECRET)

    @pytest.mark.asyncio
    async def test_consumed_marker_covers_validation_leeway(self):
        recorded = {}

        class SpyStore(InMemoryRevocation):
            async def consume(self, jti, *, exp=None):
                recorded["exp"] = exp
                return await super().consume(jti, exp=exp)

        now = int(time.time())
        pair = create_token_pair("u", secret=SECRET)
        pair.refresh_claims["exp"] = now

        await rotate_refresh_token(
            pair.refresh_claims,
            store=SpyStore(),
            secret=SECRET,
            leeway=90,
        )

        assert recorded["exp"] >= now + 90

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
    async def test_mode_b_preserves_oat_and_amr(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET, method="pwd")

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, rotate=False)
        assert rotated.access_claims["oat"] == pair.refresh_claims["oat"]
        assert rotated.access_claims["amr"] == ["pwd"]

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
    async def test_replay_burns_family_beyond_replayed_tokens_exp(self):
        """The family marker must outlive the newest descendant, not just the
        replayed token: a TTL store (e.g. DjangoCacheRevocation) drops the
        marker at ``exp``, and a descendant minted by a later rotation can
        expire well after the replayed token's own exp — the burned family
        must not become usable again in that window."""
        recorded = {}

        class SpyStore(InMemoryRevocation):
            async def revoke_family(self, fam, *, exp=None):
                recorded["exp"] = exp
                await super().revoke_family(fam, exp=exp)

        store = SpyStore()
        pair = create_token_pair("u", secret=SECRET, refresh_ttl=1000)
        # Simulate a token near its expiry: rotating it mints a descendant
        # that lives ~900s longer than the replayed token itself.
        pair.refresh_claims["exp"] = int(time.time()) + 100

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, refresh_ttl=1000)

        with pytest.raises(TokenRotationError):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, refresh_ttl=1000)

        assert recorded["exp"] >= rotated.refresh_claims["exp"]

    @pytest.mark.asyncio
    async def test_family_revoked_blocks_rotation(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        await store.revoke_family(pair.refresh_claims["fam"])

        with pytest.raises(TokenRotationError, match="family"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_family_revoked_blocks_mode_b(self):
        """A burned family must not keep minting access tokens via rotate=False."""
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        await store.revoke_family(pair.refresh_claims["fam"])

        with pytest.raises(TokenRotationError, match="family"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, rotate=False)

    @pytest.mark.asyncio
    async def test_missing_jti_rejected(self):
        store = InMemoryRevocation()
        with pytest.raises(TokenRotationError, match="jti"):
            await rotate_refresh_token({"sub": "u", "typ": "refresh"}, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_missing_family_rejected_for_full_rotation(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        del pair.refresh_claims["fam"]

        with pytest.raises(TokenRotationError, match="family"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_missing_subject_rejected(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        del pair.refresh_claims["sub"]

        with pytest.raises(TokenRotationError, match="subject"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_max_session_lifetime_enforced(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        # Backdate the origin so the session is older than the cap.
        pair.refresh_claims["oat"] = int(time.time()) - 10_000

        with pytest.raises(TokenRotationError, match="maximum lifetime"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, max_session_lifetime=3600)

    @pytest.mark.asyncio
    async def test_missing_oat_fails_closed_when_cap_configured(self):
        """A refresh token without ``oat`` cannot prove its session age; with
        a cap configured it must be rejected, not re-minted with a fresh
        origin time that resets the cap."""
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        del pair.refresh_claims["oat"]

        with pytest.raises(TokenRotationError, match="origin auth time"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, max_session_lifetime=3600)

    @pytest.mark.asyncio
    async def test_malformed_oat_is_rotation_error(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        pair.refresh_claims["oat"] = "not-an-integer"

        with pytest.raises(TokenRotationError, match="invalid origin auth time"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, max_session_lifetime=3600)

    @pytest.mark.asyncio
    async def test_future_oat_is_rejected(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        pair.refresh_claims["oat"] = int(time.time()) + 3600

        with pytest.raises(TokenRotationError, match="future"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_missing_oat_allowed_when_no_cap_configured(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        del pair.refresh_claims["oat"]

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
        assert rotated.access_claims["typ"] == "access"

    @pytest.mark.asyncio
    async def test_access_token_clamped_to_remaining_session_lifetime(self):
        """A rotation just before the cap elapses must not issue an access
        token that outlives the absolute session cap."""
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        oat = int(time.time()) - 3000  # ~600s of a 3600s session remain
        pair.refresh_claims["oat"] = oat

        rotated = await rotate_refresh_token(
            pair.refresh_claims,
            store=store,
            secret=SECRET,
            access_ttl=900,
            max_session_lifetime=3600,
        )
        assert rotated.access_claims["exp"] <= oat + 3600

    @pytest.mark.asyncio
    async def test_access_token_clamped_to_session_cap_in_mode_b(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        oat = int(time.time()) - 3000
        pair.refresh_claims["oat"] = oat

        rotated = await rotate_refresh_token(
            pair.refresh_claims,
            store=store,
            secret=SECRET,
            access_ttl=900,
            max_session_lifetime=3600,
            rotate=False,
        )
        assert rotated.access_claims["exp"] <= oat + 3600

    @pytest.mark.asyncio
    async def test_access_ttl_unclamped_while_session_is_young(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)

        rotated = await rotate_refresh_token(
            pair.refresh_claims,
            store=store,
            secret=SECRET,
            access_ttl=900,
            max_session_lifetime=3600,
        )
        # Fresh session: the full access TTL (within clock-tick tolerance).
        assert rotated.access_claims["exp"] - rotated.access_claims["iat"] >= 899

    @pytest.mark.asyncio
    async def test_new_pair_carries_current_user_version(self):
        store = InMemoryRevocation()
        await store.bump_user_version("u")  # version now 1
        pair = create_token_pair("u", secret=SECRET, version=1)

        rotated = await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)
        assert rotated.access_claims["ver"] == 1

    @pytest.mark.asyncio
    async def test_stale_version_rejected(self):
        """Global logout: after bump_user_version, refresh tokens minted with
        the old version must not rotate into a fresh valid pair."""
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)  # ver=0
        await store.bump_user_version("u")  # version now 1 → pair is stale

        with pytest.raises(TokenRotationError, match="version"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)

    @pytest.mark.asyncio
    async def test_stale_version_rejected_in_mode_b(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        await store.bump_user_version("u")

        with pytest.raises(TokenRotationError, match="version"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, rotate=False)

    @pytest.mark.asyncio
    async def test_future_version_is_rejected(self):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        pair.refresh_claims["ver"] = 100

        with pytest.raises(TokenRotationError, match="version"):
            await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET)


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


class TestAccessTokenWithRevocationStore:
    """Issue #304: a revocation store auto-enables ``require_jti``.

    Access tokens must then carry a ``jti`` or every request is rejected.
    """

    def test_access_token_authenticates_when_revocation_is_configured(self):
        store = InMemoryRevocation()
        api = BoltAPI()

        @api.get(
            "/me",
            auth=[JWTAuthentication(secret=SECRET, revocation_store=store)],
            guards=[IsAuthenticated()],
        )
        async def me(request):
            return {"sub": request["context"]["auth_claims"]["sub"]}

        pair = create_token_pair("user-304", secret=SECRET)
        with TestClient(api) as c:
            r = c.get("/me", headers={"Authorization": f"Bearer {pair.access_token}"})
            assert r.status_code == 200, r.text
            assert r.json() == {"sub": "user-304"}

    def test_access_token_can_be_revoked_by_jti(self):
        store = InMemoryRevocation()
        api = BoltAPI()

        @api.get(
            "/me",
            auth=[JWTAuthentication(secret=SECRET, revocation_store=store)],
            guards=[IsAuthenticated()],
        )
        async def me(request):
            return {"ok": True}

        pair = create_token_pair("user-304", secret=SECRET)
        asyncio.run(store.revoke(pair.access_claims["jti"]))
        with TestClient(api) as c:
            r = c.get("/me", headers={"Authorization": f"Bearer {pair.access_token}"})
            assert r.status_code == 401

    @pytest.mark.parametrize("rotate", [True, False])
    def test_rotated_access_token_has_fresh_jti(self, rotate):
        store = InMemoryRevocation()
        pair = create_token_pair("u", secret=SECRET)
        new = asyncio.run(rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, rotate=rotate))
        assert new.access_claims["jti"]
        assert new.access_claims["jti"] != pair.access_claims["jti"]


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
            claims = request["context"]["auth_claims"]
            return {"typ": claims["typ"], "amr": claims.get("amr")}

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

    def test_refresh_claims_preserve_amr_array_through_rust(self, client):
        pair = create_token_pair("u", secret=SECRET, method="pwd")
        r = client.request("POST", "/refresh", headers={"Authorization": f"Bearer {pair.refresh_token}"})
        assert r.status_code == 200
        assert r.json()["amr"] == ["pwd"]
