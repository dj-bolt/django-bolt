"""The async ORM reads on the event loop, against a real PostgreSQL database.

With ``BOLT_ASYNC_ORM_ON_LOOP``, ``async for`` over a QuerySet and ``aget()``
send their query from the event loop of the worker. No thread runs the query.
Django builds the SQL and builds the rows, so each result must equal the
result of the sync ORM.

Each test adds one database to Django: a new PostgreSQL database with the
tables of the test models. ``default`` stays the SQLite database.

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
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connections, models, transaction
from django.db.backends.signals import connection_created
from django.db.backends.utils import CursorWrapper
from django.db.models import Avg, Count, Func, IntegerField, Max, Min, QuerySet, Sum
from django.db.models.expressions import RawSQL
from django.db.models.query import ModelIterable
from django.db.models.signals import post_init
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, LimitOffsetPagination, Request, async_orm, concurrency, paginate
from django_bolt.auth import IsAuthenticated, JWTAuthentication
from django_bolt.auth.jwt_utils import create_jwt_for_user
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.serializers import Serializer, computed_field, field_validator
from django_bolt.testing import TestClient
from tests.test_models import Author, BlogPost, Tag
from tests.thread_pools import shutdown_closing_connections

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


class ProbeWithQueryingValidator(Serializer):
    """A Serializer whose validator reads the database, as one that checks a value against a table."""

    label: str
    built_on: str

    @field_validator("label")
    def label_is_known(cls, value: str) -> str:  # noqa: N805
        assert Author.objects.using(ALIAS).filter(name="Ada").exists()
        return value


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
    # The process has one ORM pool, and the first use sets its size from the databases: 4 threads with this
    # database, 1 thread with only SQLite. The test thus gets a pool of its own. The pool of the other tests
    # comes back after the test.
    monkeypatch.setattr(concurrency, "_orm_executor", None)
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
            try:
                orm_pool = concurrency._orm_executor
                if orm_pool is not None:
                    shutdown_closing_connections(orm_pool)
            finally:
                connections[ALIAS].close()
                # With ``OPTIONS["pool"]``, Django keeps a connection pool for the alias.
                connections[ALIAS].close_pool()
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

    def __init__(self, params: dict[str, Any], connect_seconds: float = 0.0) -> None:
        # Each new connection waits this long before it reaches the database.
        self.connect_seconds = connect_seconds
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
        # With a list here, the relay records each part that it moves: the side that sent it, and the bytes.
        self.traffic: list[tuple[str, bytes]] | None = None
        self.threads = [threading.Thread(target=self._accept, daemon=True)]
        self.threads[0].start()

    def _accept(self) -> None:
        while not self.closed:
            try:
                client, _address = self.listener.accept()
            except TimeoutError:
                continue
            client.settimeout(None)
            time.sleep(self.connect_seconds)
            family = socket.AF_UNIX if isinstance(self.target, str) else socket.AF_INET
            server = socket.socket(family, socket.SOCK_STREAM)
            server.connect(self.target)
            pumps = [
                threading.Thread(target=self._pump, args=(client, server, "client"), daemon=True),
                threading.Thread(target=self._pump, args=(server, client, "server"), daemon=True),
            ]
            with self.lock:
                self.sockets += [client, server]
                self.threads += pumps
            for pump in pumps:
                pump.start()

    def _pump(self, source: socket.socket, sink: socket.socket, side: str) -> None:
        with contextlib.suppress(OSError):
            while data := source.recv(self.CHUNK):
                if self.traffic is not None:
                    with self.lock:
                        self.traffic.append((side, data))
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


@contextlib.contextmanager
def _setting_on(postgres_database, monkeypatch, django_db_blocker, options: dict[str, Any]) -> Any:
    """Add the database, with ``options`` in its ``OPTIONS``, and turn the setting on for the servers of the test."""
    for database in _add_database(postgres_database, monkeypatch, django_db_blocker, options):
        try:
            with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True):
                yield database
        finally:
            # A server that starts with the setting off gives Django its own methods back.
            concurrency.check_pool_environment()


@pytest.fixture
def on_loop(request, postgres_database, monkeypatch, django_db_blocker):
    """The database of the test, with the setting on. An indirect parameter gives the ``OPTIONS`` of the database."""
    options = getattr(request, "param", {})
    with _setting_on(postgres_database, monkeypatch, django_db_blocker, options) as database:
        yield database


@pytest.fixture
def on_loop_through_a_slow_link(request, postgres_database, monkeypatch, django_db_blocker):
    """``on_loop``, with the database behind a ``_SlowLink``. An indirect parameter gives its connect time."""
    link = _SlowLink(postgres_database, getattr(request, "param", 0.0))
    behind = {**postgres_database, "host": "127.0.0.1", "port": str(link.port)}
    try:
        with _setting_on(behind, monkeypatch, django_db_blocker, {}) as database:
            yield database
    finally:
        link.close()


@pytest.fixture
def recorded_link(postgres_database, monkeypatch, django_db_blocker):
    """``on_loop``, with the database behind a ``_SlowLink`` that records its traffic. Gives the relay."""
    link = _SlowLink(postgres_database)
    link.traffic = []
    behind = {**postgres_database, "host": "127.0.0.1", "port": str(link.port)}
    try:
        with _setting_on(behind, monkeypatch, django_db_blocker, {}):
            yield link
    finally:
        link.close()


def _with_the_setting_off(api: Callable[[], BoltAPI], path: str) -> Any:
    """The response of ``path`` from a server that reads on the ORM thread pool."""
    with override_settings(BOLT_ASYNC_ORM_ON_LOOP=False), TestClient(api()) as client:
        return client.get(path).json()


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
    # Django selects ``id`` to order the rows, and removes it from each row.
    "distinct_with_another_order": (lambda: _posts().values_list("title").order_by("id").distinct(), list),
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
    "distinct_with_another_order": lambda: _posts().values_list("title").order_by("id").distinct(),
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


# Name -> the QuerySet, and the arguments of ``aggregate``.
AGGREGATES: dict[str, Callable[[], tuple[Any, tuple[Any, ...], dict[str, Any]]]] = {
    "count": lambda: (_posts(), (), {"posts": Count("id")}),
    "positional": lambda: (_posts(), (Count("id"),), {}),
    "several": lambda: (_posts(), (), {"first": Min("title"), "last": Max("title"), "ids": Sum("id")}),
    "average": lambda: (_posts(), (), {"mean": Avg("author_id")}),
    "filtered": lambda: (_posts().filter(published=True), (), {"posts": Count("id")}),
    "no_rows": lambda: (_posts().filter(title="No such post"), (), {"total": Sum("id"), "posts": Count("id")}),
    "no_rows_with_a_default": lambda: (_posts().filter(title="No such post"), (), {"total": Sum("id", default=7)}),
    # Django sends no statement for a QuerySet that can match nothing.
    "empty_in": lambda: (_posts().filter(id__in=[]), (), {"total": Sum("id", default=7), "posts": Count("id")}),
    "none": lambda: (_posts().none(), (), {"posts": Count("id")}),
    # Django puts each of these QuerySets in a subquery.
    "sliced": lambda: (_posts().order_by("id")[:2], (), {"posts": Count("id")}),
    "distinct": lambda: (_posts().values("published").distinct(), (), {"kinds": Count("published")}),
    "over_an_annotation": lambda: (
        Author.objects.using(ALIAS).annotate(written=Count("posts")),
        (),
        {"most": Max("written")},
    ),
    "union": lambda: (
        _posts().filter(published=True).union(_posts().filter(published=False)),
        (),
        {"posts": Count("id")},
    ),
    "no_expression": lambda: (_posts(), (), {}),
    "prefetch": lambda: (_posts().prefetch_related("tags"), (), {"posts": Count("id")}),
    # Errors of Django, before the statement.
    "no_alias": lambda: (_posts(), (Count("id") + 1,), {}),
    "unknown_field": lambda: (_posts(), (), {"total": Sum("no_such_field")}),
    "distinct_fields": lambda: (_posts().distinct("title"), (), {"posts": Count("id")}),
    # An error of the database.
    "division_by_zero": lambda: (_posts(), (), {"total": Sum(RawSQL("1 / (id - id)", []))}),
    # Django needs a connection to compile this QuerySet, so its query runs on the thread path.
    "needs_a_connection_to_compile": lambda: (_probes().annotate(value=_OpensConnection()), (), {"most": Max("value")}),
}


def _aggregate_call(name: str, method: str) -> Callable[[], Any]:
    queryset, args, kwargs = AGGREGATES[name]()
    return lambda: getattr(queryset, method)(*args, **kwargs)


def _aggregate_outcome(error: Exception | None, result: dict[str, Any] | None) -> Any:
    """The result of ``aggregate`` with the type of each value, or the error of the call."""
    if error is not None:
        return {"error": type(error).__qualname__, "message": str(error)}
    return {name: repr(value) for name, value in result.items()}


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

    @api.get("/returned/validator-sync")
    def returned_validator_sync() -> list[ProbeWithQueryingValidator]:
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


def _skip_the_query(execute: Any, sql: str, params: Any, many: bool, context: Any) -> None:
    """A wrapper that does not call ``execute``."""


# The calls of the methods that the classes of ``_own_method_class`` replace.
own_method_calls: list[str] = []


def _own_method_class(method: str) -> type[QuerySet]:
    """A QuerySet class with its own ``method``, as a cache library or a soft-delete library has."""

    def recorded(self: QuerySet, *args: Any, **kwargs: Any) -> Any:
        result = getattr(QuerySet, method)(self, *args, **kwargs)
        # The lines after the call of Django are part of the method too.
        own_method_calls.append(method)
        return result

    return type("OwnMethodQuerySet", (QuerySet,), {method: recorded})


class _OrderFromTheDatabase(QuerySet):
    """A QuerySet class whose ``order_by`` queries, as for an order that the database holds."""

    def order_by(self, *field_names: str) -> QuerySet:
        assert Author.objects.using(ALIAS).filter(name="Bob").exists()
        return super().order_by(*field_names)


# The async call -> the name of the author that it gives.
ORDER_HOOK_READS: dict[str, Callable[[QuerySet], Any]] = {
    "afirst": lambda authors: authors.afirst(),
    "alast": lambda authors: authors.alast(),
    "aget": lambda authors: authors.aget(name="Bob"),
}


# The sync method that a class replaces -> the async call that runs it with the setting off.
OWN_METHODS: dict[str, Callable[[QuerySet], Any]] = {
    "_fetch_all": lambda authors: _names(authors),
    "get": lambda authors: authors.aget(),
    "first": lambda authors: authors.afirst(),
    "last": lambda authors: authors.alast(),
    "exists": lambda authors: authors.aexists(),
    "exists_with_rows": lambda authors: _exists_with_rows(authors),
    "count": lambda authors: authors.acount(),
    "aggregate": lambda authors: authors.aaggregate(authors=Count("id")),
}


async def _names(authors: QuerySet) -> list[str]:
    return [author.name async for author in authors]


async def _exists_with_rows(authors: QuerySet) -> bool:
    assert await _names(authors) == ["Ada"]
    return await authors.aexists()


class QueryingIterable(ModelIterable):
    """An iterable class that sends one more query, as django-polymorphic does for the real class of each row."""

    def __iter__(self) -> Any:
        names = dict(Author.objects.using(self.queryset.db).values_list("id", "name"))
        for author in super().__iter__():
            author.looked_up = names[author.id]
            yield author


class CloningIterable(ModelIterable):
    """An iterable class that reads a filtered copy of its own QuerySet."""

    def __iter__(self) -> Any:
        authors = list(super().__iter__())
        others = self.queryset.filter(name="Bob")
        others._iterable_class = ModelIterable
        names = [author.name for author in others]
        for author in authors:
            author.looked_up = names
            yield author


ITERABLE_CLASSES = {"querying": QueryingIterable, "cloning": CloningIterable}


class _AnotherDatabaseForEachCall:
    """A database router, as one that spreads the reads over replicas: each call can give another database."""

    calls = 0

    def db_for_read(self, model: Any, **hints: Any) -> str | None:
        if model is not LoopProbe:
            return None
        type(self).calls += 1
        # ``default`` is the SQLite database, which has no table of this model.
        return ALIAS if type(self).calls == 1 else "default"


class _RouterThatQueries:
    """A database router that reads the database to choose it, as one that reads a table of tenants."""

    def db_for_read(self, model: Any, **hints: Any) -> str | None:
        if model is not LoopProbe:
            return None
        assert Author.objects.using(ALIAS).filter(name="Ada").exists()
        return ALIAS


async def _reads_of_the_probes() -> dict[str, Any]:
    """One read of each kind, with no ``using()``: the database router gives the database."""
    probes = LoopProbe.objects.order_by("id")
    return {
        "labels": [probe.label async for probe in probes],
        "get": (await LoopProbe.objects.aget(label="a")).label,
        "first": (await LoopProbe.objects.order_by("id").afirst()).label,
        "exists": await LoopProbe.objects.aexists(),
        "count": await LoopProbe.objects.acount(),
        "aggregate": await LoopProbe.objects.aaggregate(probes=Count("id")),
    }


def _api() -> BoltAPI:
    api = BoltAPI()
    _probe_threads_route(api)
    _returned_queryset_routes(api)

    @api.get("/order-hook/{read}")
    async def order_hook(read: str):
        author = await ORDER_HOOK_READS[read](_OrderFromTheDatabase(model=Author, using=ALIAS))
        return {"name": author.name}

    @api.get("/iterator-before-a-wrapper")
    async def iterator_before_a_wrapper():
        def passes(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
            return execute(sql, params, many, context)

        # No wrapper is on the connection when the iterator is made.
        iterator = aiter(LoopProbe.objects.using(ALIAS).order_by("id"))
        with connections[ALIAS].execute_wrapper(passes):
            return {"built_on": [probe.built_on async for probe in iterator]}

    @api.get("/two-marked-reads")
    async def two_marked_reads():
        probes = LoopProbe.objects.using(ALIAS)
        slow = probes.annotate(mark=RawSQL("%s", ["first-statement"]), nap=RawSQL("(SELECT 1 FROM pg_sleep(0.3))", []))
        fast = probes.annotate(mark=RawSQL("%s", ["second-statement"]))
        first, second = await asyncio.gather(slow.aget(label="a"), fast.aget(label="a"))
        return [first.mark, second.mark]

    @api.get("/own-method/{method}")
    async def own_method(method: str):
        authors = _own_method_class(method.removesuffix("_with_rows"))(model=Author, using=ALIAS).filter(name="Ada")
        own_method_calls.clear()
        await OWN_METHODS[method](authors)
        return {"calls": own_method_calls}

    @api.get("/iterable-class/{name}")
    async def iterable_class(name: str):
        authors = Author.objects.using(ALIAS).filter(name="Ada")
        authors._iterable_class = ITERABLE_CLASSES[name]
        return [author.looked_up async for author in authors]

    @api.get("/thread-transaction")
    async def thread_transaction():
        def work() -> list[str]:
            with transaction.atomic(using=ALIAS):
                Author.objects.using(ALIAS).create(name="Uncommitted", email="new@example.com")
                # Sync code that calls async code, as a sync view that calls an async helper.
                names = async_to_sync(_names)(Author.objects.using(ALIAS).filter(name="Uncommitted"))
                transaction.set_rollback(True, using=ALIAS)
            return names

        return await sync_to_async(work)()

    @api.get("/returned-with-its-rows")
    async def returned_with_its_rows():
        probes = _probes().order_by("id").values("label")
        for row in [row async for row in probes]:
            row["label"] = row["label"].upper()
        return probes

    @api.get("/query-log-of-a-statement-with-no-sql")
    async def query_log_of_a_statement_with_no_sql():
        assert [name async for name in _ada()] == ["Ada"]
        try:
            # PostgreSQL text has no NUL character, so psycopg cannot build the statement.
            await Author.objects.using(ALIAS).filter(name="a\x00b").aexists()
        except Exception as error:
            return {"error": type(error).__qualname__, "sql": connections[ALIAS].queries[-1]["sql"]}
        return {"error": None}

    @api.get("/two-iterators")
    async def two_iterators():
        posts = _posts().order_by("id")
        # Two iterators of one QuerySet, made before one of them reads.
        first_iterator, second_iterator = posts.__aiter__(), posts.__aiter__()
        first = [post async for post in first_iterator]
        second = [post async for post in second_iterator]
        return {"rows": len(first), "same_objects": all(a is b for a, b in zip(first, second, strict=True))}

    @api.get("/routed")
    async def routed():
        _AnotherDatabaseForEachCall.calls = 0
        # ``DISTINCT ON`` is PostgreSQL SQL. The compiler of the SQLite database refuses it.
        probes = [probe async for probe in LoopProbe.objects.order_by("label").distinct("label")]
        return {"databases": [probe._state.db for probe in probes], "labels": [probe.label for probe in probes]}

    @api.get("/routed-by-a-query")
    async def routed_by_a_query():
        return await _reads_of_the_probes()

    @api.get("/count-of-rows-from-a-thread")
    async def count_of_rows_from_a_thread():
        authors = Author.objects.using(ALIAS).order_by("id")
        # The ORM pool reads the rows, so the event loop has no connection yet.
        rows = await concurrency.run_in_orm_executor(list, authors)
        return {"rows": len(rows), "count": await authors.acount()}

    @api.get("/nap/{milliseconds}")
    async def nap(milliseconds: int):
        return {"pid": await _backend_pid(nap=milliseconds / 1000)}

    @api.get("/cancel-request-that-does-not-end")
    async def cancel_request_that_does_not_end():
        # Open the one connection of the loop.
        before = await _backend_pid()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_backend_pid(nap=0.1), timeout=0.05)
        # The cancel request waits in the relay. The pool keeps the connection out of use for that time.
        after = await asyncio.wait_for(_backend_pid(), timeout=10)
        return {"same_connection": after == before}

    @api.get("/routed-count")
    async def routed_count():
        _AnotherDatabaseForEachCall.calls = 0
        return {"count": await LoopProbe.objects.acount()}

    @api.get("/slow-read-outcome")
    async def slow_read_outcome():
        try:
            return {"pid": await asyncio.wait_for(_backend_pid(nap=20), 30)}
        except Exception as error:
            return {"module": type(error).__module__, "error": type(error).__qualname__}

    @api.get("/current-user")
    async def current_user():
        probe = await _probes().annotate(who=RawSQL("current_user", [])).aget(label="a")
        return {"who": probe.who}

    @api.get("/greedy-and-other")
    async def greedy_and_other():
        loop = asyncio.get_running_loop()
        start = loop.time()

        async def greedy() -> None:
            # One read after the other with no pause, as a loop of ``aget`` calls in a handler.
            while loop.time() - start < 1:
                await _backend_pid(nap=0.02)

        async def other() -> float:
            # The greedy task holds the one connection now.
            await asyncio.sleep(0.1)
            await _backend_pid(nap=0)
            return loop.time() - start

        _none, waited = await asyncio.gather(greedy(), other())
        return {"other_read_in_time": waited < 0.5}

    @api.get("/cancelled-open")
    async def cancelled_open():
        try:
            # The first read of a loop opens a connection, which takes more time than this.
            await asyncio.wait_for(_backend_pid(), timeout=0.001)
        except TimeoutError:
            return {"cancelled": True, "after": [name async for name in _ada()]}
        return {"cancelled": False}

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

    @api.get("/count/{name}")
    async def count(name: str):
        return {"outcome": await _one_row_outcome_async(ONE_ROW_QUERIES[name]().acount)}

    @api.get("/aggregate/{name}")
    async def aggregate(name: str):
        try:
            result = await _aggregate_call(name, "aaggregate")()
        except Exception as error:
            return {"outcome": _aggregate_outcome(error, None)}
        return {"outcome": _aggregate_outcome(None, result)}

    @api.get("/authors-after/{offset}")
    @paginate(LimitOffsetPagination)
    async def authors_after(request, offset: int):
        return Author.objects.using(ALIAS).order_by("id")

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
            "count": await probes.acount(),
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

    @api.get("/late-cancel-request")
    async def late_cancel_request():
        # Open the one connection of the loop.
        await _backend_pid()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_backend_pid(nap=0.2), timeout=0.1)
        # The cancelled query ends at 0.2 seconds. Its cancel request reaches the server later.
        try:
            await asyncio.wait_for(_backend_pid(nap=1), timeout=10)
        except Exception as error:
            return {"next": type(error).__qualname__}
        return {"next": "ok"}

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

    @api.get("/behind-a-slow-read")
    async def behind_a_slow_read():
        async def value(index: int) -> Any:
            probes = LoopProbe.objects.using(ALIAS).annotate(pid=RawSQL("pg_backend_pid()", []))
            try:
                probe = await probes.annotate(value=RawSQL("%s", [index])).aget(label="a")
            except Exception as error:
                return type(error).__qualname__, None
            return probe.value, probe.pid

        # The slow read is first, so each other read starts while its statement is in flight.
        slow, *others = await asyncio.gather(_backend_pid(nap=0.5), *(value(index) for index in range(12)))
        return {
            "values": [outcome for outcome, _pid in others],
            "on_the_connection_of_the_slow_read": [pid == slow for _outcome, pid in others],
        }

    @api.get("/an-error-between-reads")
    async def an_error_between_reads():
        async def divide(by: int) -> Any:
            try:
                probe = await LoopProbe.objects.using(ALIAS).annotate(value=RawSQL("6 / %s", [by])).aget(label="a")
            except Exception as error:
                return {"module": type(error).__module__, "error": type(error).__qualname__}
            return probe.value

        _slow, *outcomes = await asyncio.gather(_backend_pid(nap=0.5), divide(1), divide(0), divide(2))
        return outcomes

    @api.get("/beside-a-slow-read")
    async def beside_a_slow_read():
        # Each of these reads is alone on its connection, so the pool learns the time of a fast read.
        for _ in range(5):
            await _backend_pid(nap=0)
        slow = asyncio.ensure_future(_backend_pid(nap=1.5))
        # The statement of the slow read is now in flight for much longer than a fast read.
        await asyncio.sleep(0.2)
        loop = asyncio.get_running_loop()
        start = loop.time()
        fast = await asyncio.gather(*(_backend_pid(nap=0) for _ in range(20)))
        seconds = loop.time() - start
        return {
            "connections_of_the_fast_reads": len(set(fast)),
            "a_fast_read_behind_the_slow_read": await slow in fast,
            "fast_reads_in_time": seconds < 1,
        }

    @api.get("/stop-a-read-behind-another")
    async def stop_a_read_behind_another():
        # Open the one connection of the loop.
        await _backend_pid()
        first = asyncio.ensure_future(_backend_pid(nap=0.5))
        second = asyncio.ensure_future(_backend_pid())
        await asyncio.sleep(0.1)
        # The server runs the statement of the first read, and the statement of the second read is behind it.
        second.cancel()
        try:
            pid = await first
        except Exception as error:
            return {"first": type(error).__qualname__}
        return {"first": "ok", "second_cancelled": second.cancelled(), "same_connection": await _backend_pid() == pid}

    @api.get("/unsent-statement")
    async def unsent_statement():
        outcomes = []
        # psycopg has no SQL text for this parameter, so the statement of the first read does not go out.
        for parameter in (object(), 7):
            try:
                probe = await _probes().annotate(value=RawSQL("%s", [parameter])).aget(label="a")
            except Exception as error:
                outcomes.append({"module": type(error).__module__, "error": type(error).__qualname__})
            else:
                outcomes.append(probe.value)
        return outcomes

    @api.get("/two-slow-reads-outcome")
    async def two_slow_reads_outcome():
        outcomes = await asyncio.gather(_backend_pid(nap=20), _backend_pid(nap=20), return_exceptions=True)
        return [{"module": type(outcome).__module__, "error": type(outcome).__qualname__} for outcome in outcomes]

    @api.get("/session")
    async def session():
        probe = await (
            LoopProbe.objects.using(ALIAS)
            .annotate(
                statement_timeout=RawSQL("current_setting('statement_timeout')", []),
                tenant=RawSQL("current_setting('app.tenant', true)", []),
            )
            .aget(label="a")
        )
        published = [flag async for flag in _posts().values_list("published", flat=True).order_by("id")]
        return {
            "statement_timeout": probe.statement_timeout,
            "tenant": probe.tenant,
            "published": published,
            "built_on": probe.built_on,
        }

    @api.get("/chunks/{name}")
    async def chunks(name: str):
        queryset, plain = QUERIES[name]
        # Two rows for each chunk, so a query with three rows needs two chunks.
        return [plain(row) async for row in queryset().aiterator(chunk_size=2)]

    @api.get("/chunk-threads")
    async def chunk_threads():
        probes = LoopProbe.objects.using(ALIAS).order_by("id")
        return {
            "handler": threading.current_thread().name,
            "aiterator": [probe.built_on async for probe in probes.aiterator(chunk_size=1)],
        }

    @api.get("/chunk-backends/{rows}")
    async def chunk_backends(rows: int):
        """Read ``rows`` rows of a cursor, with another read after each row. Give the connection of each row."""
        pids = []
        other = []
        async for probe in _probe_with_backend_pid().aiterator(chunk_size=1):
            pids.append(probe.pid)
            other.append(await _backend_pid())
            if len(pids) == rows:
                break
        return {"pids": pids, "other": other}

    @api.get("/cancelled-cursor")
    async def cancelled_cursor():
        # Open the one connection of the loop.
        before = await _backend_pid()
        slow = _probe_with_backend_pid().annotate(nap=RawSQL("(SELECT 1 FROM pg_sleep(0.3))", []))
        try:
            await asyncio.wait_for(anext(slow.aiterator(chunk_size=1)), timeout=0.05)
        except TimeoutError:
            # This read needs the place that the cursor of the cancelled read has on the connection.
            following = await asyncio.wait_for(_backend_pid(), timeout=10)
            return {"same_connection": following == before}
        return {"same_connection": None}

    @api.get("/chunks-together")
    async def chunks_together():
        async def labels() -> list[Any]:
            return [[probe.label, probe.pid] async for probe in _probe_with_backend_pid().aiterator(chunk_size=1)]

        return await asyncio.gather(labels(), labels(), labels())

    @api.get("/chunks-with-prefetch")
    async def chunks_with_prefetch():
        posts = _posts().prefetch_related("tags").order_by("id")
        return [
            [post.title, sorted(tag.name for tag in post.tags.all())] async for post in posts.aiterator(chunk_size=2)
        ]

    @api.get("/chunks-of-size/{size}")
    async def chunks_of_size(size: int):
        try:
            return [name async for name in Author.objects.using(ALIAS).values_list("name", flat=True).aiterator(size)]
        except ValueError as error:
            return {"error": str(error)}

    @api.get("/chunks-division-by-zero")
    async def chunks_division_by_zero():
        authors = Author.objects.using(ALIAS).extra(where=["1 / (id - id) = 1"])
        try:
            return [author.id async for author in authors.aiterator(chunk_size=2)]
        except Exception as error:
            return {"module": type(error).__module__, "error": type(error).__qualname__}

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


def _built_on_the_handler_thread(threads: dict[str, Any]) -> bool:
    return all(name == threads["handler"] for name in (*threads["async_for"], threads["aget"]))


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


@pytest.mark.parametrize("path", ["/wrapped", "/skipped", "/blocked"])
def test_a_wrapper_that_a_handler_installs_has_the_effect_of_the_thread_path(on_loop, path):
    """The wrapper is on the connection of the event loop thread. A thread that reads has its own connection."""
    expected = _with_the_setting_off(_api, path)

    with TestClient(_api()) as client:
        response = client.get(path)
        after = client.get("/authors")

    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_wrapper_that_a_connection_created_receiver_installs_gets_the_query_and_its_error(on_loop, caplog):
    """A tool installs its wrapper on each new connection of Django. A wrapper is sync code around the query."""
    seen = []

    def record(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        try:
            return execute(sql, params, many, context)
        except Exception as error:
            seen.append(type(error).__qualname__)
            raise
        finally:
            seen.append(sql)

    def install(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias == ALIAS:
            connection.execute_wrappers.append(record)

    caplog.set_level(logging.INFO, logger="django_bolt.async_orm")
    connection_created.connect(install)
    try:
        with TestClient(_api()) as client:
            threads = client.get("/threads").json()
            seen.clear()
            error = client.get("/division-by-zero").json()
            connections[ALIAS].close()
            # The loop connection that showed the wrapper is closed.
            on_loop.wait_for_backends(0)
    finally:
        connection_created.disconnect(install)

    assert _built_on_orm_threads(threads), threads
    assert error == {"module": "django.db.utils", "error": "DataError"}
    assert len(seen) == 2, seen
    assert seen[0] == "DataError"
    assert seen[1].startswith('SELECT "django_bolt_author"."id"')
    assert _loop_log(caplog) == [
        f"The async ORM reads of the database {ALIAS!r} run on the event loop.",
        f"The async ORM reads of the database {ALIAS!r} use the ORM thread pool: "
        "a connection_created receiver installs an execute wrapper.",
    ]


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


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_aiterator_gives_the_rows_of_the_sync_orm(on_loop, name):
    queryset, plain = QUERIES[name]
    expected = _as_json([plain(row) for row in queryset().iterator(chunk_size=2)])

    with TestClient(_api()) as client:
        response = client.get(f"/chunks/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_aiterator_builds_its_rows_on_the_thread_of_the_handler(on_loop):
    with TestClient(_api()) as client:
        threads = client.get("/chunk-threads").json()

    handler = threads["handler"]
    assert not handler.startswith(ORM_THREAD)
    assert threads == {"handler": handler, "aiterator": [handler, handler]}


def test_aiterator_uses_the_orm_pool_when_the_setting_is_off(loop_db):
    with TestClient(_api()) as client:
        threads = client.get("/chunk-threads").json()

    assert all(name.startswith(ORM_THREAD) for name in threads["aiterator"]), threads


def _wait_for_last_statement(db: LoopDb, pid: int, start: str) -> str:
    """Wait until the last statement of the connection ``pid`` starts with ``start``."""
    deadline = time.monotonic() + 5
    while not (statement := _last_statement(db, pid)).startswith(start):
        assert time.monotonic() < deadline, statement
        time.sleep(0.01)
    return statement


@pytest.mark.parametrize("rows", [2, 1], ids=["to_the_last_row", "stopped_after_one_row"])
def test_aiterator_reads_a_cursor_of_the_server_on_one_connection_and_closes_it(on_loop, rows):
    """Django reads the chunks from a cursor of the server, and closes the cursor also when the read stops early."""
    with TestClient(_api()) as client:
        backends = client.get(f"/chunk-backends/{rows}").json()
        pids = backends["pids"]
        assert len(pids) == rows
        # Each chunk comes from the connection that has the cursor. The other reads use the other connection.
        assert len(set(pids)) == 1
        assert set(backends["other"]).isdisjoint(pids)
        _wait_for_last_statement(on_loop, pids[0], "CLOSE ")
        # The cursor gave its place on the connection back.
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]


def test_the_cursor_of_aiterator_gives_its_connection_back(on_loop, monkeypatch, one_statement_for_each_connection):
    """With one connection that takes one statement, the read after a cursor needs the place of the cursor."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_ACQUIRE_SECONDS", "5")
    with TestClient(_api()) as client:
        threads = client.get("/chunk-threads").json()
        after = client.get("/authors")

    assert threads["aiterator"] == [threads["handler"]] * 2
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_read_that_stops_while_its_cursor_opens_gives_its_connection_back(
    on_loop, monkeypatch, one_statement_for_each_connection
):
    """The server can make the cursor after the read stopped. The cursor then closes, and its place comes back."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get("/cancelled-cursor")

    assert response.status_code == 200, response.text
    assert response.json() == {"same_connection": True}


def test_aiterator_of_a_model_whose_rows_need_a_query_builds_its_chunks_on_a_thread(on_loop):
    built = []

    def count_authors(sender: Any, instance: Any, **kwargs: Any) -> None:
        built.append(threading.current_thread().name)
        instance.authors = Author.objects.using(ALIAS).count()

    post_init.connect(count_authors, sender=LoopProbe)
    try:
        with TestClient(_api()) as client:
            response = client.get("/chunk-threads")
    finally:
        post_init.disconnect(count_authors, sender=LoopProbe)

    assert response.status_code == 200, response.text
    threads = response.json()
    assert all(name.startswith(ORM_THREAD) for name in threads["aiterator"]), threads
    # The event loop stopped at the first row. A thread then built that row again, and the row of the next chunk.
    assert len(threads["aiterator"]) == 2
    assert [name.startswith(ORM_THREAD) for name in built] == [False, True, True]


def test_aiterator_reads_many_cursors_at_one_time(on_loop):
    with TestClient(_api()) as client:
        streams = client.get("/chunks-together").json()

    assert len(streams) == 3
    for stream in streams:
        assert [label for label, _pid in stream] == ["b", "a"]
        assert len({pid for _label, pid in stream}) == 1


def test_aiterator_with_one_connection_reads_other_statements_between_its_chunks(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        backends = client.get("/chunk-backends/2").json()

    assert len(set(backends["pids"] + backends["other"])) == 1
    assert len(backends["pids"]) == 2


def test_aiterator_without_server_side_cursors_reads_the_rows_in_one_statement(on_loop, monkeypatch):
    """``DISABLE_SERVER_SIDE_CURSORS`` is for a connection pooler that gives each statement another connection."""
    monkeypatch.setitem(connections.settings[ALIAS], "DISABLE_SERVER_SIDE_CURSORS", True)
    with TestClient(_api()) as client:
        threads = client.get("/chunk-threads").json()
        backends = client.get("/chunk-backends/2").json()

    assert threads["aiterator"] == [threads["handler"]] * 2
    # No cursor keeps a place on the connection, so the other reads take the same idle connection.
    assert len(set(backends["pids"])) == 1
    assert backends["other"] == backends["pids"]


def test_aiterator_with_prefetch_related_loads_its_relations(on_loop):
    """The prefetch queries are sync code, so such a QuerySet stays on the thread path."""
    with TestClient(_api()) as client:
        response = client.get("/chunks-with-prefetch")

    assert response.status_code == 200, response.text
    assert response.json() == [["Loops", ["django", "rust"]], ["Lanes", ["rust"]], ["Pools", ["django", "rust"]]]


@pytest.mark.parametrize("size", [1, 3, 2000, 0])
def test_aiterator_takes_each_chunk_size_of_django(on_loop, size):
    with TestClient(_api()) as client:
        response = client.get(f"/chunks-of-size/{size}")

    expected = ["Ada", "Bob", "Cy"] if size else {"error": "Chunk size must be strictly positive."}
    assert sorted(response.json()) == sorted(expected)


def test_a_query_error_of_aiterator_is_the_django_error_and_the_next_query_works(on_loop):
    with TestClient(_api()) as client:
        error = client.get("/chunks-division-by-zero").json()
        after = client.get("/authors")

    assert error == {"module": "django.db.utils", "error": "DataError"}
    assert after.json() == ["Ada", "Bob", "Cy"]


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


@pytest.mark.parametrize("name", sorted(ONE_ROW_QUERIES))
def test_acount_gives_the_result_or_the_error_of_the_sync_call(on_loop, name):
    expected = {"outcome": _one_row_outcome(ONE_ROW_QUERIES[name]().count)}

    with TestClient(_api()) as client:
        response = client.get(f"/count/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize("name", sorted(AGGREGATES))
def test_aaggregate_gives_the_result_or_the_error_of_the_sync_call(on_loop, name):
    try:
        expected = {"outcome": _aggregate_outcome(None, _aggregate_call(name, "aggregate")())}
    except Exception as error:
        expected = {"outcome": _aggregate_outcome(error, None)}

    with TestClient(_api()) as client:
        response = client.get(f"/aggregate/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    ("path", "sync_call"),
    [
        ("/count/ordered", lambda: ONE_ROW_QUERIES["ordered"]().count()),
        # Django counts the rows of a slice in a subquery.
        ("/count/sliced", lambda: ONE_ROW_QUERIES["sliced"]().count()),
        ("/aggregate/several", lambda: _aggregate_call("several", "aggregate")()),
        ("/aggregate/over_an_annotation", lambda: _aggregate_call("over_an_annotation", "aggregate")()),
    ],
    ids=["count", "count_of_a_slice", "aggregate", "aggregate_over_a_subquery"],
)
def test_acount_and_aaggregate_send_the_sql_of_the_sync_call(on_loop, path, sync_call):
    sync_call()
    expected = _last_statement(on_loop, connections[ALIAS].connection.info.backend_pid)
    connections[ALIAS].close()

    with TestClient(_api()) as client:
        assert client.get(path).status_code == 200
        with psycopg.connect(**on_loop.params, autocommit=True) as admin:
            (sent,) = admin.execute(
                "SELECT query FROM pg_stat_activity WHERE application_name = %s ORDER BY query_start DESC LIMIT 1",
                (on_loop.application_name,),
            ).fetchone()

    assert sent == expected


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


@pytest.mark.parametrize(
    "path",
    [
        "/count/ordered",
        "/count/sliced",
        "/aggregate/several",
        "/aggregate/over_an_annotation",
        # The offset is after the last row, so the pagination sends only the statement of the count.
        "/authors-after/0?offset=50",
        # The pagination sends the statement of the count and the statement of the rows of the page.
        "/authors-after/0?limit=2&offset=1",
    ],
)
def test_a_count_and_an_aggregate_open_no_django_connection(on_loop, path):
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
            response = client.get(path)
        finally:
            connection_created.disconnect(count)

    assert response.status_code == 200, response.text
    assert opened == []


def test_the_pagination_of_bolt_counts_the_rows_as_with_the_setting_off(on_loop):
    expected = _with_the_setting_off(_api, "/authors-after/0?limit=2&offset=1")
    assert expected["total"] == 3
    assert [author["name"] for author in expected["items"]] == ["Bob", "Cy"]

    with TestClient(_api()) as client:
        response = client.get("/authors-after/0?limit=2&offset=1")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_a_serializer_validator_of_a_sync_handler_can_query(on_loop):
    """The validators of a sync handler run on a thread. Sync code cannot query on the event loop."""
    expected = _with_the_setting_off(_api, "/returned/validator-sync")
    assert [row["label"] for row in expected] == ["a", "b"]

    with TestClient(_api(), raise_server_exceptions=False) as client:
        response = client.get("/returned/validator-sync")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_a_read_uses_a_thread_when_the_database_router_queries(on_loop):
    """Django's own methods run the router on a thread. Sync code cannot query on the event loop."""
    with override_settings(DATABASE_ROUTERS=[_RouterThatQueries()]):
        expected = _with_the_setting_off(_api, "/routed-by-a-query")
        assert expected["labels"] == ["a", "b"]

        with TestClient(_api()) as client:
            response = client.get("/routed-by-a-query")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_acount_of_a_queryset_with_its_rows_uses_no_loop_connection(on_loop):
    """Django counts the rows that the QuerySet has. A connection that the count does not need can fail."""
    opened = []

    def count(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias == ALIAS:
            opened.append(threading.current_thread().name)

    with TestClient(_api()) as client:
        connection_created.connect(count)
        try:
            response = client.get("/count-of-rows-from-a-thread")
        finally:
            connection_created.disconnect(count)

    assert response.status_code == 200, response.text
    assert response.json() == {"rows": 3, "count": 3}
    # The ORM pool opened one connection for the rows.
    assert len(opened) == 1, opened


def test_acount_uses_one_database_when_the_router_gives_another_for_each_call(on_loop):
    with override_settings(DATABASE_ROUTERS=[_AnotherDatabaseForEachCall()]), TestClient(_api()) as client:
        response = client.get("/routed-count")

    assert response.status_code == 200, response.text
    assert response.json() == {"count": 2}


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
        "count": 1,
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


def test_two_reads_that_start_together_open_two_connections(on_loop):
    """The first read of a loop opens one connection before it compiles. One of the two reads then uses it."""
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
    assert len(opened) == 2, opened
    assert all(name.startswith(ORM_THREAD) for name in opened), opened


@pytest.fixture
def one_statement_for_each_connection(monkeypatch):
    """A connection takes one statement at a time, so a read at the connection limit waits for a connection."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_PIPELINE_DEPTH", "1")


def test_a_loop_opens_ten_connections_by_default(on_loop):
    """Each read at the same time gets a connection, up to the limit. The other reads use these connections."""
    with TestClient(_api()) as client:
        response = client.get("/many-at-once/12")

    assert response.status_code == 200, response.text
    assert response.json() == {"connections": 10}


def test_a_read_that_waits_for_the_acquire_time_fails_with_the_django_error(
    on_loop, monkeypatch, one_statement_for_each_connection
):
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


def test_a_read_at_the_connection_limit_goes_out_behind_the_statement_in_flight(on_loop, monkeypatch):
    """The reads do not wait for the connection: a read that waits fails after the acquire time, which is short here."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_ACQUIRE_SECONDS", "0.2")
    with TestClient(_api()) as client:
        response = client.get("/behind-a-slow-read")

    assert response.status_code == 200, response.text
    assert response.json() == {"values": list(range(12)), "on_the_connection_of_the_slow_read": [True] * 12}


