"""Tests for request.method (HttpMethod enum round-trip + unreachable Unknown).

The Rust side stores the HTTP method as a compact ``HttpMethod`` enum with an
``Unknown`` variant that maps to the string ``"UNKNOWN"``. These tests lock in
two invariants:

1. ``request.method`` round-trips the exact verb for every standard method, so
   the enum mapping never silently degrades a real verb to ``"UNKNOWN"``.
2. Non-standard verbs are rejected by the router (404) *before* a ``PyRequest``
   is ever constructed, so the ``Unknown`` arm is unreachable via routing — a
   handler can never observe ``request.method == "UNKNOWN"``.

Invariant (2) is enforced at both ends in ``src/router.rs``: ``Router::register``
rejects any non-standard method with a ``ValueError`` (so no route can be
registered for one), and ``Router::find`` returns ``None`` for any non-standard
method (so a request carrying one 404s before ``PyRequest`` is built). It cannot
be exercised through ``TestClient``: the in-process harness coerces any
unsupported method to GET when building the Actix request
(``src/testing.rs`` ``_ => Method::GET``), so a non-standard verb never even
reaches routing there. Real-server coverage belongs in a ``server_integration``
test that opens a raw socket — see the plan note in the PR discussion.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient


@pytest.fixture(scope="module")
def api():
    api = BoltAPI()

    @api.get("/echo")
    async def echo_get(request):
        return {"method": request.method}

    @api.post("/echo")
    async def echo_post(request):
        return {"method": request.method}

    @api.put("/echo")
    async def echo_put(request):
        return {"method": request.method}

    @api.patch("/echo")
    async def echo_patch(request):
        return {"method": request.method}

    @api.delete("/echo")
    async def echo_delete(request):
        return {"method": request.method}

    @api.head("/echo")
    async def echo_head(request):
        # HEAD strips the body; nothing to echo, just prove the route runs.
        return {"method": request.method}

    return api


@pytest.fixture(scope="module")
def client(api):
    return TestClient(api)


class TestRequestMethodRoundTrip:
    """request.method must return the exact verb for every standard method."""

    @pytest.mark.parametrize(
        "verb",
        ["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def test_method_round_trips(self, client, verb):
        response = client.request(verb, "/echo")
        assert response.status_code == 200
        assert response.json()["method"] == verb, (
            f"request.method must report {verb!r}, not a degraded 'UNKNOWN'"
        )

    def test_head_route_is_reachable(self, client):
        # HEAD responses carry no body, so we can only assert the route matched
        # (proving HEAD maps to a real enum variant, not Unknown).
        response = client.head("/echo")
        assert response.status_code == 200
