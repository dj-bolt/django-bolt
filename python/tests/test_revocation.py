from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.db import IntegrityError, connection, models

from django_bolt.auth.revocation import (
    _DEFAULT_TTL_SECONDS,
    DjangoCacheRevocation,
    DjangoORMRevocation,
    InMemoryRevocation,
    RevocationStore,
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


def test_in_memory_consume_is_single_use():
    store = InMemoryRevocation()
    assert asyncio.run(store.consume("tok")) is True
    assert asyncio.run(store.consume("tok")) is False


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


def test_cache_consume_uses_atomic_add():
    store, mock_cache = _make_cache_revocation()
    mock_cache.add.side_effect = [True, False]

    assert asyncio.run(store.consume("single-use")) is True
    assert asyncio.run(store.consume("single-use")) is False
    assert mock_cache.add.call_count == 2


def test_cache_first_version_bump_seeds_with_atomic_add():
    store, mock_cache = _make_cache_revocation()
    mock_cache.incr.side_effect = [ValueError, 2]
    mock_cache.add.return_value = False

    assert asyncio.run(store.bump_user_version("u")) == 2
    mock_cache.add.assert_called_once_with("revoked:ver:u", 1, timeout=None)


def test_file_cache_is_rejected_for_atomic_operations():
    store = DjangoCacheRevocation()

    class FileCache:
        def add(self, *args, **kwargs):
            return True

    FileCache.__module__ = "django.core.cache.backends.filebased"
    mock_cache = FileCache()
    store._cache = mock_cache

    with pytest.raises(NotImplementedError, match="atomic"):
        asyncio.run(store.consume("tok"))


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


def test_orm_consume_returns_false_on_duplicate():
    store, mock_model = _make_orm_revocation()
    mock_model.objects.acreate = AsyncMock(side_effect=[None, IntegrityError])

    assert asyncio.run(store.consume("single-use")) is True
    assert asyncio.run(store.consume("single-use")) is False


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


# ---------------------------------------------------------------------------
# RevocationStore – optional operations
# ---------------------------------------------------------------------------


class MinimalStore(RevocationStore):
    """A store that implements only the two required methods."""

    async def is_revoked(self, jti):
        return False

    async def revoke(self, jti, *, exp=None):
        pass


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda s: s.revoke_all("u"), "This revocation store does not support revoke_all"),
        (lambda s: s.consume("j"), "This revocation store does not support atomic token consumption"),
        (lambda s: s.get_user_version("u"), "This revocation store does not support token versioning"),
        (lambda s: s.bump_user_version("u"), "This revocation store does not support token versioning"),
        (lambda s: s.revoke_family("f"), "This revocation store does not support family revocation"),
        (lambda s: s.is_family_revoked("f"), "This revocation store does not support family revocation"),
    ],
    ids=["revoke_all", "consume", "get_user_version", "bump_user_version", "revoke_family", "is_family_revoked"],
)
def test_optional_operations_raise_not_implemented_error(call, message):
    """The token functions rely on this error to find out what a store supports."""
    with pytest.raises(NotImplementedError) as raised:
        asyncio.run(call(MinimalStore()))

    assert str(raised.value) == message


# ---------------------------------------------------------------------------
# DjangoCacheRevocation – behavior on a real cache
# ---------------------------------------------------------------------------


class SeedRaceCache(LocMemCache):
    """A cache where another caller always wins the race to ``add`` a key.

    The other caller sets the key to 4 before the ``add`` of this cache fails.
    """

    def add(self, key, value, timeout=None, version=None):
        self.set(key, 4, timeout=None, version=version)
        return False


@pytest.fixture
def cache_store(settings):
    """Build a ``DjangoCacheRevocation`` factory that uses an empty local-memory cache."""
    settings.CACHES = {
        **settings.CACHES,
        "revocation": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "revocation-tests"},
        "revocation-race": {"BACKEND": "tests.test_revocation.SeedRaceCache", "LOCATION": "revocation-race-tests"},
    }
    for alias in ("revocation", "revocation-race"):
        caches[alias].clear()

    def build(alias="revocation", **kwargs):
        return DjangoCacheRevocation(cache_alias=alias, **kwargs)

    return build


