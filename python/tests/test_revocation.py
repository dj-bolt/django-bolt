from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from django_bolt.auth.revocation import (
    _DEFAULT_TTL_SECONDS,
    DjangoCacheRevocation,
    DjangoORMRevocation,
    InMemoryRevocation,
    _ttl_for,
)

# ---------------------------------------------------------------------------
# InMemoryRevocation
# ---------------------------------------------------------------------------


def test_in_memory_revoke_and_check():
    store = InMemoryRevocation()
    jti = "token-abc"

    assert asyncio.run(store.is_revoked(jti)) is False
    asyncio.run(store.revoke(jti))
    assert asyncio.run(store.is_revoked(jti)) is True


def test_in_memory_clear():
    store = InMemoryRevocation()
    asyncio.run(store.revoke("tok1"))
    asyncio.run(store.revoke("tok2"))
    store.clear()
    assert asyncio.run(store.is_revoked("tok1")) is False


def test_in_memory_accepts_exp_kwarg():
    """exp is silently ignored (no expiration mechanism), but the kwarg
    must be accepted so InMemoryRevocation is signature-compatible with
    the production stores."""
    store = InMemoryRevocation()
    asyncio.run(store.revoke("tok", exp=int(time.time()) + 3600))
    assert asyncio.run(store.is_revoked("tok")) is True


# ---------------------------------------------------------------------------
# _ttl_for — single source of truth for entry lifetime
# ---------------------------------------------------------------------------


def test_ttl_for_derives_from_future_exp():
    """exp in the future → seconds-until-exp."""
    exp = int(time.time()) + 3600
    resolved = _ttl_for(exp)
    # Allow ±2s drift between the time.time() inside _ttl_for and the
    # one in the test setup
    assert 3598 <= resolved <= 3600


def test_ttl_for_clamps_past_exp_to_zero():
    """An already-expired token must produce 0, never a negative value
    (timedelta(seconds=-N) would silently subtract from now)."""
    past = int(time.time()) - 3600
    assert _ttl_for(past) == 0


def test_ttl_for_falls_back_to_default_when_exp_none():
    """No exp → falls back to the supplied default."""
    assert _ttl_for(None) == _DEFAULT_TTL_SECONDS


def test_ttl_for_uses_supplied_default():
    """Stores pass their own per-instance default_ttl through to the resolver."""
    assert _ttl_for(None, default=12345) == 12345


# ---------------------------------------------------------------------------
# DjangoCacheRevocation
# ---------------------------------------------------------------------------


def _make_cache_revocation(cache_alias="default"):
    store = DjangoCacheRevocation(cache_alias=cache_alias)
    mock_cache = MagicMock()
    store._cache = mock_cache
    return store, mock_cache


def test_cache_revocation_no_exp_uses_default():
    """revoke(jti) with no exp → cache timeout = 30-day default."""
    store, mock_cache = _make_cache_revocation()
    asyncio.run(store.revoke("jti-1"))
    mock_cache.set.assert_called_once_with("revoked:jti-1", "1", timeout=_DEFAULT_TTL_SECONDS)


def test_cache_revocation_exp_drives_timeout():
    """revoke(jti, exp=future) → cache timeout ≈ exp - now."""
    store, mock_cache = _make_cache_revocation()
    exp = int(time.time()) + 3600
    asyncio.run(store.revoke("jti-2", exp=exp))

    _, call_kwargs = mock_cache.set.call_args
    timeout = call_kwargs["timeout"]
    assert 3598 <= timeout <= 3600


def test_cache_revocation_past_exp_clamps_to_zero():
    """An exp in the past → cache timeout=0 (not the 30-day default,
    not a negative value)."""
    store, mock_cache = _make_cache_revocation()
    past = int(time.time()) - 3600
    asyncio.run(store.revoke("jti-3", exp=past))
    mock_cache.set.assert_called_once_with("revoked:jti-3", "1", timeout=0)


def test_cache_revocation_per_instance_default_ttl():
    """default_ttl on the constructor overrides the module default for
    this store instance only. Verifies the per-instance override path."""
    store = DjangoCacheRevocation(default_ttl=900)  # 15 min
    mock_cache = MagicMock()
    store._cache = mock_cache
    asyncio.run(store.revoke("jti-tt"))
    mock_cache.set.assert_called_once_with("revoked:jti-tt", "1", timeout=900)


