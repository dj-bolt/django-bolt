"""The async ORM reads on the event loop, against a real PostgreSQL database.

With ``BOLT_ASYNC_ORM_ON_LOOP``, ``async for`` over a QuerySet and ``aget()``
send their query from the event loop of the worker. No thread runs the query.
Django builds the SQL and builds the rows, so each result must equal the
result of the sync ORM.

Each test adds one database to Django: a new PostgreSQL database with the
tables of the test models. ``default`` stays the SQLite database.

Each ``on_loop`` test runs two times, one time for each way to send a query:
Rust on the connection of psycopg, and the async cursor of psycopg.

Set ``DJANGO_BOLT_TEST_POSTGRES_DSN`` to run them. Without the variable, they skip.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

import msgspec
import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connections, models, transaction
from django.db.backends.signals import connection_created
from django.db.models import Count, Func, IntegerField
from django.db.models.expressions import RawSQL
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, Request, async_orm, concurrency
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.auth.jwt_utils import create_jwt_for_user
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.serializers import Serializer, computed_field
from django_bolt.testing import TestClient
from tests.test_models import Author, BlogPost, Tag

from .test_dead_connection_recovery_server_integration import backend_pids, terminate_backends

pytestmark = pytest.mark.postgres

# The alias of the PostgreSQL database of the running test. ``loop_db`` sets it.
ALIAS = ""
ORM_THREAD = "bolt_orm"


class ThreadNameField(models.CharField):
    """A field that reads as the name of the thread that built its row."""

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> str:
        return threading.current_thread().name


class LoopProbe(models.Model):
    label = models.CharField(max_length=50)
    built_on = ThreadNameField(max_length=50, default="")

    class Meta:
        app_label = "django_bolt"


MODELS = (Author, Tag, BlogPost, LoopProbe)


class ProbeItem(msgspec.Struct):
    label: str
    built_on: str


class ProbeRenamed(msgspec.Struct):
    label: str = msgspec.field(name="Label")
    built_on: str = msgspec.field(name="builtOn")


class AuthorItem(msgspec.Struct):
    name: str


class PostWithAuthor(msgspec.Struct):
    title: str
    author: AuthorItem


class ProbeSerializer(Serializer):
    label: str
    built_on: str


class ProbeWithComputedField(Serializer):
    label: str
    built_on: str

    @computed_field
    def shout(self) -> str:
        return self.label.upper()


FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)

# More than the socket buffers of a slow link hold, so one send or one read cannot move the data.
LARGE_STATEMENT = 12_000_000
# name -> how many times each row repeats its text: a result of 0.4 MB, which
# the server sends at one time, and a result of 11 MB.
RESULT_SIZES = {"one-burst": 2_000, "many-buffers": 50_000}

# Functions of the test database: an error with each field that a client can read, and a notice.
DATABASE_FUNCTIONS = (
    """
    CREATE FUNCTION bolt_fail() RETURNS integer AS $$
    BEGIN
        RAISE EXCEPTION 'custom failure' USING ERRCODE = '22B99', DETAIL = 'the detail', HINT = 'the hint';
    END $$ LANGUAGE plpgsql
    """,
    """
    CREATE FUNCTION bolt_notice() RETURNS integer AS $$
    BEGIN
        RAISE NOTICE 'a notice from the server';
        RETURN 7;
    END $$ LANGUAGE plpgsql
    """,
)


class LoopDb(NamedTuple):
    params: dict[str, Any]
    application_name: str

    def wait_for_backends(self, count: int) -> None:
        """Wait until the server has ``count`` connections with the name of this database."""
        deadline = time.monotonic() + 10
        while len(pids := backend_pids(self.params, self.application_name)) != count:
            assert time.monotonic() < deadline, f"{len(pids)} backends, expected {count}"
            time.sleep(0.02)


def _seed() -> None:
    ada = Author.objects.using(ALIAS).create(name="Ada", email="ada@example.com")
    bob = Author.objects.using(ALIAS).create(name="Bob", email="bob@example.com")
    Author.objects.using(ALIAS).create(name="Cy", email="cy@example.com")
    tags = [Tag.objects.using(ALIAS).create(name=name) for name in ("rust", "django")]
    # More rows than ``get`` reads before it reports that there are too many.
    Tag.objects.using(ALIAS).bulk_create([Tag(name=f"unused-{number}") for number in range(25)])
    for title, author, published in (("Loops", ada, True), ("Lanes", ada, False), ("Pools", bob, True)):
        post = BlogPost.objects.using(ALIAS).create(title=title, content="text", author=author, published=published)
        post.tags.set(tags if published else tags[:1])
    LoopProbe.objects.using(ALIAS).create(label="a")
    LoopProbe.objects.using(ALIAS).create(label="b")


def _add_database(postgres_database, monkeypatch, django_db_blocker, options: dict[str, Any]) -> Any:
    """Add a PostgreSQL database to Django, with the tables and the rows of the tests.

    Each test gets a new alias. The threads of the framework live longer than
    a test, and each keeps its connection wrapper for an alias. With one alias
    for all tests, such a wrapper would name the database of an earlier test.
    """
    monkeypatch.setattr(sys.modules[__name__], "ALIAS", f"loopdb_{secrets.token_hex(4)}")
    application_name = f"bolt-loop-{secrets.token_hex(4)}"
    database = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": postgres_database["dbname"],
        "USER": postgres_database.get("user", ""),
        "PASSWORD": postgres_database.get("password", ""),
        "HOST": postgres_database.get("host", ""),
        "PORT": postgres_database.get("port", ""),
        "OPTIONS": {"application_name": application_name, **options},
    }
    configured = connections.configure_settings({"default": connections.settings["default"], ALIAS: database})
    monkeypatch.setitem(connections.settings, ALIAS, configured[ALIAS])
    with django_db_blocker.unblock():
        try:
            with connections[ALIAS].schema_editor() as editor:
                for model in MODELS:
                    editor.create_model(model)
            with connections[ALIAS].cursor() as cursor:
                for function in DATABASE_FUNCTIONS:
                    cursor.execute(function)
            _seed()
            yield LoopDb(postgres_database, application_name)
        finally:
            connections[ALIAS].close()
            del connections[ALIAS]


@pytest.fixture
def loop_db(postgres_database, monkeypatch, django_db_blocker):
    yield from _add_database(postgres_database, monkeypatch, django_db_blocker, {})


class _SlowLink:
    """A relay to the database that moves 16 KB in each millisecond.

    On the loopback interface, the server reads a statement as fast as the
    client sends it, and the client reads a result as fast as the server sends
    it. Behind this relay, the socket of the client fills during a large
    statement, and a large result comes in many small parts.
    """

    CHUNK = 16 * 1024

    def __init__(self, params: dict[str, Any]) -> None:
        host = params.get("host") or "/var/run/postgresql"
        port = int(params.get("port") or 5432)
        # libpq reads a host that starts with a slash as the directory of a Unix socket.
        self.target: Any = f"{host}/.s.PGSQL.{port}" if host.startswith("/") else (host, port)
        self.listener = socket.create_server(("127.0.0.1", 0))
        # ``accept`` must return so that its thread can see ``closed``.
        self.listener.settimeout(0.05)
        self.closed = False
        self.port = self.listener.getsockname()[1]
        self.sockets: list[socket.socket] = []
        self.lock = threading.Lock()
        self.threads = [threading.Thread(target=self._accept, daemon=True)]
        self.threads[0].start()

    def _accept(self) -> None:
        while not self.closed:
            try:
                client, _address = self.listener.accept()
            except TimeoutError:
                continue
            client.settimeout(None)
            family = socket.AF_UNIX if isinstance(self.target, str) else socket.AF_INET
            server = socket.socket(family, socket.SOCK_STREAM)
            server.connect(self.target)
            pumps = [
                threading.Thread(target=self._pump, args=(client, server), daemon=True),
                threading.Thread(target=self._pump, args=(server, client), daemon=True),
            ]
            with self.lock:
                self.sockets += [client, server]
                self.threads += pumps
            for pump in pumps:
                pump.start()

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while data := source.recv(self.CHUNK):
                sink.sendall(data)
                time.sleep(0.001)
        # The other side must see the end of the data.
        for end in (source, sink):
            with contextlib.suppress(OSError):
                end.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self.closed = True
        # The accept thread is first. After it ends, no new socket and no new thread can come.
        self.threads[0].join(timeout=10)
        self.listener.close()
        for link in self.sockets:
            with contextlib.suppress(OSError):
                link.shutdown(socket.SHUT_RDWR)
        for thread in self.threads:
            thread.join(timeout=10)
        for link in self.sockets:
            link.close()


RUST = "rust"
PSYCOPG = "psycopg"
# The database options that select each way to send a query. With
# ``server_side_binding``, psycopg must send the query: see ``async_orm._new_pool``.
TRANSPORT_OPTIONS: dict[str, dict[str, Any]] = {RUST: {}, PSYCOPG: {"server_side_binding": True}}


@pytest.fixture(params=sorted(TRANSPORT_OPTIONS))
def transport(request) -> str:
    return request.param


@pytest.fixture
def on_loop(transport, postgres_database, monkeypatch, django_db_blocker):
    """Add the database and turn the setting on for the servers of the test."""
    for database in _add_database(postgres_database, monkeypatch, django_db_blocker, TRANSPORT_OPTIONS[transport]):
        with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True):
            yield database
        # A server that starts with the setting off gives Django its own methods back.
        concurrency.check_pool_environment()


@pytest.fixture
def on_loop_through_a_slow_link(transport, postgres_database, monkeypatch, django_db_blocker):
    """``on_loop``, with the database behind a ``_SlowLink``."""
    link = _SlowLink(postgres_database)
    behind = {**postgres_database, "host": "127.0.0.1", "port": str(link.port)}
    try:
        for database in _add_database(behind, monkeypatch, django_db_blocker, TRANSPORT_OPTIONS[transport]):
            with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True):
                yield database
            concurrency.check_pool_environment()
    finally:
        link.close()


def _post(post: BlogPost) -> dict[str, Any]:
    return {
        "id": post.id,
        "title": post.title,
        "published": post.published,
        "created_at": post.created_at.isoformat(),
        "author_id": post.author_id,
    }


def _posts() -> Any:
    return BlogPost.objects.using(ALIAS)


# name -> (the QuerySet, the plain form of one of its rows)
QUERIES: dict[str, tuple[Callable[[], Any], Callable[[Any], Any]]] = {
    "instances": (lambda: _posts().order_by("id"), _post),
    "select_related": (
        lambda: _posts().select_related("author").order_by("id"),
        # ``post.author`` must not query: the event loop refuses a sync query.
        lambda post: {**_post(post), "author": post.author.name},
    ),
    "filtered": (
        lambda: (
            _posts()
            .filter(published=True, author__name__startswith="A", created_at__lte=FAR_FUTURE, id__in=[1, 2, 3])
            .order_by("-id")[:2]
        ),
        _post,
    ),
    "deferred": (lambda: _posts().only("title").order_by("id"), lambda post: [post.id, post.title]),
    "values": (lambda: _posts().values("id", "title", "author__name").order_by("id"), dict),
    "values_list": (lambda: _posts().values_list("id", "title").order_by("id"), list),
    "flat": (lambda: _posts().values_list("title", flat=True).order_by("id"), str),
    "named": (lambda: _posts().values_list("id", "title", named=True).order_by("id"), lambda row: [row.id, row.title]),
    "annotated": (
        lambda: Author.objects.using(ALIAS).annotate(posts_count=Count("posts")).order_by("id"),
        lambda author: [author.name, author.posts_count],
    ),
    "union": (
        lambda: (
            _posts()
            .filter(published=True)
            .values_list("title", flat=True)
            .union(_posts().filter(author__name="Ada").values_list("title", flat=True))
            .order_by("title")
        ),
        str,
    ),
    "empty_in": (lambda: _posts().filter(id__in=[]), _post),
    "none": (lambda: _posts().none(), _post),
    "datetimes": (lambda: _posts().values_list("created_at", flat=True).order_by("id"), lambda at: at.isoformat()),
}

# name -> (the QuerySet, the arguments of ``get``)
GETS: dict[str, tuple[Callable[[], Any], dict[str, Any]]] = {
    "one": (lambda: _posts().filter(published=False), {}),
    "by_arguments": (_posts, {"title": "Pools", "published": True}),
    "missing": (lambda: _posts().filter(title="No such post"), {}),
    "many": (lambda: _posts().filter(published=True), {}),
    "sliced": (lambda: _posts().order_by("id")[:1], {}),
    "too_many_to_count": (lambda: Tag.objects.using(ALIAS), {}),
    # ``get`` refuses arguments after a union.
    "union_with_arguments": (lambda: _posts().union(_posts()), {"title": "Pools"}),
    # A row lock needs a transaction.
    "locked": (lambda: _posts().select_for_update().filter(published=False), {}),
}


def _probes() -> Any:
    return LoopProbe.objects.using(ALIAS)


# name -> the QuerySet for ``first``, ``last`` and ``exists``
ONE_ROW_QUERIES: dict[str, Callable[[], Any]] = {
    "ordered": lambda: _posts().order_by("title"),
    # The model has a default order.
    "default_order": _posts,
    # The model has no default order, so ``first`` and ``last`` order by the primary key.
    "no_default_order": _probes,
    "no_rows": lambda: _posts().filter(title="No such post"),
    "values": lambda: _posts().order_by("id").values("id", "title"),
    "values_list": lambda: _posts().order_by("-id").values_list("title", flat=True),
    # ``last`` cannot reverse a slice.
    "sliced": lambda: _posts().order_by("id")[1:3],
    "slice_after_the_rows": lambda: _posts().order_by("id")[5:6],
    "empty_in": lambda: _posts().filter(id__in=[]),
    "none": lambda: _posts().none(),
    "union": lambda: _posts().filter(published=True).union(_posts().filter(published=False)).order_by("title"),
    # ``first`` and ``last`` refuse an aggregation with no order.
    "aggregation_with_no_order": lambda: Author.objects.using(ALIAS).values("name").annotate(Count("posts")),
    "distinct": lambda: _posts().order_by("published").values_list("published", flat=True).distinct(),
    # The prefetch queries are sync code, so this QuerySet stays on the thread path.
    "prefetch": lambda: _posts().prefetch_related("tags").order_by("id"),
    # Django needs a connection to compile this QuerySet, so its query runs on the thread path.
    "needs_a_connection_to_compile": lambda: _probes().annotate(value=_OpensConnection()).filter(value=7),
}


def _one_row_outcome(call: Callable[[], Any]) -> Any:
    """The plain form of a row, or of the error of the call."""
    try:
        row = call()
    except Exception as error:
        return {"error": type(error).__qualname__, "message": str(error)}
    if isinstance(row, models.Model):
        return {"model": type(row).__name__, "pk": row.pk}
    return _as_json(row)


async def _one_row_outcome_async(call: Callable[[], Any]) -> Any:
    try:
        row = await call()
    except Exception as error:
        return {"error": type(error).__qualname__, "message": str(error)}
    if isinstance(row, models.Model):
        return {"model": type(row).__name__, "pk": row.pk}
    return row


def _get_outcome(error: Exception | None, post: BlogPost | None) -> dict[str, Any]:
    if error is not None:
        return {"error": type(error).__qualname__, "message": str(error)}
    return {"title": post.title}


def _probe_with_backend_pid() -> Any:
    """A QuerySet with a default order, so ``get`` must drop the order."""
    return LoopProbe.objects.using(ALIAS).annotate(pid=RawSQL("pg_backend_pid()", [])).order_by("-label")


async def _backend_pid(nap: float = 0.05) -> int:
    probe = await (
        LoopProbe.objects.using(ALIAS)
        .annotate(pid=RawSQL("pg_backend_pid()", []), nap=RawSQL("(SELECT 1 FROM pg_sleep(%s))", [nap]))
        .aget(label="a")
    )
    return probe.pid


def _large_values(size: str) -> Any:
    """27 rows, each with its name repeated."""
    return (
        Tag.objects.using(ALIAS)
        .annotate(big=RawSQL("repeat(name, %s)", [RESULT_SIZES[size]]))
        .values_list("big", flat=True)
        .order_by("id")
    )


def _digest(values: list[str]) -> dict[str, Any]:
    return {"rows": len(values), "sha256": hashlib.sha256("".join(values).encode()).hexdigest()}


# name -> a QuerySet that the database refuses
ERROR_QUERIES: dict[str, Callable[[], Any]] = {
    "cast": lambda: _probes().extra(where=["label::integer = 1"]),
    "syntax": lambda: _probes().extra(where=["no such syntax"]),
    # An SQLSTATE that psycopg has no class for, with a detail and a hint. Its first 2 characters give the class.
    "custom": lambda: _probes().annotate(value=RawSQL("bolt_fail()", [])),
}


def _error_outcome(error: Exception) -> dict[str, Any]:
    """What a caller can read from a database error: the Django error, and the psycopg error that it wraps."""
    cause = error.__cause__
    return {
        "module": type(error).__module__,
        "error": type(error).__qualname__,
        "message": str(error),
        "cause": type(cause).__qualname__,
        "sqlstate": cause.sqlstate,
        "severity": cause.diag.severity_nonlocalized,
        "primary": cause.diag.message_primary,
        "detail": cause.diag.message_detail,
        "hint": cause.diag.message_hint,
        "position": cause.diag.statement_position,
    }


# name -> a statement that is not one SELECT
STATEMENTS = {
    "copy": "COPY (SELECT 1) TO STDOUT",
    "no-rows": "DO $$ BEGIN END $$",
    "empty": "",
    "two": "SELECT 1; SELECT 2",
    "second-fails": "SELECT 1; SELECT 1 / 0",
}


class _ServerVersion(Func):
    """An expression that reads the server version when Django compiles it."""

    output_field = IntegerField()

    def as_sql(self, compiler: Any, connection: Any, **extra: Any) -> tuple[str, list[Any]]:
        return "%s", [connection.pg_version]


class _OpensConnection(Func):
    """An expression that needs an open connection of Django when Django compiles it."""

    output_field = IntegerField()

    def as_sql(self, compiler: Any, connection: Any, **extra: Any) -> tuple[str, list[Any]]:
        connection.ensure_connection()
        return "%s", [7]


class _PassMiddleware(MiddlewareMixin):
    pass


def _probe_threads_route(api: BoltAPI) -> None:
    @api.get("/threads")
    async def threads():
        probes = LoopProbe.objects.using(ALIAS)
        return {
            "handler": threading.current_thread().name,
            "async_for": [probe.built_on async for probe in probes.order_by("id")],
            "aget": (await probes.aget(label="a")).built_on,
        }

    @api.get("/first-and-last-threads")
    async def first_and_last_threads():
        probes = LoopProbe.objects.using(ALIAS)
        return {
            "handler": threading.current_thread().name,
            "afirst": (await probes.afirst()).built_on,
            "alast": (await probes.order_by("label").alast()).built_on,
        }


def _lane_api() -> BoltAPI:
    """The route runs behind Django middleware, so its request has a lane."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_PassMiddleware])])
    _probe_threads_route(api)
    return api


