"""App under test for recovery from a database connection that died.

``/me`` is the path that failed in production: an async handler behind the
default ``JWTAuthentication`` reads ``request.user``. The lazy load runs
``get_user_sync`` on the bounded ORM pool through ``run_orm_blocking``. The
Django connection it uses is parked on a pool thread between requests. When
the database server closes that connection, the next load must fail once at
most, and the request after it must reconnect.

``/orm-threads`` runs one query on each of ``count`` ORM pool threads, so a
test can park a connection on every thread and see which threads reconnect.

``/seed`` migrates the database of the subprocess and creates the test user,
since server projects start with an unmigrated database. ``SECRET`` and
``USERNAME`` are exported so tests can mint matching tokens.

``/me-inline`` is ``/me`` with no await, so Bolt dispatches it synchronously
on the worker thread.

``/echo`` sends a token through a query and returns what the database sent
back, with the process that served it. Two processes that share a socket read
each other's results, so a token that comes back wrong shows the sharing.

``/stream`` returns a sync streaming body that queries on the thread that
iterates it.

``/orm-threads-both-databases`` is ``/orm-threads`` with one query on the
``other`` alias before the query on ``default``.

``/queued-orm-calls`` queues ``count`` calls on the ORM pool at once and
returns how many connections they opened or checked out of a pool.

``/default-executor-query`` queries through ``sync_to_async`` with
``thread_sensitive=False``, which runs on the default executor of the loop.

``/lose-the-pool`` queries on the worker thread, as ``/me-inline`` does. Then
it makes the return of the connection to its pool fail. With ``fail``, the
call raises too. With ``kill``, the call ends its own database session, so the
connection is unusable. The response names the result or the error of the call.

``/pool-stats`` returns the statistics of the connection pool of ``default``.

``/lose-the-pool-on-an-orm-thread`` queries on an ORM pool thread and makes the
return of its connection fail. The worker gives the connection back when its
queue is empty, so the return fails on the idle path of the worker.

``/stream-and-lose-the-pool`` is ``/stream``, but the return of the connection
of the streaming thread fails when the body ends.

``/autocommit-off-then-fail`` runs a call on the ORM pool that turns autocommit
off, creates a temporary table and raises. Then it returns the state of the
connection that the next call gets.

``/query-in-a-helper`` is a sync handler with no query in its own source, so
Bolt runs it on the worker thread that accepted the request. Its helper
queries. With ``fail``, the handler raises after the query.

``lane_api`` is a second API, under ``/lane``, with a Django middleware stack.
Its sync route ``/lane/query`` runs complete on a request lane. A test project
lists both APIs in ``BOLT_API``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, connections
from django.db.backends.signals import connection_created
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.concurrency import run_in_orm_executor, run_orm_blocking
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.responses import StreamingResponse

SECRET = "dead-connection-recovery-secret-key-32-bytes"
USERNAME = "recovered"

api = BoltAPI()

# Django sends connection_created for each new connection and for each checkout from a pool.
_checkouts = [0]
_checkouts_lock = threading.Lock()


def _count_checkout(sender, connection, **kwargs) -> None:
    with _checkouts_lock:
        _checkouts[0] += 1


connection_created.connect(_count_checkout, dispatch_uid="dead_connection_recovery.count_checkout")


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/seed")
def seed():
    call_command("migrate", interactive=False, verbosity=0)
    user, _ = User.objects.get_or_create(username=USERNAME)
    return {"user_id": user.id}


@api.get("/me", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
async def me(request):
    # Forces the lazy load: load_via_sync -> run_orm_blocking -> get_user_sync.
    return {"username": request.user.username}


@api.get("/me-inline", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
async def me_inline(request):
    # No await: Bolt runs this handler on the worker thread with no event
    # loop step, and the lazy user load blocks the loop of that thread.
    return {"username": request.user.username}


@api.get("/orm-threads")
async def orm_threads(count: int):
    """Run ``SELECT 1`` on ``count`` ORM pool threads at the same time.

    The barrier holds each call until all of them run, so each call has a
    thread of its own. Each result is ``"ok"`` or the name of the error.
    """
    barrier = threading.Barrier(count, timeout=5)

    def select_one() -> str:
        barrier.wait()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return "ok"

    results = await asyncio.gather(*(run_in_orm_executor(select_one) for _ in range(count)), return_exceptions=True)
    return {"results": [result if isinstance(result, str) else type(result).__name__ for result in results]}


@api.get("/break-own-connection")
async def break_own_connection():
    """Close the raw connection of an ORM pool thread, then query on it.

    The query fails with no network I/O, so the thread finds a dead connection
    while the network to the database stays as it is.
    """

    def close_then_query() -> None:
        connection.ensure_connection()
        connection.connection.close()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

    await run_in_orm_executor(close_then_query)
    return {"ok": True}


@api.get("/echo")
def echo(token: str):
    with connection.cursor() as cursor:
        # The sleep keeps the query on the socket long enough for another
        # process that shares it to write its own query.
        cursor.execute("SELECT %s::text, pg_sleep(0.002)", [token])
        (echoed, _) = cursor.fetchone()
    return {"token": echoed, "pid": os.getpid()}


@api.get("/stream")
def stream(rows: int):
    def body():
        with connection.cursor() as cursor:
            cursor.execute("SELECT generate_series(1, %s)", [rows])
            for (value,) in cursor.fetchall():
                yield f"{value}\n"

    return StreamingResponse(body(), media_type="text/plain")


@api.get("/orm-threads-both-databases")
async def orm_threads_both_databases(count: int):
    """Run one query on ``other`` and then one on ``default``, on ``count`` ORM pool threads at once."""
    barrier = threading.Barrier(count, timeout=5)

    def select_on_both() -> str:
        barrier.wait()
        for alias in ("other", "default"):
            with connections[alias].cursor() as cursor:
                cursor.execute("SELECT 1")
        return "ok"

    results = await asyncio.gather(*(run_in_orm_executor(select_on_both) for _ in range(count)), return_exceptions=True)
    return {"results": [result if isinstance(result, str) else type(result).__name__ for result in results]}


def _select_one() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return "ok"


@api.get("/queued-orm-calls")
async def queued_orm_calls(count: int):
    """Queue ``count`` calls on the ORM pool at once.

    The first call waits until the other calls are in the queue, so with one
    ORM thread every call after the first finds work queued behind it.
    """
    others_queued = threading.Event()

    def first() -> str:
        if not others_queued.wait(5):
            return "the other calls were not queued in 5 s"
        return _select_one()

    before = _checkouts[0]
    calls = [asyncio.ensure_future(run_in_orm_executor(first))]
    calls += [asyncio.ensure_future(run_in_orm_executor(_select_one)) for _ in range(count - 1)]
    # Each task runs to its first await, which hands its call to the pool.
    await asyncio.sleep(0)
    others_queued.set()
    results = await asyncio.gather(*calls)
    return {"results": results, "checkouts": _checkouts[0] - before}


@api.get("/default-executor-query")
async def default_executor_query():
    return {"result": await sync_to_async(_select_one, thread_sensitive=False)()}


def _select_then_lose_the_pool(fail: bool, kill: bool) -> str:
    _select_one()
    # psycopg_pool clears this reference when it closes a connection. Django
    # then cannot give the connection back, and its close() raises.
    connection.connection._pool = None
    if kill:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pg_backend_pid())")
    if fail:
        raise ValueError("the call failed")
    return "ok"


@api.get("/lose-the-pool")
async def lose_the_pool(fail: bool = False, kill: bool = False):
    # No await: the query runs as one guarded call on the worker thread. That
    # call gives the pooled connection back when it ends.
    try:
        result = run_orm_blocking(_select_then_lose_the_pool, fail, kill)
    except Exception as exc:
        result = type(exc).__name__
    return {"result": result}


@api.get("/pool-stats")
async def pool_stats():
    """The statistics of the connection pool of ``default``: ``pool_size``, ``pool_available`` and so on."""
    return connection.pool.get_stats()


def _lose_the_pool_on_this_thread() -> str:
    _select_one()
    # See _select_then_lose_the_pool: the return of this connection to its pool fails.
    connection.connection._pool = None
    return "ok"


@api.get("/lose-the-pool-on-an-orm-thread")
async def lose_the_pool_on_an_orm_thread():
    # The worker gives its connection back when its queue is empty, so the
    # failed return happens on the idle path of the worker, not in this call.
    return {"result": await run_in_orm_executor(_lose_the_pool_on_this_thread)}


@api.get("/stream-and-lose-the-pool")
def stream_and_lose_the_pool(rows: int):
    def body():
        with connection.cursor() as cursor:
            cursor.execute("SELECT generate_series(1, %s)", [rows])
            for (value,) in cursor.fetchall():
                yield f"{value}\n"
        connection.connection._pool = None

    return StreamingResponse(body(), media_type="text/plain")


_backend_pids: list[int] = []


def _autocommit_off_then_fail() -> None:
    connection.set_autocommit(False)
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMP TABLE left_open (x int)")
    _backend_pids.append(connection.connection.info.backend_pid)
    raise ValueError("the call failed with autocommit off")


def _connection_state() -> dict[str, object]:
    state = {
        "autocommit": connection.get_autocommit(),
        "transaction_status": connection.connection.info.transaction_status.name,
        "same_backend": connection.connection.info.backend_pid == _backend_pids[-1],
    }
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('pg_temp.left_open') IS NULL")
        state["rolled_back"] = cursor.fetchone()[0]
    return state


@api.get("/autocommit-off-then-fail")
async def autocommit_off_then_fail():
    with contextlib.suppress(ValueError):
        await run_in_orm_executor(_autocommit_off_then_fail)
    return await run_in_orm_executor(_connection_state)


@api.get("/query-in-a-helper")
def query_in_a_helper(fail: bool = False):
    result = _select_one()
    if fail:
        raise ValueError("the handler failed after its query")
    return {"result": result, "thread": threading.current_thread().name}


class _QueryMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _select_one()


# A sync route behind Django middleware runs complete on a request lane. The
# lane gives its pooled connection back at the end of its request.
lane_api = BoltAPI(prefix="/lane", middleware=[DjangoMiddlewareStack([_QueryMiddleware])])


@lane_api.get("/health")
async def lane_health():
    return {"status": "ok"}


@lane_api.get("/query")
def lane_query():
    return {"result": _select_one(), "thread": threading.current_thread().name}
