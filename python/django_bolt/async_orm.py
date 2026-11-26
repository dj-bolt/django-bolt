"""Run the read calls of Django's async ORM on the event loop of the worker.

Django's async ORM API is ``sync_to_async`` around the sync API, so each call
runs on a thread. With ``BOLT_ASYNC_ORM_ON_LOOP = True``, Bolt replaces these
read calls of ``QuerySet``: ``async for`` (``__aiter__``), ``aget()``,
``afirst()``, ``alast()`` and ``aexists()``.

The flow of one read:

1. Django compiles the QuerySet to SQL on the event loop thread. The compile
   step is CPU work and sends nothing to the database.
2. Rust sends the SQL on a loop connection, and reads the result: see
   ``_RustCursor``. The event loop runs other requests while the database
   works.
3. Django's own iterable class builds the result from the rows. Thus the
   field conversion and the model instances are those of the sync ORM.

Django opens each loop connection: see ``_connect``. The connection thus has
the parameters, the session state and the type adapters of each Django
connection to the same database, and the same query gives the same rows. No
thread has a loop connection, so it does not see the open transaction of a
thread. Each read that the loop cannot run as the sync ORM runs it uses
Django's own method, which runs on a thread: see ``_pool_for``.
"""

from __future__ import annotations

import asyncio
import collections
import ctypes
import logging
import os
import sys
import threading
from typing import Any

from asgiref.sync import AsyncToSync, SyncToAsync
from django.conf import settings
from django.core.exceptions import EmptyResultSet, ImproperlyConfigured, SynchronousOnlyOperation
from django.db import connections
from django.db.backends.utils import CursorDebugWrapper, CursorWrapper
from django.db.models.query import MAX_GET_RESULTS, QuerySet
from django.db.utils import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)

from . import _core
from ._worker_loop import WorkerLoop
from .concurrency import _thread_count, in_lane_mode, run_in_orm_executor

logger = logging.getLogger(__name__)

SETTING = "BOLT_ASYNC_ORM_ON_LOOP"
# The limits of a pool, with the defaults of the sqlx pool.
CONNECTIONS_VARIABLE = "DJANGO_BOLT_LOOP_CONNECTIONS"
DEFAULT_CONNECTIONS = 10
ACQUIRE_VARIABLE = "DJANGO_BOLT_LOOP_ACQUIRE_SECONDS"
DEFAULT_ACQUIRE_SECONDS = 30.0
IDLE_VARIABLE = "DJANGO_BOLT_LOOP_IDLE_SECONDS"
DEFAULT_IDLE_SECONDS = 600.0
LIFETIME_VARIABLE = "DJANGO_BOLT_LOOP_LIFETIME_SECONDS"
DEFAULT_LIFETIME_SECONDS = 1800.0

# The module of the backend that Bolt supports. A subclass can change how a
# connection opens or how a cursor runs a query, and a loop connection would
# not have that behavior.
_BACKEND_MODULE = "django.db.backends.postgresql.base"

# The order of ``DatabaseErrorWrapper``: the first match gives the Django class.
_DJANGO_ERRORS = (
    DataError,
    OperationalError,
    IntegrityError,
    InternalError,
    ProgrammingError,
    NotSupportedError,
    DatabaseError,
    InterfaceError,
    Error,
)

_django_aiter = QuerySet.__aiter__
_django_aget = QuerySet.aget
_django_afirst = QuerySet.afirst
_django_alast = QuerySet.alast
_django_aexists = QuerySet.aexists
_django_fetch_all = QuerySet._fetch_all
_django_get = QuerySet.get
_django_first = QuerySet.first
_django_last = QuerySet.last
_django_exists = QuerySet.exists

# The module of the iterable classes of Django. Another iterable class can
# query while it builds the rows, which only a thread can do.
_ITERABLES_MODULE = QuerySet.__module__

_thread_sensitive_context = SyncToAsync.thread_sensitive_context
_MISSING: Any = object()

# The Rust object that sends the queries of a ``_RustCursor``. The Windows
# build does not have it, and the setting has no effect there.
_Libpq: Any = getattr(_core, "LibpqConnection", None)

# The codes of the error fields of libpq (``PG_DIAG_SEVERITY`` and ``PG_DIAG_SQLSTATE``).
_SEVERITY = ord("S")
_SQLSTATE = ord("C")

# True when the setting is on. ``configure`` sets it.
enabled = False

# The connections of each loop for each database. ``configure`` sets the limit.
_connection_limit = DEFAULT_CONNECTIONS

# ``configure`` sets these times. A read waits this long for a free connection.
_acquire_seconds = DEFAULT_ACQUIRE_SECONDS
# A connection that no query used for this time closes.
_idle_seconds = DEFAULT_IDLE_SECONDS
# A connection older than this time closes before its next use.
_lifetime_seconds = DEFAULT_LIFETIME_SECONDS

# The models whose rows only a thread can build: see ``_fetch_all``.
_models_on_threads: set[type] = set()

