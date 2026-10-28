"""Bolt logs an unhandled exception only when Bolt answers it with a 5xx.

A 4xx is the fault of the client, and the access log of ``runbolt`` records
it at WARNING. Thus a client that sends bad requests cannot fill
``django.server`` or the ``exception_logging_handler``. An ``error_formatter``
changes the response, not this choice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated

import msgspec
import pytest
from django.core.exceptions import DisallowedHost, PermissionDenied
from django.db import DatabaseError, connection
from django.db.models import QuerySet

from django_bolt import BoltAPI, responses
from django_bolt.exceptions import HTTPException, RequestValidationError, ResponseValidationError
from django_bolt.logging import LoggingConfig, LoggingMiddleware
from django_bolt.params import Query
from django_bolt.responses import JSON, File, FileResponse
from django_bolt.testing import TestClient

from .test_models import Article


class Filters(msgspec.Struct):
    page: int


def _routes(api: BoltAPI) -> BoltAPI:
    # The async handlers await, so they take the async dispatch path. A
    # handler with no await takes the sync path, as a sync handler does.
    @api.get("/async/crash")
    async def async_crash():
        await asyncio.sleep(0)
        raise RuntimeError("server fault")

    @api.get("/sync/crash")
    def sync_crash():
        raise RuntimeError("server fault")

    @api.get("/async/denied")
    async def async_denied():
        await asyncio.sleep(0)
        raise PermissionDenied("no access")

    @api.get("/sync/denied")
    def sync_denied():
        raise PermissionDenied("no access")

    @api.get("/sync/invalid")
    def sync_invalid():
        raise msgspec.ValidationError("bad value")

    @api.get("/query")
    async def query(filters: Annotated[Filters, Query()]):
        await asyncio.sleep(0)
        return {"page": filters.page}

    @api.post("/body")
    async def body(filters: Filters):
        await asyncio.sleep(0)
        return {"page": filters.page}

    return api


@pytest.fixture(scope="module")
def client():
    with TestClient(_routes(BoltAPI())) as client:
        yield client


@pytest.fixture
def server_errors(caplog, monkeypatch):
    """Return a function that gives the ERROR records of ``django.server``."""
    # dictConfig() disables each existing logger that its config does not name. Thus a
    # logging test can leave django.server disabled. The tests that expect a record
    # show that the capture works.
    monkeypatch.setattr(logging.getLogger("django.server"), "disabled", False)
    caplog.set_level(logging.ERROR, logger="django.server")

    def records() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "django.server" and r.levelno >= logging.ERROR]

    return records


@pytest.mark.parametrize("path", ["/async/crash", "/sync/crash"])
def test_an_exception_with_a_5xx_response_is_logged(client, server_errors, path):
    response = client.get(path)

    assert response.status_code == 500
    assert server_errors() == [f"Exception in GET {path}: RuntimeError: server fault"]


@pytest.mark.parametrize(
    ("send", "status"),
    [
        (lambda c: c.get("/async/denied"), 403),
        (lambda c: c.get("/sync/denied"), 403),
        (lambda c: c.get("/sync/invalid"), 422),
        (lambda c: c.get("/query"), 422),
        (lambda c: c.post("/body", json={"page": "x"}), 422),
        (lambda c: c.post("/body", content=b"{", headers={"Content-Type": "application/json"}), 422),
    ],
    ids=["async-denied", "sync-denied", "sync-invalid", "query-struct", "body-struct", "malformed-body"],
)
def test_an_exception_with_a_4xx_response_is_not_logged(client, server_errors, send, status):
    response = send(client)

    assert response.status_code == status
    assert server_errors() == []


def test_the_exception_logging_handler_gets_only_5xx_exceptions():
    seen: list[str] = []

    def handler(logger, request, exc, exc_info):
        seen.append(f"{request['path']}: {type(exc).__name__}")

    api = _routes(BoltAPI(logging_config=LoggingConfig(exception_logging_handler=handler)))
    with TestClient(api) as client:
        assert client.get("/async/denied").status_code == 403
        assert client.get("/query").status_code == 422
        assert client.get("/async/crash").status_code == 500

    assert seen == ["/async/crash: RuntimeError"]


_ERRORS = {
    "runtime": (lambda: RuntimeError("server fault"), 500),
    "os-error": (lambda: FileNotFoundError("config.json"), 500),
    "permission-error": (lambda: PermissionError("key.pem"), 500),
    "response-validation": (
        lambda: ResponseValidationError([{"loc": ["response"], "msg": "bad", "type": "value_error"}]),
        500,
    ),
    "denied": (lambda: PermissionDenied("no access"), 403),
    "request-validation": (
        lambda: RequestValidationError([{"loc": ["query", "q"], "msg": "bad", "type": "value_error"}]),
        422,
    ),
    "msgspec-validation": (lambda: msgspec.ValidationError("bad value"), 422),
}


def _swap_status(error, request) -> JSON:
    """Answer 400 for a server error and 503 for a client error."""
    return JSON({"detail": error.detail}, status_code=400 if error.status >= 500 else 503)


@pytest.mark.parametrize("formatter", [None, _swap_status], ids=["default", "swapped-status"])
@pytest.mark.parametrize("name", list(_ERRORS))
def test_a_formatter_does_not_change_which_exceptions_are_logged(server_errors, formatter, name):
    # A formatter that answers 400 for every server error must not hide the
    # errors from the log. One that answers 503 for a client error must not
    # let a client fill the log.
    make_error, default_status = _ERRORS[name]
    api = BoltAPI(error_formatter=formatter)

    @api.get("/raise")
    async def raise_error():
        await asyncio.sleep(0)
        raise make_error()

    with TestClient(api) as client:
        response = client.get("/raise")

    if formatter is None:
        assert response.status_code == default_status
    else:
        assert response.status_code == (400 if default_status >= 500 else 503)
    assert len(server_errors()) == (1 if default_status >= 500 else 0)


def test_an_error_while_bolt_builds_the_error_response_is_logged(server_errors):
    # Rust answers 500 for an exception that escapes the error handler, and Rust does not log it.
    api = BoltAPI()

    @api.get("/unencodable")
    async def unencodable():
        await asyncio.sleep(0)
        raise RequestValidationError([{"loc": ["query", "q"], "msg": "bad", "type": "value_error", "input": object()}])

    with TestClient(api) as client:
        response = client.get("/unencodable")

    assert response.status_code == 500
    assert [message.split(":")[:2] for message in server_errors()] == [["Exception in GET /unencodable", " TypeError"]]


def test_an_error_while_bolt_builds_an_http_exception_response_is_logged(server_errors):
    api = BoltAPI()

    @api.get("/unencodable")
    async def unencodable():
        await asyncio.sleep(0)
        raise HTTPException(400, "bad", extra={"value": object()})

    with TestClient(api) as client:
        response = client.get("/unencodable")

    assert response.status_code == 500
    assert [message.split(":")[:2] for message in server_errors()] == [["Exception in GET /unencodable", " TypeError"]]


def _break_the_connection_close() -> None:
    """Make the close of this thread's connection fail one time, after it closes."""
    connection.ensure_connection()
    close = connection.close

    def close_then_fail():
        del connection.close
        close()
        raise DatabaseError("close failed")

    connection.close = close_then_fail
    # The connection is obsolete, so the error path closes it.
    connection.close_at = time.monotonic() - 1


