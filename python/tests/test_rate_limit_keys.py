"""
Integration tests for rate-limit key strategies (issues #301 and #302).

#301: key="user" and key="api_key" must segment by authenticated identity,
      not silently share one "unknown" bucket.
#302: key="ip" must not trust X-Forwarded-For unless BOLT_TRUSTED_PROXIES
      marks the peer as a trusted proxy.

All tests go through TestClient, which runs the full Rust pipeline.
TestClient requests have no TCP peer address. When BOLT_TRUSTED_PROXIES is
set, the missing peer counts as trusted, so tests can fake client IPs with
X-Forwarded-For. When it is unset, forwarding headers are ignored and every
request lands in one shared bucket.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

import jwt
import pytest
from django.conf import settings

from django_bolt import BoltAPI, _core
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from django_bolt.middleware import rate_limit
from django_bolt.testing import TestClient

SECRET = "test-rate-limit-secret"


def make_token(sub: str) -> str:
    return jwt.encode({"sub": sub, "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")


@contextmanager
def trusted_proxies(*cidrs: str):
    settings.BOLT_TRUSTED_PROXIES = list(cidrs)
    try:
        yield
    finally:
        del settings.BOLT_TRUSTED_PROXIES


@pytest.fixture(autouse=True)
def _fresh_limiters():
    """Clear the process-global limiter map so buckets never leak across tests."""
    reset = getattr(_core, "_reset_rate_limiters", None)
    if reset is not None:
        reset()
    yield


# ---------------------------------------------------------------------------
# Issue #301: identity-keyed rate limits
# ---------------------------------------------------------------------------


def test_user_key_segments_per_user():
    """Ten distinct JWT users must not share one bucket (issue #301)."""
    api = BoltAPI()

    @api.get("/limited", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
    @rate_limit(rps=1, burst=5, key="user")
    async def limited():
        return {"ok": True}

    with TestClient(api) as client:
        statuses = []
        for i in range(10):
            r = client.get("/limited", headers={"authorization": f"Bearer {make_token(f'user-{i}')}"})
            statuses.append(r.status_code)
        assert statuses == [200] * 10, f"distinct users shared a bucket: {statuses}"

        # One user hammering the route is still limited.
        token = make_token("user-0")
        repeat = [client.get("/limited", headers={"authorization": f"Bearer {token}"}).status_code for _ in range(8)]
        assert 429 in repeat, f"single user was never limited: {repeat}"

        # Other users are unaffected by user-0 exhausting their bucket.
        r = client.get("/limited", headers={"authorization": f"Bearer {make_token('user-1')}"})
        assert r.status_code == 200


def test_api_key_key_segments_per_key():
    """Distinct API keys must not share one bucket (issue #301)."""
    keys = {f"key-{i}" for i in range(10)}
    api = BoltAPI()

    @api.get(
        "/limited",
        auth=[APIKeyAuthentication(api_keys=keys, header="x-api-key")],
        guards=[IsAuthenticated()],
    )
    @rate_limit(rps=1, burst=5, key="api_key")
    async def limited():
        return {"ok": True}

    with TestClient(api) as client:
        statuses = [client.get("/limited", headers={"x-api-key": f"key-{i}"}).status_code for i in range(10)]
        assert statuses == [200] * 10, f"distinct API keys shared a bucket: {statuses}"

        repeat = [client.get("/limited", headers={"x-api-key": "key-0"}).status_code for _ in range(8)]
        assert 429 in repeat, f"single API key was never limited: {repeat}"


def test_user_key_falls_back_to_ip_for_anonymous():
    """On a user-keyed route, anonymous requests share the IP bucket while
    authenticated users get their own bucket (issue #301)."""
    api = BoltAPI()

    # Auth configured but not required: anonymous requests pass through.
    @api.get("/limited", auth=[JWTAuthentication(secret=SECRET)])
    @rate_limit(rps=1, burst=3, key="user")
    async def limited():
        return {"ok": True}

    with TestClient(api) as client:
        # Anonymous requests all resolve to the same client IP bucket.
        anon = [client.get("/limited").status_code for _ in range(6)]
        assert anon[:3] == [200] * 3
        assert 429 in anon, f"anonymous requests were never limited: {anon}"

        # An authenticated user is keyed by identity, not by the drained IP bucket.
        r = client.get("/limited", headers={"authorization": f"Bearer {make_token('user-42')}"})
        assert r.status_code == 200, "authenticated user shared the anonymous bucket"


# ---------------------------------------------------------------------------
# Issue #302: X-Forwarded-For trust
# ---------------------------------------------------------------------------


def test_ip_key_ignores_forwarded_headers_without_trusted_proxies():
    """With no BOLT_TRUSTED_PROXIES, spoofed forwarding headers must not
    segment the limit (issue #302)."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=5, key="ip")
    async def limited():
        return {"ok": True}

    with TestClient(api) as client:
        statuses = [
            client.get("/limited", headers={"x-forwarded-for": f"203.0.113.{i}"}).status_code for i in range(10)
        ]
        assert 429 in statuses, f"X-Forwarded-For spoofing bypassed the limit: {statuses}"

        # X-Real-IP is equally untrusted.
        real_ip = [client.get("/limited", headers={"x-real-ip": f"198.51.100.{i}"}).status_code for i in range(4)]
        assert real_ip == [429] * 4, f"X-Real-IP spoofing bypassed the limit: {real_ip}"


def test_ip_key_honors_forwarded_for_with_trusted_proxies():
    """With BOLT_TRUSTED_PROXIES set, X-Forwarded-For segments per client IP."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=5, key="ip")
    async def limited():
        return {"ok": True}

    with trusted_proxies("127.0.0.1/32", "10.0.0.0/8"), TestClient(api) as client:
        statuses = [
            client.get("/limited", headers={"x-forwarded-for": f"203.0.113.{i}"}).status_code for i in range(10)
        ]
        assert statuses == [200] * 10, f"distinct client IPs shared a bucket: {statuses}"

        repeat = [client.get("/limited", headers={"x-forwarded-for": "198.51.100.7"}).status_code for _ in range(8)]
        assert 429 in repeat, f"single client IP was never limited: {repeat}"


def test_ip_key_uses_rightmost_untrusted_forwarded_entry():
    """The client IP is the rightmost X-Forwarded-For entry that is not a
    trusted proxy; leftmost entries are client-controlled (issue #302)."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=5, key="ip")
    async def limited():
        return {"ok": True}

    with trusted_proxies("127.0.0.1/32", "10.0.0.0/8"), TestClient(api) as client:
        # Same real client (198.51.100.9) behind a trusted proxy (10.0.0.5),
        # spoofing a different leftmost entry on every request.
        statuses = [
            client.get(
                "/limited",
                headers={"x-forwarded-for": f"203.0.113.{i}, 198.51.100.9, 10.0.0.5"},
            ).status_code
            for i in range(10)
        ]
        assert 429 in statuses, f"leftmost spoofed entries bypassed the limit: {statuses}"


def test_header_key_missing_falls_back_to_client_ip():
    """A header-keyed route no longer lumps clients that omit the header into
    one shared bucket; it falls back to the client IP (issue #301)."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=5, key="x-client-id")
    async def limited():
        return {"ok": True}

    with trusted_proxies("127.0.0.1/32"), TestClient(api) as client:
        statuses = [
            client.get("/limited", headers={"x-forwarded-for": f"203.0.113.{i}"}).status_code for i in range(10)
        ]
        assert statuses == [200] * 10, f"clients without the key header shared a bucket: {statuses}"

        # The header still segments when present.
        withheader = [client.get("/limited", headers={"x-client-id": "client-a"}).status_code for _ in range(8)]
        assert withheader[:5] == [200] * 5
        assert 429 in withheader, f"header-keyed client was never limited: {withheader}"


def test_long_key_hashed_not_rejected():
    """Keys longer than 256 bytes are hashed, not rejected with 400 (issue #301)."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=1, key="authorization")
    async def limited():
        return {"ok": True}

    long_a = "Bearer " + "a" * 300
    long_b = "Bearer " + "b" * 300

    with TestClient(api) as client:
        assert client.get("/limited", headers={"authorization": long_a}).status_code == 200
        # A different long value gets its own bucket.
        assert client.get("/limited", headers={"authorization": long_b}).status_code == 200
        # The same long value is limited (burst=1).
        assert client.get("/limited", headers={"authorization": long_a}).status_code == 429