def test_a_statement_goes_to_the_database_before_the_result_of_the_statement_before(recorded_link, monkeypatch):
    """The relay sees the second statement while the server still runs the first one."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get("/two-marked-reads")

    assert response.status_code == 200, response.text
    assert response.json() == ["first-statement", "second-statement"]
    traffic = recorded_link.traffic

    def sent_at(marker: bytes) -> int:
        """The place in the traffic where the client sent all of ``marker``. A statement can come in parts."""
        sent = b""
        for at, (side, data) in enumerate(traffic):
            if side == "client":
                sent += data
                if marker in sent:
                    return at
        raise AssertionError(f"the client did not send {marker!r}")

    first_sent = sent_at(b"first-statement")
    second_sent = sent_at(b"second-statement")
    # The server sends nothing for the first statement until its sleep ends.
    first_result = next(at for at, (side, _data) in enumerate(traffic) if side == "server" and at > first_sent)
    assert second_sent < first_result, [(side, data[:40]) for side, data in traffic[first_sent:]]


def test_a_connection_takes_statements_up_to_the_pipeline_depth(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_PIPELINE_DEPTH", "3")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_ACQUIRE_SECONDS", "0.2")
    with TestClient(_api()) as client:
        response = client.get("/behind-a-slow-read")

    assert response.status_code == 200, response.text
    # The slow read and two more reads have a place. The other reads wait for the acquire time.
    assert response.json()["values"] == [0, 1] + ["OperationalError"] * 10


def test_an_error_of_one_statement_does_not_fail_the_statements_behind_it(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_ACQUIRE_SECONDS", "0.2")
    with TestClient(_api()) as client:
        response = client.get("/an-error-between-reads")

    assert response.status_code == 200, response.text
    assert response.json() == [6, {"module": "django.db.utils", "error": "DataError"}, 3]


def test_a_statement_that_does_not_go_out_gives_its_place_back(on_loop, monkeypatch, one_statement_for_each_connection):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_ACQUIRE_SECONDS", "0.2")
    with TestClient(_api()) as client:
        response = client.get("/unsent-statement")

    assert response.status_code == 200, response.text
    assert response.json() == [{"module": "django.db.utils", "error": "ProgrammingError"}, 7]


def test_a_read_does_not_go_out_behind_a_slow_statement(on_loop, monkeypatch):
    """A statement behind a slow statement waits for it. The other connection takes the fast reads."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "2")
    with TestClient(_api()) as client:
        response = client.get("/beside-a-slow-read")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "connections_of_the_fast_reads": 1,
        "a_fast_read_behind_the_slow_read": False,
        "fast_reads_in_time": True,
    }