def test_cache_store_uses_the_default_cache_unless_told_otherwise():
    assert DjangoCacheRevocation().cache is caches["default"]


def test_cache_consume_is_single_use_for_each_jti(cache_store):
    store = cache_store()

    assert asyncio.run(store.consume("a")) is True
    assert asyncio.run(store.consume("a")) is False
    assert asyncio.run(store.consume("b")) is True


def test_cache_consumed_token_counts_as_revoked(cache_store):
    """A consumed refresh token must fail the revocation check at once."""
    store = cache_store()

    asyncio.run(store.consume("a"))

    assert asyncio.run(store.is_revoked("a")) is True
    assert asyncio.run(store.is_revoked("b")) is False
    assert caches["revocation"].get("revoked:a") == "1"


FUTURE = 3600
PAST = -3600


@pytest.mark.parametrize(
    ("ttl", "exp_offset", "second_call_is_blocked"),
    [
        (None, FUTURE, True),
        (None, PAST, False),
        (0, None, False),
        (900, None, True),
    ],
    ids=["exp-in-future", "exp-in-past", "default-ttl-zero", "default-ttl-900"],
)
def test_cache_consume_marker_lifetime(cache_store, ttl, exp_offset, second_call_is_blocked):
    """The marker lives until ``exp``. With no ``exp`` it lives for ``default_ttl`` seconds."""
    store = cache_store(**({} if ttl is None else {"default_ttl": ttl}))
    exp = None if exp_offset is None else int(time.time()) + exp_offset

    assert asyncio.run(store.consume("a", exp=exp)) is True
    assert asyncio.run(store.consume("a", exp=exp)) is (not second_call_is_blocked)


@pytest.mark.parametrize(
    ("ttl", "exp_offset", "is_revoked_after"),
    [
        (None, FUTURE, True),
        (None, PAST, False),
        (0, None, False),
        (900, None, True),
    ],
    ids=["exp-in-future", "exp-in-past", "default-ttl-zero", "default-ttl-900"],
)
def test_cache_family_marker_lifetime(cache_store, ttl, exp_offset, is_revoked_after):
    store = cache_store(**({} if ttl is None else {"default_ttl": ttl}))
    exp = None if exp_offset is None else int(time.time()) + exp_offset

    asyncio.run(store.revoke_family("f", exp=exp))

    assert asyncio.run(store.is_family_revoked("f")) is is_revoked_after


def test_cache_family_revocation_is_per_family(cache_store):
    store = cache_store()

    assert asyncio.run(store.is_family_revoked("f")) is False
    asyncio.run(store.revoke_family("f"))

    assert asyncio.run(store.is_family_revoked("f")) is True
    assert asyncio.run(store.is_family_revoked("g")) is False
    # A family and a token with the same name are not the same thing.
    assert asyncio.run(store.is_revoked("f")) is False


def test_cache_user_version_counts_bumps_for_each_user(cache_store):
    store = cache_store()

    assert asyncio.run(store.get_user_version("u")) == 0
    assert asyncio.run(store.bump_user_version("u")) == 1
    assert asyncio.run(store.bump_user_version("u")) == 2
    assert asyncio.run(store.get_user_version("u")) == 2
    assert asyncio.run(store.get_user_version("other")) == 0
    assert asyncio.run(store.bump_user_version("other")) == 1
    assert asyncio.run(store.get_user_version("u")) == 2


def test_cache_user_version_bump_keeps_the_count_of_the_caller_that_won_the_seed_race(cache_store):
    """Two first bumps can race. The loser adds one to the version that the winner set."""
    store = cache_store("revocation-race")

    assert asyncio.run(store.bump_user_version("u")) == 5
    assert asyncio.run(store.get_user_version("u")) == 5


