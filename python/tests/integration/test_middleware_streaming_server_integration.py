"""A StreamingResponse of a Python middleware, behind Django middleware, over real TCP.

The in-process tests check the bridge. This test checks the runbolt request
pipeline: the chunks reach the client as a stream with the content type of the
response, and the Django middleware still sees the response.
"""

from __future__ import annotations

import pytest

from .apps import app_module

pytestmark = pytest.mark.server_integration


def test_middleware_streaming_response_streams_behind_django_middleware(make_server_project):
    project = make_server_project(api_module=app_module("middleware_streaming"))
    with project.start(startup_path="/health") as server:
        resp = server.get("/stream", headers={"Accept-Encoding": "identity"})

    assert resp.status_code == 200
    assert resp.headers.get("content-type") == "text/plain"
    assert resp.headers.get("x-seen") == "yes"
    assert resp.headers.get("content-length") is None
    assert resp.headers.get("transfer-encoding", "").lower() == "chunked"
    assert resp.content == b"chunk-0\nchunk-1\nchunk-2\n"