def test_a_read_that_stops_behind_another_statement_does_not_stop_that_statement(on_loop, monkeypatch):
    """A cancel request names a connection. The server stops the statement that it runs then."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get("/stop-a-read-behind-another")

    assert response.status_code == 200, response.text
    assert response.json() == {"first": "ok", "second_cancelled": True, "same_connection": True}


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


def test_a_query_waits_when_each_connection_is_in_use(on_loop, monkeypatch, one_statement_for_each_connection):
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


def test_a_cancelled_waiter_gives_its_turn_to_the_next_waiter(on_loop, monkeypatch, one_statement_for_each_connection):
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
    """A ``connection_created`` receiver prepares each connection of Django. Django opens the loop connections too."""

    def prepare(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias != ALIAS:
            return
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 4321")
            # A setting of the application, as a row-level-security policy reads it.
            cursor.execute("SET app.tenant = 'acme'")
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

    assert session == {
        "statement_timeout": "4321ms",
        "tenant": "acme",
        "published": ["yes", "no", "yes"],
        "built_on": handler,
    }


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
        # The one connection that stays is the loop connection.
        on_loop.wait_for_backends(1)

    on_loop.wait_for_backends(0)


def _loop_log(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "django_bolt.async_orm"]


def test_a_log_line_tells_where_the_reads_of_a_database_run(on_loop, caplog):
    caplog.set_level(logging.INFO, logger="django_bolt.async_orm")
    with TestClient(_api()) as client:
        assert client.get("/authors").status_code == 200
        assert client.get("/authors").status_code == 200

    assert _loop_log(caplog) == [f"The async ORM reads of the database {ALIAS!r} run on the event loop."]


@pytest.mark.parametrize("on_loop", [{"server_side_binding": True}], indirect=True, ids=["server_side_binding"])
def test_a_database_with_server_side_binding_reads_on_the_orm_pool(on_loop, caplog):
    """The server merges the parameters. Rust sends only a statement with its parameters in it."""
    caplog.set_level(logging.INFO, logger="django_bolt.async_orm")
    with TestClient(_api()) as client:
        threads = client.get("/threads").json()

    assert _built_on_orm_threads(threads), threads
    assert _loop_log(caplog) == [
        f"The async ORM reads of the database {ALIAS!r} use the ORM thread pool: server_side_binding is on."
    ]


def test_a_database_with_autocommit_off_reads_on_the_orm_pool(on_loop, monkeypatch, caplog):
    """Each read of a thread is then in the transaction of that thread."""
    monkeypatch.setitem(connections.settings[ALIAS], "AUTOCOMMIT", False)
    caplog.set_level(logging.INFO, logger="django_bolt.async_orm")
    with TestClient(_api()) as client:
        threads = client.get("/threads").json()

    assert _built_on_orm_threads(threads), threads
    assert _loop_log(caplog) == [
        f"The async ORM reads of the database {ALIAS!r} use the ORM thread pool: AUTOCOMMIT is off."
    ]


@pytest.fixture
def assumed_role(postgres_database):
    """A role with no login, which a connection takes with ``SET ROLE``."""
    role = psycopg.sql.Identifier(f"bolt_role_{secrets.token_hex(4)}")
    with psycopg.connect(**postgres_database, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL("CREATE ROLE {}").format(role))
    try:
        with psycopg.connect(**postgres_database, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(role))
        yield role.as_string()
    finally:
        # A role is in each database of the server. It must not stay after a failed test.
        with psycopg.connect(**postgres_database, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(role))
            admin.execute(psycopg.sql.SQL("DROP ROLE {}").format(role))


def test_a_loop_connection_has_the_role_of_assume_role(assumed_role, postgres_database, monkeypatch, django_db_blocker):
    """The role sets what a query can read. A read on the event loop must not have more access than a thread has."""
    role = assumed_role.strip('"')
    with _setting_on(postgres_database, monkeypatch, django_db_blocker, {"assume_role": role}):
        on_a_thread = _probes().annotate(who=RawSQL("current_user", [])).get(label="a").who
        with TestClient(_api()) as client:
            on_the_loop = client.get("/current-user").json()

    assert on_a_thread == role
    assert on_the_loop == {"who": role}


@pytest.mark.parametrize("method", sorted(OWN_METHODS))
def test_a_queryset_class_with_its_own_method_runs_it(on_loop, method):
    expected = _with_the_setting_off(_api, f"/own-method/{method}")
    assert expected == {"calls": [method.removesuffix("_with_rows")]}

    with TestClient(_api()) as client:
        response = client.get(f"/own-method/{method}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize("read", sorted(ORDER_HOOK_READS))
def test_a_queryset_class_whose_order_by_queries_reads_on_a_thread(on_loop, read):
    """``first``, ``last`` and ``get`` of Django call ``order_by`` of the class. Sync code cannot query on the event loop."""
    expected = _with_the_setting_off(_api, f"/order-hook/{read}")
    assert expected == {"name": {"afirst": "Ada", "alast": "Cy", "aget": "Bob"}[read]}

    with TestClient(_api()) as client:
        response = client.get(f"/order-hook/{read}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_an_iterator_reads_where_the_code_that_reads_it_is(on_loop):
    """The iterator is made with no execute wrapper, and read under one. A read under a wrapper uses a thread."""
    with TestClient(_api()) as client:
        response = client.get("/iterator-before-a-wrapper")

    assert response.status_code == 200, response.text
    built_on = response.json()["built_on"]
    assert len(built_on) == 2
    assert all(name.startswith(ORM_THREAD) for name in built_on), built_on


@pytest.mark.parametrize("name", sorted(ITERABLE_CLASSES))
def test_an_iterable_class_that_is_not_of_django_runs_on_a_thread(on_loop, name):
    """Such a class can query while it builds the rows. Only a thread can run it."""
    expected = _with_the_setting_off(_api, f"/iterable-class/{name}")
    assert expected == {"querying": ["Ada"], "cloning": [[]]}[name]

    with TestClient(_api()) as client:
        response = client.get(f"/iterable-class/{name}")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_a_model_whose_rows_need_a_query_reads_on_a_thread(on_loop):
    """A receiver, a ``from_db`` method or a field converter can query for each row. The event loop cannot."""
    built = []

    def count_authors(sender: Any, instance: Any, **kwargs: Any) -> None:
        built.append(threading.current_thread().name)
        instance.authors = Author.objects.using(ALIAS).count()

    post_init.connect(count_authors, sender=LoopProbe)
    try:
        with TestClient(_api()) as client:
            first = client.get("/threads")
            builds_of_the_first_request = len(built)
            built.clear()
            second = client.get("/threads")
    finally:
        post_init.disconnect(count_authors, sender=LoopProbe)

    assert first.status_code == 200, first.text
    assert _built_on_orm_threads(first.json()), first.json()
    assert _built_on_orm_threads(second.json()), second.json()
    # ``/threads`` builds two rows and then one row. The first read started on the event loop and stopped at
    # its first row. A read of this model then starts on a thread.
    assert builds_of_the_first_request == 4
    assert len(built) == 3
    assert all(name.startswith(ORM_THREAD) for name in built), built


def test_a_read_that_waits_gets_the_next_free_connection(on_loop, monkeypatch, one_statement_for_each_connection):
    """A task that reads again at once must not take the connection before the read that waits for it."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get("/greedy-and-other")

    assert response.status_code == 200, response.text
    assert response.json() == {"other_read_in_time": True}


