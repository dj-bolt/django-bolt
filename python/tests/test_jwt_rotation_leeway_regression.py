"""A refresh replay marker must survive every JWT validation leeway window."""

from __future__ import annotations

import time

import pytest

from django_bolt.auth import InMemoryRevocation, TokenRotationError, create_token_pair, rotate_refresh_token

SECRET = "a-long-test-secret-for-jwt-rotation-leeway"


class _RecordingStore(InMemoryRevocation):
    def __init__(self):
        super().__init__()
        self.consumed_exp = None
        self.family_exp = None

    async def consume(self, jti, *, exp=None):
        self.consumed_exp = exp
        return await super().consume(jti, exp=exp)

    async def revoke_family(self, fam, *, exp=None):
        self.family_exp = exp
        await super().revoke_family(fam, exp=exp)


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds_until_exp", [5, 3600], ids=["near-expiry", "far-from-expiry"])
async def test_consumed_refresh_marker_survives_exp_plus_leeway(seconds_until_exp):
    """Before the fix the marker could expire while Rust still accepts JWT."""
    store = _RecordingStore()
    pair = create_token_pair("user", secret=SECRET)
    pair.refresh_claims["exp"] = int(time.time()) + seconds_until_exp

    await rotate_refresh_token(pair.refresh_claims, store=store, secret=SECRET, leeway=90)

    assert store.consumed_exp >= pair.refresh_claims["exp"] + 90


@pytest.mark.asyncio
@pytest.mark.parametrize("original_lifetime", [100, 5000], ids=["short-parent", "long-parent"])
async def test_burned_family_survives_latest_descendant_and_leeway(original_lifetime):
    """Reuse must burn the entire family for the longest valid token window."""
    store = _RecordingStore()
    pair = create_token_pair("user", secret=SECRET)
    pair.refresh_claims["exp"] = int(time.time()) + original_lifetime
    successor = await rotate_refresh_token(
        pair.refresh_claims,
        store=store,
        secret=SECRET,
        refresh_ttl=1000,
        leeway=90,
    )

    with pytest.raises(TokenRotationError, match="already used"):
        await rotate_refresh_token(
            pair.refresh_claims,
            store=store,
            secret=SECRET,
            refresh_ttl=1000,
            leeway=90,
        )

    assert store.family_exp >= max(pair.refresh_claims["exp"], successor.refresh_claims["exp"]) + 90
