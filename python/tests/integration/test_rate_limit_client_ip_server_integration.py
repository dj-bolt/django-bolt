"""Real-server tests for how a rate limit picks its bucket key.

These run against an actual ``runbolt`` process over TCP, which is the only way
to give the limit a real peer address. The in-process ``TestClient`` builds its
request with ``TestRequest``, which leaves ``peer_addr`` unset, so every caller
resolves to the same constant and no proxy can ever be trusted.

``BOLT_TRUSTED_PROXIES`` is read once per process at server startup, so each
trust setting needs one server. The tests for one setting share that server.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .apps.rate_limit_client_ip import BURST

pytestmark = pytest.mark.server_integration

# RFC 5737 and RFC 3849 documentation addresses, so nothing here looks routable.
CALLER = "203.0.113.10"
OTHER_CALLER = "203.0.113.11"
THIRD_CALLER = "203.0.113.12"
CALLER_V6 = "2001:db8::9"
PEER = "127.0.0.1"

APP = app_module("rate_limit_client_ip")
TRUST_LOOPBACK = 'BOLT_TRUSTED_PROXIES = ["127.0.0.0/8"]'


def _exhaust(server, path="/limited", **headers):
    """Spend the bucket for one caller. Return the last status code."""
    status = None
    for _ in range(BURST + 2):
        status = server.request("GET", path, headers=headers).status_code
    return status


def _remote_addr(server, headers):
    response = server.request("GET", "/remote-addr", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["remote_addr"]


# ---------------------------------------------------------------------------
# Default: no trusted proxy
# ---------------------------------------------------------------------------


def test_without_a_trusted_proxy_every_forwarding_header_is_ignored(make_server_project):
    """A client must not buy a new bucket by changing one header."""
    project = make_server_project(api_module=APP)

    with project.start() as server:
        # REMOTE_ADDR is the peer no matter what the headers say.
        assert _remote_addr(server, {"X-Forwarded-For": CALLER}) == PEER
        assert _remote_addr(server, {"X-Real-IP": CALLER}) == PEER

        assert _exhaust(server, **{"X-Forwarded-For": CALLER}) == 429

        # Both callers reach the server from the same address, so the limit
        # still applies no matter what the header says.
        forwarded = server.request("GET", "/limited", headers={"X-Forwarded-For": OTHER_CALLER})
        real_ip = server.request("GET", "/limited", headers={"X-Real-IP": OTHER_CALLER})
        bare = server.request("GET", "/limited")

    assert forwarded.status_code == 429, forwarded.text
    assert real_ip.status_code == 429, real_ip.text
    assert bare.status_code == 429, bare.text


def test_a_header_key_buckets_on_the_header_value(make_server_project):
    """``key="X-Tenant"`` buckets on that header and ignores the address."""
    project = make_server_project(api_module=APP)

    with project.start() as server:
        assert _exhaust(server, "/limited-by-header", **{"X-Tenant": "acme"}) == 429

        other_tenant = server.request("GET", "/limited-by-header", headers={"X-Tenant": "globex"})
        # Header names are matched without regard to case.
        same_tenant = server.request("GET", "/limited-by-header", headers={"x-tenant": "acme"})

        # Callers that send no header share one bucket.
        assert _exhaust(server, "/limited-by-header") == 429
        missing_again = server.request("GET", "/limited-by-header")

        # The header route and the address route keep separate buckets.
        by_ip = server.request("GET", "/limited")

        # A long value is hashed like any other, so nothing caps its length.
        # `key="authorization"` with a JWT lands here.
        long_value = server.request("GET", "/limited-by-header", headers={"X-Tenant": "t" * 4096})
        other_long_value = server.request(
            "GET", "/limited-by-header", headers={"X-Tenant": "u" * 4096}
        )

    assert other_tenant.status_code == 200, other_tenant.text
    assert same_tenant.status_code == 429, same_tenant.text
    assert missing_again.status_code == 429, missing_again.text
    assert by_ip.status_code == 200, by_ip.text
    # Two long values, two buckets. Neither is rejected for its length.
    assert long_value.status_code == 200, long_value.text
    assert other_long_value.status_code == 200, other_long_value.text


# ---------------------------------------------------------------------------
# Trusted proxy that covers the peer
# ---------------------------------------------------------------------------


def test_a_trusted_proxy_makes_the_header_usable_again(make_server_project):
    """
    One server covers both halves of the trusting case.

    Spinning a second ``runbolt`` for the spoof check is not worth the port
    pressure, and both halves read the same setting.
    """
    project = make_server_project(api_module=APP, settings_extra=TRUST_LOOPBACK)

    with project.start() as server:
        # The peer is 127.0.0.1 and the setting covers it, so the client is
        # CALLER: the rightmost entry that is not a declared proxy.
        assert _exhaust(server, **{"X-Forwarded-For": CALLER}) == 429

        # A second caller through the same proxy gets its own bucket.
        allowed = server.request("GET", "/limited", headers={"X-Forwarded-For": OTHER_CALLER})

        # The first caller now invents a prefix. It resolves to the same bucket.
        spoofed = server.request("GET", "/limited", headers={"X-Forwarded-For": f"{OTHER_CALLER}, {CALLER}"})

        # A caller that sends no header is keyed on the peer, a third bucket.
        bare = server.request("GET", "/limited")

    assert allowed.status_code == 200, allowed.text
    assert spoofed.status_code == 429, spoofed.text
    assert bare.status_code == 200, bare.text


def test_remote_addr_follows_the_forwarding_rules(make_server_project):
    """Django code and the limiter must agree on the canonical client IP."""
    project = make_server_project(api_module=APP, settings_extra=TRUST_LOOPBACK)

    with project.start() as server:
        # Rule 3: rightmost entry that is not a trusted proxy.
        assert _remote_addr(server, {"X-Forwarded-For": CALLER}) == CALLER
        assert _remote_addr(server, {"X-Forwarded-For": f"{OTHER_CALLER}, {CALLER}, 127.0.0.7"}) == CALLER

        # Rule 4: every entry is trusted, so the leftmost is the client.
        assert _remote_addr(server, {"X-Forwarded-For": "127.0.0.55, 127.0.0.7"}) == "127.0.0.55"

        # Rule 5: X-Real-IP is used when X-Forwarded-For is absent, not as a
        # second opinion when it is present.
        assert _remote_addr(server, {"X-Real-IP": CALLER}) == CALLER
        assert _remote_addr(server, {"X-Forwarded-For": CALLER, "X-Real-IP": OTHER_CALLER}) == CALLER
        assert _remote_addr(server, {"X-Real-IP": "attacker-chosen-bucket"}) == PEER

        # Rule 6: a malformed hop never exposes an entry farther left.
        assert _remote_addr(server, {"X-Forwarded-For": f"{OTHER_CALLER}, not-an-ip, 127.0.0.1"}) == PEER
        assert _remote_addr(server, {"X-Forwarded-For": "not-an-ip"}) == PEER
        assert _remote_addr(server, {"X-Forwarded-For": ""}) == PEER
        assert _remote_addr(server, {"X-Forwarded-For": f"{CALLER},,127.0.0.1"}) == PEER

        # Proxy output with a port is accepted for both families.
        assert _remote_addr(server, {"X-Forwarded-For": f"{CALLER}:54321"}) == CALLER
        assert _remote_addr(server, {"X-Forwarded-For": f"[{CALLER_V6}]:443"}) == CALLER_V6
        assert _remote_addr(server, {"X-Forwarded-For": CALLER_V6}) == CALLER_V6

        # An IPv4-mapped IPv6 entry normalizes to IPv4.
        assert _remote_addr(server, {"X-Forwarded-For": f"::ffff:{CALLER}"}) == CALLER

        # Whitespace around a comma does not matter.
        assert _remote_addr(server, {"X-Forwarded-For": f"{CALLER}  ,  127.0.0.9"}) == CALLER


def test_duplicate_forwarded_for_headers_are_read_in_wire_order(make_server_project):
    """Two ``X-Forwarded-For`` headers form one chain, earlier header first."""
    project = make_server_project(api_module=APP, settings_extra=TRUST_LOOPBACK)

    with project.start() as server:
        chain = [("X-Forwarded-For", f"{OTHER_CALLER}, {CALLER}"), ("X-Forwarded-For", "127.0.0.7")]
        assert _remote_addr(server, chain) == CALLER

        # A malformed hop in the later header hides the earlier header.
        broken = [("X-Forwarded-For", OTHER_CALLER), ("X-Forwarded-For", "not-an-ip")]
        assert _remote_addr(server, broken) == PEER

        # The limiter keys on the same chain.
        status = None
        for _ in range(BURST + 2):
            status = server.request("GET", "/limited", headers=chain).status_code
        assert status == 429
        spoofed = server.request(
            "GET", "/limited", headers=[("X-Forwarded-For", THIRD_CALLER), ("X-Forwarded-For", CALLER)]
        )
        fresh = server.request("GET", "/limited", headers=[("X-Forwarded-For", THIRD_CALLER)])

    assert spoofed.status_code == 429, spoofed.text
    assert fresh.status_code == 200, fresh.text


def test_equivalent_addresses_share_one_bucket(make_server_project):
    """One client, spelled three ways, is one bucket."""
    project = make_server_project(api_module=APP, settings_extra=TRUST_LOOPBACK)

    with project.start() as server:
        assert _exhaust(server, **{"X-Forwarded-For": CALLER}) == 429
        mapped = server.request("GET", "/limited", headers={"X-Forwarded-For": f"::ffff:{CALLER}"})
        with_port = server.request("GET", "/limited", headers={"X-Forwarded-For": f"{CALLER}:4000"})
        padded = server.request("GET", "/limited", headers={"X-Forwarded-For": f"{CALLER} , 127.0.0.9"})

    assert mapped.status_code == 429, mapped.text
    assert with_port.status_code == 429, with_port.text
    assert padded.status_code == 429, padded.text


# ---------------------------------------------------------------------------
# Trusted proxy that does not cover the peer
# ---------------------------------------------------------------------------


def test_a_peer_outside_the_trusted_list_is_not_believed(make_server_project):
    """The setting may be a tuple. A peer outside it proves nothing."""
    project = make_server_project(api_module=APP, settings_extra='BOLT_TRUSTED_PROXIES = ("10.0.0.0/8", "::1")')

    with project.start() as server:
        assert _remote_addr(server, {"X-Forwarded-For": CALLER}) == PEER
        assert _exhaust(server, **{"X-Forwarded-For": CALLER}) == 429
        response = server.request("GET", "/limited", headers={"X-Forwarded-For": OTHER_CALLER})

    assert response.status_code == 429, response.text


def test_a_bare_address_entry_matches_only_itself(make_server_project):
    """``127.0.0.2`` does not cover the peer ``127.0.0.1``."""
    project = make_server_project(api_module=APP, settings_extra='BOLT_TRUSTED_PROXIES = ["127.0.0.2"]')

    with project.start() as server:
        assert _remote_addr(server, {"X-Forwarded-For": CALLER}) == PEER


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setting",
    [
        '["10.0.0.0/8", "not-an-ip"]',
        '["10.0.0.0/33"]',
        '["::1/129"]',
        '["10.0.0.0/eight"]',
        '"10.0.0.0/8"',
        "[10]",
    ],
)
def test_an_invalid_setting_aborts_startup(make_server_project, setting):
    """A wrong list must stop the server, not run with a weaker limit."""
    project = make_server_project(api_module=APP, settings_extra=f"BOLT_TRUSTED_PROXIES = {setting}")

    with pytest.raises(AssertionError) as excinfo, project.start(timeout=15):
        pass
    assert "BOLT_TRUSTED_PROXIES" in str(excinfo.value)