def test_a_read_that_waits_opens_a_connection_when_the_one_in_use_closes(
    on_loop, monkeypatch, one_statement_for_each_connection
):
    """The one connection is too old when its read gives it back, so it closes. Its place goes to the read that waits."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_LIFETIME_SECONDS", "0.01")
    monkeypatch.setenv("DJANGO_BOLT_LOOP_ACQUIRE_SECONDS", "3")
    with TestClient(_api(), raise_server_exceptions=False) as client:
        response = client.get("/two-at-once")

    assert response.status_code == 200, response.text
    first, second = response.json()
    assert first != second


def test_a_read_that_stops_while_its_connection_opens_leaves_no_connection(on_loop):
    with TestClient(_api()) as client:
        response = client.get("/cancelled-open")
    connections[ALIAS].close()

    assert response.json() == {"cancelled": True, "after": ["Ada"]}
    on_loop.wait_for_backends(0)


@pytest.mark.parametrize("on_loop", [{"pool": {"min_size": 1, "max_size": 2}}], indirect=True, ids=["django_pool"])
def test_a_database_with_the_connection_pool_of_django_reads_on_the_event_loop(on_loop):
    """A loop connection is not a connection of that pool: the pool wants each connection back."""
    with TestClient(_api()) as client:
        threads = client.get("/threads").json()
        many = client.get("/many-at-once/4").json()

    assert _built_on_the_handler_thread(threads), threads
    assert many == {"connections": 4}


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
        # The server refuses a string with two statements, as for psycopg in the pipeline mode.
        ("two", {**PROGRAMMING_ERROR, "message": "cannot insert multiple commands into a prepared statement"}),
        ("second-fails", {**PROGRAMMING_ERROR, "message": "cannot insert multiple commands into a prepared statement"}),
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


def _terminate_the_backend_that_sleeps(db: LoopDb) -> None:
    """Wait until a backend of the test runs ``pg_sleep``, then end it."""
    deadline = time.monotonic() + 10
    with psycopg.connect(**db.params, autocommit=True) as admin:
        while time.monotonic() < deadline:
            killed = admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = %s AND state = 'active' AND query LIKE '%%pg_sleep%%'",
                (db.application_name,),
            ).fetchall()
            if killed:
                return
            time.sleep(0.02)


def test_a_connection_that_dies_during_a_query_fails_the_read_and_not_the_next_read(on_loop):
    """The database can have run the statement, so the pool does not send it again."""
    with TestClient(_api()) as client:
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
        connections[ALIAS].close()

        killer = threading.Thread(target=_terminate_the_backend_that_sleeps, args=(on_loop,))
        killer.start()
        try:
            response = client.get("/slow-read-outcome")
        finally:
            killer.join()
        after = client.get("/authors")

    assert response.json() == {"module": "django.db.utils", "error": "OperationalError"}
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_a_connection_that_dies_fails_each_read_in_flight_on_it(on_loop, monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
        connections[ALIAS].close()

        killer = threading.Thread(target=_terminate_the_backend_that_sleeps, args=(on_loop,))
        killer.start()
        try:
            response = client.get("/two-slow-reads-outcome")
        finally:
            killer.join()
        after = client.get("/authors")

    assert response.json() == [{"module": "django.db.utils", "error": "OperationalError"}] * 2
    assert after.json() == ["Ada", "Bob", "Cy"]


def test_two_iterators_of_one_queryset_read_the_rows_one_time(on_loop):
    with TestClient(_api()) as client:
        response = client.get("/two-iterators")

    assert response.json() == {"rows": 3, "same_objects": True}


def test_a_read_uses_one_database_when_the_router_gives_another_for_each_call(on_loop):
    """The pool, the statement and the instances of one read must be of one database."""
    with override_settings(DATABASE_ROUTERS=[_AnotherDatabaseForEachCall()]), TestClient(_api()) as client:
        response = client.get("/routed")

    assert response.status_code == 200, response.text
    assert response.json() == {"databases": [ALIAS, ALIAS], "labels": ["a", "b"]}


def test_a_read_uses_a_thread_when_its_new_connection_has_an_execute_wrapper(on_loop):
    """A receiver can add its wrapper after the first connection of the loop. The loop cannot call a wrapper around a query."""
    seen = []
    armed = []

    def record(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        seen.append(sql)
        return execute(sql, params, many, context)

    def install(sender: Any, connection: Any, **kwargs: Any) -> None:
        if connection.alias == ALIAS and armed:
            connection.execute_wrappers.append(record)

    connection_created.connect(install)
    try:
        with TestClient(_api()) as client:
            # The loop opens its first connection, with no wrapper.
            assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
            armed.append(True)
            # Two reads at one time need a second connection. That connection gets the wrapper.
            first, second = client.get("/two-at-once").json()
            later = client.get("/threads").json()
            connections[ALIAS].close()
    finally:
        connection_created.disconnect(install)

    assert first != second
    # The read that could not use its new loop connection ran on a thread, with the wrapper around its query.
    assert any("pg_backend_pid" in sql for sql in seen), seen
    assert _built_on_orm_threads(later), later


def _wait_for_a_backend_that_sleeps(db: LoopDb) -> None:
    deadline = time.monotonic() + 10
    with psycopg.connect(**db.params, autocommit=True) as admin:
        while time.monotonic() < deadline:
            sleeping = admin.execute(
                "SELECT 1 FROM pg_stat_activity"
                " WHERE application_name = %s AND state = 'active' AND query LIKE '%%pg_sleep%%'",
                (db.application_name,),
            ).fetchall()
            if sleeping:
                return
            time.sleep(0.02)
    raise AssertionError("no backend of the test runs pg_sleep")


def test_a_client_that_closes_does_not_stop_the_read_of_another_client(on_loop):
    """The test clients of one process use the same worker threads, and thus the same loop connections."""
    outcome = []
    with TestClient(_api()) as busy:
        reader = threading.Thread(target=lambda: outcome.append(busy.get("/nap/700").json()), daemon=True)
        reader.start()
        _wait_for_a_backend_that_sleeps(on_loop)
        with TestClient(_api()):
            pass
        reader.join(timeout=10)
        finished = not reader.is_alive()
        connections[ALIAS].close()

    assert finished, "the read of the open client got no result"
    assert isinstance(outcome[0]["pid"], int), outcome


@pytest.mark.parametrize("on_loop_through_a_slow_link", [1.0], indirect=True, ids=["slow_connect"])
def test_a_cancel_request_that_does_not_end_closes_its_connection(on_loop_through_a_slow_link, monkeypatch, caplog):
    """libpq sets no time limit for a cancel request. A request that hangs would keep the connection out of use."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    # The relay holds each new connection for 1 second, and a cancel request is a new connection.
    monkeypatch.setattr(async_orm, "_CANCEL_SECONDS", 0.2)
    caplog.set_level(logging.WARNING, logger="django_bolt.async_orm")
    with TestClient(_api()) as client:
        response = client.get("/cancel-request-that-does-not-end")

    assert response.status_code == 200, response.text
    assert response.json() == {"same_connection": False}
    assert "The cancel request of a loop query did not end in 0.2 seconds. Its connection closes." in caplog.messages


