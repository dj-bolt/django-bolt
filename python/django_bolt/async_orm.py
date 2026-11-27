"""Run the read calls of Django's async ORM on the event loop of the worker.

Django's async ORM API is ``sync_to_async`` around the sync API, so each call
runs on a thread. With ``BOLT_ASYNC_ORM_ON_LOOP = True``, Bolt replaces these
read calls of ``QuerySet``: ``async for`` (``__aiter__``), ``aiterator()``,
``aget()``, ``afirst()``, ``alast()``, ``aexists()``, ``acount()`` and
``aaggregate()``.

The flow of one read:

1. Django compiles the QuerySet to SQL on the event loop thread. The compile
   step is CPU work and sends nothing to the database.
2. Rust sends the SQL on a loop connection, and reads the result: see
   ``_Connection``. The event loop runs other requests while the database
   works. A connection can have many statements in flight: see
   ``_Pool.acquire``.
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
import contextlib
import ctypes
import logging
import math
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
from django.db.models.sql.constants import SINGLE
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
# The statements that one connection can have in flight. With 1, a connection has one statement at a time.
PIPELINE_VARIABLE = "DJANGO_BOLT_LOOP_PIPELINE_DEPTH"
DEFAULT_PIPELINE_DEPTH = 16

# How ``_Pool.finished`` finds a slow statement. The time of the fastest
# statements goes up by this factor for each statement that is slower.
_FASTEST_DRIFT = 1.05
# A statement is slow after this many times the time of the fastest
# statements, plus this margin in seconds.
_SLOW_FACTOR = 2.0
_SLOW_MARGIN = 0.001
# A cancel request that does not end in this time closes its connection: see
# ``_Connection.abandon``. ``cancel_safe`` of psycopg waits for the same time.
_CANCEL_SECONDS = 30.0

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
_django_aiterator = QuerySet.aiterator
_django_aget = QuerySet.aget
_django_afirst = QuerySet.afirst
_django_alast = QuerySet.alast
_django_aexists = QuerySet.aexists
_django_acount = QuerySet.acount
_django_aaggregate = QuerySet.aaggregate
_django_fetch_all = QuerySet._fetch_all
_django_get = QuerySet.get
_django_first = QuerySet.first
_django_last = QuerySet.last
_django_exists = QuerySet.exists
_django_count = QuerySet.count
_django_aggregate = QuerySet.aggregate

# The module of the iterable classes of Django. Another iterable class can
# query while it builds the rows, which only a thread can do.
_ITERABLES_MODULE = QuerySet.__module__

_thread_sensitive_context = SyncToAsync.thread_sensitive_context
_MISSING: Any = object()

# The Rust object that sends the statements of a ``_Connection``. The Windows
# build does not have it, and the setting has no effect there.
_Libpq: Any = getattr(_core, "LibpqConnection", None)

# The codes of the error fields of libpq (``PG_DIAG_SEVERITY`` and ``PG_DIAG_SQLSTATE``).
_SEVERITY = ord("S")
_SQLSTATE = ord("C")

# True when the setting is on. ``configure`` sets it.
enabled = False

# The connections of each loop for each database. ``configure`` sets the limit.
_connection_limit = DEFAULT_CONNECTIONS
# The statements in flight on one connection. ``configure`` sets the limit.
_pipeline_depth = DEFAULT_PIPELINE_DEPTH

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


def _deliver(statement: _Statement, result: tuple[int, Any, Any], service: float | None) -> None:
    """Take the result of a statement from Rust, or the end of a cancel request. Runs on the event loop.

    ``service`` is the time from the send to the result, in seconds. Only a
    statement that went out on an idle connection has one.
    """
    link = statement.link
    kind = result[0]
    if kind == _Libpq.CANCEL_SENT:
        link.cancelling = False
        # The timer is gone when the request ended after ``_CANCEL_SECONDS``.
        if link.cancel_timer is not None:
            link.cancel_timer.cancel()
            link.cancel_timer = None
        if result[2] is not None:
            logger.warning("The server did not get the cancel request of a loop query: %s", result[2])
        if not link.in_flight:
            link.pool.settle(link)
        return
    statement.pending = False
    if statement.abandoned:
        # The read stopped, and does not wait for the result.
        link.abandoned -= 1
    else:
        statement.waiter.set_result(result)
    link.pool.finished(link, kind, service)


class _StaleConnection(Exception):  # noqa: N818 - not an error of the caller: the pool takes another connection
    """The connection ended before the send. The server did not get the statement."""


class _OnThread(Exception):  # noqa: N818 - not an error of the caller: the read uses Django's own method
    """The pool found that only a thread can run its reads: see ``_Pool._open``."""


class _Connection:
    """A loop connection, with its statements sent from Rust: see ``src/pg.rs``.

    psycopg owns the connection. The cursor of Django merges the parameters
    into each statement, and its loaders make the Python values, as for a
    query of the sync ORM. Rust sends the statements and reads the results on
    the Tokio reactor. No Python code runs while a statement waits for the
    database.

    The connection is in the pipeline mode of libpq. Thus many statements can
    be in flight on it at one time: see ``_Pool.acquire``.
    """

    __slots__ = (
        "abandoned",
        "born",
        "call_later",
        "cancel_timer",
        "cancelling",
        "closed",
        "connection",
        "create_future",
        "cursors",
        "database",
        "encoding",
        "idle_since",
        "in_flight",
        "merge",
        "native",
        "oids",
        "pool",
        "suspect",
        "transformer",
    )

    def __init__(self, pool: _Pool, cursor: Any, loop: WorkerLoop) -> None:
        connection = cursor.connection
        pgconn = connection.pgconn
        database = pool.database
        self.pool = pool
        self.connection = connection
        self.database = database
        self.encoding = connection.info.encoding
        # ``mogrify`` of the cursor of Django: it merges the parameters into a statement.
        self.merge = cursor.mogrify
        self.transformer = database.adapt.Transformer(cursor)
        # The column types that the loaders of ``transformer`` are for.
        self.oids: tuple[int, ...] | None = None
        self.create_future = loop.create_future
        self.call_later = loop.call_later
        # The loop time at which the connection opened.
        self.born = loop.time()
        # The loop time at which the connection became idle.
        self.idle_since = 0.0
        # The reads that have a place on this connection: see ``_Pool.acquire``.
        self.in_flight = 0
        # The statements in flight whose read stopped. No task waits for their results.
        self.abandoned = 0
        # True while Rust sends the cancel request of a statement. The connection takes no statement then.
        self.cancelling = False
        # The timer that closes the connection when the cancel request does not end, or None.
        self.cancel_timer: asyncio.TimerHandle | None = None
        # True after a result that can leave a transaction open: see ``_Pool.settle``.
        self.suspect = False
        # The cursors that the connection opened: see ``_Pool.declare``. Each cursor gets a name with its number.
        self.cursors = 0
        self.closed = False
        self.native = _Libpq(pool.addresses, pgconn.pgconn_ptr, pgconn.socket, connection, loop.call_soon, _deliver)

    def abandon(self, statement: _Statement) -> None:
        """The read of a statement in flight stopped. Ask the server to stop the statement, when that is safe.

        Without the request, the statement runs to its end and holds back the
        statements behind it. The server stops the statement that it runs when
        the request comes. With more statements in flight, that can be a
        statement of another read, so the connection sends no request then.
        With one statement, the connection takes no statement until the
        request ended: a late request would stop the next statement.

        The request opens a connection of its own and blocks, so Rust sends
        it from another thread, with no GIL. libpq sets no time limit for the
        request. A request that does not end keeps the connection out of
        use, so the connection closes after ``_CANCEL_SECONDS``.
        """
        statement.abandoned = True
        self.abandoned += 1
        if self.in_flight == 1 and not self.cancelling and self.native.cancel(statement):
            self.cancelling = True
            self.cancel_timer = self.call_later(_CANCEL_SECONDS, self.pool.cancel_expired, self)

    def in_transaction(self) -> bool:
        """True when the server holds a transaction open. Call it with no statement in flight."""
        return self.connection.pgconn.transaction_status != self.database.pq.TransactionStatus.IDLE

    def close(self) -> None:
        """Close the connection. Rust makes no call on it after ``native.close()`` returns."""
        self.closed = True
        if self.cancel_timer is not None:
            self.cancel_timer.cancel()
            self.cancel_timer = None
        self.native.close()
        self.connection.pgconn.finish()


class _Statement:
    """One statement on a loop connection, and its result.

    ``CursorWrapper`` of Django uses this object as a cursor: ``execute``.
    """

    __slots__ = ("abandoned", "link", "pending", "query", "result", "waiter")

    def __init__(self, link: _Connection) -> None:
        self.link = link
        # The future of the result, or None until the statement went out.
        self.waiter: Any = None
        # True from the send until Rust gives the result.
        self.pending = False
        # True when the read stopped while the statement was in flight.
        self.abandoned = False
        # The statement with its parameters. Django logs it when the statement has an error.
        self.query: bytes | None = None
        self.result: Any = None

    @property
    def _query(self) -> _Statement:
        # ``last_executed_query`` of Django reads ``cursor._query.query`` for the debug log.
        return self

    def execute(self, sql: Any, params: Any = None) -> None:
        """Send the statement. ``CursorWrapper.execute`` of Django calls this method, as for a cursor of psycopg.

        The method does not wait: ``wait`` gives the outcome. Code around
        ``CursorWrapper.execute`` thus gets the statement, and an error of
        its parameters, but no query time and no error of the database.

        Rust sends the statement with the extended query protocol, as psycopg
        does in the pipeline mode. Thus the string has one statement.
        """
        link = self.link
        native = link.native
        # psycopg reads the connection to escape a parameter, so Rust keeps its task out of libpq for that time.
        query = self.query = native.exclusive(link.merge, sql, params).encode(link.encoding)
        waiter = link.create_future()
        if not native.query(query, self):
            link.pool.fail(link)
            raise _StaleConnection("the connection ended")
        self.waiter = waiter
        self.pending = True

    async def wait(self) -> None:
        """Wait for the statement that ``execute`` sent, and raise its error."""
        waiter = self.waiter
        link = self.link
        database = link.database
        if waiter is None:
            # The code around ``CursorWrapper.execute`` did not call it. A cursor of psycopg then has no result.
            raise database.ProgrammingError("no result available")
        try:
            kind, first, second = await waiter
        except asyncio.CancelledError:
            if self.pending:
                link.abandon(self)
            raise
        if kind == _Libpq.ROWS:
            self.result = (first, second)
            return
        if kind == _Libpq.SERVER_ERROR:
            raise self._server_error(first, second)
        if kind == _Libpq.CONNECTION_ERROR:
            raise database.OperationalError(second)
        if kind == _Libpq.STALE:
            raise _StaleConnection(second)
        status = database.pq.ExecStatus(first)
        if status in (status.COMMAND_OK, status.EMPTY_QUERY):
            self.result = status
            return
        # The errors of psycopg for these results.
        if status in (status.COPY_OUT, status.COPY_IN, status.COPY_BOTH):
            # The connection stays in the COPY, and Rust ended it.
            link.pool.fail(link)
            raise database.ProgrammingError("COPY cannot be used with this method; use copy() instead")
        raise database.InternalError(f"unexpected result status from query: {status.name}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        result = self.result
        link = self.link
        if type(result) is not tuple:
            if result is None:
                raise link.database.ProgrammingError("no result available")
            raise link.database.ProgrammingError(
                f"the last operation didn't produce records (result status: {result.name})"
            )
        self.result = None
        oids, rows = result
        transformer = link.transformer
        if oids != link.oids:
            transformer.set_loader_types(oids, link.database.pq.Format.TEXT)
            link.oids = oids
        return list(map(transformer.load_sequence, rows))

    def _server_error(self, fields: dict[int, bytes], message: bytes) -> Exception:
        """Return the psycopg error for an error of the server, as ``psycopg.errors.error_from_result`` makes it."""
        link = self.link
        errors = link.database.errors
        encoding = link.encoding
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


class _Declare(_Statement):
    """The statement that opens a cursor of the server for a query: see ``_Pool.declare``."""

    __slots__ = ("name",)

    def __init__(self, link: _Connection, name: str) -> None:
        super().__init__(link)
        self.name = name

    def execute(self, sql: Any, params: Any = None) -> None:
        # The statement of the server-side cursor that Django makes on a connection with ``AUTOCOMMIT``.
        # ``WITH HOLD`` keeps the cursor after the transaction of this statement.
        super().execute(f'DECLARE "{self.name}" NO SCROLL CURSOR WITH HOLD FOR {sql}', params)


class _Pool:
    """The connections of one event loop to one database.

    Only the thread of the loop uses a pool, with one exception:
    ``close_connections`` runs on another thread when no request runs.

    The pool follows the pool of sqlx. A read takes an idle connection. With
    no idle connection, the pool opens one, up to the limit. A connection
    closes after the idle time, or when it is older than the lifetime.

    Three things differ from sqlx. At the limit, a read of sqlx waits for an
    idle connection. A read of this pool goes out on a connection that is in
    use, behind the statements in flight there: see ``acquire``. sqlx tests a
    connection with a ping before each use, which adds a round trip to each
    query. Rust looks at the socket of an idle connection before it sends
    (see ``fetch``). sqlx takes the connection that was idle for the longest
    time. This pool takes the one that became idle last, so the connections
    that the load does not need stay idle and close.
    """

    __slots__ = (
        "addresses",
        "alias",
        "closed",
        "database",
        "expirer",
        "fastest",
        "idle",
        "links",
        "opened",
        "pg_version",
        "preparing",
        "reaper",
        "settings_dict",
        "slow_after",
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
        # Each open connection.
        self.links: list[_Connection] = []
        # The connections with no statement, in the order in which they became idle.
        self.idle: collections.deque[_Connection] = collections.deque()
        # The timer that closes idle connections, or None when no connection is idle.
        self.reaper: asyncio.TimerHandle | None = None
        # A waiter gets a connection with a place for its statement, or None when it must open a connection.
        # While a read waits, ``idle`` is empty and ``opened`` is the limit.
        self.waiters: collections.deque[asyncio.Future[_Connection | None]] = collections.deque()
        # The loop time at which each waiter started to wait, in the order of ``waiters``.
        self.waiting_since: collections.deque[float] = collections.deque()
        # The timer that fails the waiters that waited for the acquire time, or None.
        self.expirer: asyncio.TimerHandle | None = None
        # The connections that are open, plus those that a read opens now.
        self.opened = 0
        self.closed = False
        # The service time of the fastest statements, in seconds: see ``finished``.
        self.fastest = math.inf
        # A statement that is first on its connection for longer than this time is slow.
        self.slow_after = math.inf
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
                # A connection with an execute wrapper does not come back: see ``_open``.
                with contextlib.suppress(_OnThread):
                    self.finished(await self.acquire())

    async def acquire(self) -> _Connection:
        """Return a connection with a place for one statement. ``finished`` gives the place back.

        The order of the choices:

        1. An idle connection.
        2. A new connection, below the limit of ``DJANGO_BOLT_LOOP_CONNECTIONS``.
        3. The connection with the fewest statements in flight, below the
           limit of ``DJANGO_BOLT_LOOP_PIPELINE_DEPTH``. The statement goes
           out behind those statements, and the server runs it after them.
           The read does not wait for a round trip of another read.
        4. The read waits in a queue, and fails after the acquire time.

        A statement behind a slow statement waits for it. Choice 3 thus
        skips a connection whose first statement is slow: see ``finished``.
        """
        loop = asyncio.get_running_loop()
        idle = self.idle
        if idle:
            now = loop.time()
            while idle:
                link = idle.pop()
                if now - link.born <= _lifetime_seconds:
                    link.in_flight = 1
                    return link
                self.discard(link)
        if self.opened < _connection_limit:
            self.opened += 1
            return await self._open(loop)
        fewest = _pipeline_depth
        if fewest > 1:
            # Each connection has a statement in flight here.
            chosen = None
            slow_after = self.slow_after
            now = loop.time()
            for link in self.links:
                if (
                    link.in_flight < fewest
                    and not link.cancelling
                    and now - link.born <= _lifetime_seconds
                    and not link.native.stalled(slow_after)
                ):
                    chosen = link
                    fewest = link.in_flight
            if chosen is not None:
                chosen.in_flight += 1
                return chosen
        waiter: asyncio.Future[_Connection | None] = loop.create_future()
        self.waiters.append(waiter)
        self.waiting_since.append(loop.time())
        if self.expirer is None:
            self.expirer = loop.call_later(_acquire_seconds, self._expire_waiters, loop)
        try:
            link = await waiter
        except asyncio.CancelledError:
            # A read that got its turn and then stops gives the turn to the next read.
            if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                taken = waiter.result()
                if taken is None:
                    self._closed_one()
                else:
                    self.finished(taken)
            raise
        if link is None:
            return await self._open(loop)
        return link

    def _next_waiter(self) -> asyncio.Future[_Connection | None] | None:
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

    async def _open(self, loop: asyncio.AbstractEventLoop) -> _Connection:
        """Open one connection, with a place for the statement of the caller. ``opened`` counts it already."""
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
            link = _Connection(self, django_cursor, loop)
        except BaseException:
            own.connection.pgconn.finish()
            self._closed_one()
            raise
        self.pg_version = own.pg_version
        if own.execute_wrappers:
            # The loop cannot call the wrapper around a query: see ``_pool_of``. The read
            # that opened this connection uses a thread, and so does each later read.
            if not self.wrapped:
                self.wrapped = True
                logger.info(
                    "The async ORM reads of the database %r use the ORM thread pool: "
                    "a connection_created receiver installs an execute wrapper.",
                    self.alias,
                )
            link.close()
            self._closed_one()
            # A receiver can add its wrapper after the first connection. The reads in
            # flight on the connections before end there, and then these connections close.
            self.close()
            raise _OnThread
        link.in_flight = 1
        self.links.append(link)
        return link

    def _close_unused(self, opening: asyncio.Future[tuple[Any, Any]]) -> None:
        """Close a connection that opened after its read stopped."""
        if not opening.cancelled() and opening.exception() is None:
            own, _django_cursor = opening.result()
            own.connection.pgconn.finish()
        self._closed_one()

    def finished(self, link: _Connection, kind: int = 0, service: float | None = None) -> None:
        """Give one place of ``link`` back: its statement has a result of ``kind``, or did not go out.

        ``service`` is the time that the database and the network took for a
        statement with no statement before it. The pool keeps the time of the
        fastest ones. A statement that is first on its connection for much
        longer than that time is slow, and ``acquire`` puts no statement
        behind it.
        """
        link.in_flight -= 1
        if service is not None:
            # A slow statement moves the time up by a small step, and a fast one moves it down at once.
            fastest = self.fastest * _FASTEST_DRIFT
            if service < fastest:
                fastest = service
            self.fastest = fastest
            self.slow_after = fastest * _SLOW_FACTOR + _SLOW_MARGIN
        if link.closed:
            return
        if kind >= _Libpq.CONNECTION_ERROR:
            # The connection ended, and Rust gives each of its statements this result.
            # A database restart also ends each idle connection.
            self.discard(link)
            self.discard_idle()
            return
        if kind:
            link.suspect = True
        self.settle(link)

    def settle(self, link: _Connection) -> None:
        """Find the next work of ``link`` after a statement or a cancel request ended."""
        if link.closed or link.cancelling:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        retired = self.closed or now - link.born > _lifetime_seconds
        if link.in_flight:
            if self.waiters and not retired:
                self._serve_waiters(link)
            return
        if link.suspect:
            # A result with no rows, or an error. A statement such as BEGIN leaves a
            # transaction open, and the next statement would run in it.
            link.suspect = False
            retired = retired or link.in_transaction()
        if retired:
            self.discard(link)
            return
        if self.waiters:
            self._serve_waiters(link)
            if link.in_flight:
                return
        link.idle_since = now
        self.idle.append(link)
        if self.reaper is None:
            self.reaper = loop.call_later(_idle_seconds, self._close_idle)

    def _serve_waiters(self, link: _Connection) -> None:
        """Give the free places of ``link`` to the reads that waited longest."""
        places = _pipeline_depth - link.in_flight
        while places > 0:
            waiter = self._next_waiter()
            if waiter is None:
                return
            link.in_flight += 1
            places -= 1
            waiter.set_result(link)

    def _close_idle(self) -> None:
        """Close each connection that was idle for the idle time. The oldest is first in ``idle``."""
        idle = self.idle
        loop = asyncio.get_running_loop()
        expired = loop.time() - _idle_seconds
        while idle and idle[0].idle_since <= expired:
            self.discard(idle.popleft())
        self.reaper = loop.call_later(idle[0].idle_since - expired, self._close_idle) if idle else None

    def cancel_expired(self, link: _Connection) -> None:
        """The cancel request of ``link`` did not end in ``_CANCEL_SECONDS``: see ``_Connection.abandon``."""
        link.cancel_timer = None
        if not link.closed:
            logger.warning(
                "The cancel request of a loop query did not end in %g seconds. Its connection closes.", _CANCEL_SECONDS
            )
            self.discard(link)

    def fail(self, link: _Connection) -> None:
        """Close a connection that ended. The statements in flight on it get their results from Rust."""
        if not link.closed:
            self.discard(link)

    def discard(self, link: _Connection) -> None:
        """Close ``link``, which is not in ``idle``. This call does not wait, so a cancelled task can make it."""
        self.links.remove(link)
        link.close()
        self._closed_one()

    def discard_idle(self) -> None:
        idle = self.idle
        while idle:
            self.discard(idle.pop())

    def close(self) -> None:
        """Close each connection with no read in flight. The other connections close when their reads end: see ``settle``.

        A statement whose read stopped does not keep its connection open.
        """
        self.closed = True
        self.idle.clear()
        for link in list(self.links):
            if link.in_flight == link.abandoned:
                self.discard(link)
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
        ``_Statement.execute``. With ``wrapper.queries_logged``,
        ``connection.queries`` and the ``django.db.backends`` logger get the
        query, as with the cursor of Django.

        A database restart ends each idle connection of the pool. Rust finds
        such a connection before it sends the statement. The pool then closes
        its idle connections, which are of the same age, and the read takes
        another connection.

        A connection that ends after the send fails each read in flight on
        it. The database can have run their statements, so the pool does not
        send them again.
        """
        database_error = self.database.Error
        while True:
            link = await self.acquire()
            statement = _Statement(link)
            try:
                if wrapper.queries_logged:
                    with CursorDebugWrapper(statement, wrapper).debug_sql(sql, params, use_last_executed_query=True):
                        CursorWrapper(statement, wrapper).execute(sql, params)
                        await statement.wait()
                else:
                    CursorWrapper(statement, wrapper).execute(sql, params)
                    await statement.wait()
                return statement.fetchall()
            except _StaleConnection:
                continue
            except database_error as error:
                raise _django_error(self.database, error) from error
            finally:
                if statement.waiter is None:
                    # The statement did not go out, so no result gives its place back.
                    self.finished(link)

    async def declare(self, wrapper: Any, sql: str, params: Any) -> tuple[_Connection, str]:
        """Open a cursor of the server for one read, and return its connection and its name.

        Only the connection of a cursor can read it. Thus the cursor keeps
        one place on its connection until ``close_cursor``. The connection
        then does not close between two chunks. Other reads use the other
        places of the connection.

        The statement goes through ``CursorWrapper.execute`` of Django, as in ``fetch``.
        """
        database_error = self.database.Error
        while True:
            link = await self.acquire()
            link.cursors += 1
            name = f"_bolt_curs_{link.cursors}"
            statement = _Declare(link, name)
            # The place from ``acquire`` is the place of the cursor. Each statement takes one more place.
            link.in_flight += 1
            try:
                if wrapper.queries_logged:
                    with CursorDebugWrapper(statement, wrapper).debug_sql(sql, params, use_last_executed_query=True):
                        CursorWrapper(statement, wrapper).execute(sql, params)
                        await statement.wait()
                else:
                    CursorWrapper(statement, wrapper).execute(sql, params)
                    await statement.wait()
                return link, name
            except _StaleConnection:
                self.finished(link)
                continue
            except database_error as error:
                # The server made no cursor.
                self.finished(link)
                raise _django_error(self.database, error) from error
            except BaseException:
                if statement.waiter is None:
                    self.finished(link)
                else:
                    # The read stopped, and the server can still make the cursor.
                    self.close_cursor(link, name)
                raise
            finally:
                if statement.waiter is None:
                    # The statement did not go out, so no result gives its place back.
                    self.finished(link)

    async def fetch_cursor(self, link: _Connection, name: str, size: int) -> list[tuple[Any, ...]]:
        """Return the next ``size`` rows of a cursor of ``declare``, or fewer rows at its end."""
        if link.closed:
            raise OperationalError("the connection of the cursor ended")
        statement = _Statement(link)
        link.in_flight += 1
        try:
            statement.execute(f'FETCH FORWARD {size} FROM "{name}"')
            await statement.wait()
            return statement.fetchall()
        except _StaleConnection as error:
            # Another connection does not have the cursor.
            raise OperationalError(str(error)) from error
        except self.database.Error as error:
            raise _django_error(self.database, error) from error
        finally:
            if statement.waiter is None:
                self.finished(link)

    def close_cursor(self, link: _Connection, name: str) -> None:
        """Close a cursor of ``declare``, and give its place back when the server closed it.

        This call does not wait, so a read that stops can make it. The
        statement goes out behind the statements in flight of the cursor.
        """
        if not link.closed:
            statement = _Statement(link)
            link.in_flight += 1
            try:
                statement.execute(f'CLOSE "{name}"')
            except _StaleConnection:
                self.finished(link)
            else:
                statement.waiter.add_done_callback(lambda _waiter: self.finished(link))
                return
        self.finished(link)


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
        # The server then merges the parameters. Rust sends statements that have their parameters.
        reason = "server_side_binding is on"
    elif not settings_dict["AUTOCOMMIT"]:
        # Each read of a thread is then in the transaction of that thread.
        reason = "AUTOCOMMIT is off"
    elif "cursor_factory" in settings_dict["OPTIONS"]:
        # Django then makes cursors of that class. Only its own client cursor merges the parameters as Rust needs.
        reason = "OPTIONS has a cursor_factory"
    else:
        addresses = _libpq_addresses(database)
        # The pipeline mode needs libpq 14.
        reason = f"the {database.pq.__impl__} implementation of psycopg does not give the libpq functions of Bolt"
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