@pytest.mark.django_db(transaction=True)
def test_a_failure_to_drop_the_connections_does_not_replace_the_error(server_errors):
    # A close can fail, for example when a pooled connection goes back to its pool.
    # The handler calls a helper, so it runs inline on the thread of the dispatch.
    api = BoltAPI()

    @api.get("/db")
    def db():
        _break_the_connection_close()
        raise RuntimeError("database is down")

    with TestClient(api) as client:
        response = client.get("/db")

    assert response.status_code == 500
    assert server_errors() == ["Exception in GET /db: RuntimeError: database is down"]


def test_log_exception_logs_each_exception_that_it_gets(server_errors):
    # Dispatch decides what to log. A direct call logs, also for a SuspiciousOperation.
    LoggingMiddleware(LoggingConfig()).log_exception({"method": "GET", "path": "/x"}, DisallowedHost("bad"), False)

    assert server_errors() == ["Exception in GET /x: DisallowedHost: bad"]


class DeniedArticles(QuerySet):
    def _fetch_all(self):
        raise PermissionDenied("no access")


class BrokenArticles(QuerySet):
    def _fetch_all(self):
        raise RuntimeError("server fault")


@pytest.mark.parametrize(("queryset_type", "status", "records"), [(DeniedArticles, 403, 0), (BrokenArticles, 500, 1)])
def test_an_error_while_a_queryset_loads_on_the_orm_pool(server_errors, queryset_type, status, records):
    # A sync handler that returns a QuerySet from a helper loads the rows on the ORM pool.
    def articles() -> QuerySet:
        return queryset_type(model=Article)

    api = BoltAPI()

    @api.get("/articles")
    def listing(request):
        return articles()

    with TestClient(api) as client:
        response = client.get("/articles")

    assert response.status_code == status
    assert len(server_errors()) == records