@pytest.mark.parametrize("on_loop", [{"cursor_factory": psycopg.ClientCursor}], indirect=True, ids=["cursor_factory"])
def test_a_database_with_a_cursor_factory_reads_on_the_orm_pool(on_loop, caplog):
    """Django then makes cursors of that class, and not of its own class."""
    caplog.set_level(logging.INFO, logger="django_bolt.async_orm")
    with TestClient(_api()) as client:
        threads = client.get("/threads").json()

    assert _built_on_orm_threads(threads), threads
    assert _loop_log(caplog) == [
        f"The async ORM reads of the database {ALIAS!r} use the ORM thread pool: OPTIONS has a cursor_factory."
    ]


def test_auser_gives_a_user_whose_model_queries_when_it_builds_an_instance(users):
    """Without the thread, the load fails, and the backend then gives no user for a valid token."""
    user_model = get_user_model()

    def count_groups(sender: Any, instance: Any, **kwargs: Any) -> None:
        instance.groups_in_the_database = Group.objects.count()

    post_init.connect(count_groups, sender=user_model)
    try:
        with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True), TestClient(_auth_api()) as client:
            assert client.get("/warm-up").status_code == 200
            first = _me(client, users["active"])
            second = _me(client, users["active"])
    finally:
        post_init.disconnect(count_groups, sender=user_model)
        concurrency.check_pool_environment()

    assert first == second == {"username": "ada", "is_user": True}