@pytest.mark.parametrize(
    "backend",
    [
        "django.core.cache.backends.dummy.DummyCache",
        "django.core.cache.backends.filebased.FileBasedCache",
        "django.core.cache.backends.db.DatabaseCache",
    ],
    ids=["dummy", "filebased", "database"],
)
def test_cache_without_atomic_operations_is_rejected(settings, tmp_path, backend):
    """A rotation or a version bump on a cache with a racy ``add`` or ``incr`` fails at once."""
    location = "revocation_cache_table" if backend.endswith("DatabaseCache") else str(tmp_path)
    settings.CACHES = {**settings.CACHES, "plain": {"BACKEND": backend, "LOCATION": location}}
    store = DjangoCacheRevocation(cache_alias="plain")
    message = (
        "^DjangoCacheRevocation refresh rotation and version bumps require a cache backend with atomic "
        "add/incr operations; file-based, database, dummy, and other non-atomic caches are not supported$"
    )

    with pytest.raises(NotImplementedError, match=message):
        asyncio.run(store.consume("a"))
    with pytest.raises(NotImplementedError, match=message):
        asyncio.run(store.bump_user_version("u"))


# ---------------------------------------------------------------------------
# DjangoORMRevocation – behavior on a real model
# ---------------------------------------------------------------------------


class RevokedTokenRow(models.Model):
    """The model that the docstring of ``DjangoORMRevocation`` asks the project to define."""

    jti = models.CharField(max_length=255, unique=True)
    revoked_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        app_label = "django_bolt"


@pytest.fixture
def orm_store(transactional_db):
    """Build a ``DjangoORMRevocation`` factory on a real table. Drop the table after the test."""
    table = RevokedTokenRow._meta.db_table
    with connection.schema_editor() as schema_editor:
        if table not in connection.introspection.table_names():
            schema_editor.create_model(RevokedTokenRow)
    try:
        yield lambda **kwargs: DjangoORMRevocation(model="django_bolt.RevokedTokenRow", **kwargs)
    finally:
        with connection.schema_editor() as schema_editor:
            if table in connection.introspection.table_names():
                schema_editor.delete_model(RevokedTokenRow)


def _expires_in(jti: str) -> float:
    """Seconds from now until the stored ``expires_at`` of a row."""
    row = RevokedTokenRow.objects.get(jti=jti)
    return (row.expires_at - datetime.now(UTC)).total_seconds()


def test_orm_revoke_marks_only_that_token(orm_store):
    store = orm_store()

    assert asyncio.run(store.is_revoked("a")) is False
    asyncio.run(store.revoke("a"))
    asyncio.run(store.revoke("a"))

    assert asyncio.run(store.is_revoked("a")) is True
    assert asyncio.run(store.is_revoked("b")) is False
    assert RevokedTokenRow.objects.filter(jti="a").count() == 1


def test_orm_consume_is_single_use_for_each_jti(orm_store):
    store = orm_store()

    assert asyncio.run(store.consume("a")) is True
    assert asyncio.run(store.consume("a")) is False
    assert asyncio.run(store.consume("b")) is True
    assert asyncio.run(store.is_revoked("a")) is True
    assert RevokedTokenRow.objects.filter(jti="a").count() == 1


@pytest.mark.parametrize(
    ("default_ttl", "exp_offset", "expected"),
    [
        (None, FUTURE, FUTURE),
        (None, PAST, 0),
        (None, None, _DEFAULT_TTL_SECONDS),
        (900, None, 900),
    ],
    ids=["exp-in-future", "exp-in-past", "module-default", "default-ttl-900"],
)
def test_orm_consume_row_expires_with_the_token(orm_store, default_ttl, exp_offset, expected):
    store = orm_store(**({} if default_ttl is None else {"default_ttl": default_ttl}))
    exp = None if exp_offset is None else int(time.time()) + exp_offset

    assert asyncio.run(store.consume("a", exp=exp)) is True

    assert _expires_in("a") == pytest.approx(expected, abs=3)


def test_orm_family_revocation_is_per_family(orm_store):
    store = orm_store()

    assert asyncio.run(store.is_family_revoked("f")) is False
    asyncio.run(store.revoke_family("f", exp=int(time.time()) + FUTURE))

    assert asyncio.run(store.is_family_revoked("f")) is True
    assert asyncio.run(store.is_family_revoked("g")) is False
    # A family and a token with the same name are not the same thing.
    assert asyncio.run(store.is_revoked("f")) is False
    assert asyncio.run(store.is_revoked("family:f")) is True
    assert _expires_in("family:f") == pytest.approx(FUTURE, abs=3)