async def _probe_threads_on_this_loop() -> list[str]:
    return [probe.built_on async for probe in LoopProbe.objects.using(ALIAS).order_by("id")]


def _returned_queryset_routes(api: BoltAPI) -> None:
    """Async handlers that return a QuerySet. Bolt reads the rows after the handler."""

    @api.get("/returned/struct")
    async def returned_struct() -> list[ProbeItem]:
        return _probes().order_by("id")

    @api.get("/returned/renamed")
    async def returned_renamed() -> list[ProbeRenamed]:
        return _probes().order_by("id")

    @api.get("/returned/nested")
    async def returned_nested() -> list[PostWithAuthor]:
        return _posts().order_by("id")

    @api.get("/returned/serializer")
    async def returned_serializer() -> list[ProbeSerializer]:
        return _probes().order_by("id")

    @api.get("/returned/computed")
    async def returned_computed() -> list[ProbeWithComputedField]:
        return _probes().order_by("id")

    @api.get("/returned/values")
    async def returned_values():
        return _probes().order_by("id").values("label", "built_on")

    @api.get("/returned/no-rows")
    async def returned_no_rows() -> list[ProbeItem]:
        return _probes().filter(label="no such label")

    @api.get("/returned/unknown-field")
    async def returned_unknown_field() -> list[PostWithAuthor]:
        # ``LoopProbe`` has no ``title`` and no ``author``.
        return _probes()


