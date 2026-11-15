"""Django's connection pool and dead connections against a real PostgreSQL database, in-process.

``test_dead_connection_recovery_server_integration.py`` checks the same code
on a real ``runbolt`` server. The tests here call the public helpers of
``django_bolt.concurrency`` on threads of the test, so they can hold a
connection on a thread, end its backend, and read the state of the pool.

Set ``DJANGO_BOLT_TEST_POSTGRES_DSN`` to run them. Without the variable, they skip.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import pytest
from django.db import OperationalError, connections, transaction
from django.db.backends.signals import connection_created
from django.utils.deprecation import MiddlewareMixin
from psycopg_pool import ConnectionPool

from django_bolt import BoltAPI, concurrency
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.testing import TestClient

from .test_dead_connection_recovery_server_integration import backend_pids, terminate_backends

pytestmark = pytest.mark.postgres

ALIAS = "pooled"
# The pool has a fixed size, so its statistics count no connection that opens in the background.
SIZE = 3


class Pooled(NamedTuple):
    params: dict[str, Any]
    application_name: str

    def pids(self) -> set[int]:
        return backend_pids(self.params, self.application_name)

    def wait_for_backends(self, count: int) -> set[int]:
        """Wait until the pool has ``count`` connections to the server, and return their backends."""
        deadline = time.monotonic() + 10
        while len(pids := self.pids()) != count:
            assert time.monotonic() < deadline, f"{len(pids)} backends, expected {count}"
            time.sleep(0.02)
        return pids

    def checked_out(self) -> int:
        stats = connections[ALIAS].pool.get_stats()
        return stats["pool_size"] - stats["pool_available"]


@pytest.fixture
def pooled(postgres_database, monkeypatch, django_db_blocker):
    """Add the database ``pooled`` to Django: the PostgreSQL test database behind a connection pool."""
    application_name = f"bolt-pooled-{secrets.token_hex(4)}"
    database = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": postgres_database["dbname"],
        "USER": postgres_database.get("user", ""),
        "PASSWORD": postgres_database.get("password", ""),
        "HOST": postgres_database.get("host", ""),
        "PORT": postgres_database.get("port", ""),
        "OPTIONS": {
            "application_name": application_name,
            "pool": {"min_size": SIZE, "max_size": SIZE, "timeout": 5},
        },
    }
    configured = connections.configure_settings({"default": connections.settings["default"], ALIAS: database})
    monkeypatch.setitem(connections.settings, ALIAS, configured[ALIAS])
    # The state of the module that this database changes.
    for name in ("_pooled_aliases", "_check_before_call", "_generation_epoch"):
        monkeypatch.setattr(concurrency, name, getattr(concurrency, name))
    monkeypatch.setattr(concurrency, "_generations", dict(concurrency._generations))
    monkeypatch.setattr(concurrency, "_pool_epochs", {})
    concurrency.configure_connection_checks()
    with django_db_blocker.unblock():
        try:
            pooled = Pooled(postgres_database, application_name)
            # The first checkout opens the pool. Wait until each connection is available.
            connections[ALIAS].ensure_connection()
            connections[ALIAS].close()
            connections[ALIAS].pool.wait(timeout=10)
            yield pooled
        finally:
            connection = connections[ALIAS]
            connection.close()
            connection.close_pool()
            del connections[ALIAS]


def blocking(fn: Callable[..., Any], *args: Any) -> Any:
    """Run ``fn`` as a sync read on an event loop runs: one guarded call on the calling thread."""

    async def call() -> Any:
        return concurrency.run_orm_blocking(fn, *args)

    return asyncio.run(call())


def select_one() -> int:
    with connections[ALIAS].cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone()[0]


def test_a_nested_call_keeps_the_pooled_connection_until_the_outermost_call_ends(pooled):
    """A guarded call can run a loop of its own, and that loop can make a guarded call on the same thread.

    The inner call must not give the connection back, because the outer call
    still uses it. The outermost call gives it back.
    """
    checkouts = []

    def count_checkout(sender, connection, **kwargs) -> None:
        checkouts.append(connection.alias)

    def outer() -> tuple[int, int, int]:
        select_one()
        inner = blocking(select_one)
        return inner, select_one(), pooled.checked_out()

    # Django sends connection_created for each checkout from a pool.
    connection_created.connect(count_checkout)
    try:
        assert blocking(outer) == (1, 1, 1)
    finally:
        connection_created.disconnect(count_checkout)
    assert checkouts == [ALIAS]
    assert pooled.checked_out() == 0


def test_a_pooled_connection_goes_back_after_its_atomic_block(pooled):
    """An open ``atomic`` block owns its connection, so a call inside the block does not give it back.

    The thread must not forget that it holds the connection. The first call
    after the block gives it back.
    """
    with transaction.atomic(using=ALIAS):
        assert blocking(select_one) == 1
        assert pooled.checked_out() == 1
    assert blocking(select_one) == 1
    assert pooled.checked_out() == 0


def test_close_connections_and_pools_closes_the_pool_of_a_database_after_one_with_no_pool(pooled):
    """``runbolt`` calls this before each fork. A pool that stays open shares its connections with the worker.

    ``default`` has no pool and comes before ``pooled`` in the settings.
    """
    assert list(connections.settings) == ["default", ALIAS]
    assert blocking(select_one) == 1
    assert len(pooled.pids()) == SIZE

    concurrency.close_connections_and_pools()

    assert pooled.wait_for_backends(0) == set()


class _PassMiddleware(MiddlewareMixin):
    pass


def _lane_api() -> BoltAPI:
    """A sync route behind Django middleware: its request runs complete on a lane."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_PassMiddleware])])

    @api.get("/query")
    def query():
        # The handler handles the error, so only the end of the lane request sees the dead connection.
        try:
            return {"result": select_one()}
        except OperationalError:
            return {"result": "OperationalError"}

    return api