def test_cache_revocation_per_call_exp_beats_per_instance_default():
    """Per-call exp must override the per-instance default_ttl — exp is
    always more accurate than a static fallback."""
    store = DjangoCacheRevocation(default_ttl=900)
    mock_cache = MagicMock()
    store._cache = mock_cache
    asyncio.run(store.revoke("jti-tt2", exp=int(time.time()) + 3600))

    _, call_kwargs = mock_cache.set.call_args
    timeout = call_kwargs["timeout"]
    assert 3598 <= timeout <= 3600


# ---------------------------------------------------------------------------
# DjangoORMRevocation – revoke() behavior
# ---------------------------------------------------------------------------


def _make_orm_revocation(model_path="myapp.RevokedToken"):
    store = DjangoORMRevocation(model=model_path)

    mock_model = MagicMock()
    mock_model.objects.aupdate_or_create = AsyncMock()
    store._model = mock_model
    return store, mock_model


def test_orm_revocation_no_exp_uses_default():
    """revoke(jti) with no exp → expires_at ≈ now + 30 days."""
    store, mock_model = _make_orm_revocation()
    before = datetime.now(UTC)
    asyncio.run(store.revoke("jti-orm-1"))
    after = datetime.now(UTC)

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    expected_min = before + timedelta(seconds=_DEFAULT_TTL_SECONDS)
    expected_max = after + timedelta(seconds=_DEFAULT_TTL_SECONDS)
    assert expected_min <= expires_at <= expected_max


def test_orm_revocation_exp_drives_expires_at():
    """revoke(jti, exp=future) → expires_at ≈ datetime(exp)."""
    store, mock_model = _make_orm_revocation()
    exp = int(time.time()) + 7200
    asyncio.run(store.revoke("jti-orm-2", exp=exp))

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    expected = datetime.fromtimestamp(exp, tz=UTC)
    # ±2s for clock drift between time.time() in _ttl_for and datetime.now()
    # in revoke
    assert abs((expires_at - expected).total_seconds()) <= 2


def test_orm_revocation_past_exp_clamps_to_now():
    """An exp already in the past → expires_at ≈ now (row is immediately
    eligible for cleanup; is_revoked still returns True until cleanup
    runs)."""
    store, mock_model = _make_orm_revocation()
    before = datetime.now(UTC)
    asyncio.run(store.revoke("jti-orm-3", exp=int(time.time()) - 3600))
    after = datetime.now(UTC)

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    assert before <= expires_at <= after + timedelta(seconds=1)


def test_orm_revocation_per_instance_default_ttl():
    """default_ttl on the constructor overrides the module default for
    this store instance only."""
    store = DjangoORMRevocation(model="myapp.RevokedToken", default_ttl=900)
    mock_model = MagicMock()
    mock_model.objects.aupdate_or_create = AsyncMock()
    store._model = mock_model

    before = datetime.now(UTC)
    asyncio.run(store.revoke("jti-tt"))
    after = datetime.now(UTC)

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    expected_min = before + timedelta(seconds=900)
    expected_max = after + timedelta(seconds=900)
    assert expected_min <= expires_at <= expected_max


# ---------------------------------------------------------------------------
# DjangoORMRevocation – model path validation
# ---------------------------------------------------------------------------


def test_orm_revocation_valid_model_path():
    """A two-part model path 'app.Model' should work without error."""
    store = DjangoORMRevocation(model="myapp.RevokedToken")
    mock_model = MagicMock()
    with patch("django_bolt.auth.revocation.apps.get_model", return_value=mock_model):
        result = store.model
    assert result is mock_model


@pytest.mark.parametrize(
    "bad_path",
    [
        "myapp.models.RevokedToken",  # 3 parts
        "RevokedToken",  # no dot
        "myapp.",  # trailing dot, empty model name
        ".RevokedToken",  # leading dot, empty app label
        "",  # empty string
    ],
)
def test_orm_revocation_invalid_model_path_raises_clear_error(bad_path):
    """Invalid model paths must raise a ValueError naming the expected format.

    Without validation, `.split(".")` either raises a cryptic
    `ValueError: too many values to unpack` for >2 parts, or silently
    accepts empty parts that then fail deep inside `apps.get_model`.
    """
    store = DjangoORMRevocation(model=bad_path)
    with pytest.raises(ValueError, match="app_label.ModelName"):
        _ = store.model
