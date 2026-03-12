import asyncio
import pytest
from django_bolt.api import BoltAPI


def test_startup_hook_registered():
    api = BoltAPI()

    @api.on_startup
    def my_hook():
        pass

    assert my_hook in api._startup_hooks


def test_shutdown_hook_registered():
    api = BoltAPI()

    @api.on_shutdown
    def my_hook():
        pass

    assert my_hook in api._shutdown_hooks


def test_sync_startup_hook_runs():
    api = BoltAPI()
    called = []

    @api.on_startup
    def my_hook():
        called.append("started")

    asyncio.run(api._run_lifecycle_hooks(api._startup_hooks))
    assert called == ["started"]


def test_async_shutdown_hook_runs():
    api = BoltAPI()
    called = []

    @api.on_shutdown
    async def my_hook():
        called.append("stopped")

    asyncio.run(api._run_lifecycle_hooks(api._shutdown_hooks))
    assert called == ["stopped"]