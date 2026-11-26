"""Run the read calls of Django's async ORM on the event loop of the worker.

Django's async ORM API is ``sync_to_async`` around the sync API, so each call
runs on a thread. With ``BOLT_ASYNC_ORM_ON_LOOP = True``, Bolt replaces two
read calls of ``QuerySet``: ``async for`` (``__aiter__``) and ``aget()``.

The flow of one read:

1. Django compiles the QuerySet to SQL on the event loop thread. The compile
   step is CPU work and sends nothing to the database.
2. A psycopg async connection sends the SQL. The event loop watches its
   socket and runs other requests while the database works.
3. Django's own iterable class builds the result from the rows. Thus the
   field conversion and the model instances are those of the sync ORM.

A loop connection is not the connection of Django. It copies the type
adapters and the session settings of a Django connection to the same
database, so the same query gives the same rows. It does not see an open
transaction. Each read that needs the connection of Django uses Django's own
method, which runs on a thread: see ``_pool_for``.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import os
import sys
import threading
from typing import Any, NamedTuple

from asgiref.sync import SyncToAsync
from django.conf import settings
from django.core.exceptions import EmptyResultSet, ImproperlyConfigured, SynchronousOnlyOperation
from django.db import connections
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

from ._worker_loop import WorkerLoop
from .concurrency import _thread_count, in_lane_mode, run_in_orm_executor

logger = logging.getLogger(__name__)

SETTING = "BOLT_ASYNC_ORM_ON_LOOP"
CONNECTIONS_VARIABLE = "DJANGO_BOLT_LOOP_CONNECTIONS"
DEFAULT_CONNECTIONS = 4
IDLE_VARIABLE = "DJANGO_BOLT_LOOP_IDLE_SECONDS"
DEFAULT_IDLE_SECONDS = 300.0

# The time that a read waits for a connection before the pool has timed a query.
_FIRST_WAIT_SECONDS = 0.005

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

_thread_sensitive_context = SyncToAsync.thread_sensitive_context
_MISSING: Any = object()

# The connections of each loop for each database. ``configure`` sets the limit.
_connection_limit = DEFAULT_CONNECTIONS

# A connection that no query used for this time closes. ``configure`` sets it.
_idle_seconds = DEFAULT_IDLE_SECONDS

# alias -> what a loop connection copies from a Django connection.
_templates: dict[str, _Template] = {}

# The loops that have a pool, for ``close_connections``.
_loops: list[WorkerLoop] = []
_loops_lock = threading.Lock()


class _Template(NamedTuple):
    """The state of one Django connection that each loop connection to the same database copies."""

    adapters: Any
    session_sql: str
    session_values: tuple[str, ...]
    pg_version: int


def _read_template(alias: str) -> _Template:
    """Read the template from the Django connection of the calling thread. Runs on an ORM pool thread.

    Django opens that connection in its usual way: the connection parameters,
    the time zone and the role of the settings, then each ``connection_created``
    receiver. ``pg_settings`` lists each setting that this session changed.
    """
    wrapper = connections[alias]
    was_open = wrapper.connection is not None
    try:
        with wrapper.cursor() as cursor:
            cursor.execute("SELECT name, setting FROM pg_settings WHERE source = 'session'")
            session = cursor.fetchall()
        return _Template(
            adapters=wrapper.Database.adapt.AdaptersMap(wrapper.connection.adapters),
            session_sql="SELECT " + ", ".join(["set_config(%s, %s, false)"] * len(session)) if session else "",
            session_values=tuple(value for pair in session for value in pair),
            pg_version=wrapper.pg_version,
        )
    finally:
        # With each read on the loop, this thread can get no more work. It must not keep a connection open.
        if not was_open:
            wrapper.close()


class _Pool:
    """The connections of one event loop to one database.

    Only the thread of the loop uses a pool, with one exception:
    ``close_connections`` runs on another thread when no request runs.

    The pool holds cursors. One cursor serves each query of its connection,
    which is cheaper than a new cursor for each query.

    A query takes the cursor that came back last. Thus a pool with more
    connections than its load needs leaves the others idle, and they close
    after the idle time.

    A read with no free connection waits for one. The pool opens a new
    connection only for a read that waited for the time of one query: the
    connections then come back too slowly for the load. The first connection
    opens at once.
    """

    __slots__ = (
        "alias",
        "closed",
        "database",
        "grower",
        "idle",
        "idle_since",
        "opened",
        "query_seconds",
        "preparing",
        "reaper",
        "template",
        "waiters",
        "waiting_since",
    )

    def __init__(self, alias: str, database: Any) -> None:
        self.alias = alias
        self.database = database
        self.idle: collections.deque[Any] = collections.deque()
        # The loop time at which each idle cursor came back, in the order of ``idle``.
        self.idle_since: collections.deque[float] = collections.deque()
        # The timer that closes idle connections, or None when no connection is idle.
        self.reaper: asyncio.TimerHandle | None = None
        # A waiter gets True when it must open a connection, and None when one came back.
        self.waiters: collections.deque[asyncio.Future[bool | None]] = collections.deque()
        # The loop time at which each waiter started to wait, in the order of ``waiters``.
        self.waiting_since: collections.deque[float] = collections.deque()
        # The timer that lets the oldest waiter open a connection, or None.
        self.grower: asyncio.TimerHandle | None = None
        # The time for which a query holds its connection: a moving average. 0 before the first query.
        self.query_seconds = 0.0
        self.opened = 0
        self.closed = False
        # None until ``prepare`` runs.
        self.template: _Template | None = None
        # Reads that start together wait for one ``prepare``.
        self.preparing = asyncio.Lock()

    async def prepare(self) -> None:
        """Read the template of the database. The first read of the pool calls this before it compiles."""
        alias = self.alias
        async with self.preparing:
            template = _templates.get(alias)
            if template is None:
                template = _templates[alias] = await run_in_orm_executor(_read_template, alias)
        # The compile step reads the server version for some expressions.
        # Without this value, the connection wrapper of the loop thread would
        # open a sync connection to read it.
        connections[alias].__dict__.setdefault("pg_version", template.pg_version)
        self.template = template

    async def acquire(self) -> Any:
        """Return a cursor that no other query uses. Wait when each connection is in use."""
        must_open = not self.opened
        while True:
            if self.idle:
                self.idle_since.pop()
                return self.idle.pop()
            if must_open and self.opened < _connection_limit:
                self.opened += 1
                try:
                    return await self._open()
                except BaseException:
                    self._closed_one()
                    raise
            loop = asyncio.get_running_loop()
            waiter = loop.create_future()
            self.waiters.append(waiter)
            self.waiting_since.append(loop.time())
            if self.grower is None and self.opened < _connection_limit:
                self.grower = loop.call_later(self._patience(), self._grow, loop)
            try:
                must_open = await waiter
            except BaseException:
                # A waiter that got its turn and then stops gives the turn to the next one.
                if waiter.done() and not waiter.cancelled():
                    self._wake()
                raise

    def _patience(self) -> float:
        """The time that a read waits for a connection before the pool opens one for it."""
        return self.query_seconds or _FIRST_WAIT_SECONDS

    def _grow(self, loop: asyncio.AbstractEventLoop) -> None:
        """Let the oldest waiter open a connection when it waited for the time of one query."""
        self.grower = None
        waiters = self.waiters
        waiting_since = self.waiting_since
        while waiters and waiters[0].done():
            waiters.popleft()
            waiting_since.popleft()
        if not waiters or self.opened >= _connection_limit:
            return
        patience = self._patience()
        remaining = waiting_since[0] + patience - loop.time()
        if remaining <= 0:
            waiting_since.popleft()
            waiters.popleft().set_result(True)
            if not waiters:
                return
            remaining = max(waiting_since[0] + patience - loop.time(), 0.0)
        self.grower = loop.call_later(remaining, self._grow, loop)

    async def _open(self) -> Any:
        template = self.template
        wrapper = connections[self.alias]
        database = self.database
        params = wrapper.get_connection_params()
        server_side = wrapper.settings_dict["OPTIONS"].get("server_side_binding") is True
        params["cursor_factory"] = database.AsyncCursor if server_side else database.AsyncClientCursor
        params["context"] = template.adapters
        connection = await database.AsyncConnection.connect(**params, autocommit=True)
        try:
            cursor = connection.cursor()
            if template.session_sql:
                await cursor.execute(template.session_sql, template.session_values)
        except BaseException:
            connection.pgconn.finish()
            raise
        return cursor

    def release(self, cursor: Any) -> None:
        if self.closed:
            self.discard(cursor)
            return
        loop = asyncio.get_running_loop()
        self.idle.append(cursor)
        self.idle_since.append(loop.time())
        if self.reaper is None:
            self.reaper = loop.call_later(_idle_seconds, self._close_idle, loop)
        self._wake()

    def _close_idle(self, loop: asyncio.AbstractEventLoop) -> None:
        """Close each connection that was idle for the idle time. The oldest is first in ``idle``."""
        idle = self.idle
        idle_since = self.idle_since
        expired = loop.time() - _idle_seconds
        while idle and idle_since[0] <= expired:
            idle_since.popleft()
            self.discard(idle.popleft())
        self.reaper = loop.call_later(idle_since[0] - expired, self._close_idle, loop) if idle else None

    def _give_back(self, cursor: Any) -> None:
        """Take ``cursor`` back after a query that did not complete.

        psycopg cancels the query of a cancelled task and reads its end, so
        the connection is idle again. A connection that is not idle still
        runs a query, and the next query on it would fail.
        """
        if cursor.connection.pgconn.transaction_status == self.database.pq.TransactionStatus.IDLE:
            self.release(cursor)
        else:
            self.discard(cursor)

    def discard(self, cursor: Any) -> None:
        """Close the connection of ``cursor``. ``finish`` does not wait, so a cancelled task can call it."""
        cursor.connection.pgconn.finish()
        self._closed_one()

    def discard_idle(self) -> None:
        idle = self.idle
        self.idle_since.clear()
        while idle:
            self.discard(idle.pop())

    def close(self) -> None:
        self.closed = True
        self.discard_idle()
        for name in ("reaper", "grower"):
            timer = getattr(self, name)
            if timer is not None:
                timer.cancel()
                setattr(self, name, None)

    def _closed_one(self) -> None:
        self.opened -= 1
        self._wake()

    def _wake(self) -> None:
        waiters = self.waiters
        waiting_since = self.waiting_since
        while waiters:
            waiter = waiters.popleft()
            waiting_since.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    async def fetch(self, sql: str, params: Any) -> list[tuple[Any, ...]]:
        """Run one read and return its rows.

        A database restart ends each connection of the pool. The query then
        fails on a dead connection, before the database runs it. The pool
        closes its idle connections and sends the query one more time on a
        new connection.
        """
        database_error = self.database.Error
        loop = asyncio.get_running_loop()
        for first_try in (True, False):
            try:
                cursor = await self.acquire()
            except database_error as error:
                raise _django_error(self.database, error) from error
            started = loop.time()
            try:
                await cursor.execute(sql, params)
                rows = await cursor.fetchall()
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
                seconds = loop.time() - started
                average = self.query_seconds
                self.query_seconds = average * 0.8 + seconds * 0.2 if average else seconds
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
    """Make the pool of ``loop`` for ``alias``, or return None for a database that Bolt does not support."""
    wrapper = connections[alias]
    database = wrapper.Database
    # psycopg 2 has no async connection.
    if type(wrapper).__module__ != _BACKEND_MODULE or getattr(database, "AsyncClientCursor", None) is None:
        return None
    pool = _Pool(alias, database)
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
    - The request has Django middleware. Its lane keeps state for the
      request, such as the schema of a tenant.
    - The database is not PostgreSQL with psycopg 3 and Django's backend.
    - The connection of the thread is in a transaction. The test client gives
      the handlers such a connection, and only it has the rows of the test.
    - Django logs the queries, as with ``DEBUG``. The log comes from the cursor of Django.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if type(loop) is not WorkerLoop:
        return None
    if queryset._prefetch_related_lookups:
        return None
    if in_lane_mode() or _thread_sensitive_context.get(None) is not None:
        return None
    alias = queryset.db
    pools = loop._query_pools
    pool = pools.get(alias, _MISSING)
    if pool is _MISSING:
        pool = pools[alias] = _new_pool(loop, alias)
    if pool is None:
        return None
    wrapper = connections[alias]
    if wrapper.in_atomic_block or wrapper.queries_logged:
        return None
    return pool


async def _fetch_all(queryset: QuerySet, pool: _Pool) -> None:
    """Fill the result cache of ``queryset``: ``QuerySet._fetch_all`` with the query on the event loop."""
    if pool.template is None:
        await pool.prepare()
    query = queryset.query
    compiler = query.get_compiler(using=queryset.db)
    try:
        sql, params = compiler.as_sql()
    except EmptyResultSet:
        # ``execute_sql`` gives no rows for a query that can match nothing.
        sql = ""
    except SynchronousOnlyOperation:
        # An expression needed the database to compile. The thread path gives it a connection.
        await SyncToAsync(queryset._fetch_all)()
        return
    rows = await pool.fetch(sql, params) if sql else ()

    # The iterable class asks the query for a compiler and the compiler for
    # the rows. Give it this compiler, which has the rows.
    compiler.execute_sql = lambda *_args, **_kwargs: (rows,)
    query.get_compiler = lambda *_args, **_kwargs: compiler
    try:
        queryset._result_cache = list(queryset._iterable_class(queryset))
    finally:
        del query.get_compiler


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
    if self.query.combinator and (args or kwargs):
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


def _idle_time() -> float:
    """
    Read the idle time from ``DJANGO_BOLT_LOOP_IDLE_SECONDS``.

    Raises:
        ImproperlyConfigured: The value is not a number more than 0.
    """
    raw = os.environ.get(IDLE_VARIABLE, "")
    if not raw.strip():
        return DEFAULT_IDLE_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        raise ImproperlyConfigured(f"{IDLE_VARIABLE} must be a number more than 0, got {raw!r}.")
    return seconds


def configure() -> None:
    """Read the setting, the connection limit and the idle time. The server calls this function at startup.

    With the setting on, ``QuerySet`` gets the two methods of this module.
    With the setting off, it gets Django's methods back.

    Raises:
        ImproperlyConfigured: BOLT_ASYNC_ORM_ON_LOOP is not a bool,
            DJANGO_BOLT_LOOP_CONNECTIONS is not an int of 1 or more, or
            DJANGO_BOLT_LOOP_IDLE_SECONDS is not a number more than 0.
    """
    global _connection_limit, _idle_seconds
    limit = _thread_count(CONNECTIONS_VARIABLE)
    idle_seconds = _idle_time()
    if not settings.configured:
        return
    _connection_limit = limit or DEFAULT_CONNECTIONS
    _idle_seconds = idle_seconds
    # The settings of a database can change between two servers of one process, as in tests.
    _templates.clear()
    enabled = getattr(settings, SETTING, False)
    if type(enabled) is not bool:
        raise ImproperlyConfigured(f"{SETTING} must be a bool, got {type(enabled).__name__} {enabled!r}.")
    if enabled and sys.platform == "win32":
        # The event loop of a worker watches sockets only on Unix.
        logger.warning("%s has no effect on Windows. The async ORM uses the ORM thread pool.", SETTING)
        enabled = False
    QuerySet.__aiter__ = _aiter if enabled else _django_aiter
    QuerySet.aget = _aget if enabled else _django_aget


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
