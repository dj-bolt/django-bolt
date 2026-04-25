from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# InMemoryRevocation
# ---------------------------------------------------------------------------


def test_in_memory_revoke_and_check():
    from django_bolt.auth.revocation import InMemoryRevocation

    store = InMemoryRevocation()
    jti = "token-abc"

    assert asyncio.run(store.is_revoked(jti)) is False
    asyncio.run(store.revoke(jti))
    assert asyncio.run(store.is_revoked(jti)) is True


def test_in_memory_clear():
    from django_bolt.auth.revocation import InMemoryRevocation

    store = InMemoryRevocation()
    asyncio.run(store.revoke("tok1"))
    asyncio.run(store.revoke("tok2"))
    store.clear()
    assert asyncio.run(store.is_revoked("tok1")) is False


# ---------------------------------------------------------------------------
# DjangoCacheRevocation – TTL handling
# ---------------------------------------------------------------------------


def _make_cache_revocation(cache_alias="default"):
    from django_bolt.auth.revocation import DjangoCacheRevocation

    store = DjangoCacheRevocation(cache_alias=cache_alias)
    mock_cache = MagicMock()
    store._cache = mock_cache
    return store, mock_cache


def test_cache_revocation_ttl_none_uses_default():
    """ttl=None should store with 30-day timeout."""
    store, mock_cache = _make_cache_revocation()
    asyncio.run(store.revoke("jti-1", ttl=None))
    mock_cache.set.assert_called_once_with("revoked:jti-1", "1", timeout=86400 * 30)


def test_cache_revocation_ttl_positive_uses_given_value():
    """A positive ttl should be passed directly to the cache."""
    store, mock_cache = _make_cache_revocation()
    asyncio.run(store.revoke("jti-2", ttl=3600))
    mock_cache.set.assert_called_once_with("revoked:jti-2", "1", timeout=3600)


def test_cache_revocation_ttl_zero_uses_zero_not_default():
    """ttl=0 must be stored with timeout=0, NOT the 30-day default.

    Bug: `ttl or (86400 * 30)` evaluates `0 or 2592000 = 2592000`
    because Python treats 0 as falsy. The fix uses `ttl if ttl is not None`.
    """
    store, mock_cache = _make_cache_revocation()
    asyncio.run(store.revoke("jti-3", ttl=0))
    mock_cache.set.assert_called_once_with("revoked:jti-3", "1", timeout=0)


# ---------------------------------------------------------------------------
# DjangoORMRevocation – TTL handling
# ---------------------------------------------------------------------------


def _make_orm_revocation(model_path="myapp.RevokedToken"):
    from django_bolt.auth.revocation import DjangoORMRevocation

    store = DjangoORMRevocation(model=model_path)

    mock_model = MagicMock()
    mock_model.objects.aupdate_or_create = AsyncMock()
    store._model = mock_model
    return store, mock_model


def test_orm_revocation_ttl_none_uses_default():
    """ttl=None should compute expires_at = now + 30 days."""
    store, mock_model = _make_orm_revocation()
    before = datetime.now(UTC)
    asyncio.run(store.revoke("jti-orm-1", ttl=None))
    after = datetime.now(UTC)

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    expected_min = before + timedelta(seconds=86400 * 30)
    expected_max = after + timedelta(seconds=86400 * 30)
    assert expected_min <= expires_at <= expected_max


def test_orm_revocation_ttl_positive():
    """A positive ttl should compute expires_at = now + ttl seconds."""
    store, mock_model = _make_orm_revocation()
    before = datetime.now(UTC)
    asyncio.run(store.revoke("jti-orm-2", ttl=7200))
    after = datetime.now(UTC)

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    expected_min = before + timedelta(seconds=7200)
    expected_max = after + timedelta(seconds=7200)
    assert expected_min <= expires_at <= expected_max


def test_orm_revocation_ttl_zero_computes_now_not_30_days():
    """ttl=0 must compute expires_at ~= now, NOT now + 30 days.

    Bug: `ttl or 86400 * 30` maps 0 → 2592000. The fix uses
    `ttl if ttl is not None else 86400 * 30`.
    """
    store, mock_model = _make_orm_revocation()
    before = datetime.now(UTC)
    asyncio.run(store.revoke("jti-orm-3", ttl=0))
    after = datetime.now(UTC)

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    # expires_at should be approximately now (within a few seconds), NOT 30 days from now
    assert before <= expires_at <= after + timedelta(seconds=1)
    assert expires_at < before + timedelta(days=1)


# ---------------------------------------------------------------------------
# DjangoORMRevocation – model path validation
# ---------------------------------------------------------------------------


def test_orm_revocation_valid_model_path():
    """A two-part model path 'app.Model' should work without error."""
    from django_bolt.auth.revocation import DjangoORMRevocation

    store = DjangoORMRevocation(model="myapp.RevokedToken")
    mock_model = MagicMock()
    with patch("django_bolt.auth.revocation.apps.get_model", return_value=mock_model):
        result = store.model
    assert result is mock_model


def test_orm_revocation_three_part_path_raises_clear_error():
    """A three-part path like 'myapp.models.RevokedToken' must raise a clear ValueError.

    Bug: `app_label, model_name = self.model_path.split(".")` raises
    `ValueError: too many values to unpack` — cryptic, gives no guidance.
    The fix validates the parts count and raises a descriptive message.
    """
    from django_bolt.auth.revocation import DjangoORMRevocation

    store = DjangoORMRevocation(model="myapp.models.RevokedToken")
    with pytest.raises(ValueError, match="app_label.ModelName"):
        _ = store.model
