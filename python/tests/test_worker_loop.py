"""Unit coverage for WorkerLoop helpers that lean on private asyncio APIs.

``_with_running_loop`` uses ``asyncio.events._get_running_loop`` /
``_set_running_loop``. These are stable across CPython 3.12-3.14 but private;
this test fails loudly if a future CPython moves them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from django_bolt._worker_loop import _ThreadsafeServerProxy, _with_running_loop


def test_with_running_loop_installs_and_restores():
    loop = asyncio.new_event_loop()
    try:
        assert _with_running_loop(loop, asyncio.get_running_loop) is loop
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
    finally:
        loop.close()


def test_with_running_loop_restores_on_exception():
    loop = asyncio.new_event_loop()

    def boom():
        raise ValueError("boom")

    try:
        with pytest.raises(ValueError):
            _with_running_loop(loop, boom)
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
    finally:
        loop.close()


def test_server_proxy_close_clients_is_detected_and_dispatched():
    selector_loop = Mock()
    raw_server = Mock()
    proxy = _ThreadsafeServerProxy(SimpleNamespace(_selector_loop=selector_loop), raw_server)

    close_clients = proxy.close_clients
    close_clients()

    selector_loop.call_soon_threadsafe.assert_called_once_with(raw_server.close_clients)


def test_server_proxy_close_clients_lookup_preserves_missing_attribute():
    proxy = _ThreadsafeServerProxy(SimpleNamespace(_selector_loop=Mock()), object())

    with pytest.raises(AttributeError):
        _ = proxy.close_clients