def test_a_read_that_sync_code_runs_in_its_transaction_has_the_rows_of_the_transaction(on_loop):
    """``async_to_sync`` runs the coroutine on the event loop, and the thread that called it has the transaction."""
    expected = _with_the_setting_off(_api, "/thread-transaction")
    assert expected == ["Uncommitted"]

    with TestClient(_api()) as client:
        response = client.get("/thread-transaction")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_a_returned_queryset_that_has_its_rows_sends_no_query(on_loop):
    """The handler read the rows and changed them. The response has the rows of the handler."""
    with TestClient(_api()) as client:
        response = client.get("/returned-with-its-rows")

    assert response.status_code == 200, response.text
    assert response.json() == [{"label": "A"}, {"label": "B"}]


def test_debug_logs_a_statement_that_psycopg_cannot_build_as_django_logs_it(on_loop):
    """Each connection keeps its last statement. The log must not show the statement of an earlier query."""
    with override_settings(DEBUG=True):
        with CaptureQueriesContext(connections[ALIAS]) as captured, pytest.raises(Exception) as sync_error:  # noqa: PT011
            Author.objects.using(ALIAS).filter(name="a\x00b").exists()
        expected = {"error": type(sync_error.value).__qualname__, "sql": captured[-1]["sql"]}
        assert expected["error"] == "DataError"
        # Django 6.1 logs the statement and its parameters. The versions before log no statement.
        assert expected["sql"] is None or expected["sql"].startswith("QUERY = ")

        with TestClient(_api()) as client:
            response = client.get("/query-log-of-a-statement-with-no-sql")

    assert response.status_code == 200, response.text
    assert response.json() == expected