RETURNED = ("struct", "renamed", "nested", "serializer", "computed", "values", "no-rows", "unknown-field")


def _api() -> BoltAPI:
    api = BoltAPI()
    _probe_threads_route(api)
    _returned_queryset_routes(api)

    @api.get("/compiled/{name}")
    async def compiled(name: str):
        expression = {"server_version": _ServerVersion, "opens_connection": _OpensConnection}[name]
        probe = await LoopProbe.objects.using(ALIAS).annotate(value=expression()).aget(label="a")
        return {"handler": threading.current_thread().name, "built_on": probe.built_on, "value": probe.value}

    @api.get("/rows/{name}")
    async def rows(name: str):
        queryset, plain = QUERIES[name]
        return [plain(row) async for row in queryset()]

    @api.get("/get/{name}")
    async def get(name: str):
        queryset, arguments = GETS[name]
        try:
            post = await queryset().aget(**arguments)
        except Exception as error:
            return _get_outcome(error, None)
        return _get_outcome(None, post)

    @api.get("/get-backend-pid")
    async def get_backend_pid():
        return {"pid": (await _probe_with_backend_pid().aget(label="a")).pid}

    @api.get("/first/{name}")
    async def first(name: str):
        return {"outcome": await _one_row_outcome_async(ONE_ROW_QUERIES[name]().afirst)}

    @api.get("/last/{name}")
    async def last(name: str):
        return {"outcome": await _one_row_outcome_async(ONE_ROW_QUERIES[name]().alast)}

    @api.get("/exists/{name}")
    async def exists(name: str):
        return {"outcome": await _one_row_outcome_async(ONE_ROW_QUERIES[name]().aexists)}

    @api.get("/cached")
    async def cached():
        posts = _posts().filter(published=True).order_by("id")
        rows = [post async for post in posts]
        # The QuerySet has its rows now. Django answers ``exists`` and ``first`` from the rows, with no query.
        # ``last`` reverses the QuerySet, which gives a new QuerySet and a new query.
        last = await posts.alast()
        probes = _probes().filter(label="b")
        assert [probe.label async for probe in probes] == ["b"]
        await _probes().filter(label="b").adelete()
        return {
            # The row is gone from the table, and the QuerySet still has it.
            "exists": await probes.aexists(),
            "first_is_cached": await posts.afirst() is rows[0],
            "last_is_cached": last is rows[-1],
            "last_is_the_last_row": last.pk == rows[-1].pk,
        }

    @api.get("/query-log")
    async def query_log():
        names = [name async for name in _ada()]
        # The connection wrapper of this thread has the log, as ``connection.queries`` in a Django view.
        queries = connections[ALIAS].queries
        return {
            "handler": threading.current_thread().name,
            "names": names,
            "sql": queries[-1]["sql"],
            "has_time": float(queries[-1]["time"]) >= 0,
        }

    @api.get("/wrapped")
    async def wrapped():
        seen = []

        def spy(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
            seen.append(
                {
                    "sql": sql,
                    "params": list(params),
                    "many": many,
                    "alias": context["connection"].alias,
                    "cursor": type(context["cursor"]).__name__,
                }
            )
            return execute(sql, params, many, context)

        with connections[ALIAS].execute_wrapper(spy):
            names = [name async for name in _ada()]
        return {"names": names, "seen": seen}

    @api.get("/skipped")
    async def skipped():
        try:
            with connections[ALIAS].execute_wrapper(_skip_the_query):
                return [name async for name in _ada()]
        except Exception as error:
            return {"module": type(error).__module__, "error": type(error).__qualname__}

    @api.get("/blocked")
    async def blocked():
        def blocker(*args: Any) -> Any:
            raise RuntimeError("No queries in this block.")

        try:
            with connections[ALIAS].execute_wrapper(blocker):
                return [name async for name in _ada()]
        except RuntimeError as error:
            return {"error": type(error).__qualname__, "message": str(error)}

    @api.get("/twice")
    async def twice():
        posts = _posts().order_by("id")
        first = [post async for post in posts]
        second = [post async for post in posts]
        return {"rows": len(first), "same_objects": all(a is b for a, b in zip(first, second, strict=True))}

    @api.get("/cancelled-query")
    async def cancelled_query():
        # Open the one connection of the loop.
        before = await _backend_pid()
        try:
            await asyncio.wait_for(_backend_pid(nap=30), timeout=0.05)
        except TimeoutError:
            # With one connection for the loop, this read needs the connection of the cancelled query.
            following = await asyncio.wait_for(_backend_pid(), timeout=5)
            return {"same_connection": following == before}
        return {"same_connection": None}

    @api.get("/cancelled-waiter")
    async def cancelled_waiter():
        # Open the one connection of the loop, so the first query below takes it at once.
        await _backend_pid()

        async def first_query() -> int:
            pid = await _backend_pid()
            # The connection is free again and the second query has its turn, but did not run yet.
            second.cancel()
            return pid

        first = asyncio.ensure_future(first_query())
        second = asyncio.ensure_future(_backend_pid())
        third = asyncio.ensure_future(_backend_pid())
        pids = await asyncio.wait_for(asyncio.gather(first, third), timeout=5)
        return {"same_connection": pids[0] == pids[1], "second_cancelled": second.cancelled()}

    @api.get("/connect-error")
    async def connect_error():
        try:
            return [name async for name in Author.objects.using(ALIAS).values_list("name", flat=True)]
        except Exception as error:
            return {"module": type(error).__module__, "error": type(error).__qualname__}

    @api.get("/tags")
    async def tags():
        posts = _posts().prefetch_related("tags").order_by("id")
        # ``post.tags.all()`` reads the prefetched rows. Without them it would query on the event loop.
        return [[post.title, sorted(tag.name for tag in post.tags.all())] async for post in posts]

    @api.get("/division-by-zero")
    async def division_by_zero():
        try:
            return [author.id async for author in Author.objects.using(ALIAS).extra(where=["1 / (id - id) = 1"])]
        except Exception as error:
            return {"module": type(error).__module__, "error": type(error).__qualname__}

    @api.get("/large-result/{size}")
    async def large_result(size: str):
        return _digest([value async for value in _large_values(size)])

    @api.get("/large-statement")
    async def large_statement():
        text = "x" * LARGE_STATEMENT
        probe = await _probes().annotate(length=RawSQL("length(%s)", [text])).aget(label="a")
        return {"length": probe.length}

    @api.get("/error/{name}")
    async def error(name: str):
        try:
            return [probe.label async for probe in ERROR_QUERIES[name]()]
        except Exception as error:
            return _error_outcome(error)

    @api.get("/notice")
    async def notice():
        probe = await asyncio.wait_for(_probes().annotate(value=RawSQL("bolt_notice()", [])).aget(label="a"), 10)
        return {"value": probe.value}

    @api.get("/statement/{name}")
    async def statement(name: str):
        try:
            return {"rows": await async_orm.fetch(ALIAS, STATEMENTS[name], None)}
        except Exception as error:
            return {"module": type(error).__module__, "error": type(error).__qualname__, "message": str(error)}

    @api.get("/slow-backend-pid")
    async def slow_backend_pid():
        return {"pid": await asyncio.wait_for(_backend_pid(nap=1), 10)}

    @api.get("/two-at-once")
    async def two_at_once():
        return list(await asyncio.gather(_backend_pid(), _backend_pid()))

    @api.get("/many-at-once/{count}")
    async def many_at_once(count: int):
        return {"connections": len(set(await asyncio.gather(*(_backend_pid() for _ in range(count)))))}

    @api.get("/wait-too-long")
    async def wait_too_long():
        slow = LoopProbe.objects.using(ALIAS).annotate(nap=RawSQL("(SELECT 1 FROM pg_sleep(1))", []))
        held, waited = await asyncio.gather(slow.aget(label="a"), _backend_pid(), return_exceptions=True)
        return {
            "held": held.label,
            "module": type(waited).__module__,
            "error": type(waited).__qualname__,
            "message": str(waited),
        }

    @api.get("/session")
    async def session():
        probe = await (
            LoopProbe.objects.using(ALIAS)
            .annotate(statement_timeout=RawSQL("current_setting('statement_timeout')", []))
            .aget(label="a")
        )
        published = [flag async for flag in _posts().values_list("published", flat=True).order_by("id")]
        return {"statement_timeout": probe.statement_timeout, "published": published, "built_on": probe.built_on}

    @api.get("/authors")
    async def authors():
        return [name async for name in Author.objects.using(ALIAS).values_list("name", flat=True).order_by("id")]

    return api


def _as_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def test_async_for_and_aget_build_their_rows_on_the_thread_of_the_handler(on_loop):
    """The query runs from the event loop, so Django builds the rows on the thread of the handler."""
    with TestClient(_api()) as client:
        threads = client.get("/threads").json()

    handler = threads["handler"]
    assert not handler.startswith(ORM_THREAD)
    assert threads == {"handler": handler, "async_for": [handler, handler], "aget": handler}


def test_the_reads_use_the_orm_pool_when_the_setting_is_off(loop_db):
    with TestClient(_api()) as client:
        threads = client.get("/threads").json()

    assert _built_on_orm_threads(threads), threads
    assert not threads["handler"].startswith(ORM_THREAD)


def _thread_names(rows: Any) -> list[str]:
    """The thread names in the rows of a response, and the rows with no thread name."""
    names = []
    if isinstance(rows, list):
        for row in rows:
            for key in ("built_on", "builtOn"):
                if key in row:
                    names.append(row.pop(key))
    return names


@pytest.mark.parametrize("name", RETURNED)
def test_a_returned_queryset_gives_the_response_of_the_thread_path(loop_db, name):
    # The route with the unknown field gives a 500. The client must not raise the error of the server.
    with TestClient(_api(), raise_server_exceptions=False) as client:
        threads = client.get(f"/returned/{name}")
    with (
        override_settings(BOLT_ASYNC_ORM_ON_LOOP=True),
        TestClient(_api(), raise_server_exceptions=False) as client,
    ):
        loop = client.get(f"/returned/{name}")
    concurrency.check_pool_environment()

    assert loop.status_code == threads.status_code == (500 if name == "unknown-field" else 200)
    thread_rows, loop_rows = threads.json(), loop.json()
    built_by_threads, built_by_loop = _thread_names(thread_rows), _thread_names(loop_rows)
    assert loop_rows == thread_rows
    assert all(name.startswith(ORM_THREAD) for name in built_by_threads), built_by_threads
    assert not any(name.startswith(ORM_THREAD) for name in built_by_loop), built_by_loop
    if name in ("struct", "renamed", "serializer", "computed", "values"):
        assert len(built_by_loop) == 2


def _built_on_orm_threads(threads: dict[str, Any]) -> bool:
    return all(name.startswith(ORM_THREAD) for name in (*threads["async_for"], threads["aget"]))


def test_a_route_with_django_middleware_reads_on_its_lane(on_loop):
    """The lane of the request keeps state for the request, such as the schema of a tenant."""
    with TestClient(_lane_api()) as client:
        threads = client.get("/threads").json()

    lane = threads["aget"]
    assert lane != threads["handler"], threads
    assert not lane.startswith(ORM_THREAD), threads
    assert threads["async_for"] == [lane, lane]


def test_a_database_with_a_custom_backend_reads_on_the_orm_pool(on_loop, monkeypatch):
    """A custom backend can change how a connection opens. A loop connection would not have that change."""
    custom = {**connections.settings[ALIAS], "ENGINE": "tests.integration.custom_postgresql_backend"}
    monkeypatch.setitem(connections.settings, ALIAS, custom)
    # The next read of the alias makes a wrapper of the custom class.
    connections[ALIAS].close()
    del connections[ALIAS]
    try:
        with TestClient(_api()) as client:
            threads = client.get("/threads").json()
    finally:
        connections[ALIAS].close()

    assert _built_on_orm_threads(threads), threads


def _ada() -> Any:
    return Author.objects.using(ALIAS).filter(name="Ada").values_list("name", flat=True)


def test_debug_logs_a_read_of_the_loop_as_django_logs_a_query(on_loop, caplog):
    """With ``DEBUG``, ``connection.queries`` and the ``django.db.backends`` logger get each query."""
    with CaptureQueriesContext(connections[ALIAS]) as captured:
        assert list(_ada()) == ["Ada"]
    expected_sql = captured[-1]["sql"]

    caplog.set_level(logging.DEBUG, logger="django.db.backends")
    with override_settings(DEBUG=True), TestClient(_api()) as client:
        # The first read of a loop reads the state of a Django connection, with a query of its own.
        assert client.get("/authors").status_code == 200
        caplog.clear()
        logged = client.get("/query-log").json()

    handler = logged["handler"]
    assert not handler.startswith(ORM_THREAD)
    assert logged == {"handler": handler, "names": ["Ada"], "sql": expected_sql, "has_time": True}
    records = [record for record in caplog.records if record.name == "django.db.backends"]
    assert [(record.sql, record.params, record.alias) for record in records] == [(expected_sql, ("Ada",), ALIAS)]
    assert records[0].duration > 0


def test_a_wrapper_of_execute_wrapper_gets_each_read(on_loop):
    with TestClient(_api()) as client:
        wrapped = client.get("/wrapped").json()

    assert wrapped["names"] == ["Ada"]
    (seen,) = wrapped["seen"]
    assert seen["sql"].endswith('WHERE "django_bolt_author"."name" = %s')
    assert seen == {"sql": seen["sql"], "params": ["Ada"], "many": False, "alias": ALIAS, "cursor": "CursorWrapper"}


def _skip_the_query(execute: Any, sql: str, params: Any, many: bool, context: Any) -> None:
    """A wrapper that does not call ``execute``."""


def test_a_wrapper_that_sends_nothing_gives_the_error_of_django(on_loop):
    with pytest.raises(Exception) as sync_error, connections[ALIAS].execute_wrapper(_skip_the_query):  # noqa: PT011
        list(_ada())
    expected = {"module": type(sync_error.value).__module__, "error": type(sync_error.value).__qualname__}

    with TestClient(_api()) as client:
        skipped = client.get("/skipped").json()
        after = client.get("/authors")

    assert skipped == expected == {"module": "django.db.utils", "error": "ProgrammingError"}
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_wrapper_that_raises_stops_the_read(on_loop):
    with TestClient(_api()) as client:
        blocked = client.get("/blocked").json()
        after = client.get("/authors")

    assert blocked == {"error": "RuntimeError", "message": "No queries in this block."}
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_wrapper_that_a_connection_created_receiver_installs_gets_each_read(on_loop):
    """A tool installs its wrapper on each new connection of Django. The loop connections copy that state."""
    statements = []

    def record(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        statements.append(sql)
        return execute(sql, params, many, context)

    def install(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias == ALIAS:
            connection.execute_wrappers.append(record)

    connection_created.connect(install)
    try:
        with TestClient(_api()) as client:
            assert client.get("/authors").status_code == 200
            statements.clear()
            assert client.get("/get/one").status_code == 200
    finally:
        connection_created.disconnect(install)

    assert len(statements) == 1, statements
    assert statements[0].startswith('SELECT "django_bolt_blogpost"."id"')


def test_a_read_on_another_event_loop_uses_the_orm_pool(on_loop):
    """A pool of loop connections lives as long as its loop. Only the loop of a worker lives that long."""
    with TestClient(_api()):
        built_on = asyncio.run(_probe_threads_on_this_loop())

    assert len(built_on) == 2
    assert all(name.startswith(ORM_THREAD) for name in built_on), built_on


def test_an_expression_that_reads_the_server_version_compiles_on_the_event_loop(on_loop):
    """Each request has a connection wrapper of its own on the event loop thread, so each read gives it the version."""
    with TestClient(_api()) as client:
        results = [client.get("/compiled/server_version").json() for _ in range(2)]

    version = connections[ALIAS].pg_version
    for result in results:
        assert result == {"handler": result["handler"], "built_on": result["handler"], "value": version}


def test_an_expression_that_needs_a_connection_to_compile_uses_the_orm_pool(on_loop):
    with TestClient(_api()) as client:
        response = client.get("/compiled/opens_connection")

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["value"] == 7
    assert result["built_on"].startswith(ORM_THREAD), result


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_async_for_gives_the_rows_of_the_sync_orm(on_loop, name):
    queryset, plain = QUERIES[name]
    expected = _as_json([plain(row) for row in queryset()])

    with TestClient(_api()) as client:
        response = client.get(f"/rows/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == expected
    if name not in ("empty_in", "none"):
        assert expected, "the query of this case must select rows"


@pytest.mark.parametrize("name", sorted(GETS))
def test_aget_gives_the_object_or_the_error_of_get(on_loop, name):
    queryset, arguments = GETS[name]
    try:
        expected = _get_outcome(None, queryset().get(**arguments))
    except Exception as error:
        expected = _get_outcome(error, None)

    with TestClient(_api()) as client:
        response = client.get(f"/get/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def _last_statement(db: LoopDb, pid: int) -> str:
    """The last statement that the server got on the connection ``pid``."""
    with psycopg.connect(**db.params, autocommit=True) as admin:
        (statement,) = admin.execute("SELECT query FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
    return statement


def test_aget_sends_the_sql_of_get(on_loop):
    """``get`` drops the order of the QuerySet and limits the rows. The database must get the same statement."""
    expected = _last_statement(on_loop, _probe_with_backend_pid().get(label="a").pid)

    with TestClient(_api()) as client:
        sent = _last_statement(on_loop, client.get("/get-backend-pid").json()["pid"])

    assert sent == expected
    assert "ORDER BY" not in sent
    assert "LIMIT 21" in sent


@pytest.mark.parametrize("name", sorted(ONE_ROW_QUERIES))
@pytest.mark.parametrize("method", ["first", "last", "exists"])
def test_afirst_alast_and_aexists_give_the_result_or_the_error_of_the_sync_call(on_loop, method, name):
    expected = {"outcome": _one_row_outcome(getattr(ONE_ROW_QUERIES[name](), method))}

    with TestClient(_api()) as client:
        response = client.get(f"/{method}/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_aexists_sends_the_sql_of_exists(on_loop):
    """``exists`` selects no column and limits the rows to one. The database must get the same statement."""
    assert ONE_ROW_QUERIES["ordered"]().exists()
    expected = _last_statement(on_loop, connections[ALIAS].connection.info.backend_pid)
    connections[ALIAS].close()

    with TestClient(_api()) as client:
        assert client.get("/exists/ordered").json() == {"outcome": True}
        with psycopg.connect(**on_loop.params, autocommit=True) as admin:
            (sent,) = admin.execute(
                "SELECT query FROM pg_stat_activity WHERE application_name = %s ORDER BY query_start DESC LIMIT 1",
                (on_loop.application_name,),
            ).fetchone()

    assert sent == expected
    assert "LIMIT 1" in sent


def test_afirst_and_alast_build_their_row_on_the_thread_of_the_handler(on_loop):
    with TestClient(_api()) as client:
        threads = client.get("/first-and-last-threads").json()

    handler = threads["handler"]
    assert not handler.startswith(ORM_THREAD)
    assert threads == {"handler": handler, "afirst": handler, "alast": handler}


def test_aexists_opens_no_django_connection(on_loop):
    """A read on the thread path opens a connection of Django on an ORM thread. A read on the loop opens none."""
    opened = []

    def count(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias == ALIAS:
            opened.append(threading.current_thread().name)

    with TestClient(_api()) as client:
        # The first read of a loop reads the state of a Django connection, and closes that connection.
        assert client.get("/authors").status_code == 200
        connection_created.connect(count)
        try:
            response = client.get("/exists/ordered")
        finally:
            connection_created.disconnect(count)

    assert response.json() == {"outcome": True}
    assert opened == []


def test_a_queryset_that_has_its_rows_answers_from_them(on_loop):
    with TestClient(_api()) as client:
        response = client.get("/cached")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "exists": True,
        "first_is_cached": True,
        "last_is_cached": False,
        "last_is_the_last_row": True,
    }


def test_a_second_async_for_reads_the_cache_of_the_queryset(on_loop):
    with TestClient(_api()) as client:
        response = client.get("/twice")

    assert response.json() == {"rows": 3, "same_objects": True}


def test_a_queryset_with_prefetch_related_loads_its_relations(on_loop):
    """The prefetch queries are sync code, so such a QuerySet stays on the thread path."""
    with TestClient(_api()) as client:
        response = client.get("/tags")

    assert response.status_code == 200, response.text
    assert response.json() == [["Loops", ["django", "rust"]], ["Lanes", ["rust"]], ["Pools", ["django", "rust"]]]


def test_a_query_error_is_the_django_error_and_the_next_query_works(on_loop):
    with TestClient(_api()) as client:
        error = client.get("/division-by-zero").json()
        after = client.get("/authors")

    assert error == {"module": "django.db.utils", "error": "DataError"}
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_two_queries_of_one_request_run_on_two_connections(on_loop):
    with TestClient(_api()) as client:
        first, second = client.get("/two-at-once").json()

    assert first != second


def test_reads_that_start_together_open_one_django_connection(on_loop):
    """The first read of a loop copies the state of a Django connection. One copy serves each loop connection."""
    opened = []

    def count(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias == ALIAS:
            opened.append(threading.current_thread().name)

    connections[ALIAS].close()
    connection_created.connect(count)
    try:
        with TestClient(_api()) as client:
            first, second = client.get("/two-at-once").json()
    finally:
        connection_created.disconnect(count)

    assert first != second
    assert len(opened) == 1, opened


def test_a_loop_opens_ten_connections_by_default(on_loop):
    """Each read at the same time gets a connection, up to the limit. The other reads wait."""
    with TestClient(_api()) as client:
        response = client.get("/many-at-once/12")

    assert response.status_code == 200, response.text
    assert response.json() == {"connections": 10}


def test_a_read_that_waits_for_the_acquire_time_fails_with_the_django_error(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_ACQUIRE_SECONDS", "0.2")
    with TestClient(_api()) as client:
        outcome = client.get("/wait-too-long").json()
        after = client.get("/authors")

    assert outcome == {
        "held": "a",
        "module": "django.db.utils",
        "error": "OperationalError",
        "message": (
            f"No loop connection to the database {ALIAS!r} came free in 0.2 seconds. "
            "Each of the 1 connections of DJANGO_BOLT_LOOP_CONNECTIONS was in use."
        ),
    }
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_connection_older_than_the_lifetime_closes(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_LIFETIME_SECONDS", "0.3")
    with TestClient(_api()) as client:
        first = client.get("/get-backend-pid").json()["pid"]
        again = client.get("/get-backend-pid").json()["pid"]
        time.sleep(0.4)
        later = client.get("/get-backend-pid").json()["pid"]
        connections[ALIAS].close()
        on_loop.wait_for_backends(1)

    assert again == first
    assert later != first


def test_a_query_waits_when_each_connection_is_in_use(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        first, second = client.get("/two-at-once").json()

    assert first == second


def test_the_connection_of_a_cancelled_query_serves_the_next_query(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get("/cancelled-query")

    assert response.status_code == 200, response.text
    assert response.json() == {"same_connection": True}


def test_a_cancelled_waiter_gives_its_turn_to_the_next_waiter(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get("/cancelled-waiter")

    assert response.status_code == 200, response.text
    assert response.json() == {"same_connection": True, "second_cancelled": True}


def _allow_connections(db: LoopDb, allow: bool) -> None:
    with psycopg.connect(**{**db.params, "dbname": "postgres"}, autocommit=True) as admin:
        admin.execute(
            psycopg.sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS {}").format(
                psycopg.sql.Identifier(db.params["dbname"]), psycopg.sql.SQL("true" if allow else "false")
            )
        )


def test_a_failed_connection_is_the_django_error_and_a_later_read_works(on_loop):
    with TestClient(_api()) as client:
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
        connections[ALIAS].close()
        _allow_connections(on_loop, False)
        try:
            # The database of the test refuses the connection of this helper too.
            terminate_backends({**on_loop.params, "dbname": "postgres"}, on_loop.application_name)
            refused = client.get("/connect-error").json()
        finally:
            _allow_connections(on_loop, True)
        after = client.get("/authors")

    assert refused == {"module": "django.db.utils", "error": "OperationalError"}
    assert after.json() == ["Ada", "Bob", "Cy"]


class _YesNo(psycopg.adapt.Loader):
    def load(self, data: Any) -> str:
        return "yes" if bytes(data) == b"t" else "no"


def test_a_loop_connection_has_the_session_settings_and_the_adapters_of_a_django_connection(on_loop):
    """A ``connection_created`` receiver prepares each connection of Django. The loop connections must match."""

    def prepare(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias != ALIAS:
            return
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 4321")
        connection.connection.adapters.register_loader("bool", _YesNo)

    connection_created.connect(prepare)
    try:
        with TestClient(_api()) as client:
            session = client.get("/session").json()
            handler = client.get("/threads").json()["handler"]
    finally:
        connection_created.disconnect(prepare)
        # The connection of the test opened before the receiver. The next read opens a new one.
        connections[ALIAS].close()

    assert session == {"statement_timeout": "4321ms", "published": ["yes", "no", "yes"], "built_on": handler}


def test_a_handler_reads_the_uncommitted_rows_of_the_test(on_loop):
    """The test client gives the handlers the connection of an open transaction. Only that connection has its rows."""
    with transaction.atomic(using=ALIAS):
        Author.objects.using(ALIAS).create(name="Uncommitted", email="new@example.com")
        with TestClient(_api()) as client:
            response = client.get("/authors")
        transaction.set_rollback(True, using=ALIAS)

    assert response.json() == ["Ada", "Bob", "Cy", "Uncommitted"]


class _AuthModelsOnTheTestDatabase:
    """A database router: the user model is in the PostgreSQL database of the test."""

    def db_for_read(self, model: Any, **hints: Any) -> str | None:
        return ALIAS if model._meta.app_label in ("auth", "contenttypes") else None

    db_for_write = db_for_read


class _FilteredUserManager(models.Manager):
    """A manager with its own QuerySet. The user loader must ask it for each user."""

    def get_queryset(self) -> Any:
        return super().get_queryset().filter(is_staff=False)


@pytest.fixture
def users(loop_db):
    """The tables of the user model in the test database, with an active and an inactive user."""
    user_model = get_user_model()
    with connections[ALIAS].schema_editor() as editor:
        for model in (ContentType, Permission, Group, user_model):
            editor.create_model(model)
    with override_settings(DATABASE_ROUTERS=[_AuthModelsOnTheTestDatabase()]):
        yield {
            "active": user_model.objects.create(username="ada"),
            "inactive": user_model.objects.create(username="bob", is_active=False),
        }


def _auth_api() -> BoltAPI:
    api = BoltAPI()

    @api.get("/me", auth=[JWTAuthentication()], guards=[IsAuthenticated()])
    async def me(request: Request):
        user = await request.auser()
        return {"username": getattr(user, "username", None), "is_user": isinstance(user, get_user_model())}

    @api.get("/warm-up")
    async def warm_up():
        return [name async for name in Author.objects.using(ALIAS).values_list("name", flat=True)]

    return api


def _me(client: TestClient, user: Any) -> Any:
    response = client.get("/me", headers={"Authorization": f"Bearer {create_jwt_for_user(user)}"})
    assert response.status_code == 200, response.text
    return response.json()


def test_auser_loads_the_user_with_no_django_connection(users):
    """The user load of the thread path opens a connection of Django on an ORM thread. The loop opens none."""
    opened = []

    def count(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias == ALIAS:
            opened.append(threading.current_thread().name)

    connections[ALIAS].close()
    with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True), TestClient(_auth_api()) as client:
        # The first read of a loop reads the state of a Django connection, and closes that connection.
        assert client.get("/warm-up").status_code == 200
        connection_created.connect(count)
        try:
            me = _me(client, users["active"])
        finally:
            connection_created.disconnect(count)
    concurrency.check_pool_environment()

    assert me == {"username": "ada", "is_user": True}
    assert opened == []


@pytest.mark.parametrize("case", ["active", "inactive", "deleted"])
def test_auser_gives_the_user_of_the_thread_path(users, case, capfd):
    # The token of the deleted case names a user that is not in the table.
    token_user = get_user_model()(pk=10_000, username="gone") if case == "deleted" else users[case]

    with TestClient(_auth_api()) as client:
        threads = _me(client, token_user)
    with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True), TestClient(_auth_api()) as client:
        loop = _me(client, token_user)
    concurrency.check_pool_environment()

    assert loop == threads
    assert loop == ({"username": "ada", "is_user": True} if case == "active" else {"username": None, "is_user": False})
    # The backend reports an error of the load and gives no user. A missing user is not an error.
    assert "Error loading user" not in capfd.readouterr().err


def test_auser_asks_a_manager_that_builds_its_own_queryset(users, monkeypatch):
    """Bolt does not compile the statement of such a manager one time, so its load stays on the ORM pool."""
    user_model = get_user_model()
    manager = _FilteredUserManager()
    manager.model = user_model
    # ``Model._default_manager`` reads this value of the model options.
    monkeypatch.setitem(user_model._meta.__dict__, "default_manager", manager)
    staff = user_model.objects.create(username="staff", is_staff=True)

    with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True), TestClient(_auth_api()) as client:
        hidden = _me(client, staff)
        shown = _me(client, users["active"])
    concurrency.check_pool_environment()

    assert hidden == {"username": None, "is_user": False}
    assert shown == {"username": "ada", "is_user": True}


def test_a_dead_connection_fails_no_request(on_loop):
    """A database restart ends the idle connections of the loop. The next read opens a new one."""
    with TestClient(_api()) as client:
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
        connections[ALIAS].close()
        assert terminate_backends(on_loop.params, on_loop.application_name) >= 1

        response = client.get("/authors")

    assert response.status_code == 200, response.text
    assert response.json() == ["Ada", "Bob", "Cy"]


def test_an_idle_connection_closes_and_the_next_read_opens_one(on_loop, monkeypatch):
    """A burst must not leave its connections open for the life of the server."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_IDLE_SECONDS", "0.2")
    with TestClient(_api()) as client:
        assert client.get("/two-at-once").status_code == 200
        connections[ALIAS].close()

        on_loop.wait_for_backends(0)
        after = client.get("/authors")

    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_closed_client_leaves_no_connection_open(on_loop):
    with TestClient(_api()) as client:
        assert client.get("/authors").status_code == 200
    connections[ALIAS].close()

    on_loop.wait_for_backends(0)


def _loop_log(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "django_bolt.async_orm"]


def test_a_log_line_tells_how_a_loop_sends_its_queries(on_loop, transport, caplog):
    caplog.set_level(logging.INFO, logger="django_bolt.async_orm")
    with TestClient(_api()) as client:
        assert client.get("/authors").status_code == 200
        assert client.get("/authors").status_code == 200

    expected = {
        RUST: f"The loop connections to the database {ALIAS!r} send their queries from Rust.",
        PSYCOPG: f"The loop connections to the database {ALIAS!r} use psycopg: server_side_binding is on.",
    }
    assert _loop_log(caplog) == [expected[transport]]


def _check_large_result(size: str) -> None:
    expected = _digest(list(_large_values(size)))
    assert expected["rows"] == 27

    with TestClient(_api()) as client:
        response = client.get(f"/large-result/{size}")
        after = client.get("/authors")

    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert after.json() == ["Ada", "Bob", "Cy"]


@pytest.mark.parametrize("size", sorted(RESULT_SIZES))
def test_a_result_larger_than_the_read_buffer_has_each_row(on_loop, size):
    """The socket can hold more data than one read takes. No event of the socket then tells of the rest."""
    _check_large_result(size)


def test_a_result_that_comes_in_many_parts_has_each_row(on_loop_through_a_slow_link):
    _check_large_result("many-buffers")


def test_a_statement_larger_than_the_socket_buffer_reaches_the_database(on_loop_through_a_slow_link):
    with TestClient(_api()) as client:
        response = client.get("/large-statement")
        after = client.get("/authors")

    assert response.status_code == 200, response.text
    assert response.json() == {"length": LARGE_STATEMENT}
    assert after.json() == ["Ada", "Bob", "Cy"]


@pytest.mark.parametrize("name", sorted(ERROR_QUERIES))
def test_a_database_error_has_the_fields_of_the_error_of_the_sync_orm(on_loop, name):
    with pytest.raises(Exception) as sync_error:  # noqa: PT011
        list(ERROR_QUERIES[name]())
    expected = _error_outcome(sync_error.value)
    assert expected["module"] == "django.db.utils"
    assert expected["sqlstate"] == {"cast": "22P02", "syntax": "42601", "custom": "22B99"}[name]
    assert expected["error"] == {"cast": "DataError", "syntax": "ProgrammingError", "custom": "DataError"}[name]

    with TestClient(_api()) as client:
        response = client.get(f"/error/{name}")
        after = client.get("/authors")

    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_notice_of_the_server_does_not_stop_the_query(on_loop):
    with TestClient(_api()) as client:
        response = client.get("/notice")

    assert response.status_code == 200, response.text
    assert response.json() == {"value": 7}


PROGRAMMING_ERROR = {"module": "django.db.utils", "error": "ProgrammingError"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("copy", {**PROGRAMMING_ERROR, "message": "COPY cannot be used with this method; use copy() instead"}),
        ("no-rows", PROGRAMMING_ERROR),
        ("empty", PROGRAMMING_ERROR),
        # The rows of the first statement, as a psycopg cursor gives them.
        ("two", {"rows": [[1]]}),
        ("second-fails", {"module": "django.db.utils", "error": "DataError"}),
    ],
)
def test_a_statement_that_is_not_one_select_gives_the_outcome_of_psycopg(on_loop, monkeypatch, name, expected):
    # With one connection, the read after the statement needs the connection of the statement, or a new one.
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get(f"/statement/{name}")
        after = client.get("/authors")

    assert response.status_code == 200, response.text
    outcome = response.json()
    # psycopg and Rust name the result of a statement with no rows in different words.
    assert {key: outcome[key] for key in expected} == expected
    assert after.json() == ["Ada", "Bob", "Cy"]


def _terminate_backend(db: LoopDb, pid: int) -> None:
    with psycopg.connect(**db.params, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))


def test_a_connection_that_dies_during_a_query_fails_no_request(on_loop):
    """The pool sends the query again on a new connection: see ``_Pool.fetch``."""
    with TestClient(_api()) as client:
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
        connections[ALIAS].close()
        (before,) = backend_pids(on_loop.params, on_loop.application_name)

        # The query takes 1 second. The server ends its connection after 0.3 seconds.
        killer = threading.Timer(0.3, _terminate_backend, args=(on_loop, before))
        killer.start()
        try:
            response = client.get("/slow-backend-pid")
        finally:
            killer.join()

    assert response.status_code == 200, response.text
    assert response.json()["pid"] != before