def _loop_for_reads() -> WorkerLoop | None:
    """Return the event loop of this thread when a read can run on it, or None.

    A read uses a thread in these conditions:

    - The loop is not the loop of a Bolt worker. A pool lives as long as its loop.
    - The request has Django middleware. Its lane keeps state for the
      request, such as the schema of a tenant.
    - Sync code called the coroutine with ``async_to_sync``. Django's method
      then runs on the thread of that code, with its transaction.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if type(loop) is not WorkerLoop:
        return None
    if in_lane_mode() or _thread_sensitive_context.get(None) is not None:
        return None
    if getattr(AsyncToSync.executors, "current", None) is not None:
        return None
    return loop


def _pool_of(loop: WorkerLoop, alias: str) -> _Pool | None:
    """Return the pool of ``loop`` for the database ``alias``, or None when its reads use a thread.

    A read uses a thread in these conditions:

    - The database is not PostgreSQL with psycopg 3, Django's backend,
      client-side parameters and ``AUTOCOMMIT``.
    - The connection of the thread is in a transaction. The test client gives
      the handlers such a connection, and only it has the rows of the test.
    - The connection has an execute wrapper. A wrapper is sync code around the
      query: it measures the time of the query and gets its error. On the
      event loop, the wrapper would return before the query runs.
    """
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


def _pool_of_queryset(queryset: QuerySet) -> _Pool | None:
    """Return the pool for a read of ``queryset`` that builds no row of a model, or None.

    None is for each condition of ``_loop_for_reads`` and of ``_pool_of``, and
    for a database router that queries. Django's method runs the router on a thread.
    """
    loop = _loop_for_reads()
    if loop is None:
        return None
    try:
        # Without ``using()``, the database router gives the database.
        alias = queryset.db
    except SynchronousOnlyOperation:
        return None
    return _pool_of(loop, alias)


def _pool_for(queryset: QuerySet) -> _Pool | None:
    """Return the pool for a read that builds rows, or None when the read must use Django's own method.

    Django's method runs the sync ORM on a thread, with the connection of
    Django. A read needs it in these conditions:

    - The QuerySet has ``prefetch_related``. The prefetch queries are sync code.
    - The class of the QuerySet has its own ``_fetch_all``, or the QuerySet
      has an iterable class that is not of Django. Such code can query.
    - The code that builds a row of this model queried before: see ``_fetch_all``.
    - Each condition of ``_pool_of_queryset``.
    """
    if queryset._prefetch_related_lookups:
        return None
    if type(queryset)._fetch_all is not _django_fetch_all or queryset._iterable_class.__module__ != _ITERABLES_MODULE:
        return None
    if queryset.model in _models_on_threads:
        return None
    return _pool_of_queryset(queryset)


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
    loop = _loop_for_reads()
    pool = _pool_of(loop, alias) if loop is not None else None
    if pool is None:
        return None
    wrapper = connections[alias]
    if not await _prepare(pool, wrapper):
        return None
    try:
        return await pool.fetch(wrapper, sql, params)
    except _OnThread:
        return None


async def _compiled(pool: _Pool, compiler: Any) -> Any:
    """Compile the query of ``compiler`` and return its statement and its parameters.

    Returns ``()`` for a query that can match nothing: ``execute_sql`` gives
    no rows for it. Returns None when the read must use a thread: the pool
    found that after its first connection, or an expression needed the
    database to compile. The thread path gives such an expression a connection.
    """
    if not await _prepare(pool, compiler.connection):
        return None
    try:
        sql, params = compiler.as_sql()
    except EmptyResultSet:
        return ()
    except SynchronousOnlyOperation:
        return None
    return (sql, params) if sql else ()


async def _all_columns(pool: _Pool, compiler: Any) -> Any:
    """Return the rows of the query of ``compiler`` from the database, with each column of the statement.

    Returns None when the read must use a thread: see ``_compiled``.
    """
    statement = await _compiled(pool, compiler)
    if not statement:
        return statement
    try:
        return await pool.fetch(compiler.connection, *statement)
    except _OnThread:
        return None


async def _rows(pool: _Pool, compiler: Any) -> Any:
    """Return the rows of the query of ``compiler``, as ``execute_sql`` gives them, or None: see ``_all_columns``."""
    rows = await _all_columns(pool, compiler)
    if rows and compiler.has_extra_select:
        # ``execute_sql`` removes the columns that the query selects only to order the rows.
        count = compiler.col_count
        rows = [row[:count] for row in rows]
    return rows


def _build(queryset: QuerySet, compiler: Any, rows: Any, alias: str, **options: Any) -> list[Any]:
    """Build the result of ``queryset`` from ``rows`` of the database, with the iterable class of Django.

    ``compiler`` is the compiler that gave the statement of the rows.
    ``options`` are the arguments of the iterable class.
    """
    query = queryset.query
    # The iterable class asks the query for a compiler and the compiler for
    # the rows. Give it this compiler, which has the rows.
    compiler.execute_sql = lambda *_args, **_kwargs: (rows,)
    query.get_compiler = lambda *_args, **_kwargs: compiler
    # The iterable class reads ``queryset.db`` for the instances that it builds.
    database_of_the_queryset = queryset._db
    queryset._db = alias
    try:
        return list(queryset._iterable_class(queryset, **options))
    finally:
        queryset._db = database_of_the_queryset
        del query.get_compiler


async def _fetch_all(queryset: QuerySet, pool: _Pool) -> None:
    """Fill the result cache of ``queryset``: ``QuerySet._fetch_all`` with the query on the event loop."""
    # A router can give another database for each call of ``queryset.db``. The read uses the database of its pool.
    alias = pool.alias
    compiler = queryset.query.get_compiler(using=alias)
    rows = await _rows(pool, compiler)
    if rows is None:
        await SyncToAsync(queryset._fetch_all)()
        return
    try:
        queryset._result_cache = _build(queryset, compiler, rows, alias)
        return
    except SynchronousOnlyOperation:
        # The code that builds a row queried: a ``from_db`` method, a field
        # converter or a signal receiver. Only a thread can run it. The
        # thread reads the rows again, and each later read of this model
        # starts on a thread.
        _models_on_threads.add(queryset.model)
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

    async def generator() -> Any:
        # Another iterator of this QuerySet can have read the rows after this one was made.
        if self._result_cache is None:
            # The code that reads the iterator can be in a transaction that started after this call.
            pool = _pool_for(self)
            if pool is None:
                async for item in _django_aiter(self):
                    yield item
                return
            await _fetch_all(self, pool)
        for item in self._result_cache:
            yield item

    return generator()


async def _aiterator(self: QuerySet, chunk_size: int = 2000) -> Any:
    """``QuerySet.aiterator``: the rows in chunks of ``chunk_size``, and no result cache.

    As Django, the read opens a cursor of the server, and each chunk is one
    ``FETCH`` statement. The cursor closes at the last row, and also when the
    code that reads the iterator stops before that row. With
    ``DISABLE_SERVER_SIDE_CURSORS``, Django reads all rows with one statement,
    and builds them in chunks.
    """
    if chunk_size <= 0:
        raise ValueError("Chunk size must be strictly positive.")
    pool = _pool_for(self)
    compiler = cursor = rows = None
    if pool is not None:
        alias = pool.alias
        compiler = self.query.get_compiler(using=alias)
        wrapper = compiler.connection
        if wrapper.settings_dict.get("DISABLE_SERVER_SIDE_CURSORS"):
            rows = await _all_columns(pool, compiler)
        else:
            statement = await _compiled(pool, compiler)
            if statement:
                with contextlib.suppress(_OnThread):
                    cursor = await pool.declare(wrapper, *statement)
            elif statement is not None:
                rows = ()
    if cursor is None and rows is None:
        async for item in _django_aiterator(self, chunk_size):
            yield item
        return

    loop = asyncio.get_running_loop()
    options = {"chunked_fetch": cursor is not None, "chunk_size": chunk_size}
    # ``execute_sql`` removes the columns that the query selects only to order the rows.
    columns = compiler.col_count if compiler.has_extra_select else None
    on_thread = False
    start = 0
    try:
        while True:
            if cursor is None:
                chunk = rows[start : start + chunk_size]
                start += chunk_size
            else:
                chunk = await pool.fetch_cursor(*cursor, chunk_size)
            if columns is not None:
                chunk = [row[:columns] for row in chunk]
            if not on_thread:
                try:
                    items = _build(self, compiler, chunk, alias, **options)
                except SynchronousOnlyOperation:
                    # The code that builds a row queried: see ``_fetch_all``. A thread builds
                    # this chunk again, and each later chunk.
                    _models_on_threads.add(self.model)
                    on_thread = True
            if on_thread:
                items = await SyncToAsync(_build)(self, compiler, chunk, alias, **options)
            for item in items:
                yield item
            if len(chunk) < chunk_size:
                return
    finally:
        if cursor is not None:
            # The garbage collector can end this generator on another thread. Only the thread of the loop uses a pool.
            if asyncio._get_running_loop() is loop:
                pool.close_cursor(*cursor)
            else:
                loop.call_soon_threadsafe(pool.close_cursor, *cursor)


async def _aget(self: QuerySet, *args: Any, **kwargs: Any) -> Any:
    """``QuerySet.aget``. The lines after the pool are those of ``QuerySet.get``, with the fetch awaited."""
    if (self.query.combinator and (args or kwargs)) or type(self).get is not _django_get:
        return await _django_aget(self, *args, **kwargs)
    pool = _pool_for(self)
    if pool is None:
        return await _django_aget(self, *args, **kwargs)
    try:
        clone = self._chain() if self.query.combinator else self.filter(*args, **kwargs)
        if self.query.can_filter() and not self.query.distinct_fields:
            clone = clone.order_by()
    except SynchronousOnlyOperation:
        # A method of the class queried, for example its own ``order_by``. Only a thread can run it.
        return await _django_aget(self, *args, **kwargs)
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


async def _first_row(queryset: QuerySet, method: str, pool: _Pool, django_method: Any) -> Any:
    """Run ``first`` or ``last`` of Django, with the query on the event loop.

    The two methods differ between Django versions, so this function does not
    copy them. It calls the method of Django on a clone that stops where it
    would query. That clone gives the QuerySet that Django built: ordered,
    reversed and limited to one row. The event loop then reads its rows.

    The method of Django calls other methods of the class, such as
    ``order_by``. When one of them queries, ``django_method`` runs the read
    on a thread.
    """
    queryset_class = type(queryset)
    try:
        clone = queryset._chain()
        clone.__class__ = _stopping_class(queryset_class)
        return getattr(clone, method)()
    except _Unfetched as unfetched:
        built = unfetched.queryset
    except SynchronousOnlyOperation:
        return await django_method(queryset)
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
    return await _first_row(self, "first", pool, _django_afirst)


async def _alast(self: QuerySet) -> Any:
    """``QuerySet.alast``."""
    pool = _pool_for(self) if type(self).last is _django_last else None
    if pool is None:
        return await _django_alast(self)
    return await _first_row(self, "last", pool, _django_alast)


async def _aexists(self: QuerySet) -> bool:
    """``QuerySet.aexists``: the lines of ``QuerySet.exists`` and ``Query.has_results``, with the query awaited."""
    if type(self).exists is not _django_exists:
        return await _django_aexists(self)
    if self._result_cache is not None:
        return bool(self._result_cache)
    pool = _pool_for(self)
    if pool is None:
        return await _django_aexists(self)
    rows = await _rows(pool, self.query.exists().get_compiler(using=pool.alias))
    if rows is None:
        return await _django_aexists(self)
    return bool(rows)


class _Reached(Exception):  # noqa: N818 - not an error: it carries a compiler out of a sync method
    """Sync ORM code reached the point where it sends a statement."""

    def __init__(self, compiler: Any, result_type: Any) -> None:
        super().__init__()
        self.compiler = compiler
        self.result_type = result_type


# Compiler class of Django -> its subclass that stops where it would send a statement.
_stopping_compilers: dict[type, type] = {}


def _stop(self: Any, result_type: Any = None, *args: Any, **kwargs: Any) -> Any:
    """``SQLCompiler.execute_sql`` of a stopping compiler."""
    raise _Reached(self, result_type)


def _to_the_statement(queryset: QuerySet, alias: str, ops: Any, method: Any, args: Any, kwargs: Any) -> Any:
    """Call ``method(queryset, ...)`` of Django. Raise ``_Reached`` where Django would send a statement.

    ``ops`` is of the connection wrapper of this request, and this function
    does not wait. Thus no other read gets a stopping compiler.
    """
    make = type(ops).compiler

    def compiler(name: str) -> type:
        real = make(ops, name)
        stopping = _stopping_compilers.get(real)
        if stopping is None:
            stopping = _stopping_compilers[real] = type(
                real.__name__, (real,), {"execute_sql": _stop, "__module__": __name__}
            )
        return stopping

    # A router can give another database for each call of ``queryset.db``. The read uses the database of its pool.
    database_of_the_queryset = queryset._db
    queryset._db = alias
    ops.compiler = compiler
    try:
        return method(queryset, *args, **kwargs)
    finally:
        del ops.compiler
        queryset._db = database_of_the_queryset


async def _aggregation(queryset: QuerySet, pool: _Pool, method: Any, args: Any, kwargs: Any, key: str | None) -> Any:
    """Run ``count`` or ``aggregate`` of Django, with the statement on the event loop.

    ``key`` is the entry of the aggregation that the method returns, or None
    for the whole aggregation. Returns ``_MISSING`` when a thread must run
    the method.

    Django builds an aggregate query in more than one way, and that code
    differs between versions. So this function does not copy it. It runs the
    method of Django to the point where Django would send the statement.
    That gives the compiler. The event loop reads the row. The last lines
    are those of ``Query.get_aggregation`` after the statement, which are the
    same in each supported version of Django.
    """
    alias = pool.alias
    wrapper = connections[alias]
    if not await _prepare(pool, wrapper):
        return _MISSING
    try:
        # A call that needs no statement returns here, for example ``aggregate()`` with no expression.
        return _to_the_statement(queryset, alias, wrapper.ops, method, args, kwargs)
    except _Reached as reached:
        if reached.result_type != SINGLE:
            return _MISSING
        compiler = reached.compiler
    except SynchronousOnlyOperation:
        return _MISSING
    rows = await _all_columns(pool, compiler)
    if rows is None:
        return _MISSING
    annotations = compiler.query.annotation_select
    if rows:
        # ``execute_sql`` gives the first row, with the columns that the query asked for.
        cols = annotations.values()
        converted = compiler.apply_converters((rows[0][: compiler.col_count],), compiler.get_converters(cols))
        if compiler.has_composite_fields(cols):
            converted = compiler.composite_fields_to_tuples(converted, cols)
        result = next(converted)
    else:
        # Django sends no statement for a query that can match nothing.
        result = [expression.empty_result_set_value for expression in annotations.values()]
    aggregation = dict(zip(annotations, result, strict=False))
    return aggregation if key is None else aggregation[key]


async def count(queryset: QuerySet) -> int | None:
    """Count the rows of ``queryset`` from the event loop: ``QuerySet.count``.

    Returns None for a read that must use the ORM thread pool (see
    ``_pool_of``), and for a QuerySet class with its own ``count``. Call this
    function only when ``enabled`` is true.
    """
    if type(queryset).count is not _django_count:
        return None
    if queryset._result_cache is not None:
        # Django counts the rows that the QuerySet has, and uses no connection.
        return len(queryset._result_cache)
    pool = _pool_of_queryset(queryset)
    if pool is None:
        return None
    # ``Query.get_count`` gives this entry of its aggregation.
    result = await _aggregation(queryset, pool, _django_count, (), {}, "__count")
    return None if result is _MISSING else result


async def _acount(self: QuerySet) -> int:
    """``QuerySet.acount``."""
    result = await count(self)
    if result is None:
        return await _django_acount(self)
    return result


async def _aaggregate(self: QuerySet, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """``QuerySet.aaggregate``."""
    pool = _pool_of_queryset(self) if type(self).aggregate is _django_aggregate else None
    if pool is None:
        return await _django_aaggregate(self, *args, **kwargs)
    result = await _aggregation(self, pool, _django_aggregate, args, kwargs, None)
    if result is _MISSING:
        return await _django_aaggregate(self, *args, **kwargs)
    return result


def configure() -> None:
    """Read the setting and the limits of the pools. The server calls this function at startup.

    With the setting on, ``QuerySet`` gets the methods of this module.
    With the setting off, it gets Django's methods back in their place.

    Raises:
        ImproperlyConfigured: BOLT_ASYNC_ORM_ON_LOOP is not a bool,
            DJANGO_BOLT_LOOP_CONNECTIONS or DJANGO_BOLT_LOOP_PIPELINE_DEPTH
            is not an int of 1 or more, or
            DJANGO_BOLT_LOOP_ACQUIRE_SECONDS, DJANGO_BOLT_LOOP_IDLE_SECONDS or
            DJANGO_BOLT_LOOP_LIFETIME_SECONDS is not a number that ``_seconds`` accepts.
    """
    global enabled, _connection_limit, _pipeline_depth, _acquire_seconds, _idle_seconds, _lifetime_seconds
    limit = _thread_count(CONNECTIONS_VARIABLE)
    depth = _thread_count(PIPELINE_VARIABLE)
    acquire_seconds = _seconds(ACQUIRE_VARIABLE, DEFAULT_ACQUIRE_SECONDS)
    idle_seconds = _seconds(IDLE_VARIABLE, DEFAULT_IDLE_SECONDS)
    lifetime_seconds = _seconds(LIFETIME_VARIABLE, DEFAULT_LIFETIME_SECONDS)
    if not settings.configured:
        return
    _connection_limit = limit or DEFAULT_CONNECTIONS
    _pipeline_depth = depth or DEFAULT_PIPELINE_DEPTH
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
        ("aiterator", _aiterator, _django_aiterator),
        ("aget", _aget, _django_aget),
        ("afirst", _afirst, _django_afirst),
        ("alast", _alast, _django_alast),
        ("aexists", _aexists, _django_aexists),
        ("acount", _acount, _django_acount),
        ("aaggregate", _aaggregate, _django_aaggregate),
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
