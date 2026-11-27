from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from django.core.cache import caches
from django.core.cache.backends.db import DatabaseCache
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.memcached import BaseMemcachedCache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import IntegrityError, connection

from django_bolt import BoltAPI
from django_bolt.auth import JWTAuthentication, JWTAuthViews, TokenRotationError, rotate_refresh_token
from django_bolt.auth import revocation as revocation_module
from django_bolt.auth.revocation import (
    _DEFAULT_TTL_SECONDS,
    DjangoCacheRevocation,
    DjangoORMRevocation,
    InMemoryRevocation,
    ReservedJtiError,
    RevocationStore,
    _ttl_for,
)
from django_bolt.auth.revocation_db.models import RevokedToken
from django_bolt.testing import TestClient

from .test_models import CaseInsensitiveRevokedToken

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


@pytest.mark.parametrize(("leeway", "expected"), [(0, 3601), (60, 3661)], ids=["no-leeway", "leeway-60"])
def test_ttl_for_derives_from_future_exp(leeway, expected):
    """exp in the future → seconds until the end of the second ``exp + leeway``."""
    exp = int(time.time()) + 3600
    resolved = _ttl_for(exp, leeway=leeway)
    # Allow ±2s drift between the time.time() inside _ttl_for and the
    # one in the test setup
    assert expected - 2 <= resolved <= expected


@pytest.mark.parametrize("leeway", [0, 60])
def test_ttl_for_clamps_past_exp_to_zero(leeway):
    """A token that is after ``exp + leeway`` must produce 0, never a negative
    value (timedelta(seconds=-N) would silently subtract from now)."""
    past = int(time.time()) - 3600
    assert _ttl_for(past, leeway=leeway) == 0


def test_ttl_for_keeps_an_entry_for_a_token_inside_the_leeway():
    """The backend still accepts a token 10 seconds after ``exp``. Its entry must live."""
    just_expired = int(time.time()) - 10
    assert 49 <= _ttl_for(just_expired, leeway=60) <= 51
    assert _ttl_for(just_expired, leeway=0) == 0


def test_ttl_for_ends_an_entry_in_the_last_year_of_a_datetime():
    """``exp`` plus the leeway can pass 9999-12-31. ``datetime`` then raised ``OverflowError``."""
    last = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    for exp in (253402300799, 2**62, 10**300):
        assert datetime.now(UTC) + timedelta(seconds=_ttl_for(exp, leeway=60)) <= last


def test_orm_store_keeps_an_entry_for_an_exp_in_the_last_second_of_a_datetime(orm_store):
    store = orm_store(leeway=60)
    asyncio.run(store.revoke("a", exp=253402300799))
    assert asyncio.run(store.consume("b", exp=253402300799)) is True
    assert asyncio.run(store.is_revoked("a")) is True
    assert asyncio.run(store.consume("b", exp=253402300799)) is False
    assert RevokedToken.objects.get(jti="a").expires_at.year == 9999


def test_ttl_for_falls_back_to_default_when_exp_none():
    """No exp → falls back to the supplied default. The leeway does not change it."""
    assert _ttl_for(None, leeway=60) == _DEFAULT_TTL_SECONDS


def test_ttl_for_uses_supplied_default():
    """Stores pass their own per-instance default_ttl through to the resolver."""
    assert _ttl_for(None, default=12345, leeway=60) == 12345


# ---------------------------------------------------------------------------
# DjangoCacheRevocation
# ---------------------------------------------------------------------------


def _make_cache_revocation(cache_alias="default"):
    store = DjangoCacheRevocation(cache_alias=cache_alias)
    mock_cache = MagicMock()
    store._cache = mock_cache
    return store, mock_cache


def test_cache_revocation_no_exp_uses_default():
    """revoke(jti) with no exp → cache timeout = 7-day default."""
    store, mock_cache = _make_cache_revocation()
    asyncio.run(store.revoke("jti-1"))
    mock_cache.set.assert_called_once_with("revoked:jti-1", "1", timeout=_DEFAULT_TTL_SECONDS)


def test_cache_revocation_exp_drives_timeout():
    """revoke(jti, exp=future) → cache timeout ≈ exp + leeway + 1 - now."""
    store, mock_cache = _make_cache_revocation()
    exp = int(time.time()) + 3600
    asyncio.run(store.revoke("jti-2", exp=exp))

    _, call_kwargs = mock_cache.set.call_args
    timeout = call_kwargs["timeout"]
    assert 3659 <= timeout <= 3661