@pytest.mark.parametrize("found_by", ["a-call-that-raises", "the-end-of-a-lane-request"])
def test_each_event_that_ends_the_connections_drains_the_pool_one_time(pooled, found_by):
    """A restart of PostgreSQL ends each connection of the pool. Bolt drains the pool at the first dead connection.

    A second thread holds a connection from before the restart, and it meets
    its dead connection after the drain. That connection is part of the same
    event, so it must not drain the pool again: a drain closes the new idle
    connections. A second restart is a new event, and Bolt drains the pool again.
    """
    with TestClient(_lane_api()) as client:
        for _ in range(2):
            holding, go = threading.Event(), threading.Event()
            errors = []

            def hold_a_connection_then_query(holding=holding, go=go) -> None:
                select_one()
                holding.set()
                go.wait(10)
                select_one()

            def on_a_second_thread(call=hold_a_connection_then_query, errors=errors) -> None:
                try:
                    blocking(call)
                except OperationalError as error:
                    errors.append(error)

            thread = threading.Thread(target=on_a_second_thread)
            thread.start()
            try:
                assert holding.wait(10)
                terminate_backends(pooled.params, pooled.application_name)

                # This call gets a dead idle connection of the pool.
                if found_by == "a-call-that-raises":
                    with pytest.raises(OperationalError):
                        blocking(select_one)
                else:
                    assert client.get("/query").json() == {"result": "OperationalError"}
                # Without a drain, each other dead idle connection fails one call.
                for _ in range(SIZE + 1):
                    assert blocking(select_one) == 1
                new = pooled.wait_for_backends(SIZE - 1)
            finally:
                go.set()
                thread.join()
            assert len(errors) == 1

            # The pool replaces the connection of the second thread. A drain would replace the others too.
            assert pooled.wait_for_backends(SIZE) >= new


@pytest.mark.parametrize(
    ("pool", "health_checks", "warning"),
    [
        pytest.param({"min_size": 1}, False, None, id="pool"),
        pytest.param({"min_size": 1, "check": ConnectionPool.check_connection}, False, "a check callback.", id="check"),
        pytest.param({"min_size": 1}, True, "a check callback through CONN_HEALTH_CHECKS.", id="health-checks"),
        # CONN_HEALTH_CHECKS of a database with no pool adds no check to a pool.
        pytest.param(None, True, None, id="no-pool"),
    ],
)
def test_check_pool_environment_warns_only_for_a_pool_with_a_check(
    pooled, monkeypatch, caplog, pool, health_checks, warning
):
    """The startup check logs one warning for a pool whose checkouts run a check, and nothing for other databases.

    The installed psycopg_pool has ``drain()``, so the check logs no warning about its version.
    """
    options = {"pool": pool} if pool else {}
    caplog.set_level(logging.WARNING, logger="django_bolt")
    # The settings go back before the fixture closes the pool of this database.
    with monkeypatch.context() as patch:
        # An earlier test can leave Bolt's logging setup in place, which stops the records at its own handler.
        patch.setattr(logging.getLogger("django_bolt"), "propagate", True)
        patch.setitem(connections.settings[ALIAS], "OPTIONS", options)
        patch.setitem(connections.settings[ALIAS], "CONN_HEALTH_CHECKS", health_checks)
        concurrency.check_pool_environment()

    messages = [record.getMessage() for record in caplog.records if record.name == "django_bolt.concurrency"]
    if warning is None:
        assert messages == []
    else:
        assert len(messages) == 1, messages
        assert f"DATABASES[{ALIAS!r}] gives its connection pool {warning} " in messages[0]
