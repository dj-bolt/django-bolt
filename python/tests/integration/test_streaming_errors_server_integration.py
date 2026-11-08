"""runbolt ends a streaming body at its error and logs the error."""

from __future__ import annotations

import httpx
import pytest

from .apps import app_module

LOG_MESSAGE = "The body of a streaming response raised an error. The response body ends here."


@pytest.mark.server_integration
def test_runbolt_ends_a_streaming_body_at_its_error_and_logs_it(make_server_project):
    project = make_server_project(api_module=app_module("streaming_errors"))
    with project.start() as server:
        with httpx.Client(base_url=server.base_url, timeout=5) as client:
            sync_response = client.get("/sync")
            async_response = client.get("/async")
            # The server still answers after the two errors.
            assert client.get("/health").status_code == 200
        _stdout, stderr = server.stop()

    assert (sync_response.status_code, sync_response.content) == (200, b"first,")
    assert (async_response.status_code, async_response.content) == (200, b"first,")
    assert stderr.count(LOG_MESSAGE) == 2
    assert "ValueError: boom in the sync body" in stderr
    assert "ValueError: boom in the async body" in stderr