def test_cache_revocation_past_exp_clamps_to_zero():
    """An exp before now - leeway → cache timeout=0 (not the 7-day default,
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
    assert 3659 <= timeout <= 3661


@pytest.mark.parametrize("store_class", [DjangoCacheRevocation, DjangoORMRevocation])
def test_store_rejects_a_negative_leeway(store_class):
    with pytest.raises(ImproperlyConfigured) as raised:
        store_class(leeway=-1)

    assert str(raised.value) == f"{store_class.__name__} leeway must be >= 0 seconds."


@pytest.mark.parametrize("store_class", [DjangoCacheRevocation, DjangoORMRevocation])
def test_default_leeway_of_a_store_is_the_default_leeway_of_the_backend(store_class):
    assert store_class().leeway == JWTAuthentication(secret="s" * 32).leeway == 60


def test_jwt_backend_rejects_a_store_with_a_smaller_leeway():
    """An entry of that store would expire while the backend still accepts the token."""
    with pytest.raises(ImproperlyConfigured) as raised:
        JWTAuthentication(secret="s" * 32, leeway=120, revocation_store=DjangoCacheRevocation())

    assert str(raised.value) == (
        "JWTAuthentication leeway is 120 seconds, but its revocation_store has a leeway of 60 seconds. "
        "A revoked token would be accepted again before it expires. Pass leeway=120 to the store."
    )
    # An equal or larger leeway of the store is correct. So is a store that keeps each entry.
    assert (
        JWTAuthentication(secret="s" * 32, leeway=120, revocation_store=DjangoORMRevocation(leeway=120)).leeway == 120
    )
    assert JWTAuthentication(secret="s" * 32, leeway=0, revocation_store=DjangoCacheRevocation()).leeway == 0
    assert JWTAuthentication(secret="s" * 32, leeway=120, revocation_store=InMemoryRevocation()).leeway == 120


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
    """revoke(jti) with no exp → expires_at ≈ now + 7 days."""
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
    """revoke(jti, exp=future) → expires_at ≈ datetime(exp + leeway)."""
    store, mock_model = _make_orm_revocation()
    exp = int(time.time()) + 7200
    asyncio.run(store.revoke("jti-orm-2", exp=exp))

    _, call_kwargs = mock_model.objects.aupdate_or_create.call_args
    expires_at = call_kwargs["defaults"]["expires_at"]

    expected = datetime.fromtimestamp(exp + 60, tz=UTC)
    # ±2s for clock drift between time.time() in _ttl_for and datetime.now()
    # in revoke
    assert abs((expires_at - expected).total_seconds()) <= 2


def test_orm_revocation_past_exp_clamps_to_now():
    """An exp before now - leeway → expires_at ≈ now (row is immediately
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
# 10 seconds after ``exp``: the default leeway of 60 seconds still accepts the token.
IN_LEEWAY = -10


@pytest.fixture
def frozen_now(monkeypatch):
    """Stop ``time.time`` at the start of a second, so that ``exp == now`` stays true in the test."""
    now = float(int(time.time()))
    monkeypatch.setattr(time, "time", lambda: now)
    return int(now)


_CACHE_MARKER_CASES = pytest.mark.parametrize(
    ("kwargs", "exp_offset", "marker_lives"),
    [
        ({}, FUTURE, True),
        ({}, PAST, False),
        ({}, IN_LEEWAY, True),
        ({"leeway": 0}, IN_LEEWAY, False),
        ({"leeway": 0}, 0, True),
        ({"default_ttl": 0}, None, False),
        ({"default_ttl": 900}, None, True),
    ],
    ids=[
        "exp-in-future",
        "exp-in-past",
        "exp-inside-leeway",
        "exp-in-past-no-leeway",
        "exp-now-no-leeway",
        "default-ttl-zero",
        "default-ttl-900",
    ],
)


@_CACHE_MARKER_CASES
def test_cache_consume_marker_lifetime(cache_store, frozen_now, kwargs, exp_offset, marker_lives):
    """The marker lives until ``exp + leeway``. With no ``exp`` it lives for ``default_ttl`` seconds."""
    store = cache_store(**kwargs)
    exp = None if exp_offset is None else frozen_now + exp_offset

    # A consume that cannot keep its marker refuses the token, on each call.
    assert asyncio.run(store.consume("a", exp=exp)) is marker_lives
    assert asyncio.run(store.consume("a", exp=exp)) is False


@_CACHE_MARKER_CASES
def test_cache_family_marker_lifetime(cache_store, frozen_now, kwargs, exp_offset, marker_lives):
    store = cache_store(**kwargs)
    exp = None if exp_offset is None else frozen_now + exp_offset

    asyncio.run(store.revoke_family("f", exp=exp))

    assert asyncio.run(store.is_family_revoked("f")) is marker_lives


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
# DjangoORMRevocation – behavior on the model of the shipped app
# ---------------------------------------------------------------------------


@pytest.fixture
def orm_store(transactional_db):
    """Build a ``DjangoORMRevocation`` factory on the table of ``django_bolt.auth.revocation_db``."""
    return lambda **kwargs: DjangoORMRevocation(**kwargs)


def test_revocation_db_migrations_match_the_model(db):
    """A model change without its migration fails here."""
    call_command("makemigrations", "bolt_revocation", "--check", "--dry-run", verbosity=0)


def _expires_in(jti: str) -> float:
    """Seconds from now until the stored ``expires_at`` of a row."""
    row = RevokedToken.objects.get(jti=jti)
    return (row.expires_at - datetime.now(UTC)).total_seconds()


def test_orm_revoke_marks_only_that_token(orm_store):
    store = orm_store()

    assert asyncio.run(store.is_revoked("a")) is False
    asyncio.run(store.revoke("a"))
    asyncio.run(store.revoke("a"))

    assert asyncio.run(store.is_revoked("a")) is True
    assert asyncio.run(store.is_revoked("b")) is False
    assert RevokedToken.objects.filter(jti="a").count() == 1


def test_orm_consume_is_single_use_for_each_jti(orm_store):
    store = orm_store()

    assert asyncio.run(store.consume("a")) is True
    assert asyncio.run(store.consume("a")) is False
    assert asyncio.run(store.consume("b")) is True
    assert asyncio.run(store.is_revoked("a")) is True
    assert RevokedToken.objects.filter(jti="a").count() == 1


@pytest.mark.parametrize(
    ("kwargs", "exp_offset", "expected"),
    [
        ({}, FUTURE, FUTURE + 60),
        ({"leeway": 0}, FUTURE, FUTURE),
        ({"leeway": 0}, 0, 1),
        ({}, IN_LEEWAY, 50),
        ({}, None, _DEFAULT_TTL_SECONDS),
        ({"default_ttl": 900}, None, 900),
    ],
    ids=[
        "exp-in-future",
        "exp-in-future-no-leeway",
        "exp-now-no-leeway",
        "exp-inside-leeway",
        "module-default",
        "default-ttl-900",
    ],
)
def test_orm_consume_row_expires_with_the_token(orm_store, frozen_now, kwargs, exp_offset, expected):
    """The row expires at ``exp + leeway``, the time when the backend stops accepting the token."""
    store = orm_store(**kwargs)
    exp = None if exp_offset is None else frozen_now + exp_offset

    assert asyncio.run(store.consume("a", exp=exp)) is True

    assert _expires_in("a") == pytest.approx(expected, abs=3)


@pytest.mark.parametrize(("kwargs", "exp_offset"), [({}, PAST), ({"leeway": 0}, IN_LEEWAY)])
def test_orm_consume_refuses_a_token_that_the_backend_no_longer_accepts(orm_store, kwargs, exp_offset):
    """The cleanup task deletes an expired row, so the row cannot make a later consume fail."""
    store = orm_store(**kwargs)
    exp = int(time.time()) + exp_offset

    assert asyncio.run(store.consume("a", exp=exp)) is False
    assert asyncio.run(store.consume("a", exp=exp)) is False
    assert not RevokedToken.objects.filter(jti="a").exists()


def test_orm_family_revocation_is_per_family(orm_store):
    store = orm_store()

    assert asyncio.run(store.is_family_revoked("f")) is False
    asyncio.run(store.revoke_family("f", exp=int(time.time()) + FUTURE))

    assert asyncio.run(store.is_family_revoked("f")) is True
    assert asyncio.run(store.is_family_revoked("g")) is False
    # A family and a token with the same name are not the same thing.
    assert asyncio.run(store.is_revoked("f")) is False
    assert _expires_in("family:f") == pytest.approx(FUTURE + 60, abs=3)


def test_orm_user_version_counts_each_logout_all(orm_store):
    store = orm_store()

    assert asyncio.run(store.get_user_version("u")) == 0
    assert asyncio.run(store.bump_user_version("u")) == 1
    assert asyncio.run(store.bump_user_version("u")) == 2

    assert asyncio.run(store.get_user_version("u")) == 2
    assert asyncio.run(store.get_user_version("v")) == 0
    # A user version is not a revoked token.
    assert asyncio.run(store.is_revoked("u")) is False


def test_orm_user_version_survives_the_cleanup_task(orm_store):
    """The documented cleanup task deletes expired rows. It must not set a version back to 0."""
    store = orm_store()
    asyncio.run(store.bump_user_version("u"))

    RevokedToken.objects.filter(expires_at__lt=datetime.now(UTC)).delete()

    assert asyncio.run(store.get_user_version("u")) == 1


class _ViewsUser:
    pk = "orm-user"
    is_active = True


def test_jwt_auth_views_work_on_the_orm_store(orm_store):
    """Login, refresh and logout-all work on a database store. Logout-all ends the old refresh token."""
    api = BoltAPI()

    async def validate(request, credentials):
        return _ViewsUser()

    JWTAuthViews(
        store=orm_store(),
        secret="a-secure-test-secret-that-is-long-enough",
        secure_cookies=False,
        credential_validator=validate,
    ).register(api)
    login = {"username": "orm-user", "password": "any"}
    no_csrf_origin = {"Sec-Fetch-Site": "none"}

    with TestClient(api) as client:
        assert client.post("/auth/login", json=login).status_code == 200
        assert client.post("/auth/refresh", headers=no_csrf_origin).status_code == 200
        old = client.cookies.get("refresh_token")
        assert client.post("/auth/logout-all", headers=no_csrf_origin).status_code == 200

        client.cookies.clear()
        stale = client.post("/auth/refresh", headers={"Cookie": f"refresh_token={old}", **no_csrf_origin})
        assert stale.status_code == 401

        assert client.post("/auth/login", json=login).status_code == 200
        assert client.post("/auth/refresh", headers=no_csrf_origin).status_code == 200


# ---------------------------------------------------------------------------
# A jti must not use the key of a family or of a user version (#887)
# ---------------------------------------------------------------------------

_TOKEN_CALLS = pytest.mark.parametrize(
    "call",
    [
        lambda store, jti: store.revoke(jti, exp=int(time.time()) + FUTURE),
        lambda store, jti: store.consume(jti, exp=int(time.time()) + FUTURE),
        lambda store, jti: store.is_revoked(jti),
    ],
    ids=["revoke", "consume", "is_revoked"],
)


@_TOKEN_CALLS
@pytest.mark.parametrize(("jti", "key"), [("fam:f", "revoked:fam:f"), ("ver:u", "revoked:ver:u")])
def test_cache_refuses_a_jti_that_uses_the_key_of_another_entry(cache_store, call, jti, key):
    store = cache_store()

    with pytest.raises(ReservedJtiError, match=f"^jti {jti!r} starts with a reserved prefix"):
        asyncio.run(call(store, jti))

    assert caches["revocation"].get(key) is None


@_TOKEN_CALLS
@pytest.mark.parametrize("jti", ["family:f", "version:u"])
def test_orm_refuses_a_jti_that_uses_the_key_of_another_entry(orm_store, call, jti):
    store = orm_store()

    with pytest.raises(ReservedJtiError, match=f"^jti {jti!r} starts with a reserved prefix"):
        asyncio.run(call(store, jti))

    assert RevokedToken.objects.filter(jti=jti).exists() is False


@pytest.mark.parametrize("jti", ["fam", "family:f", "日本語のトークン"])
def test_cache_accepts_a_jti_that_does_not_start_with_a_reserved_prefix(cache_store, jti):
    store = cache_store()

    asyncio.run(store.revoke(jti))

    assert asyncio.run(store.is_revoked(jti)) is True


@pytest.mark.parametrize("jti", ["family", "fam:f", "日本語のトークン"])
def test_orm_accepts_a_jti_that_does_not_start_with_a_reserved_prefix(orm_store, jti):
    store = orm_store()

    asyncio.run(store.revoke(jti))

    assert asyncio.run(store.is_revoked(jti)) is True


_SECRET = "a-secure-test-secret-that-is-long-enough"


def _signed(typ: str, jti: str) -> str:
    """Sign a token of type ``typ`` whose ``jti`` is not a UUID."""
    now = int(time.time())
    claims = {"sub": "u", "iat": now, "exp": now + FUTURE, "typ": typ, "jti": jti, "fam": "f", "ver": 0}
    return jwt.encode(claims, _SECRET, algorithm="HS256")


_RESERVED_JTI_STORES = pytest.mark.parametrize(
    ("store_fixture", "jti"),
    [("cache_store", "ver:u"), ("cache_store", "fam:f"), ("orm_store", "version:u"), ("orm_store", "family:f")],
)


@_RESERVED_JTI_STORES
def test_a_token_whose_jti_has_a_reserved_prefix_gets_a_401_at_authentication(request, caplog, store_fixture, jti):
    """The store refuses the jti, so the backend refuses the token. A refused token is not a server error."""
    store = request.getfixturevalue(store_fixture)()
    api = BoltAPI()

    @api.get("/protected", auth=[JWTAuthentication(secret=_SECRET, revocation_store=store)])
    async def protected():
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/protected", headers={"Authorization": f"Bearer {_signed('access', jti)}"})

    assert response.status_code == 401
    assert "reserved prefix" not in caplog.text


class _BrokenStore(InMemoryRevocation):
    """A custom store with a bug: ``is_revoked`` raises a ``ValueError`` that is not ``ReservedJtiError``."""

    async def is_revoked(self, jti: str) -> bool:
        raise ValueError("bug in the store")


def test_a_value_error_of_a_broken_store_is_not_a_revoked_token(caplog):
    """Only ``ReservedJtiError`` means a revoked token. Another error of the store stays a server error."""
    api = BoltAPI()

    @api.get("/protected", auth=[JWTAuthentication(secret=_SECRET, revocation_store=_BrokenStore())])
    async def protected():
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/protected", headers={"Authorization": f"Bearer {_signed('access', 'jti-1')}"})

    assert response.status_code == 500
    assert "bug in the store" in caplog.text


def test_logout_with_a_broken_store_is_a_server_error(caplog):
    api = BoltAPI()

    async def validate(request, credentials):
        return _ViewsUser()

    JWTAuthViews(store=_BrokenStore(), secret=_SECRET, secure_cookies=False, credential_validator=validate).register(
        api
    )
    headers = {"Cookie": f"refresh_token={_signed('refresh', 'jti-1')}", "Sec-Fetch-Site": "none"}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.post("/auth/logout", headers=headers)

    assert response.status_code == 500
    assert "bug in the store" in caplog.text


@pytest.mark.parametrize("path", ["/auth/logout", "/auth/logout-all"])
@_RESERVED_JTI_STORES
def test_logout_with_a_token_whose_jti_has_a_reserved_prefix_gives_the_uniform_401(
    request, caplog, store_fixture, jti, path
):
    store = request.getfixturevalue(store_fixture)()
    api = BoltAPI()

    async def validate(request, credentials):
        return _ViewsUser()

    JWTAuthViews(store=store, secret=_SECRET, secure_cookies=False, credential_validator=validate).register(api)
    headers = {"Cookie": f"refresh_token={_signed('refresh', jti)}", "Sec-Fetch-Site": "none"}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.post(path, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token"
    assert "reserved prefix" not in caplog.text
    # The user version stays, so the live tokens of the user stay valid.
    assert asyncio.run(store.get_user_version("u")) == 0
    assert asyncio.run(store.is_family_revoked("f")) is False


def test_refresh_of_a_token_whose_jti_has_a_reserved_prefix_gives_the_uniform_401(cache_store):
    api = BoltAPI()

    async def validate(request, credentials):
        return _ViewsUser()

    JWTAuthViews(store=cache_store(), secret=_SECRET, secure_cookies=False, credential_validator=validate).register(api)
    headers = {"Cookie": f"refresh_token={_signed('refresh', 'ver:u')}", "Sec-Fetch-Site": "none"}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.post("/auth/refresh", headers=headers)

    assert response.status_code == 401


def test_reusable_refresh_of_a_token_whose_jti_has_a_reserved_prefix_raises_token_rotation_error(cache_store):
    claims = jwt.decode(_signed("refresh", "ver:u"), _SECRET, algorithms=["HS256"])

    with pytest.raises(TokenRotationError):
        asyncio.run(rotate_refresh_token(claims, store=cache_store(), secret=_SECRET, rotate=False))


class _SteppingClock:
    """A clock that moves a little at each read, so a read order is visible.

    The first read is just before the second 101 and the next reads are in it.
    """

    def __init__(self) -> None:
        self.reads = iter([100.999, 101.0, 101.0, 101.0])

    def time(self) -> float:
        return next(self.reads)

    def now(self, tz=None) -> datetime:
        return datetime.fromtimestamp(self.time(), tz)


@pytest.mark.parametrize("method", ["revoke", "revoke_family", "consume"])
def test_orm_entry_lives_to_the_end_of_exp_plus_leeway_at_a_second_boundary(orm_store, monkeypatch, method):
    """The TTL and the base time come from two reads of the clock.

    When the second changes between the reads, ``expires_at`` must still not
    be before the end of the second ``exp + leeway``. The backend accepts the token
    until then, so the cleanup task must keep the row until then.
    """
    clock = _SteppingClock()
    monkeypatch.setattr(revocation_module, "time", clock)
    monkeypatch.setattr(revocation_module, "datetime", clock)
    store = orm_store()
    exp = 200

    asyncio.run(getattr(store, method)("jti-1", exp=exp))

    row = RevokedToken.objects.get(jti="family:jti-1" if method == "revoke_family" else "jti-1")
    assert row.expires_at.timestamp() >= exp + store.leeway + 1


# ---------------------------------------------------------------------------
# A store refuses a key that it cannot keep (#931)
# ---------------------------------------------------------------------------

_JTI_MAX_LENGTH = RevokedToken._meta.get_field("jti").max_length


class MemcachedKeyRulesCache(LocMemCache):
    """A local-memory cache with the key check of the memcached backends of Django.

    The check raises ``InvalidCacheKey`` for a key with a space or a control
    character, and for a key longer than 250 characters.
    """

    validate_key = BaseMemcachedCache.validate_key


@pytest.fixture
def memcached_store(settings):
    """Build a ``DjangoCacheRevocation`` on a cache with the key rules of memcached."""
    settings.CACHES = {
        **settings.CACHES,
        "revocation-memcached": {
            "BACKEND": "tests.test_revocation.MemcachedKeyRulesCache",
            "LOCATION": "revocation-memcached-tests",
        },
    }
    caches["revocation-memcached"].clear()
    return lambda **kwargs: DjangoCacheRevocation(cache_alias="revocation-memcached", **kwargs)


_KEY_CALLS = pytest.mark.parametrize(
    "call",
    [
        lambda store, value: store.revoke(value, exp=int(time.time()) + FUTURE),
        lambda store, value: store.consume(value, exp=int(time.time()) + FUTURE),
        lambda store, value: store.is_revoked(value),
        lambda store, value: store.revoke_family(value, exp=int(time.time()) + FUTURE),
        lambda store, value: store.is_family_revoked(value),
        lambda store, value: store.get_user_version(value),
        lambda store, value: store.bump_user_version(value),
    ],
    ids=["revoke", "consume", "is_revoked", "revoke_family", "is_family_revoked", "get_user_version", "bump"],
)


@_KEY_CALLS
@pytest.mark.parametrize("value", ["a" * (_JTI_MAX_LENGTH + 1), "a\x00b"], ids=["too-long", "nul"])
def test_orm_refuses_a_key_that_its_jti_column_cannot_keep(orm_store, call, value):
    """PostgreSQL raises ``DataError`` for such a key, and MySQL can cut it short. The store refuses it first."""
    store = orm_store()

    with pytest.raises(ReservedJtiError):
        asyncio.run(call(store, value))

    assert RevokedToken.objects.exists() is False


def test_orm_accepts_a_jti_as_long_as_its_jti_column(orm_store):
    store = orm_store()
    jti = "a" * _JTI_MAX_LENGTH

    asyncio.run(store.revoke(jti))

    assert asyncio.run(store.is_revoked(jti)) is True


def test_orm_reads_the_length_limit_from_the_model_of_the_store(transactional_db):
    store = DjangoORMRevocation(model="django_bolt.ShortJtiRevokedToken")

    asyncio.run(store.revoke("a" * 40))
    with pytest.raises(ReservedJtiError):
        asyncio.run(store.revoke("a" * 41))

    assert asyncio.run(store.is_revoked("a" * 40)) is True


@_KEY_CALLS
@pytest.mark.parametrize("value", ["has space", "a" * 251, "a\nb"], ids=["space", "too-long", "control-char"])
def test_cache_refuses_a_key_that_the_cache_backend_refuses(memcached_store, call, value):
    """A memcached backend raises ``InvalidCacheKey`` for such a key. The store raises ``ReservedJtiError``."""
    with pytest.raises(ReservedJtiError):
        asyncio.run(call(memcached_store(), value))


def test_cache_on_a_memcached_backend_keeps_a_valid_jti(memcached_store):
    store = memcached_store()

    asyncio.run(store.revoke("a-valid-jti"))

    assert asyncio.run(store.is_revoked("a-valid-jti")) is True


_UNKEEPABLE_KEY_STORES = pytest.mark.parametrize(
    ("store_fixture", "unkeepable"),
    [
        ("orm_store", lambda unique: unique.ljust(_JTI_MAX_LENGTH + 1, "a")),
        ("orm_store", lambda unique: f"{unique}\x00"),
        ("memcached_store", lambda unique: f"{unique} x"),
        ("memcached_store", lambda unique: unique.ljust(251, "a")),
    ],
    ids=["orm-too-long", "orm-nul", "memcached-space", "memcached-too-long"],
)


def _signed_claims(typ: str, **claims) -> str:
    now = int(time.time())
    # A new jti and family for each token, so no test sees the entries of another test.
    base = {"sub": "u", "iat": now, "exp": now + FUTURE, "typ": typ, "jti": uuid.uuid4().hex, "fam": uuid.uuid4().hex}
    return jwt.encode({**base, **claims}, _SECRET, algorithm="HS256")


@_UNKEEPABLE_KEY_STORES
def test_a_token_whose_jti_the_store_cannot_keep_gets_a_401_at_authentication(request, store_fixture, unkeepable):
    store = request.getfixturevalue(store_fixture)()
    value = unkeepable(uuid.uuid4().hex)
    api = BoltAPI()

    @api.get("/protected", auth=[JWTAuthentication(secret=_SECRET, revocation_store=store)])
    async def protected():
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/protected", headers={"Authorization": f"Bearer {_signed_claims('access', jti=value)}"})

    assert response.status_code == 401


@pytest.mark.parametrize("path", ["/auth/refresh", "/auth/logout", "/auth/logout-all"])
@pytest.mark.parametrize("claim", ["jti", "fam", "sub"])
@_UNKEEPABLE_KEY_STORES
def test_a_refresh_token_with_a_claim_that_the_store_cannot_keep_gets_the_uniform_401(
    request, store_fixture, unkeepable, claim, path
):
    """A family or a user version that the store cannot keep cannot be revoked, so the token is refused."""
    store = request.getfixturevalue(store_fixture)()
    value = unkeepable(uuid.uuid4().hex)
    api = BoltAPI()

    async def validate(request, credentials):
        return _ViewsUser()

    JWTAuthViews(store=store, secret=_SECRET, secure_cookies=False, credential_validator=validate).register(api)
    token = _signed_claims("refresh", **{claim: value})
    headers = {"Cookie": f"refresh_token={token}", "Sec-Fetch-Site": "none"}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.post(path, headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token"


@pytest.mark.parametrize("rotate", [True, False], ids=["rotate", "reusable"])
@pytest.mark.parametrize("claim", ["jti", "fam", "sub"])
@_UNKEEPABLE_KEY_STORES
def test_rotation_of_a_refresh_token_with_a_claim_that_the_store_cannot_keep_raises_token_rotation_error(
    request, store_fixture, unkeepable, claim, rotate
):
    store = request.getfixturevalue(store_fixture)()
    value = unkeepable(uuid.uuid4().hex)
    claims = jwt.decode(_signed_claims("refresh", **{claim: value}), _SECRET, algorithms=["HS256"])

    with pytest.raises(TokenRotationError):
        asyncio.run(rotate_refresh_token(claims, store=store, secret=_SECRET, rotate=rotate))


@pytest.mark.parametrize("exp", [1e300, 2**62, 253402300800, -1e300, float("inf"), float("nan")])
def test_rotation_with_an_exp_that_a_datetime_cannot_hold_is_a_rotation_error(orm_store, exp):
    """``auth_claims`` never gives such an ``exp``. A caller's claims gave ``OverflowError`` from the store."""
    store = orm_store()
    claims = jwt.decode(_signed_claims("refresh"), _SECRET, algorithms=["HS256"])

    with pytest.raises(TokenRotationError, match="invalid expiry"):
        asyncio.run(rotate_refresh_token({**claims, "exp": exp}, store=store, secret=_SECRET))
    assert not RevokedToken.objects.exists()
    # Nothing consumed the token, so its claims with a valid exp rotate.
    asyncio.run(rotate_refresh_token(claims, store=store, secret=_SECRET))
    asyncio.run(
        rotate_refresh_token({**claims, "jti": "last-second", "exp": 253402300799}, store=store, secret=_SECRET)
    )


# ---------------------------------------------------------------------------
# DjangoCacheRevocation on a DatabaseCache (#940)
# ---------------------------------------------------------------------------

_DB_CACHE_TABLE = "bolt_revocation_cache"


@pytest.fixture
def db_cache_store(settings, transactional_db):
    """Build a ``DjangoCacheRevocation`` on a ``DatabaseCache`` with its own table."""
    settings.CACHES = {
        **settings.CACHES,
        "revocation-db": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": _DB_CACHE_TABLE},
    }
    call_command("createcachetable", _DB_CACHE_TABLE, verbosity=0)
    yield DjangoCacheRevocation(cache_alias="revocation-db")
    # The flush of transactional_db does not empty a table that has no model.
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE {connection.ops.quote_name(_DB_CACHE_TABLE)}")


def test_cache_store_on_a_database_cache_checks_tokens_over_http(db_cache_store):
    """A request with a token gets a 200, and a 401 after logout. It is not a 500."""
    api = BoltAPI()
    auth = JWTAuthentication(secret=_SECRET, revocation_store=db_cache_store)

    @api.get("/protected", auth=[auth])
    async def protected():
        return {"ok": True}

    @api.post("/logout", auth=[auth])
    async def logout(request):
        claims = request["context"]["auth_claims"]
        await db_cache_store.revoke(claims["jti"], exp=claims["exp"])
        return {"ok": True}

    token = {"Authorization": f"Bearer {_signed('access', 'db-cache-jti')}"}
    other = {"Authorization": f"Bearer {_signed('access', 'db-cache-other')}"}

    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.get("/protected", headers=token).status_code == 200
        assert client.post("/logout", headers=token).status_code == 200
        assert client.get("/protected", headers=token).status_code == 401
        assert client.get("/protected", headers=other).status_code == 200


def test_cache_store_on_a_database_cache_keeps_tokens_families_and_versions(db_cache_store):
    """Each store method that the database cache supports runs on the event loop."""

    async def run():
        await db_cache_store.revoke("a", exp=int(time.time()) + FUTURE)
        await db_cache_store.revoke_family("f", exp=int(time.time()) + FUTURE)
        return (
            await db_cache_store.is_revoked("a"),
            await db_cache_store.is_revoked("b"),
            await db_cache_store.is_family_revoked("f"),
            await db_cache_store.is_family_revoked("g"),
            await db_cache_store.get_user_version("u"),
        )

    assert asyncio.run(run()) == (True, False, True, False, 0)


class IncrDatabaseCache(DatabaseCache):
    """A database cache with its own ``incr``. Each call still runs a query."""

    def incr(self, key, delta=1, version=None):
        return super().incr(key, delta, version)


def test_cache_store_refuses_rotation_on_a_subclass_of_the_database_cache(settings):
    """A subclass with its own ``incr`` gets the clear error, not ``SynchronousOnlyOperation``."""
    settings.CACHES = {
        **settings.CACHES,
        "incr-db": {"BACKEND": "tests.test_revocation.IncrDatabaseCache", "LOCATION": _DB_CACHE_TABLE},
    }
    store = DjangoCacheRevocation(cache_alias="incr-db")

    with pytest.raises(NotImplementedError, match="atomic add/incr"):
        asyncio.run(store.consume("a", exp=int(time.time()) + FUTURE))
    with pytest.raises(NotImplementedError, match="atomic add/incr"):
        asyncio.run(store.bump_user_version("u"))


@pytest.mark.parametrize(
    "call",
    [
        lambda store: store.is_revoked("a"),
        lambda store: store.revoke("a", exp=int(time.time()) + FUTURE),
        lambda store: store.consume("a", exp=int(time.time()) + FUTURE),
        lambda store: store.is_family_revoked("f"),
        lambda store: store.revoke_family("f", exp=int(time.time()) + FUTURE),
        lambda store: store.get_user_version("u"),
        lambda store: store.bump_user_version("u"),
    ],
    ids=["is_revoked", "revoke", "consume", "is_family_revoked", "revoke_family", "get_user_version", "bump"],
)
def test_cache_store_on_an_in_memory_cache_does_not_leave_the_event_loop_thread(cache_store, call):
    """A local-memory call ends at the first step of its coroutine, so it has no thread hop.

    ``is_revoked`` runs for each authenticated request. A hop to a thread pool costs much more.
    """
    coro = call(cache_store())

    with pytest.raises(StopIteration):
        coro.send(None)


# ---------------------------------------------------------------------------
# A collation without case or accents must not read a jti as a reserved key (#931)
# ---------------------------------------------------------------------------

# MySQL 8.0 compares each jti here equal to a key with a reserved prefix, in one or more of
# utf8mb4_0900_ai_ci, utf8mb4_general_ci and utf8mb4_unicode_ci. A tailored collation also
# compares two letters equal: y and i (utf8mb4_lt_0900_ai_ci), j and i, u and v
# (utf8mb4_la_0900_ai_ci), ü and y (utf8mb4_da_0900_ai_ci), and alef with madda and a
# (utf8mb4_persian_ci).
_CACHE_LOOK_ALIKES = [
    "FAM:f",
    "Ver:u",
    "fäm:f",
    "fa\u0308m:f",
    "f\u00adam:f",
    "v\x01er:u",
    "VE\u200bR:u",
    "ｆａｍ：f",
    "Uer:u",
    "f\u0622m:f",
]
_ORM_LOOK_ALIKES = [
    "VERSION:u",
    "Family:f",
    "vérsion:u",
    "versıon:u",
    "verßion:u",
    "versiøn:u",
    "famiły:f",
    "v\u00adersion:u",
    "version：u",
    "versyon:u",
    "versjon:u",
    "famili:f",
    "familü:f",
    "uersion:u",
    "f\u0622mily:f",
]


@_TOKEN_CALLS
@pytest.mark.parametrize("jti", _CACHE_LOOK_ALIKES)
def test_cache_refuses_a_jti_that_a_collation_reads_as_a_reserved_key(cache_store, call, jti):
    store = cache_store()

    with pytest.raises(ReservedJtiError):
        asyncio.run(call(store, jti))


@_TOKEN_CALLS
@pytest.mark.parametrize("jti", _ORM_LOOK_ALIKES)
def test_orm_refuses_a_jti_that_a_collation_reads_as_a_reserved_key(orm_store, call, jti):
    store = orm_store()

    with pytest.raises(ReservedJtiError):
        asyncio.run(call(store, jti))

    assert RevokedToken.objects.exists() is False


@pytest.mark.parametrize(
    ("store_fixture", "jti"),
    [*(("cache_store", jti) for jti in _CACHE_LOOK_ALIKES), *(("orm_store", jti) for jti in _ORM_LOOK_ALIKES)],
)
def test_a_token_whose_jti_a_collation_reads_as_a_reserved_key_gets_a_401(request, store_fixture, jti):
    store = request.getfixturevalue(store_fixture)()
    api = BoltAPI()

    @api.get("/protected", auth=[JWTAuthentication(secret=_SECRET, revocation_store=store)])
    async def protected():
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/protected", headers={"Authorization": f"Bearer {_signed('access', jti)}"})

    assert response.status_code == 401


@pytest.fixture
def nocase_orm_store(transactional_db):
    """Build a ``DjangoORMRevocation`` on a table whose ``jti`` column compares text without case."""
    return lambda **kwargs: DjangoORMRevocation(model="django_bolt.CaseInsensitiveRevokedToken", **kwargs)


def _run_cleanup_task_after(seconds: int) -> None:
    """Delete the rows that the cleanup task of the docs deletes ``seconds`` from now."""
    CaseInsensitiveRevokedToken.objects.filter(expires_at__lt=datetime.now(UTC) + timedelta(seconds=seconds)).delete()


def test_the_case_insensitive_column_reads_a_jti_as_the_user_version_row(nocase_orm_store):
    """The test table shows the collision of #931: one row answers to both keys."""
    CaseInsensitiveRevokedToken.objects.create(jti="version:u", expires_at=datetime.now(UTC))

    assert CaseInsensitiveRevokedToken.objects.filter(jti="VERSION:u").exists()


def test_a_refresh_token_cannot_put_the_user_version_in_a_row_that_expires(nocase_orm_store):
    """A jti ``VERSION:<user id>`` must not make the row that later keeps the user version.

    That row would expire with the token. The cleanup task would then delete the version,
    and logout-all would not end the old sessions any more.
    """
    api = BoltAPI()

    async def validate(request, credentials):
        return _ViewsUser()

    JWTAuthViews(
        store=nocase_orm_store(), secret=_SECRET, secure_cookies=False, credential_validator=validate
    ).register(api)
    login = {"username": "orm-user", "password": "any"}
    no_csrf_origin = {"Sec-Fetch-Site": "none"}
    attack = _signed_claims("refresh", sub=_ViewsUser.pk, jti=f"VERSION:{_ViewsUser.pk}")

    with TestClient(api, raise_server_exceptions=False) as client:
        refused = client.post("/auth/refresh", headers={"Cookie": f"refresh_token={attack}", **no_csrf_origin})
        client.cookies.clear()
        assert client.post("/auth/login", json=login).status_code == 200
        old_session = client.cookies.get("refresh_token")
        client.cookies.clear()
        assert client.post("/auth/login", json=login).status_code == 200
        assert client.post("/auth/logout-all", headers=no_csrf_origin).status_code == 200
        client.cookies.clear()

        _run_cleanup_task_after(FUTURE + 600)

        stale = client.post("/auth/refresh", headers={"Cookie": f"refresh_token={old_session}", **no_csrf_origin})

    assert stale.status_code == 401
    assert refused.status_code == 401


def test_a_user_version_bump_keeps_its_row_when_a_jti_row_has_its_key(nocase_orm_store):
    """A row of an earlier release can have a jti that the column reads as the version key.

    The bump writes the version into that row, so the row must stop expiring.
    """
    store = nocase_orm_store()
    CaseInsensitiveRevokedToken.objects.create(jti="VERSION:u", expires_at=datetime.now(UTC))

    assert asyncio.run(store.bump_user_version("u")) == 1
    _run_cleanup_task_after(FUTURE)

    assert asyncio.run(store.get_user_version("u")) == 1


_NOCASE_CACHE_TABLE = "bolt_revocation_nocase_cache"


@pytest.fixture
def nocase_db_cache_store(settings, transactional_db):
    """Build a ``DjangoCacheRevocation`` on a ``DatabaseCache`` whose key column compares text without case."""
    settings.CACHES = {
        **settings.CACHES,
        "revocation-nocase-db": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": _NOCASE_CACHE_TABLE,
        },
    }
    table = connection.ops.quote_name(_NOCASE_CACHE_TABLE)
    with connection.cursor() as cursor:
        # The table of createcachetable, with the collation of the key column changed.
        cursor.execute(
            f"CREATE TABLE {table} (cache_key varchar(255) COLLATE NOCASE NOT NULL PRIMARY KEY, "
            "value text NOT NULL, expires datetime NOT NULL)"
        )
    yield DjangoCacheRevocation(cache_alias="revocation-nocase-db")
    with connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE {table}")


def test_a_jti_cannot_end_the_revocation_of_a_family_in_a_case_insensitive_database_cache(nocase_db_cache_store):
    store = nocase_db_cache_store
    asyncio.run(store.revoke_family("f", exp=int(time.time()) + 30 * 86400))

    with pytest.raises(ReservedJtiError):
        asyncio.run(store.revoke("FAM:f", exp=int(time.time()) + PAST))

    assert asyncio.run(store.is_family_revoked("f")) is True


def test_a_token_whose_jti_is_a_family_key_gets_a_401_on_a_case_insensitive_database_cache(nocase_db_cache_store):
    api = BoltAPI()

    @api.get("/protected", auth=[JWTAuthentication(secret=_SECRET, revocation_store=nocase_db_cache_store)])
    async def protected():
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/protected", headers={"Authorization": f"Bearer {_signed('access', 'FAM:f')}"})

    assert response.status_code == 401
