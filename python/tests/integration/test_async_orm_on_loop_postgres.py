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
import json
import secrets
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

import psycopg
import pytest
from django.db import connections, models, transaction
from django.db.backends.signals import connection_created
from django.db.models import Count, Func, IntegerField
from django.db.models.expressions import RawSQL
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils.deprecation import MiddlewareMixin

from django_bolt import BoltAPI, concurrency
from django_bolt.middleware import DjangoMiddlewareStack
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
FAR_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)


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


@pytest.fixture
def loop_db(postgres_database, monkeypatch, django_db_blocker):
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
        "OPTIONS": {"application_name": application_name},
    }
    configured = connections.configure_settings({"default": connections.settings["default"], ALIAS: database})
    monkeypatch.setitem(connections.settings, ALIAS, configured[ALIAS])
    with django_db_blocker.unblock():
        try:
            with connections[ALIAS].schema_editor() as editor:
                for model in MODELS:
                    editor.create_model(model)
            _seed()
            yield LoopDb(postgres_database, application_name)
        finally:
            connections[ALIAS].close()
            del connections[ALIAS]


@pytest.fixture
def on_loop(loop_db):
    """Turn the setting on for the servers of the test."""
    with override_settings(BOLT_ASYNC_ORM_ON_LOOP=True):
        yield loop_db
    # A server that starts with the setting off gives Django its own methods back.
    concurrency.check_pool_environment()


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


def _get_outcome(error: Exception | None, post: BlogPost | None) -> dict[str, Any]:
    if error is not None:
        return {"error": type(error).__qualname__, "message": str(error)}
    return {"title": post.title}


def _probe_with_backend_pid() -> Any:
    """A QuerySet with a default order, so ``get`` must drop the order."""
    return LoopProbe.objects.using(ALIAS).annotate(pid=RawSQL("pg_backend_pid()", [])).order_by("-label")


async def _backend_pid() -> int:
    probe = await (
        LoopProbe.objects.using(ALIAS)
        .annotate(pid=RawSQL("pg_backend_pid()", []), nap=RawSQL("(SELECT 1 FROM pg_sleep(0.05))", []))
        .aget(label="a")
    )
    return probe.pid


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


def _lane_api() -> BoltAPI:
    """The route runs behind Django middleware, so its request has a lane."""
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_PassMiddleware])])
    _probe_threads_route(api)
    return api


async def _probe_threads_on_this_loop() -> list[str]:
    return [probe.built_on async for probe in LoopProbe.objects.using(ALIAS).order_by("id")]


def _api() -> BoltAPI:
    api = BoltAPI()
    _probe_threads_route(api)

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

    @api.get("/twice")
    async def twice():
        posts = _posts().order_by("id")
        first = [post async for post in posts]
        second = [post async for post in posts]
        return {"rows": len(first), "same_objects": all(a is b for a, b in zip(first, second, strict=True))}

    @api.get("/cancelled-query")
    async def cancelled_query():
        slow = LoopProbe.objects.using(ALIAS).annotate(nap=RawSQL("(SELECT 1 FROM pg_sleep(30))", []))
        try:
            await asyncio.wait_for(slow.aget(label="a"), timeout=0.05)
        except TimeoutError:
            # With one connection for the loop, this read needs the connection of the cancelled query.
            following = await asyncio.wait_for(LoopProbe.objects.using(ALIAS).aget(label="b"), timeout=5)
            return {"next": following.label}
        return {"next": None}

    @api.get("/cancelled-waiter")
    async def cancelled_waiter():
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

    @api.get("/two-at-once")
    async def two_at_once():
        return list(await asyncio.gather(_backend_pid(), _backend_pid()))

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


def test_debug_keeps_the_query_log_of_django(on_loop):
    """With ``DEBUG``, Django logs each query from its own cursor."""
    with override_settings(DEBUG=True), TestClient(_api()) as client:
        threads = client.get("/threads").json()

    assert _built_on_orm_threads(threads), threads


def test_a_read_on_another_event_loop_uses_the_orm_pool(on_loop):
    """A pool of loop connections lives as long as its loop. Only the loop of a worker lives that long."""
    with TestClient(_api()):
        built_on = asyncio.run(_probe_threads_on_this_loop())

    assert len(built_on) == 2
    assert all(name.startswith(ORM_THREAD) for name in built_on), built_on


def test_an_expression_that_reads_the_server_version_compiles_on_the_event_loop(on_loop):
    with TestClient(_api()) as client:
        result = client.get("/compiled/server_version").json()

    assert result == {
        "handler": result["handler"],
        "built_on": result["handler"],
        "value": connections[ALIAS].pg_version,
    }


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


def test_aget_sends_the_sql_of_get(on_loop):
    """``get`` drops the order of the QuerySet and limits the rows. The database must get the same statement."""
    with CaptureQueriesContext(connections[ALIAS]) as captured:
        _probe_with_backend_pid().get(label="a")
    expected = captured[-1]["sql"]

    with TestClient(_api()) as client:
        pid = client.get("/get-backend-pid").json()["pid"]
        with psycopg.connect(**on_loop.params, autocommit=True) as admin:
            (sent,) = admin.execute("SELECT query FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()

    assert sent == expected
    assert "ORDER BY" not in sent
    assert "LIMIT 21" in sent


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
    assert response.json() == {"next": "b"}


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


def test_a_dead_connection_fails_no_request(on_loop):
    """A database restart ends the idle connections of the loop. The next read opens a new one."""
    with TestClient(_api()) as client:
        assert client.get("/authors").json() == ["Ada", "Bob", "Cy"]
        connections[ALIAS].close()
        assert terminate_backends(on_loop.params, on_loop.application_name) >= 1

        response = client.get("/authors")

    assert response.status_code == 200, response.text
    assert response.json() == ["Ada", "Bob", "Cy"]


def test_a_closed_client_leaves_no_connection_open(on_loop):
    with TestClient(_api()) as client:
        assert client.get("/authors").status_code == 200
    connections[ALIAS].close()

    on_loop.wait_for_backends(0)
