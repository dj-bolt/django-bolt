"""runbolt installs the log backend of the Rust core at startup."""

from __future__ import annotations

import httpx
import pytest

from .apps import app_module

RUST_ERROR = (
    "[django-bolt] ERROR bolt_asgi::asgi_http: "
    "ASGI mount: coroutine raised an exception: RuntimeError: boom from the mounted app"
)


@pytest.mark.server_integration
def test_runbolt_writes_a_rust_error_record_to_stderr(make_server_project):
    project = make_server_project(api_module=app_module("rust_logging"))
    with project.start() as server:
        with httpx.Client(base_url=server.base_url, timeout=5) as client:
            assert client.get("/ok").status_code == 200
            assert client.get("/mounted/x").status_code == 500
        _stdout, stderr = server.stop()

    rust_records = [line for line in stderr.splitlines() if line.startswith("[django-bolt] ERROR ")]
    # Only the mounted app wrote a record. The normal route wrote none.
    assert rust_records == [RUST_ERROR]
