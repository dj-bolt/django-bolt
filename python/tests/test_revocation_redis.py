"""``DjangoCacheRevocation`` on a real Redis cache, in one process.

The other revocation tests use the local-memory cache. These tests check what
only Redis shows: the life of each key, the names of the keys, one counter for
all processes, and an ``add`` that only one of many callers wins.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication, JWTAuthViews, create_token_pair
from django_bolt.auth.revocation import DjangoCacheRevocation
from django_bolt.testing import TestClient

from .thread_pools import shutdown_closing_connections

REDIS_URL_VARIABLE = "DJANGO_BOLT_TEST_REDIS_URL"
SECRET = "a-secure-test-secret-that-is-long-enough"


# The connection pools that the Django cache made in these tests.
_pools: list[redis.ConnectionPool] = []


class RecordedPool(redis.ConnectionPool):
    """A connection pool that the test can close.

    Django makes one pool for each cache object and does not close it. The
    garbage collector then finds the open sockets, and the ResourceWarning
    fails the run.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _pools.append(self)


def _close_pools() -> None:
    while _pools:
        _pools.pop().disconnect()


def _redis_caches(location: str, prefix: str) -> dict:
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": location,
            "KEY_PREFIX": prefix,
            "OPTIONS": {"pool_class": f"{__name__}.RecordedPool"},
        }
    }


@pytest.fixture
def redis_keys():
    """Give a function that reads the keys of this test and their seconds to live.

    Parallel tests use the same Redis server, so each test gets its own key prefix.
    The tests skip without the URL variable.
    """
    url = os.environ.get(REDIS_URL_VARIABLE)
    if not url:
        pytest.skip(f"Set {REDIS_URL_VARIABLE} to run the Redis tests.")
    prefix = f"bolt_test_{secrets.token_hex(6)}"
    client = redis.Redis.from_url(url, decode_responses=True)

    def keys() -> dict[str, int]:
        # Django puts ``<KEY_PREFIX>:<version>:`` before each key. The version is 1.
        start = len(f"{prefix}:1:")
        return {name[start:]: client.ttl(name) for name in client.scan_iter(match=f"{prefix}:*")}

    try:
        with override_settings(CACHES=_redis_caches(url, prefix)):
            yield keys
    finally:
        _close_pools()
        for name in client.scan_iter(match=f"{prefix}:*"):
            client.delete(name)
        client.close()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_each_redis_entry_lives_until_the_backend_stops_accepting_its_token(redis_keys):
    store = DjangoCacheRevocation()
    exp = int(time.time()) + 100

    await store.revoke("revoked-jti", exp=exp)
    assert await store.consume("used-jti", exp=exp) is True
    await store.revoke_family("family-id", exp=exp)
    await store.revoke("no-exp-jti")

    keys = redis_keys()
    # ``exp`` plus the default leeway of 60 seconds. One second can pass in the test.
    for name in ("revoked:revoked-jti", "revoked:used-jti", "revoked:fam:family-id"):
        assert 159 <= keys.pop(name) <= 160, name
    assert 604799 <= keys.pop("revoked:no-exp-jti") <= 604800
    assert keys == {}
    assert await store.is_revoked("revoked-jti")
    assert await store.is_revoked("used-jti")
    assert await store.is_family_revoked("family-id")


@pytest.mark.redis
@pytest.mark.asyncio
async def test_user_version_on_redis_is_one_counter_that_does_not_expire(redis_keys):
    # Each worker process has its own store object.
    first_process = DjangoCacheRevocation()
    second_process = DjangoCacheRevocation()

    assert await first_process.get_user_version("7") == 0
    assert await first_process.bump_user_version("7") == 1
    assert await second_process.bump_user_version("7") == 2
    assert await first_process.get_user_version("7") == 2
    # -1 is the answer of Redis for a key with no expiry.
    assert redis_keys() == {"revoked:ver:7": -1}


@pytest.mark.redis
def test_only_one_of_the_concurrent_consumes_of_a_token_wins_on_redis(redis_keys):
    callers = 8
    store = DjangoCacheRevocation()
    exp = int(time.time()) + 100
    start = threading.Barrier(callers, timeout=5)

    def consume() -> bool:
        start.wait()
        return asyncio.run(store.consume("contested-jti", exp=exp))

    pool = ThreadPoolExecutor(max_workers=callers)
    try:
        results = [future.result() for future in [pool.submit(consume) for _ in range(callers)]]
    finally:
        shutdown_closing_connections(pool)

    assert sorted(results) == [False] * (callers - 1) + [True]


def test_a_redis_that_does_not_answer_refuses_each_request():
    """The store cannot read the state of a token. No route accepts the token."""
    # Nothing listens on a port that was bound and closed.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with override_settings(CACHES=_redis_caches(f"redis://127.0.0.1:{port}/0", "down")):
        api = BoltAPI()
        store = DjangoCacheRevocation()

        async def validate(request, credentials):
            return None

        JWTAuthViews(store=store, secret=SECRET, secure_cookies=False, credential_validator=validate).register(api)

        @api.get(
            "/protected",
            auth=[JWTAuthentication(secret=SECRET, revocation_store=store)],
            guards=[IsAuthenticated()],
        )
        async def protected():
            return {"ok": True}

        pair = create_token_pair("user-1", secret=SECRET)
        refresh_cookie = {"Cookie": f"refresh_token={pair.refresh_token}", "Sec-Fetch-Site": "none"}

        with TestClient(api, raise_server_exceptions=False) as client:
            statuses = {
                "/protected": client.get(
                    "/protected", headers={"Authorization": f"Bearer {pair.access_token}"}
                ).status_code,
                "/auth/refresh": client.post("/auth/refresh", headers=refresh_cookie).status_code,
                "/auth/logout": client.post("/auth/logout", headers=refresh_cookie).status_code,
                "/auth/logout-all": client.post("/auth/logout-all", headers=refresh_cookie).status_code,
            }

    assert statuses == dict.fromkeys(statuses, 500)


@pytest.mark.redis
@pytest.mark.asyncio
async def test_a_jti_cannot_write_the_key_of_a_family_or_of_a_user_version_on_redis(redis_keys):
    """Reproduction of #887: ``revoke("ver:u2")`` made each token of ``u2`` invalid."""
    store = DjangoCacheRevocation()
    exp = int(time.time()) + 100

    for jti in ("ver:u2", "fam:f1"):
        with pytest.raises(ValueError, match="starts with a reserved prefix"):
            await store.revoke(jti, exp=exp)

    assert redis_keys() == {}
    assert await store.get_user_version("u2") == 0
    assert await store.bump_user_version("u2") == 1
    assert await store.is_family_revoked("f1") is False


@pytest.mark.redis
@pytest.mark.asyncio
async def test_a_consume_of_an_expired_token_is_refused_on_each_call_on_redis(redis_keys):
    """Redis deletes a key with no time to live, so the store cannot keep the marker."""
    store = DjangoCacheRevocation()
    exp = int(time.time()) - 3600

    assert await store.consume("expired-jti", exp=exp) is False
    assert await store.consume("expired-jti", exp=exp) is False
    assert redis_keys() == {}
