"""Django middleware that keeps request state in ``threading.local``.

django-tenants has this shape: the middleware sets state on the current
thread, and later code on that thread reads it. Each route returns the state
that it sees, so a test can find a request that read the state of another one.
"""

from __future__ import annotations

import asyncio
import threading
import time

from asgiref.sync import sync_to_async
from django.db import connection
from django.db.backends.signals import connection_created
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI
from django_bolt.concurrency import in_lane_mode, sync_to_thread
from django_bolt.middleware import DjangoMiddlewareStack

_local = threading.local()
_created_connections = []


def _track_connection(sender, connection, **kwargs):
    _created_connections.append(connection)


connection_created.connect(_track_connection)


class ThreadLocalTenantMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _local.tenant = request.path.rsplit("/", 1)[-1]


def _read_tenant() -> str:
    # The sleep lets concurrent requests overlap on the server.
    time.sleep(0.005)
    return getattr(_local, "tenant", "<unset>")


api = BoltAPI(middleware=[DjangoMiddlewareStack([ThreadLocalTenantMiddleware])])


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/sync/{tenant}")
def sync_route(tenant: str):
    return {"expected": tenant, "actual": _read_tenant(), "on_lane": in_lane_mode()}


@api.get("/async/{tenant}")
async def async_route(tenant: str):
    await asyncio.sleep(0.005)
    return {"expected": tenant, "actual": await sync_to_thread(_read_tenant)}


@api.get("/db")
def db_route():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return {"value": cursor.fetchone()[0]}


@api.get("/connections")
async def open_connections():
    return {"open": sum(1 for conn in _created_connections if conn.connection is not None)}


def _expire_connection() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    # The age limit of CONN_MAX_AGE is in the past for the next request.
    connection.close_at = time.monotonic() - 1
    return len(_created_connections)


@api.get("/async-expire")
async def async_expire():
    return {"created": await sync_to_async(_expire_connection)()}


def _select_one() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        return cursor.fetchone()[0]


def _break_connection() -> None:
    _select_one()
    # The server side of a connection can go away between requests.
    connection.connection.close()


@api.get("/async-break")
async def async_break():
    await sync_to_async(_break_connection)()
    return {"broken": True}


@api.get("/async-db")
async def async_db_route():
    return {"value": await sync_to_async(_select_one)()}


@api.get("/sync-break-handled")
def sync_break_handled():
    _break_connection()
    try:
        _select_one()
    except Exception:
        # The handler handles the database error, so Bolt sees no exception.
        return {"handled": True}
    return {"handled": False}
