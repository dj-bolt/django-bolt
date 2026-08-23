"""Real-server test for `@rate_limit(key="api_key")`.

The identity check runs after authentication inside the production `runbolt`
dispatch. `TestClient` exercises the same Rust functions through a separate
entry point, so one test confirms the server wiring over real TCP.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .apps.rate_limit_identity import BURST

pytestmark = pytest.mark.server_integration

APP = app_module("rate_limit_identity")


def _statuses(server, n, **headers):
    return [server.request("GET", "/limited-by-api-key", headers=headers).status_code for _ in range(n)]


def test_each_api_key_gets_its_own_bucket_and_unknown_keys_share_the_peer(make_server_project):
    project = make_server_project(api_module=APP)

    with project.start() as server:
        assert _statuses(server, BURST, **{"X-API-Key": "key-a"}) == [200] * BURST
        assert _statuses(server, BURST, **{"X-API-Key": "key-b"}) == [200] * BURST
        assert _statuses(server, 1, **{"X-API-Key": "key-a"}) == [429]
        # Unknown keys have no identity. They share the peer-address bucket.
        bogus = [
            server.request("GET", "/limited-by-api-key", headers={"X-API-Key": f"no-{i}"}).status_code
            for i in range(BURST + 1)
        ]
        assert bogus == [200] * BURST + [429]
