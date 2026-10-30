"""
Tests for `settings.BOLT_TRUSTED_PROXIES`, the list that decides whether a
`key="ip"` rate limit may believe `X-Forwarded-For`, and for
`settings.BOLT_RATE_LIMIT_IPV6_PREFIX`.

The test client sends each request from 127.0.0.1. With that address in
`BOLT_TRUSTED_PROXIES`, `X-Forwarded-For` gives each request its client
address. The tests of the forwarding rules run against a real server, in
``python/tests/integration/test_rate_limit_client_ip_server_integration.py``.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.middleware import rate_limit
from django_bolt.testing import TestClient


def _api_with_limited_route():
    """Build an API with one route limited per IP."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=4, key="ip")
    async def limited():
        return {"ok": True}

    return api


@pytest.mark.parametrize(
    "entry",
    [
        "not-an-ip",
        "10.0.0.0/nine",
        "10.0.0.0/33",
        "::1/129",
        "fe80::1%eth0",
        "fe80::%1/64",
        "fe80::1%eth0/64",
        10,
        None,
    ],
)
def test_an_invalid_entry_fails_at_startup(entry):
    """A typo stops startup instead of quietly widening a bucket."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=["10.0.0.0/8", entry]),
        pytest.raises(ImproperlyConfigured, match="BOLT_TRUSTED_PROXIES"),
    ):
        TestClient(_api_with_limited_route())


@pytest.mark.parametrize(
    "configured",
    [
        pytest.param("10.0.0.0/8", id="string"),
        # Falsey, so a normalize-then-check order reads it as an empty list and
        # reports no proxies at all.
        pytest.param("", id="empty-string"),
        # Iterates its keys, which would look like a valid list of proxies.
        pytest.param({"10.0.0.0/8": "edge proxy"}, id="mapping"),
        pytest.param({}, id="empty-mapping"),
        # Not iterable, so the loop would raise a bare TypeError.
        pytest.param(42, id="integer"),
        pytest.param(True, id="boolean"),
    ],
)
def test_a_setting_that_is_not_a_list_fails_at_startup(configured):
    """Reject the whole setting by type, before anything iterates it."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=configured),
        pytest.raises(ImproperlyConfigured, match="must be a list"),
    ):
        TestClient(_api_with_limited_route())


@pytest.mark.parametrize("configured", [None, [], (), ["10.0.0.1"], ["10.0.0.0/8", "::1"], ("10.0.0.0/8",)])
def test_a_valid_setting_starts(configured):
    """Addresses and blocks are both accepted, in either family, list or tuple."""
    with override_settings(BOLT_TRUSTED_PROXIES=configured), TestClient(_api_with_limited_route()):
        pass


def test_an_invalid_deployment_setting_fails_even_without_a_limited_route():
    """The deployment-wide policy is validated independently of route shape."""
    with (
        override_settings(BOLT_TRUSTED_PROXIES=["not-an-ip"]),
        pytest.raises(ImproperlyConfigured, match="BOLT_TRUSTED_PROXIES"),
    ):
        api = BoltAPI()

        @api.get("/plain")
        async def plain():
            return {"ok": True}

        TestClient(api)


def test_the_ip_key_is_matched_without_regard_to_case():
    """`key="IP"` must key on the address, not on a header named `ip`."""
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=2, key="IP")
    async def limited():
        return {"ok": True}

    with TestClient(api) as client:
        for _ in range(3):
            status = client.get("/limited", headers={"ip": "a"}).status_code
        assert status == 429
        # A header named `ip` must not buy a new bucket.
        assert client.get("/limited", headers={"ip": "b"}).status_code == 429


@pytest.mark.parametrize("value", [-1, 129, 256, 64.0, "64", True])
def test_an_invalid_ipv6_prefix_fails_at_startup(value):
    """The prefix is an int from 0 to 128. A bool is not an int."""
    with (
        override_settings(BOLT_RATE_LIMIT_IPV6_PREFIX=value),
        pytest.raises(ImproperlyConfigured, match="BOLT_RATE_LIMIT_IPV6_PREFIX"),
    ):
        TestClient(_api_with_limited_route())


def _status_from(client, address):
    return client.get("/limited", headers={"X-Forwarded-For": address}).status_code


@pytest.mark.parametrize(
    ("prefix", "neighbour"),
    [
        pytest.param(None, 429, id="default-64"),
        pytest.param(56, 429, id="56"),
        pytest.param(128, 200, id="128"),
    ],
)
def test_an_ipv6_client_is_counted_per_network(prefix, neighbour):
    """A client cannot get a fresh bucket from each address of its /64."""
    settings = {"BOLT_TRUSTED_PROXIES": ["127.0.0.1"]}
    if prefix is not None:
        settings["BOLT_RATE_LIMIT_IPV6_PREFIX"] = prefix
    api = BoltAPI()

    @api.get("/limited")
    @rate_limit(rps=1, burst=2, key="ip")
    async def limited():
        return {"ok": True}

    with override_settings(**settings), TestClient(api) as client:
        statuses = [_status_from(client, "2001:db8:1:2::1") for _ in range(3)]
        assert statuses == [200, 200, 429]
        # Another address of the same /64.
        assert _status_from(client, "2001:db8:1:2::2") == neighbour
        # Another /48.
        assert _status_from(client, "2001:db8:2::1") == 200
        # Each IPv4 address keeps its own bucket.
        assert [_status_from(client, "203.0.113.7") for _ in range(3)] == [200, 200, 429]
        assert _status_from(client, "203.0.113.8") == 200