# The loops that have a pool, for ``close_connections``.
_loops: list[WorkerLoop] = []
_loops_lock = threading.Lock()


def _connect(wrapper_class: type, settings_dict: dict[str, Any], alias: str) -> tuple[Any, Any]:
    """Open one connection as Django opens it, and return its wrapper and a cursor. Runs on an ORM pool thread.

    Django connects with the parameters of the settings, sets the role and
    the time zone, and calls each ``connection_created`` receiver. The
    connection thus has the session state and the type adapters of each
    Django connection. The wrapper is not in ``django.db.connections``, so no
    thread has this connection.
    """
    own = wrapper_class(settings_dict, alias)
    try:
        own.ensure_connection()
        cursor = own.create_cursor()
        # The cached value. The event loop cannot read it from the server.
        own.pg_version  # noqa: B018
    except BaseException:
        own.close()
        raise
    return own, cursor


def _libpq_addresses(database: Any) -> dict[str, int] | None:
    """Return the address of each libpq function that Rust calls, or None when psycopg does not give them.

    Rust must call the libpq that holds the connections of psycopg. The
    ctypes implementation of psycopg has that library as an object. The C
    implementation is an extension module that links to the library. A symbol
    lookup from that module searches the libraries that the module needs.
    """
    pq = database.pq
    implementation = sys.modules[pq.PGconn.__module__]
    if pq.__impl__ == "python":
        library = getattr(implementation.impl, "pq", None)
        if library is None:
            return None
    else:
        library = ctypes.CDLL(implementation.__file__)
    addresses = {}
    for name in _Libpq.FUNCTIONS:
        function = getattr(library, name, None)
        if function is None:
            return None
        addresses[name] = ctypes.cast(function, ctypes.c_void_p).value
    return addresses


def _deliver(cursor: _RustCursor, result: tuple[int, Any, Any]) -> None:
    """Take the result of a query from Rust. Runs on the event loop."""
    cursor.pending = False
    waiter = cursor.waiter
    if not waiter.done():
        waiter.set_result(result)
        return
    # The query was cancelled. The pool waits for its connection.
    on_result = cursor.on_result
    if on_result is not None:
        cursor.on_result = None
        on_result(cursor)


