"""Real-server tests for how a `key="ip"` rate limit picks the client address.

These run against an actual ``runbolt`` process over TCP, which is the only way
to give the limit a real peer address. The in-process ``TestClient`` builds its
request with ``TestRequest``, which leaves ``peer_addr`` unset, so every caller
resolves to the same constant and no proxy can ever be trusted. Configuration
errors, which need no peer, are covered in
``python/tests/test_rate_limit_client_ip.py``.

``BOLT_TRUSTED_PROXIES`` is read once per process at route registration, so the
trusting case and the strict case need one server each. Everything else shares
those two.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .apps.rate_limit_client_ip import BURST

pytestmark = pytest.mark.server_integration

# RFC 5737 documentation addresses, so nothing here looks routable.
CALLER = "203.0.113.10"
OTHER_CALLER = "203.0.113.11"


def _exhaust(server, forwarded_for):
    """Spend the bucket for one caller. Return the last status code."""
    status = None
    for _ in range(BURST + 2):
        status = server.request("GET", "/limited", headers={"X-Forwarded-For": forwarded_for}).status_code
    return status


def test_forwarded_for_cannot_reset_the_bucket_without_a_trusted_proxy(make_server_project):
    """A client must not buy a new bucket by changing one header."""
    project = make_server_project(api_module=app_module("rate_limit_client_ip"))

    with project.start() as server:
        assert _exhaust(server, CALLER) == 429

        # Both callers reach the server from the same address, so the limit
        # still applies no matter what the header says.
        response = server.request("GET", "/limited", headers={"X-Forwarded-For": OTHER_CALLER})

    assert response.status_code == 429, response.text


def test_a_trusted_proxy_makes_the_header_usable_again(make_server_project):
    """
    One server covers both halves of the trusting case.

    Spinning a second ``runbolt`` for the spoof check is not worth the port
    pressure, and both halves read the same setting.
    """
    project = make_server_project(
        api_module=app_module("rate_limit_client_ip"),
        settings_extra='BOLT_TRUSTED_PROXIES = ["127.0.0.0/8"]',
    )

    with project.start() as server:
        # The peer is 127.0.0.1 and the setting covers it, so the client is
        # CALLER: the rightmost entry that is not a declared proxy.
        assert _exhaust(server, CALLER) == 429

        # A second caller through the same proxy gets its own bucket.
        allowed = server.request("GET", "/limited", headers={"X-Forwarded-For": OTHER_CALLER})

        # The first caller now invents a prefix. It resolves to the same bucket.
        spoofed = server.request("GET", "/limited", headers={"X-Forwarded-For": f"{OTHER_CALLER}, {CALLER}"})

    assert allowed.status_code == 200, allowed.text
    assert spoofed.status_code == 429, spoofed.text