def test_a_tool_that_replaces_the_execute_of_the_cursor_wrapper_gets_each_statement(on_loop, monkeypatch):
    """An APM tool replaces ``CursorWrapper.execute``. Django does not read what that method returns."""
    statements = []
    django_execute = CursorWrapper.execute

    def traced(self: CursorWrapper, sql: str, params: Any = None) -> None:
        statements.append(sql)
        django_execute(self, sql, params)

    with TestClient(_api()) as client:
        assert client.get("/authors").status_code == 200
        with monkeypatch.context() as patch:
            patch.setattr(CursorWrapper, "execute", traced)
            threads = client.get("/threads")

    assert threads.status_code == 200, threads.text
    assert _built_on_the_handler_thread(threads.json()), threads.json()
    assert len(statements) == 2, statements
    assert all(statement.startswith('SELECT "django_bolt_loopprobe"."id"') for statement in statements)


def test_a_tool_that_sends_no_statement_gives_an_error_and_not_the_rows_of_an_earlier_read(on_loop, monkeypatch):
    def blocked(self: CursorWrapper, sql: str, params: Any = None) -> None:
        """A replacement of ``CursorWrapper.execute`` that does not call it."""

    with TestClient(_api()) as client:
        # The one connection of the loop has the result of this read.
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
        with monkeypatch.context() as patch:
            patch.setattr(CursorWrapper, "execute", blocked)
            response = client.get("/connect-error")
        after = client.get("/authors")

    assert response.json() == {"module": "django.db.utils", "error": "ProgrammingError"}
    assert after.json() == ["Ada", "Bob", "Cy"]


@pytest.mark.parametrize("on_loop_through_a_slow_link", [0.3], indirect=True, ids=["slow_connect"])
def test_a_cancel_request_that_comes_late_does_not_stop_the_next_query(on_loop_through_a_slow_link, monkeypatch):
    """The request names a connection, not a query. The pool keeps the connection until the request ended."""
    monkeypatch.setenv("DJANGO_BOLT_LOOP_CONNECTIONS", "1")
    with TestClient(_api()) as client:
        response = client.get("/late-cancel-request")

    assert response.status_code == 200, response.text
    assert response.json() == {"next": "ok"}
