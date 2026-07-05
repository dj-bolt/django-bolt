from __future__ import annotations

import pytest

from . import helpers
from .apps import app_module
from .helpers import create_server_project


def test_api_module_requires_preserved_pythonpath(tmp_path):
    with pytest.raises(ValueError, match="preserve_pythonpath"):
        create_server_project(tmp_path, api_module=app_module("hello"), preserve_pythonpath=False)


def test_dev_start_requires_on_disk_api_for_api_module(make_server_project, monkeypatch):
    class FakeServer:
        def stop(self) -> None:
            pass

    def fake_spawn_process(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(helpers, "_spawn_process", fake_spawn_process)
    monkeypatch.setattr(helpers, "RunningServer", lambda **_kwargs: FakeServer())

    project = make_server_project(api_module=app_module("hello"))
    server = None
    try:
        with pytest.raises(ValueError, match="api_source"):
            server = project.start(dev=True, port=8765)
    finally:
        if server is not None:
            server.stop()
