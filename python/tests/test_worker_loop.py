"""Unit coverage for WorkerLoop helpers that lean on private asyncio APIs.

``_with_running_loop`` uses ``asyncio.events._get_running_loop`` /
``_set_running_loop``. These are stable across CPython 3.12-3.14 but private;
this test fails loudly if a future CPython moves them.
"""

from __future__ import annotations

import asyncio

import pytest

from django_bolt._worker_loop import _with_running_loop


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
