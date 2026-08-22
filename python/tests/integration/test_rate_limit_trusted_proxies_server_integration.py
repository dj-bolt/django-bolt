"""Real-server coverage for trusted-proxy client-IP rate limiting (issue #302).

The in-process tests (tests/test_rate_limit_keys.py) run through TestClient,
where requests have no TCP peer. Only a live runbolt server exercises the
production wiring: ``start_server`` reads ``BOLT_TRUSTED_PROXIES`` from Django
settings at startup, and the peer address is a real loopback IP that either is
or is not in the trusted set.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def test_forwarded_for_ignored_without_trusted_proxies(make_server_project):
    """With no BOLT_TRUSTED_PROXIES, spoofed X-Forwarded-For must not segment
    the per-IP limit: every request keys on the real peer address."""
    project = make_server_project(api_module=app_module("rate_limit_ip"))
    with project.start() as server:
        statuses = [server.get("/limited", headers={"x-forwarded-for": f"203.0.113.{i}"}).status_code for i in range(8)]
    assert statuses[:3] == [200] * 3, f"burst requests failed: {statuses}"
    assert 429 in statuses, f"X-Forwarded-For spoofing bypassed the limit: {statuses}"


def test_forwarded_for_honored_with_trusted_proxies(make_server_project):
    """With the loopback peer trusted, X-Forwarded-For segments per client IP."""
    project = make_server_project(
        api_module=app_module("rate_limit_ip"),
        settings_extra='BOLT_TRUSTED_PROXIES = ["127.0.0.1/32", "::1/128"]',
    )
    with project.start() as server:
        distinct = [server.get("/limited", headers={"x-forwarded-for": f"203.0.113.{i}"}).status_code for i in range(8)]
        same = [server.get("/limited", headers={"x-forwarded-for": "198.51.100.7"}).status_code for _ in range(6)]
    assert distinct == [200] * 8, f"distinct client IPs shared a bucket: {distinct}"
    assert same[:3] == [200] * 3, f"burst requests failed: {same}"
    assert 429 in same, f"single client IP was never limited: {same}"