class _RustCursor:
    """A loop connection, with its queries sent from Rust: see ``src/pg.rs``.

    psycopg owns the connection. The cursor of Django merges the parameters
    into the statement, and its loaders make the Python values, as for a
    query of the sync ORM. Rust sends the statement and reads the result on
    the Tokio reactor. No Python code runs while the query waits for the
    database.

    ``CursorWrapper`` of Django uses this object as a cursor: ``execute``.
    """

    __slots__ = (
        "connection",
        "create_future",
        "database",
        "encoding",
        "mogrify",
        "native",
        "oids",
        "on_result",
        "pending",
        "query",
        "result",
        "transformer",
        "waiter",
    )

    def __init__(self, database: Any, cursor: Any, addresses: dict[str, int], loop: WorkerLoop) -> None:
        connection = cursor.connection
        pgconn = connection.pgconn
        self.connection = connection
        self.database = database
        self.encoding = connection.info.encoding
        self.mogrify = cursor.mogrify
        self.transformer = database.adapt.Transformer(cursor)
        # The column types that the loaders of ``transformer`` are for.
        self.oids: tuple[int, ...] | None = None
        self.create_future = loop.create_future
        self.waiter: Any = None
        # True from the send of a query until Rust gives its result.
        self.pending = False
        # The pool sets this callback for a cancelled query. It gets this cursor when the result is here.
        self.on_result: Any = None
        self.query = b""
        self.result: Any = None
        self.native = _Libpq(addresses, pgconn.pgconn_ptr, pgconn.socket, connection, loop.call_soon, _deliver)

    @property
    def _query(self) -> _RustCursor:
        # ``last_executed_query`` of Django reads ``cursor._query.query`` for the debug log.
        return self

    def execute(self, sql: Any, params: Any = None) -> None:
        """Send the statement. ``CursorWrapper.execute`` of Django calls this method, as for a cursor of psycopg.

        The method does not wait: ``wait`` gives the outcome. Code around
        ``CursorWrapper.execute`` thus gets the statement, and an error of
        its parameters, but no query time and no error of the database.
        """
        self.result = None
        # Not the statement of the query before: Django logs ``query`` when this statement has an error.
        self.query = None
        query = self.query = self.mogrify(sql, params).encode(self.encoding)
        self.waiter = self.create_future()
        self.native.query(query, self)
        self.pending = True

    async def wait(self) -> None:
        """Wait for the statement that ``execute`` sent, and raise its error."""
        waiter = self.waiter
        if waiter is None:
            # The code around ``CursorWrapper.execute`` did not call it. A cursor of psycopg then has no result.
            raise self.database.ProgrammingError("no result available")
        try:
            kind, first, second = await waiter
        except asyncio.CancelledError:
            if self.pending:
                self._cancel_on_server()
            raise
        if kind == _Libpq.ROWS:
            self.result = (first, second)
            return
        database = self.database
        if kind == _Libpq.SERVER_ERROR:
            raise self._server_error(first, second)
        if kind == _Libpq.CONNECTION_ERROR:
            raise database.OperationalError(second)
        status = database.pq.ExecStatus(first)
        if status in (status.COMMAND_OK, status.EMPTY_QUERY):
            self.result = status
            return
        # The errors of psycopg for these results. The pool closes the connection of a COPY.
        if status in (status.COPY_OUT, status.COPY_IN, status.COPY_BOTH):
            raise database.ProgrammingError("COPY cannot be used with this method; use copy() instead")
        raise database.InternalError(f"unexpected result status from query: {status.name}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        result = self.result
        if type(result) is not tuple:
            if result is None:
                raise self.database.ProgrammingError("no result available")
            raise self.database.ProgrammingError(
                f"the last operation didn't produce records (result status: {result.name})"
            )
        self.result = None
        oids, rows = result
        transformer = self.transformer
        if oids != self.oids:
            transformer.set_loader_types(oids, self.database.pq.Format.TEXT)
            self.oids = oids
        return list(map(transformer.load_sequence, rows))

    def _server_error(self, fields: dict[int, bytes], message: bytes) -> Exception:
        """Return the psycopg error for an error of the server, as ``psycopg.errors.error_from_result`` makes it."""
        errors = self.database.errors
        encoding = self.encoding
        state = fields.get(_SQLSTATE, b"").decode(encoding, "replace")
        try:
            error_class = errors.lookup(state)
        except KeyError:
            error_class = errors.get_base_exception(state)
        # libpq starts the message with the severity, and psycopg removes it.
        severity = fields.get(_SEVERITY)
        if severity and message.startswith(severity + b":"):
            message = message[len(severity) + 1 :]
        text = message.decode(encoding, "replace").strip() or "no error details available"
        return error_class(text, info=fields, encoding=encoding)

    def _cancel_on_server(self) -> None:
        """Ask the server to stop the query, as psycopg does for a cancelled task.

        Without the request, the query runs to its end and keeps its
        connection busy. The request is a short blocking call on a connection
        of its own.
        """
        try:
            self.connection.cancel()
        except self.database.Error as error:
            logger.warning("The server did not get the cancel request of a loop query: %s", error)

    def close(self) -> None:
        """Close the connection. Rust makes no call on it after ``native.close()`` returns."""
        self.native.close()
        self.connection.pgconn.finish()


class _Pool:
    """The connections of one event loop to one database.

    Only the thread of the loop uses a pool, with one exception:
    ``close_connections`` runs on another thread when no request runs.

    The pool holds cursors: one ``_RustCursor`` for each connection.

    The pool follows the pool of sqlx. A read takes a free connection. With
    no free connection, the pool opens one, up to the limit. At the limit, the
    read waits in a queue, and fails after the acquire time. A connection that
    comes back goes to the read that waited longest. A connection closes
    after the idle time, or when it is older than the lifetime.

    Two things differ from sqlx. sqlx tests a connection with a ping before
    each use, which adds a round trip to each query. This pool sends a query
    again when its connection was dead (see ``fetch``). sqlx takes the
    connection that was idle for the longest time. This pool takes the one
    that came back last, so the connections that the load does not need go
    idle and close.
    """

    __slots__ = (
        "addresses",
        "alias",
        "born",
        "cancelled",
        "closed",
        "database",
        "expirer",
        "idle",
        "idle_since",
        "opened",
        "pg_version",
        "preparing",
        "reaper",
        "settings_dict",
        "waiters",
        "waiting_since",
        "wrapped",
        "wrapper_class",
    )

    def __init__(
        self, alias: str, database: Any, addresses: dict[str, int], wrapper_class: type, settings_dict: dict[str, Any]
    ) -> None:
        self.alias = alias
        self.database = database
        # The libpq functions that Rust calls.
        self.addresses = addresses
        # ``_connect`` opens each connection with this class and these settings.
        self.wrapper_class = wrapper_class
        self.settings_dict = settings_dict
        # The cursors of cancelled queries that Rust did not end yet.
        self.cancelled: set[_RustCursor] = set()
        self.idle: collections.deque[_RustCursor] = collections.deque()
        # The loop time at which each idle cursor came back, in the order of ``idle``.
        self.idle_since: collections.deque[float] = collections.deque()
        # The timer that closes idle connections, or None when no connection is idle.
        self.reaper: asyncio.TimerHandle | None = None
        # A waiter gets a cursor, or None when it must open a connection.
        # While a read waits, ``idle`` is empty and ``opened`` is the limit.
        self.waiters: collections.deque[asyncio.Future[_RustCursor | None]] = collections.deque()
        # The loop time at which each waiter started to wait, in the order of ``waiters``.
        self.waiting_since: collections.deque[float] = collections.deque()
        # The timer that fails the waiters that waited for the acquire time, or None.
        self.expirer: asyncio.TimerHandle | None = None
        # cursor -> the loop time at which its connection opened.
        self.born: dict[_RustCursor, float] = {}
        # The connections that are open, plus those that a read opens now.
        self.opened = 0
        self.closed = False
        # The server version, or None until the first connection opens.
        self.pg_version: int | None = None
        # True when a ``connection_created`` receiver installs an execute wrapper: see ``_pool_of``.
        self.wrapped = False
        # Reads that start together wait for one ``prepare``.
        self.preparing = asyncio.Lock()

    async def prepare(self) -> None:
        """Open the first connection. The first read of the pool calls this before it compiles.

        The connection gives the server version. It also shows if a receiver
        installs an execute wrapper on each connection.
        """
        async with self.preparing:
            if self.pg_version is None:
                self.release(await self.acquire())
        if self.wrapped:
            self.close()

    async def acquire(self) -> _RustCursor:
        """Return a cursor that no other query uses. Wait when each connection is in use."""
        loop = asyncio.get_running_loop()
        idle = self.idle
        while idle:
            self.idle_since.pop()
            cursor = idle.pop()
            if loop.time() - self.born[cursor] <= _lifetime_seconds:
                return cursor
            self.discard(cursor)
        if self.opened < _connection_limit:
            self.opened += 1
            return await self._open(loop)
        waiter: asyncio.Future[_RustCursor | None] = loop.create_future()
        self.waiters.append(waiter)
        self.waiting_since.append(loop.time())
        if self.expirer is None:
            self.expirer = loop.call_later(_acquire_seconds, self._expire_waiters, loop)
        try:
            cursor = await waiter
        except asyncio.CancelledError:
            # A read that got its turn and then stops gives the turn to the next read.
            if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                taken = waiter.result()
                if taken is None:
                    self._closed_one()
                else:
                    self.release(taken)
            raise
        if cursor is None:
            return await self._open(loop)
        return cursor

    def _next_waiter(self) -> asyncio.Future[_RustCursor | None] | None:
        """Take the read that waited longest out of the queue."""
        waiters = self.waiters
        waiting_since = self.waiting_since
        while waiters:
            waiter = waiters.popleft()
            waiting_since.popleft()
            if not waiter.done():
                return waiter
        return None

    def _expire_waiters(self, loop: asyncio.AbstractEventLoop) -> None:
        """Fail each waiter that waited for the acquire time. The oldest is first in ``waiters``."""
        waiters = self.waiters
        waiting_since = self.waiting_since
        expired = loop.time() - _acquire_seconds
        while waiters and (waiters[0].done() or waiting_since[0] <= expired):
            waiting_since.popleft()
            waiter = waiters.popleft()
            if not waiter.done():
                waiter.set_exception(
                    OperationalError(
                        f"No loop connection to the database {self.alias!r} came free in {_acquire_seconds:g} "
                        f"seconds. Each of the {_connection_limit} connections of {CONNECTIONS_VARIABLE} was in use."
                    )
                )
        self.expirer = loop.call_later(waiting_since[0] - expired, self._expire_waiters, loop) if waiters else None

    async def _open(self, loop: asyncio.AbstractEventLoop) -> _RustCursor:
        """Open one connection. ``opened`` counts it already."""
        # The connect runs to its end on its thread, also when this read stops.
        opening = asyncio.ensure_future(
            run_in_orm_executor(_connect, self.wrapper_class, self.settings_dict, self.alias)
        )
        try:
            own, django_cursor = await asyncio.shield(opening)
        except asyncio.CancelledError:
            opening.add_done_callback(self._close_unused)
            raise
        except BaseException:
            self._closed_one()
            raise
        try:
            cursor = _RustCursor(self.database, django_cursor, self.addresses, loop)
        except BaseException:
            own.connection.pgconn.finish()
            self._closed_one()
            raise
        self.pg_version = own.pg_version
        if own.execute_wrappers and not self.wrapped:
            self.wrapped = True
            logger.info(
                "The async ORM reads of the database %r use the ORM thread pool: "
                "a connection_created receiver installs an execute wrapper.",
                self.alias,
            )
        self.born[cursor] = loop.time()
        return cursor

    def _close_unused(self, opening: asyncio.Future[tuple[Any, Any]]) -> None:
        """Close a connection that opened after its read stopped."""
        if not opening.cancelled() and opening.exception() is None:
            own, _django_cursor = opening.result()
            own.connection.pgconn.finish()
        self._closed_one()

    def release(self, cursor: _RustCursor) -> None:
        loop = asyncio.get_running_loop()
        if self.closed or loop.time() - self.born[cursor] > _lifetime_seconds:
            self.discard(cursor)
            return
        waiter = self._next_waiter()
        if waiter is not None:
            # The read that waited longest gets the connection. A read that
            # starts now cannot take it first.
            waiter.set_result(cursor)
            return
        self.idle.append(cursor)
        self.idle_since.append(loop.time())
        if self.reaper is None:
            self.reaper = loop.call_later(_idle_seconds, self._close_idle, loop)

    def _close_idle(self, loop: asyncio.AbstractEventLoop) -> None:
        """Close each connection that was idle for the idle time. The oldest is first in ``idle``."""
        idle = self.idle
        idle_since = self.idle_since
        expired = loop.time() - _idle_seconds
        while idle and idle_since[0] <= expired:
            idle_since.popleft()
            self.discard(idle.popleft())
        self.reaper = loop.call_later(idle_since[0] - expired, self._close_idle, loop) if idle else None

    def _give_back(self, cursor: _RustCursor) -> None:
        """Take ``cursor`` back after a query that did not complete.

        A cancelled query asked the server to cancel it, and did not wait. Its
        connection comes back here when Rust read the end of the query. A
        connection that is not idle then still runs a query, and the next
        query on it would fail.
        """
        if cursor.pending:
            self.cancelled.add(cursor)
            cursor.on_result = self._give_back
            return
        self.cancelled.discard(cursor)
        if cursor.connection.pgconn.transaction_status == self.database.pq.TransactionStatus.IDLE:
            self.release(cursor)
        else:
            self.discard(cursor)

    def discard(self, cursor: _RustCursor) -> None:
        """Close the connection of ``cursor``. This call does not wait, so a cancelled task can make it."""
        del self.born[cursor]
        cursor.close()
        self._closed_one()

    def discard_idle(self) -> None:
        idle = self.idle
        self.idle_since.clear()
        while idle:
            self.discard(idle.pop())

    def close(self) -> None:
        self.closed = True
        self.discard_idle()
        while self.cancelled:
            cursor = self.cancelled.pop()
            # A result that is already on its way to the loop must not give this connection back.
            cursor.on_result = None
            self.discard(cursor)
        for name in ("reaper", "expirer"):
            timer = getattr(self, name)
            if timer is not None:
                timer.cancel()
                setattr(self, name, None)

    def _closed_one(self) -> None:
        """A connection closed, or did not open. The read that waited longest opens one in its place."""
        waiter = self._next_waiter()
        if waiter is None:
            self.opened -= 1
        else:
            waiter.set_result(None)

    async def fetch(self, wrapper: Any, sql: str, params: Any) -> list[tuple[Any, ...]]:
        """Run one read and return its rows. ``wrapper`` is the connection wrapper of Django for this request.

        The statement goes through ``CursorWrapper.execute`` of Django, so a
        tool that replaces that method gets each statement of the loop: see
        ``_RustCursor.execute``. With
        ``wrapper.queries_logged``, ``connection.queries`` and the
        ``django.db.backends`` logger get the query, as with the cursor of Django.

        A database restart ends each connection of the pool. The query then
        fails on a dead connection, before the database runs it. The pool
        closes its idle connections and sends the query one more time on a
        new connection.
        """
        database_error = self.database.Error
        for first_try in (True, False):
            cursor = await self.acquire()
            cursor.waiter = None
            try:
                if wrapper.queries_logged:
                    with CursorDebugWrapper(cursor, wrapper).debug_sql(sql, params, use_last_executed_query=True):
                        CursorWrapper(cursor, wrapper).execute(sql, params)
                        await cursor.wait()
                else:
                    CursorWrapper(cursor, wrapper).execute(sql, params)
                    await cursor.wait()
                rows = cursor.fetchall()
            except database_error as error:
                if not cursor.connection.broken:
                    # The query failed and the connection is good.
                    self._give_back(cursor)
                    raise _django_error(self.database, error) from error
                self.discard(cursor)
                self.discard_idle()
                if not first_try:
                    raise _django_error(self.database, error) from error
            except BaseException:
                # A cancel, or an error of a type adapter.
                self._give_back(cursor)
                raise
            else:
                self.release(cursor)
                return rows
        raise AssertionError("unreachable")  # pragma: no cover


def _django_error(database: Any, error: Exception) -> Exception:
    """Return the ``django.db`` error for a psycopg error, as ``DatabaseErrorWrapper`` does."""
    for django_type in _DJANGO_ERRORS:
        if isinstance(error, getattr(database, django_type.__name__)):
            return django_type(*error.args)
    return error


def _new_pool(loop: WorkerLoop, alias: str) -> _Pool | None:
    """Make the pool of ``loop`` for ``alias``, or return None for a database whose reads stay on threads."""
    wrapper = connections[alias]
    database = wrapper.Database
    # psycopg 2 has no async connection.
    if type(wrapper).__module__ != _BACKEND_MODULE or getattr(database, "AsyncClientCursor", None) is None:
        return None
    settings_dict = wrapper.settings_dict
    addresses = None
    if wrapper.features.uses_server_side_binding:
        # The server merges the parameters, which needs the extended query protocol. Rust sends plain statements.
        reason = "server_side_binding is on"
    elif not settings_dict["AUTOCOMMIT"]:
        # Each read of a thread is then in the transaction of that thread.
        reason = "AUTOCOMMIT is off"
    else:
        addresses = _libpq_addresses(database)
        reason = f"the {database.pq.__impl__} implementation of psycopg does not give its libpq functions"
    if addresses is None:
        logger.info("The async ORM reads of the database %r use the ORM thread pool: %s.", alias, reason)
        return None
    options = settings_dict["OPTIONS"]
    if "pool" in options:
        # The connection pool of Django wants each connection back. A loop connection closes.
        settings_dict = {**settings_dict, "OPTIONS": {key: value for key, value in options.items() if key != "pool"}}
    pool = _Pool(alias, database, addresses, type(wrapper), settings_dict)
    logger.info("The async ORM reads of the database %r run on the event loop.", alias)
    with _loops_lock:
        if loop not in _loops:
            _loops.append(loop)
    return pool


def _pool_for(queryset: QuerySet) -> _Pool | None:
    """Return the pool for this read, or None when the read must use Django's own method.

    Django's method runs the sync ORM on a thread, with the connection of
    Django. A read needs it in these conditions:

    - The loop is not the loop of a Bolt worker. A pool lives as long as its loop.
    - The QuerySet has ``prefetch_related``. The prefetch queries are sync code.
    - The class of the QuerySet has its own ``_fetch_all``, or the QuerySet
      has an iterable class that is not of Django. Such code can query.
    - The code that builds a row of this model queried before: see ``_fetch_all``.
    - Each condition of ``_pool_of``.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if type(loop) is not WorkerLoop:
        return None
    if queryset._prefetch_related_lookups:
        return None
    if type(queryset)._fetch_all is not _django_fetch_all or queryset._iterable_class.__module__ != _ITERABLES_MODULE:
        return None
    if queryset.model in _models_on_threads:
        return None
    return _pool_of(loop, queryset.db)


def _pool_of(loop: WorkerLoop, alias: str) -> _Pool | None:
    """The part of ``_pool_for`` that does not need the QuerySet.

    A read uses a thread in these conditions:

    - The request has Django middleware. Its lane keeps state for the
      request, such as the schema of a tenant.
    - Sync code called the coroutine with ``async_to_sync``. Django's method
      then runs on the thread of that code, with its transaction.
    - The database is not PostgreSQL with psycopg 3, Django's backend,
      client-side parameters and ``AUTOCOMMIT``.
    - The connection of the thread is in a transaction. The test client gives
      the handlers such a connection, and only it has the rows of the test.
    - The connection has an execute wrapper. A wrapper is sync code around the
      query: it measures the time of the query and gets its error. On the
      event loop, the wrapper would return before the query runs.
    """
    if in_lane_mode() or _thread_sensitive_context.get(None) is not None:
        return None
    if getattr(AsyncToSync.executors, "current", None) is not None:
        return None
    pools = loop._query_pools
    pool = pools.get(alias, _MISSING)
    if pool is _MISSING:
        pool = pools[alias] = _new_pool(loop, alias)
    if pool is None or pool.wrapped:
        return None
    wrapper = connections[alias]
    if wrapper.in_atomic_block or wrapper.execute_wrappers:
        return None
    return pool


async def _prepare(pool: _Pool, wrapper: Any) -> bool:
    """Prepare the connection wrapper of this request for a read of ``pool``.

    On an event loop thread, Django keeps one connection wrapper for each
    request, so each read prepares the wrapper of its request.

    Returns False when the first connection of the pool shows that the reads
    must use threads: see ``_Pool.prepare``.
    """
    if pool.pg_version is None:
        await pool.prepare()
    if pool.wrapped:
        return False
    # The compile step reads the server version for some expressions. Without
    # this value, the wrapper would open a sync connection to read it.
    wrapper.__dict__.setdefault("pg_version", pool.pg_version)
    return True


async def fetch(alias: str, sql: str, params: Any) -> list[tuple[Any, ...]] | None:
    """Run one read statement of the framework on the event loop, and return its rows.

    Returns None for a read that must use the ORM thread pool (see
    ``_pool_of``). Call this function only when ``enabled`` is true.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if type(loop) is not WorkerLoop:
        return None
    pool = _pool_of(loop, alias)
    if pool is None:
        return None
    wrapper = connections[alias]
    if not await _prepare(pool, wrapper):
        return None
    return await pool.fetch(wrapper, sql, params)


async def _rows(pool: _Pool, compiler: Any) -> Any:
    """Compile the query of ``compiler`` and return its rows from the database.

    Returns None when the read must use a thread: the pool found that after
    its first connection, or an expression needed the database to compile.
    The thread path gives such an expression a connection.
    """
    wrapper = compiler.connection
    if not await _prepare(pool, wrapper):
        return None
    try:
        sql, params = compiler.as_sql()
    except EmptyResultSet:
        # ``execute_sql`` gives no rows for a query that can match nothing.
        return ()
    except SynchronousOnlyOperation:
        return None
    if not sql:
        return ()
    rows = await pool.fetch(wrapper, sql, params)
    if compiler.has_extra_select:
        # ``execute_sql`` removes the columns that the query selects only to order the rows.
        count = compiler.col_count
        rows = [row[:count] for row in rows]
    return rows


async def _fetch_all(queryset: QuerySet, pool: _Pool) -> None:
    """Fill the result cache of ``queryset``: ``QuerySet._fetch_all`` with the query on the event loop."""
    query = queryset.query
    compiler = query.get_compiler(using=queryset.db)
    rows = await _rows(pool, compiler)
    if rows is None:
        await SyncToAsync(queryset._fetch_all)()
        return

    # The iterable class asks the query for a compiler and the compiler for
    # the rows. Give it this compiler, which has the rows.
    compiler.execute_sql = lambda *_args, **_kwargs: (rows,)
    query.get_compiler = lambda *_args, **_kwargs: compiler
    try:
        queryset._result_cache = list(queryset._iterable_class(queryset))
        return
    except SynchronousOnlyOperation:
        # The code that builds a row queried: a ``from_db`` method, a field
        # converter or a signal receiver. Only a thread can run it. The
        # thread reads the rows again, and each later read of this model
        # starts on a thread.
        _models_on_threads.add(queryset.model)
    finally:
        del query.get_compiler
    await SyncToAsync(queryset._fetch_all)()


async def load(queryset: QuerySet) -> bool:
    """Read the rows of ``queryset`` from the event loop, for a QuerySet that a handler returns.

    Returns False for a read that must use the ORM thread pool (see
    ``_pool_for``). The caller then loads the QuerySet there. Call this
    function only when ``enabled`` is true.
    """
    # A QuerySet that has its rows has no query to send. The handler can have changed the rows.
    pool = _pool_for(queryset) if queryset._result_cache is None else None
    if pool is None:
        return False
    await _fetch_all(queryset, pool)
    return True


def _aiter(self: QuerySet) -> Any:
    """``QuerySet.__aiter__``: the rows of ``async for``."""
    if self._result_cache is not None:
        return _django_aiter(self)
    pool = _pool_for(self)
    if pool is None:
        return _django_aiter(self)

    async def generator() -> Any:
        await _fetch_all(self, pool)
        for item in self._result_cache:
            yield item

    return generator()


async def _aget(self: QuerySet, *args: Any, **kwargs: Any) -> Any:
    """``QuerySet.aget``. The lines after the pool are those of ``QuerySet.get``, with the fetch awaited."""
    if (self.query.combinator and (args or kwargs)) or type(self).get is not _django_get:
        return await _django_aget(self, *args, **kwargs)
    pool = _pool_for(self)
    if pool is None:
        return await _django_aget(self, *args, **kwargs)
    clone = self._chain() if self.query.combinator else self.filter(*args, **kwargs)
    if self.query.can_filter() and not self.query.distinct_fields:
        clone = clone.order_by()
    # PostgreSQL takes a limit with ``select_for_update``, so ``get`` always sets the limit here.
    limit = MAX_GET_RESULTS
    clone.query.set_limits(high=limit)
    await _fetch_all(clone, pool)
    num = len(clone._result_cache)
    if num == 1:
        return clone._result_cache[0]
    if not num:
        raise self.model.DoesNotExist(f"{self.model._meta.object_name} matching query does not exist.")
    raise self.model.MultipleObjectsReturned(
        "get() returned more than one {} -- it returned {}!".format(
            self.model._meta.object_name,
            num if num < limit else f"more than {limit - 1}",
        )
    )


def _seconds(name: str, default: float) -> float:
    """
    Read a time in seconds from the environment variable ``name``.

    Raises:
        ImproperlyConfigured: The value is not a number more than 0, or a timer cannot wait that long.
    """
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0.0
    if not 0 < seconds <= threading.TIMEOUT_MAX:
        raise ImproperlyConfigured(
            f"{name} must be a number more than 0 and at most {threading.TIMEOUT_MAX:g}, got {raw!r}."
        )
    return seconds


class _Unfetched(Exception):  # noqa: N818 - not an error: it carries a QuerySet out of a sync method
    """A sync ``QuerySet`` method reached the point where it reads its rows."""

    def __init__(self, queryset: QuerySet) -> None:
        # The QuerySet is not an argument of the exception: the text of an exception shows its arguments.
        super().__init__()
        self.queryset = queryset


# QuerySet class -> its subclass that raises ``_Unfetched`` where it would query.
_stopping_classes: dict[type, type] = {}


def _stopping_class(queryset_class: type) -> type:
    stopping = _stopping_classes.get(queryset_class)
    if stopping is None:

        def _fetch_all(self: QuerySet) -> None:
            raise _Unfetched(self)

        stopping = _stopping_classes[queryset_class] = type(
            queryset_class.__name__, (queryset_class,), {"_fetch_all": _fetch_all, "__module__": __name__}
        )
    return stopping


async def _first_row(queryset: QuerySet, method: str, pool: _Pool) -> Any:
    """Run ``first`` or ``last`` of Django, with the query on the event loop.

    The two methods differ between Django versions, so this function does not
    copy them. It calls the method of Django on a clone that stops where it
    would query. That clone gives the QuerySet that Django built: ordered,
    reversed and limited to one row. The event loop then reads its rows.
    """
    queryset_class = type(queryset)
    clone = queryset._chain()
    clone.__class__ = _stopping_class(queryset_class)
    try:
        return getattr(clone, method)()
    except _Unfetched as unfetched:
        built = unfetched.queryset
    built.__class__ = queryset_class
    await _fetch_all(built, pool)
    # ``for obj in queryset[:1]: return obj``
    for row in built._result_cache:
        return row
    return None


async def _afirst(self: QuerySet) -> Any:
    """``QuerySet.afirst``."""
    # A QuerySet that has its rows answers from them, and has no query to move to the loop.
    pool = _pool_for(self) if self._result_cache is None and type(self).first is _django_first else None
    if pool is None:
        return await _django_afirst(self)
    return await _first_row(self, "first", pool)


async def _alast(self: QuerySet) -> Any:
    """``QuerySet.alast``."""
    pool = _pool_for(self) if type(self).last is _django_last else None
    if pool is None:
        return await _django_alast(self)
    return await _first_row(self, "last", pool)


async def _aexists(self: QuerySet) -> bool:
    """``QuerySet.aexists``: the lines of ``QuerySet.exists`` and ``Query.has_results``, with the query awaited."""
    if self._result_cache is not None:
        return bool(self._result_cache)
    pool = _pool_for(self) if type(self).exists is _django_exists else None
    if pool is None:
        return await _django_aexists(self)
    rows = await _rows(pool, self.query.exists().get_compiler(using=self.db))
    if rows is None:
        return await _django_aexists(self)
    return bool(rows)


def configure() -> None:
    """Read the setting and the limits of the pools. The server calls this function at startup.

    With the setting on, ``QuerySet`` gets the methods of this module.
    With the setting off, it gets Django's methods back in their place.

    Raises:
        ImproperlyConfigured: BOLT_ASYNC_ORM_ON_LOOP is not a bool,
            DJANGO_BOLT_LOOP_CONNECTIONS is not an int of 1 or more, or
            DJANGO_BOLT_LOOP_ACQUIRE_SECONDS, DJANGO_BOLT_LOOP_IDLE_SECONDS or
            DJANGO_BOLT_LOOP_LIFETIME_SECONDS is not a number that ``_seconds`` accepts.
    """
    global enabled, _connection_limit, _acquire_seconds, _idle_seconds, _lifetime_seconds
    limit = _thread_count(CONNECTIONS_VARIABLE)
    acquire_seconds = _seconds(ACQUIRE_VARIABLE, DEFAULT_ACQUIRE_SECONDS)
    idle_seconds = _seconds(IDLE_VARIABLE, DEFAULT_IDLE_SECONDS)
    lifetime_seconds = _seconds(LIFETIME_VARIABLE, DEFAULT_LIFETIME_SECONDS)
    if not settings.configured:
        return
    _connection_limit = limit or DEFAULT_CONNECTIONS
    _acquire_seconds = acquire_seconds
    _idle_seconds = idle_seconds
    _lifetime_seconds = lifetime_seconds
    # The code of a model can change between two servers of one process, as in tests.
    _models_on_threads.clear()
    setting = getattr(settings, SETTING, False)
    if type(setting) is not bool:
        raise ImproperlyConfigured(f"{SETTING} must be a bool, got {type(setting).__name__} {setting!r}.")
    if setting and sys.platform == "win32":
        # The event loop of a worker watches sockets only on Unix.
        logger.warning("%s has no effect on Windows. The async ORM uses the ORM thread pool.", SETTING)
        setting = False
    enabled = setting
    for name, method, django_method in (
        ("__aiter__", _aiter, _django_aiter),
        ("aget", _aget, _django_aget),
        ("afirst", _afirst, _django_afirst),
        ("alast", _alast, _django_alast),
        ("aexists", _aexists, _django_aexists),
    ):
        if enabled:
            setattr(QuerySet, name, method)
        elif QuerySet.__dict__[name] is method:
            # Only the method of this module goes away. Another library can have its own method here.
            setattr(QuerySet, name, django_method)


def close_connections() -> None:
    """Close the loop connections of the process. Call it when no request runs.

    The test client calls this function when it closes. An open connection
    blocks the drop of the test database.
    """
    with _loops_lock:
        loops = list(_loops)
        _loops.clear()
    for loop in loops:
        pools = loop._query_pools
        for pool in pools.values():
            if pool is not None:
                pool.close()
        pools.clear()
