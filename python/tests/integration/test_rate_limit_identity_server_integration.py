"""Real-server tests for `@rate_limit(key="user")` and `key="api_key"`.

The identity check runs after authentication in the production `runbolt`
HTTP dispatch and in the WebSocket upgrade. `TestClient` reaches the same
Rust functions through a separate entry point, so these tests confirm the
server wiring over real TCP. Tokens come from the public
`create_jwt_for_user()` API.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from django_bolt.auth import create_jwt_for_user

from .apps import app_module
from .apps.rate_limit_identity import BURST, SECRET
from .helpers import attempt_ws_upgrade

pytestmark = pytest.mark.server_integration

APP = app_module("rate_limit_identity")


def _bearer(username: str) -> dict[str, str]:
    # Unsaved model instance: the server verifies the signature and `sub`
    # claim only. No database access is needed on either side.
    user = get_user_model()(id=abs(hash(username)) % 100_000 + 1, username=username)
    return {"Authorization": f"Bearer {create_jwt_for_user(user, secret=SECRET)}"}


def _statuses(server, path, n, headers=None):
    return [server.request("GET", path, headers=headers or {}).status_code for _ in range(n)]


def test_each_user_gets_its_own_bucket(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start() as server:
        for name in ("alice", "bob", "carol"):
            assert _statuses(server, "/limited-by-user", BURST, _bearer(name)) == [200] * BURST
        assert _statuses(server, "/limited-by-user", 1, _bearer("alice")) == [429]
        # A new user is not affected by the exhausted buckets.
        assert _statuses(server, "/limited-by-user", 1, _bearer("dave")) == [200]


def test_unauthenticated_callers_share_the_peer_bucket(make_server_project):
    """No identity: the peer address is the bucket. A valid user is untouched."""
    project = make_server_project(api_module=APP)

    with project.start() as server:
        assert _statuses(server, "/limited-by-user", BURST + 1) == [200] * BURST + [429]
        # A bad token is not an identity either and lands in the same bucket.
        assert _statuses(server, "/limited-by-user", 1, {"Authorization": "Bearer not-a-jwt"}) == [429]
        assert _statuses(server, "/limited-by-user", BURST, _bearer("alice")) == [200] * BURST


def test_guard_rejects_before_the_bucket_is_counted(make_server_project):
    """A 401 spends nothing from the bucket it would have been counted in.

    `/limited-by-api-key-guarded` keys on `api_key` but authenticates with
    JWT, so the valid caller has no API-key identity and shares the
    peer-address bucket with the rejected callers. On `/limited-by-user-guarded`
    the two use different buckets, so it only shows that a 401 comes first.
    """
    project = make_server_project(api_module=APP)

    with project.start() as server:
        path = "/limited-by-api-key-guarded"
        assert _statuses(server, path, BURST + 2) == [401] * (BURST + 2)
        # Same bucket as the rejected calls above. Full burst means they left
        # it untouched.
        assert _statuses(server, path, BURST, _bearer("alice")) == [200] * BURST
        # Spent now, which confirms the caller really was counted in that
        # bucket rather than in one of its own.
        assert _statuses(server, path, 1, _bearer("bob")) == [429]

        assert _statuses(server, "/limited-by-user-guarded", BURST + 2) == [401] * (BURST + 2)
        assert _statuses(server, "/limited-by-user-guarded", BURST, _bearer("alice")) == [200] * BURST
        assert _statuses(server, "/limited-by-user-guarded", 1, _bearer("alice")) == [429]


def test_each_api_key_gets_its_own_bucket_and_unknown_keys_share_the_peer(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start() as server:
        assert _statuses(server, "/limited-by-api-key", BURST, {"X-API-Key": "key-a"}) == [200] * BURST
        assert _statuses(server, "/limited-by-api-key", BURST, {"X-API-Key": "key-b"}) == [200] * BURST
        assert _statuses(server, "/limited-by-api-key", 1, {"X-API-Key": "key-a"}) == [429]
        # Unknown keys have no identity. They share the peer-address bucket, so
        # a caller cannot buy a fresh bucket per request by changing the key.
        bogus = [
            server.request("GET", "/limited-by-api-key", headers={"X-API-Key": f"no-{i}"}).status_code
            for i in range(BURST + 1)
        ]
        assert bogus == [200] * BURST + [429]


def test_websocket_upgrade_is_limited_per_api_key(make_server_project):
    """The production upgrade path runs the identity check after auth.

    Only the handshake status matters here: 101 for an allowed upgrade, 429
    for a spent bucket.
    """
    project = make_server_project(api_module=APP)

    def status(key: str) -> str:
        line, _ = attempt_ws_upgrade(server.host, server.port, "/ws/limited-by-api-key", headers={"X-API-Key": key})
        return line.split(" ", 2)[1]

    with project.start() as server:
        assert [status("key-a") for _ in range(BURST)] == ["101"] * BURST
        assert [status("key-b") for _ in range(BURST)] == ["101"] * BURST
        assert status("key-a") == "429"
        # Unknown keys share the peer bucket.
        assert [status(f"no-{i}") for i in range(BURST + 1)] == ["101"] * BURST + ["429"]


def test_identity_key_without_auth_aborts_startup(make_server_project):
    """No backend can ever give an identity, so the server must not boot."""
    project = make_server_project(api_module=app_module("rate_limit_identity_no_auth"))

    with pytest.raises(AssertionError) as excinfo, project.start(timeout=15):
        pass
    assert 'key="user"' in str(excinfo.value)
