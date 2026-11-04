"""Memcached CAS integration for JWT logout-all user-version invalidation.

Run against a disposable local memcached server:
    uv run --with pymemcache pytest -q -n 0 python/tests/test_jwt_atomic_memcached_integration.py
No database flushing; keys are unique and cleaned up after each test.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.test import override_settings

from django_bolt.auth.revocation import DjangoCacheRevocation


def _require_local_memcached():
    """Skip the integration test when local Memcached is unavailable."""
    pytest.importorskip("pymemcache")
    try:
        with socket.create_connection(("127.0.0.1", 11211), timeout=1):
            pass
    except OSError:
        pytest.skip("Local Memcached server is not running")


@pytest.mark.parametrize("initial_version", [0, 1], ids=["missing-key", "existing-key-cas"])
def test_memcached_compare_and_bump_under_concurrent_clients(initial_version):
    """Verify that concurrent clients cannot both advance one version."""
    _require_local_memcached()
    with override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.memcached.PyMemcacheCache",
                "LOCATION": "127.0.0.1:11211",
            }
        }
    ):
        prefix = f"jwt-mem-cas:{uuid.uuid4().hex}:"
        user_id = "concurrent"
        probe = DjangoCacheRevocation(key_prefix=prefix)
        version_key = f"{prefix}ver:{user_id}"
        try:
            if initial_version:
                # Simulate a prior logout through the existing public API.
                assert asyncio.run(probe.bump_user_version(user_id)) == 1

            count = 12
            ready = Barrier(count)

            def attempt(_):
                ready.wait(timeout=15)
                store = DjangoCacheRevocation(key_prefix=prefix)
                try:
                    return asyncio.run(store.bump_user_version_if_current(user_id, initial_version))
                finally:
                    # Django's PyMemcacheCache owns a per-thread client/socket.
                    store.cache.close()

            with ThreadPoolExecutor(max_workers=count) as pool:
                results = list(pool.map(attempt, range(count)))
            assert results.count(True) == 1
            assert results.count(False) == count - 1
            assert asyncio.run(probe.get_user_version(user_id)) == initial_version + 1
            assert asyncio.run(probe.bump_user_version_if_current(user_id, initial_version)) is False

            # The global-logout version must not have a normal cache TTL.
            raw_key = probe.cache.make_and_validate_key(version_key)
            _, cas_token = probe.cache._cache.gets(raw_key)
            assert cas_token is not None

            # GETS proves CAS support, not that the key has no expiration.
            # Memcached's meta-get `t` flag reports -1 for an unlimited TTL.
            with socket.create_connection(("127.0.0.1", 11211), timeout=2) as connection:
                connection.sendall(b"mg " + raw_key.encode("ascii") + b" t\r\n")
                with connection.makefile("rb") as reader:
                    metadata = reader.readline().strip().split()
            assert metadata and metadata[0] == b"HD", metadata
            assert b"t-1" in metadata[1:], metadata
        finally:
            try:
                probe.cache.delete(version_key)
            finally:
                probe.cache.close()
