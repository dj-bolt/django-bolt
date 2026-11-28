"""``BOLT_ASYNC_ORM_ON_LOOP`` with a database that the event loop cannot query.

The loop connections are PostgreSQL connections. A read from another database
must use the ORM thread pool, as with the setting off.
``integration/test_async_orm_on_loop_postgres.py`` has the PostgreSQL tests.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.db.models import QuerySet
from django.test import override_settings

from django_bolt import BoltAPI, concurrency
from django_bolt.testing import TestClient


class SqliteThreadNameField(models.CharField):
    """A field that reads as the name of the thread that built its row."""

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> str:
        return threading.current_thread().name


class SqliteLoopProbe(models.Model):
    label = models.CharField(max_length=50)
    built_on = SqliteThreadNameField(max_length=50, default="")

    class Meta:
        app_label = "django_bolt"


@pytest.fixture
def probe_table():
    table_name = SqliteLoopProbe._meta.db_table
    with connection.schema_editor() as schema_editor:
        if table_name not in connection.introspection.table_names():
            schema_editor.create_model(SqliteLoopProbe)
    SqliteLoopProbe.objects.create(label="a")
    try:
        yield
    finally:
        SqliteLoopProbe.objects.all().delete()
        with connection.schema_editor() as schema_editor:
            if table_name in connection.introspection.table_names():
                schema_editor.delete_model(SqliteLoopProbe)


@pytest.fixture
def on_loop():
    """Turn the setting on for the servers of the test."""
    with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True):
        yield
    # A server that starts with the setting off gives Django its own methods back.
    concurrency.check_pool_environment()


def _api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/threads")
    async def threads():
        return {
            "async_for": [probe.built_on async for probe in SqliteLoopProbe.objects.all()],
            "aget": (await SqliteLoopProbe.objects.aget(label="a")).built_on,
        }

    return api


@pytest.mark.django_db(transaction=True)
def test_a_sqlite_read_uses_the_orm_pool(on_loop, probe_table):
    with TestClient(_api()) as client:
        response = client.get("/threads")

    assert response.status_code == 200, response.text
    threads = response.json()
    # The two reads are two calls to the pool. A pool with more than one thread can give each call its own thread.
    assert threads["aget"].startswith("bolt_orm"), threads
    assert len(threads["async_for"]) == 1, threads
    assert threads["async_for"][0].startswith("bolt_orm"), threads


@pytest.mark.parametrize("value", ["0", "many"])
@pytest.mark.parametrize("name", ["DJANGO_BOLT_LOOP_CONNECTIONS", "DJANGO_BOLT_LOOP_PIPELINE_DEPTH"])
def test_an_invalid_count_stops_the_server_at_startup(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ImproperlyConfigured, match=f"{name} must be an int of 1 or more"):
        TestClient(_api())


# A timer cannot wait for 1e19 seconds. Without the check at startup, each read would fail.
@pytest.mark.parametrize("value", ["0", "-1", "soon", "nan", "inf", "1e19"])
@pytest.mark.parametrize("name", ["ACQUIRE", "IDLE", "LIFETIME"])
def test_an_invalid_time_stops_the_server_at_startup(monkeypatch, name, value):
    variable = f"DJANGO_BOLT_LOOP_{name}_SECONDS"
    monkeypatch.setenv(variable, value)

    with pytest.raises(ImproperlyConfigured, match=f"{variable} must be a number more than 0"):
        TestClient(_api())


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_setting_that_is_not_a_bool_stops_the_server_at_startup(value):
    with (
        override_settings(BOLT_ASYNC_ORM_ON_LOOP=value),
        pytest.raises(ImproperlyConfigured, match="BOLT_ASYNC_ORM_ON_LOOP must be a bool, got "),
    ):
        TestClient(_api())
    # The next server starts with the setting off.
    concurrency.check_pool_environment()


def test_a_server_with_the_setting_off_keeps_the_queryset_method_of_another_library(monkeypatch):
    async def aget_of_a_library(self: Any, *args: Any, **kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(QuerySet, "aget", aget_of_a_library)
    with TestClient(_api()):
        kept = QuerySet.__dict__["aget"]

    assert kept is aget_of_a_library