def test_an_os_error_of_handler_code_is_a_logged_500_without_the_path(server_errors, settings, tmp_path):
    # Only a FileResponse error is a 404 or a 403. An OSError of handler code is a server fault.
    settings.DEBUG = False
    missing = tmp_path / "secrets" / "config.json"
    api = BoltAPI()

    @api.get("/config")
    def config():
        return {"size": len(missing.read_bytes())}

    @api.get("/key")
    def key():
        raise PermissionError(f"[Errno 13] Permission denied: '{missing}'")

    with TestClient(api) as client:
        config_response = client.get("/config")
        key_response = client.get("/key")

    assert (config_response.status_code, key_response.status_code) == (500, 500)
    assert str(tmp_path) not in config_response.text + key_response.text
    assert [message.split(":")[0] for message in server_errors()] == [
        "Exception in GET /config",
        "Exception in GET /key",
    ]


@pytest.fixture
def allowed_dir(settings, monkeypatch, tmp_path):
    """Allow FileResponse paths in one directory, as runbolt does at startup."""
    public = tmp_path / "public"
    public.mkdir()
    settings.BOLT_ALLOWED_FILE_PATHS = [str(public)]
    monkeypatch.setattr(responses, "_ALLOWED_FILE_PATHS_INITIALIZED", False)
    monkeypatch.setattr(responses, "_ALLOWED_FILE_PATHS_CACHE", None)
    responses.initialize_file_response_settings()
    return public


def test_a_file_response_error_is_a_4xx_without_the_path(server_errors, settings, allowed_dir, tmp_path):
    settings.DEBUG = False
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    api = BoltAPI()

    @api.get("/missing")
    def missing():
        return FileResponse(str(allowed_dir / "missing.txt"))

    @api.get("/outside")
    def outside_file():
        return FileResponse(str(outside))

    @api.get("/in-memory")
    def in_memory():
        return File(str(allowed_dir / "missing.txt"))

    with TestClient(api) as client:
        missing_response = client.get("/missing")
        outside_response = client.get("/outside")
        in_memory_response = client.get("/in-memory")

    assert (missing_response.status_code, outside_response.status_code, in_memory_response.status_code) == (
        404,
        403,
        404,
    )
    assert (
        missing_response.json()["detail"],
        outside_response.json()["detail"],
        in_memory_response.json()["detail"],
    ) == ("File not found", "Permission denied", "File not found")
    assert server_errors() == []


@pytest.mark.parametrize("response_type", [FileResponse, File], ids=["file-response", "file"])
@pytest.mark.parametrize("kind", ["directory", "below-a-file", "long-name", "nul-byte", "symlink-loop"])
def test_a_file_response_path_that_names_no_file_is_a_404(server_errors, allowed_dir, kind, response_type):
    # The path of a FileResponse often comes from the request. A client must not get a 500 for it.
    (allowed_dir / "report.txt").write_text("data")
    loop = allowed_dir / "loop"
    loop.symlink_to(loop)
    paths = {
        "directory": str(allowed_dir),
        "below-a-file": str(allowed_dir / "report.txt" / "x"),
        "long-name": str(allowed_dir / ("a" * 300)),
        "nul-byte": f"{allowed_dir}/a\x00b",
        "symlink-loop": str(loop),
    }
    api = BoltAPI()

    @api.get("/file")
    def file():
        return response_type(paths[kind])

    with TestClient(api) as client:
        response = client.get("/file")

    assert (response.status_code, response.json()["detail"]) == (404, "File not found")
    assert server_errors() == []


@pytest.fixture
def locked_dir(tmp_path):
    """Return a function that makes a directory with a file that this process cannot reach."""
    locked: list = []

    def make(path):
        path.mkdir()
        (path / "data.txt").write_text("data")
        path.chmod(0)
        locked.append(path)
        try:
            (path / "data.txt").read_text()
        except PermissionError:
            return path
        pytest.skip("this process reads files with no permission bits, for example as root")

    yield make
    for path in locked:
        path.chmod(0o700)


def test_a_file_that_the_server_cannot_read_in_an_allowed_dir_is_a_logged_500(server_errors, allowed_dir, locked_dir):
    # A deploy fault: the worker user cannot read the files that it serves.
    private = locked_dir(allowed_dir / "private")
    api = BoltAPI()

    @api.get("/file")
    def file():
        return FileResponse(str(private / "data.txt"))

    with TestClient(api) as client:
        response = client.get("/file")

    assert response.status_code == 500
    assert [message.split(":")[:2] for message in server_errors()] == [["Exception in GET /file", " PermissionError"]]


def test_a_path_out_of_the_allowed_dirs_that_the_server_cannot_read_is_a_403(
    server_errors, allowed_dir, locked_dir, tmp_path
):
    # A client that walks out of the allowed dirs gets a 403, also into a dir that the server cannot read.
    locked_dir(tmp_path / "locked")
    api = BoltAPI()

    @api.get("/file")
    def file():
        return FileResponse(f"{allowed_dir}/../locked/data.txt")

    with TestClient(api) as client:
        response = client.get("/file")

    assert (response.status_code, response.json()["detail"]) == (403, "Permission denied")
    assert server_errors() == []
