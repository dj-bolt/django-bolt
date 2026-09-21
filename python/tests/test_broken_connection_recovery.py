"""A broken database connection must not poison its executor thread.

Bolt keeps Django connections open across requests (no
``close_old_connections``). A connection that died stays on its thread, so
every later request on that thread must fail unless the framework drops it
on the error path.
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
import time

import pytest
from django.db import connection
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteWrapper

from django_bolt import BoltAPI, concurrency
from django_bolt.testing import TestClient


def _sqlite_is_usable(self) -> bool:
    # Django's sqlite backend hard-codes ``is_usable`` to True. Ping for real,
    # as the networked backends do, so a dead connection is detected.
    try:
        self.connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


@pytest.fixture
def single_thread_pool(monkeypatch):
    """Pin both pools to one thread so consecutive requests share a connection."""
    monkeypatch.setattr(SQLiteWrapper, "is_usable", _sqlite_is_usable)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="bolt_test")
    monkeypatch.setattr(concurrency, "_default_executor", pool)
    orm_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="bolt_test_orm", initializer=concurrency._mark_orm_thread
    )
    monkeypatch.setattr(concurrency, "_orm_executor", orm_pool)
    yield
    pool.shutdown(wait=True)
    orm_pool.shutdown(wait=True)


def _kill_then_query() -> None:
    # Close the DBAPI connection behind Django's back, then use it.
    connection.ensure_connection()
    connection.connection.close()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def _select_one() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone()[0]


@pytest.mark.django_db(transaction=True)
def test_blocking_sync_handler_recovers_after_dead_connection(single_thread_pool):
    api = BoltAPI()

    # ``time.sleep`` marks both handlers as blocking, so they run on the pool.
    @api.get("/break")
    def break_connection():
        time.sleep(0)
        _kill_then_query()
        return {"ok": True}

    @api.get("/ping")
    def ping():
        time.sleep(0)
        return {"value": _select_one()}

    with TestClient(api) as client:
        assert client.get("/break").status_code == 500
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.json()["value"] == 1


@pytest.mark.django_db(transaction=True)
def test_orm_executor_recovers_after_dead_connection(single_thread_pool):
    api = BoltAPI()

    @api.get("/break")
    async def break_connection():
        await concurrency.run_in_orm_executor(_kill_then_query)
        return {"ok": True}

    @api.get("/ping")
    async def ping():
        return {"value": await concurrency.run_in_orm_executor(_select_one)}

    with TestClient(api) as client:
        assert client.get("/break").status_code == 500
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.json()["value"] == 1
