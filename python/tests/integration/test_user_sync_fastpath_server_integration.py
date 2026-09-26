"""``request.user`` on the sync-dispatch fast path of a real ``runbolt`` server.

A sync read of ``request.user`` runs its query on the thread that reads it.
Rust installs the WorkerLoop around the trivially-async fast path, so Django's
``async_unsafe`` guard sees a running loop there. The loader hides the loop
during the query. The plain sync fast path has no running loop.

This needs a real server: ``TestClient`` never takes the sync-dispatch branch
(``can_sync_dispatch``), so it cannot reach either fast path.
"""

from __future__ import annotations

import time

import jwt
import pytest

from .apps import app_module
from .apps.user_sync_fastpath import SECRET

pytestmark = pytest.mark.server_integration


def _token() -> str:
    return jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")


@pytest.mark.parametrize("allow_unsafe", [False, True], ids=["strict", "allow_async_unsafe"])
def test_request_user_loads_on_both_sync_fast_paths(make_server_project, allow_unsafe):
    project = make_server_project(api_module=app_module("user_sync_fastpath"))
    env = {"DJANGO_ALLOW_ASYNC_UNSAFE": "1"} if allow_unsafe else {}
    headers = {"Authorization": f"Bearer {_token()}"}

    with project.start(extra_args=["--workers", "1"], env=env) as server:
        trivial = server.get("/me-trivial/1", headers=headers)
        plain = server.get("/me-sync/1", headers=headers)

    assert trivial.status_code == 200, trivial.text
    assert trivial.json()["username"] == "bob"
    # The worker thread has a running loop here. The query still runs on it, with no hop.
    assert trivial.json()["loaded_on"] == trivial.json()["handler_on"]

    assert plain.status_code == 200, plain.text
    assert plain.json()["username"] == "bob"
    # No running loop: the query runs on the worker thread that runs the handler.
    assert plain.json()["loaded_on"] == plain.json()["handler_on"]
