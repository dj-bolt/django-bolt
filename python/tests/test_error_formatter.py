"""BoltAPI(error_formatter=...): one hook formats every error that the framework builds.

The errors come from the Rust core (guards, routing, parameter coercion, rate
limits, payload limits) and from Python (validation, HTTPException, unhandled
exceptions). Each test checks the HTTP response that a client gets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

import msgspec
import pytest
from bolt_mcp import MCP, mount_mcp
from django.core.management.base import CommandError
from django.db.models import QuerySet
from django.utils.safestring import SafeString
from msgspec import Meta

from django_bolt import BoltAPI
from django_bolt.auth import AllowAny, IsAuthenticated, JWTAuthentication
from django_bolt.error_handlers import BoltError, problem_details
from django_bolt.exceptions import HTTPException
from django_bolt.management.commands.runbolt import Command
from django_bolt.middleware import rate_limit, skip_middleware
from django_bolt.param_functions import Form
from django_bolt.responses import JSON, FileResponse, PlainText, Response
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient

from .test_models import Article


class Body(Serializer):
    n: Annotated[int, Meta(ge=1)]


class Item(msgspec.Struct):
    id: int


def _record(error: BoltError, request) -> JSON:
    """Return every field of the error, so a test can check what the hook gets."""
    return JSON(
        {
            "status": error.status,
            "kind": error.kind,
            "detail": error.detail,
            "errors": error.errors,
            "headers": error.headers,
            "extra": error.extra,
            "exception": type(error.exception).__name__ if error.exception is not None else None,
            "method": request.method,
            "path": request.path,
            "probe": request.headers.get("x-probe"),
        },
        status_code=error.status,
    )


def _routes(api: BoltAPI) -> BoltAPI:
    @api.get("/items/{item_id}", guards=[AllowAny()])
    async def item(item_id: UUID) -> dict:
        return {}

    @api.get("/search", guards=[AllowAny()])
    async def search(limit: int) -> dict:
        return {}

    @api.get("/private", guards=[IsAuthenticated()])
    async def private() -> dict:
        return {}

    @api.get("/limited", guards=[AllowAny()])
    @rate_limit(rps=1, burst=1)
    async def limited() -> dict:
        return {}

    @api.post("/body", guards=[AllowAny()])
    async def body(body: Body) -> dict:
        return {}

    @api.post("/form", guards=[AllowAny()])
    async def form(age: Annotated[int, Form()]) -> dict:
        return {}

    @api.get("/conflict")
    async def conflict() -> dict:
        raise HTTPException(409, "Already exists", headers={"X-Reason": "dup"}, extra={"id": 3})

    @api.get("/crash")
    async def crash() -> dict:
        raise RuntimeError("boom")

    @api.get("/crash-sync")
    def crash_sync() -> dict:
        raise RuntimeError("boom")

    @api.get("/bad-response")
    async def bad_response() -> Item:
        return {"id": "not-an-int"}

    return api


@pytest.fixture
def recorded():
    with TestClient(_routes(BoltAPI(error_formatter=_record))) as client:
        yield client


@pytest.fixture
def problems():
    with TestClient(_routes(BoltAPI(error_formatter=problem_details))) as client:
        yield client


# --- Errors that the Rust core builds -------------------------------------


def test_guard_401_reaches_the_formatter_with_its_challenge(recorded):
    response = recorded.get("/private", headers={"X-Probe": "p1"})
    assert response.status_code == 401
    data = response.json()
    assert data["kind"] == "not_authenticated"
    assert data["detail"] == "Authentication required"
    assert data["headers"] == {"www-authenticate": "Bearer"}
    assert data["method"] == "GET"
    assert data["path"] == "/private"
    assert data["probe"] == "p1"
    assert data["exception"] is None
    # The error headers stay on the response that the formatter returns.
    assert response.headers["www-authenticate"] == "Bearer"


def test_route_miss_404_reaches_the_formatter(recorded):
    response = recorded.get("/nowhere")
    assert response.status_code == 404
    data = response.json()
    assert (data["kind"], data["detail"], data["path"]) == ("not_found", "Not Found", "/nowhere")


def test_path_coercion_422_gives_structured_errors(recorded):
    response = recorded.get("/items/not-a-uuid")
    assert response.status_code == 422
    data = response.json()
    assert data["kind"] == "validation"
    assert data["detail"] == "Request validation failed"
    [error] = data["errors"]
    assert error["loc"] == ["path", "item_id"]
    assert error["type"] == "validation_error"
    assert "UUID" in error["msg"]


def test_query_coercion_422_gives_structured_errors(recorded):
    response = recorded.get("/search?limit=abc")
    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error["loc"] == ["query", "limit"]


def test_form_coercion_422_gives_structured_errors(recorded):
    response = recorded.post("/form", data={"age": "old"})
    assert response.status_code == 422
    data = response.json()
    assert data["kind"] == "validation"
    # Rust coerces the form value, so no Python exception made this error.
    assert data["exception"] is None
    [error] = data["errors"]
    assert error["loc"] == ["body", "age"]


def test_rate_limit_429_keeps_retry_after(recorded):
    assert recorded.get("/limited").status_code == 200
    response = recorded.get("/limited")
    assert response.status_code == 429
    data = response.json()
    assert data["kind"] == "rate_limited"
    assert data["extra"] == {"retry_after": 1}
    assert data["headers"]["retry-after"] == "1"
    assert response.headers["retry-after"] == "1"
    assert response.headers["x-ratelimit-limit"] == "1"


def test_payload_too_large_413_reaches_the_formatter(settings):
    settings.BOLT_MAX_UPLOAD_SIZE = 16
    with TestClient(_routes(BoltAPI(error_formatter=_record))) as client:
        response = client.post("/body", content=b'{"n": 1' + b" " * 64 + b"}")
    assert response.status_code == 413
    assert response.json()["kind"] == "payload_too_large"


# --- Errors that Python builds -------------------------------------------


def test_body_validation_422_reaches_the_formatter(recorded):
    response = recorded.post("/body", json={"n": 0})
    assert response.status_code == 422
    data = response.json()
    assert data["kind"] == "validation"
    assert data["exception"] is not None
    [error] = data["errors"]
    assert error["loc"] == ["body", "n"]


def test_http_exception_reaches_the_formatter(recorded):
    response = recorded.get("/conflict")
    assert response.status_code == 409
    data = response.json()
    assert data["kind"] == "conflict"
    assert data["detail"] == "Already exists"
    assert data["extra"] == {"id": 3}
    assert data["exception"] == "HTTPException"
    assert response.headers["x-reason"] == "dup"


@pytest.mark.parametrize("path", ["/crash", "/crash-sync"])
def test_unhandled_exception_reaches_the_formatter(recorded, path, caplog):
    with caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"):
        response = recorded.get(path)
    assert response.status_code == 500
    data = response.json()
    assert data["kind"] == "internal_error"
    assert data["detail"] == "Internal Server Error"
    assert data["exception"] == "RuntimeError"
    # The formatter does not stop the log of the server error. The access log also names
    # the exception, so only the records of the error handlers count.
    logged = [record.getMessage() for record in caplog.records if record.name == "django_bolt.error_handlers"]
    assert logged == ["Internal Server Error: RuntimeError - boom"]


def test_suspicious_operation_400_reaches_the_formatter(settings, caplog):
    """DisallowedHost gives 400 and one security record, with a formatter as without one."""
    settings.ALLOWED_HOSTS = ["example.com"]
    api = BoltAPI(error_formatter=_record)

    @api.get("/absolute")
    async def absolute(request) -> dict:
        return {"uri": request.build_absolute_uri()}

    with caplog.at_level(logging.ERROR), TestClient(api) as client:
        response = client.get("/absolute", headers={"Host": "evil.example"})
    assert response.status_code == 400
    data = response.json()
    assert (data["kind"], data["detail"], data["exception"]) == ("bad_request", "Bad Request", "DisallowedHost")
    assert [record.name for record in caplog.records if record.levelno >= logging.ERROR] == [
        "django.security.DisallowedHost"
    ]


@pytest.mark.parametrize(("trusted_proxies", "port"), [([], "80"), (["127.0.0.1"], "443")])
def test_formatter_request_takes_the_scheme_from_a_trusted_proxy_only(settings, trusted_proxies, port):
    """The request of a Rust core error follows BOLT_TRUSTED_PROXIES, as a handler request does."""
    settings.BOLT_TRUSTED_PROXIES = trusted_proxies

    def formatter(error: BoltError, request) -> JSON:
        return JSON({"port": request.META["SERVER_PORT"]}, status_code=error.status)

    with TestClient(_routes(BoltAPI(error_formatter=formatter))) as client:
        response = client.get("/missing", headers={"Host": "example.com", "X-Forwarded-Proto": "https"})
    assert response.status_code == 404
    assert response.json() == {"port": port}


def test_response_validation_500_reaches_the_formatter(recorded, caplog, settings):
    # The formatter gets the field errors only when DEBUG is on.
    settings.DEBUG = True
    with caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"):
        response = recorded.get("/bad-response")
    assert response.status_code == 500
    data = response.json()
    assert data["kind"] == "response_validation"
    assert data["errors"]
    logged = [record.getMessage() for record in caplog.records if record.name == "django_bolt.error_handlers"]
    assert len(logged) == 1
    assert logged[0].startswith("Response Validation Error:")


# --- The built-in RFC 9457 formatter ---------------------------------------


def test_problem_details_for_a_rust_error(problems):
    response = problems.get("/private")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "detail": "Authentication required",
        "instance": "/private",
    }


def test_problem_details_for_field_errors(problems):
    response = problems.get("/items/not-a-uuid")
    assert response.headers["content-type"] == "application/problem+json"
    data = response.json()
    assert data["status"] == 422
    # The RFC 9110 name. Python 3.12 calls it "Unprocessable Entity".
    assert data["title"] == "Unprocessable Content"
    assert data["errors"][0]["loc"] == ["path", "item_id"]


def test_problem_details_for_a_python_error(problems):
    response = problems.post("/body", json={"n": 0})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"][0]["loc"] == ["body", "n"]


def test_problem_details_keeps_extra_as_extension_members(problems):
    """RFC 9457 extension members: the extra data of the error, under the standard members."""
    conflict = problems.get("/conflict")
    problems.get("/limited")
    limited = problems.get("/limited")

    assert conflict.json()["id"] == 3
    assert conflict.json()["detail"] == "Already exists"
    assert limited.status_code == 429
    assert limited.json()["retry_after"] >= 1


# --- Formatter results ------------------------------------------------------


def test_formatter_header_wins_over_the_error_header():
    def formatter(error: BoltError, request) -> Response:
        return Response(
            {"kind": error.kind},
            status_code=error.status,
            headers={"WWW-Authenticate": 'Bearer realm="api"'},
            media_type="application/vnd.error+json",
        )

    with TestClient(_routes(BoltAPI(error_formatter=formatter))) as client:
        response = client.get("/private")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/vnd.error+json"
    assert response.headers.get_list("www-authenticate") == ['Bearer realm="api"']
    assert response.json() == {"kind": "not_authenticated"}


def test_formatter_can_return_plain_text():
    def formatter(error: BoltError, request) -> PlainText:
        return PlainText(f"{error.status} {error.kind}", status_code=error.status)

    with TestClient(_routes(BoltAPI(error_formatter=formatter))) as client:
        rust_error = client.get("/nowhere")
        python_error = client.get("/conflict")
    assert rust_error.text == "404 not_found"
    assert rust_error.headers["content-type"].startswith("text/plain")
    assert python_error.text == "409 conflict"


def test_formatter_can_return_an_int_enum_status_and_a_str_subclass_header():
    def formatter(error: BoltError, request) -> JSON:
        return JSON(
            {"kind": error.kind}, status_code=HTTPStatus(error.status), headers={"X-Kind": SafeString(error.kind)}
        )

    with TestClient(_routes(BoltAPI(error_formatter=formatter))) as client:
        rust_error = client.get("/nowhere")
        python_error = client.get("/conflict")

    assert (rust_error.status_code, rust_error.headers["x-kind"]) == (404, "not_found")
    assert (python_error.status_code, python_error.headers["x-kind"]) == (409, "conflict")


@pytest.mark.parametrize(
    "formatter",
    [
        pytest.param(lambda _error, _request: 1 / 0, id="raises"),
        pytest.param(lambda _error, _request: {"not": "a response"}, id="wrong-type"),
        # A header value must be a str. An int such as Retry-After: 30 is a common mistake.
        pytest.param(
            lambda error, _request: JSON({}, status_code=error.status, headers={"Retry-After": 30}), id="int-header"
        ),
        pytest.param(
            lambda error, _request: JSON({}, status_code=error.status, headers={"X-Detail": "a\nb"}),
            id="newline-header",
        ),
        pytest.param(
            lambda error, _request: JSON({}, status_code=error.status, headers={"Bad Name": "x"}), id="bad-header-name"
        ),
        # A 204 or a 304 has no body. Its body bytes would break a keep-alive connection.
        pytest.param(lambda _error, _request: JSON({"kind": "x"}, status_code=204), id="status-204"),
        pytest.param(lambda _error, _request: JSON({"kind": "x"}, status_code=302), id="status-302"),
        pytest.param(lambda _error, _request: JSON({"kind": "x"}, status_code=1000), id="status-1000"),
    ],
)
def test_broken_formatter_logs_and_sends_the_default_error(formatter, caplog):
    with (
        caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"),
        TestClient(_routes(BoltAPI(error_formatter=formatter))) as client,
    ):
        rust_error = client.get("/nowhere")
        python_error = client.get("/conflict")
    assert rust_error.status_code == 404
    assert rust_error.json() == {"detail": "Not Found"}
    assert python_error.status_code == 409
    assert python_error.json() == {"detail": "Already exists", "extra": {"id": 3}}
    failures = [record for record in caplog.records if "error_formatter failed" in record.getMessage()]
    assert len(failures) == 2


def test_file_response_errors_reach_the_formatter(tmp_path):
    """FileResponse checks its file when the handler makes it, and Rust opens the file again to send it.

    A file that is missing at the first check gives a Python error. A file
    that goes away before Rust opens it gives a Rust error. Both reach the
    formatter.
    """
    path = tmp_path / "report.txt"
    api = BoltAPI(error_formatter=_record)

    @api.get("/missing")
    async def missing():
        return FileResponse(str(tmp_path / "absent.txt"))

    @api.get("/gone")
    async def gone():
        path.write_text("data")
        response = FileResponse(str(path))
        path.unlink()
        return response

    with TestClient(api) as client:
        missing_response = client.get("/missing")
        gone_response = client.get("/gone")

    assert missing_response.status_code == 404
    assert missing_response.json()["kind"] == "not_found"
    # A subclass of FileNotFoundError, so only a FileResponse error gives a 404.
    assert missing_response.json()["exception"] == "ResponseFileNotFound"
    # The exception text has the path of the file. The detail for the client does not.
    assert missing_response.json()["detail"] == "File not found"
    assert gone_response.status_code == 404
    assert gone_response.headers["content-type"] == "application/json"
    assert gone_response.json()["kind"] == "not_found"
    assert gone_response.json()["exception"] is None


def test_skip_cors_route_keeps_its_skip_after_the_formatter(tmp_path):
    """The formatter does not see the internal skip marker, and the route still gets no CORS headers."""
    api = BoltAPI(error_formatter=_record)

    @api.get("/gone")
    @skip_middleware("cors")
    async def gone():
        path = tmp_path / "report.txt"
        path.write_text("data")
        response = FileResponse(str(path))
        path.unlink()
        return response

    with TestClient(api, use_http_layer=True, cors_allowed_origins=["https://example.com"]) as client:
        response = client.get("/gone", headers={"Origin": "https://example.com"})

    assert response.status_code == 404
    assert response.json()["headers"] == {}
    assert "access-control-allow-origin" not in response.headers


class Conflict(msgspec.Struct):
    code: str


def test_typed_http_exception_body_skips_the_formatter():
    """A typed body is the response that the handler chose, and its route declares it."""
    api = BoltAPI(error_formatter=problem_details)

    @api.get("/typed", response_model={409: Conflict})
    async def typed():
        raise HTTPException(409, body=Conflict(code="dup"))

    with TestClient(api) as client:
        response = client.get("/typed")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"code": "dup"}


@pytest.mark.parametrize("formatter", [None, _record], ids=["default", "formatter"])
def test_http_exception_500_is_not_logged_as_a_server_error(formatter, caplog):
    """An HTTPException is a planned response. Bolt logs it with a formatter as without one."""
    api = BoltAPI(error_formatter=formatter)

    @api.get("/planned")
    async def planned():
        raise HTTPException(500, "planned outage")

    with caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"), TestClient(api) as client:
        response = client.get("/planned")

    assert response.status_code == 500
    assert [record for record in caplog.records if record.name == "django_bolt.error_handlers"] == []


def test_rust_server_error_keeps_its_reason_in_the_log(tmp_path, caplog):
    """The formatter replaces the body that tells the reason of a Rust 500. The log keeps the reason."""
    path = tmp_path / "report.txt"
    api = BoltAPI(error_formatter=_record)

    @api.get("/swapped")
    async def swapped():
        path.write_text("data")
        response = FileResponse(str(path))
        # Rust opens a directory, and reading it fails.
        path.unlink()
        path.mkdir()
        return response

    with caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"), TestClient(api) as client:
        response = client.get("/swapped")

    assert response.status_code == 500
    assert response.json()["kind"] == "internal_error"
    messages = [record.getMessage() for record in caplog.records if record.name == "django_bolt.error_handlers"]
    assert len(messages) == 1
    assert messages[0].startswith("Internal Server Error: Failed to read file:"), messages


class _Escapes(BaseException):
    """Not an Exception. ``_dispatch`` does not catch it, so it reaches Rust."""


def test_exception_that_reaches_rust_reaches_the_formatter(caplog):
    api = BoltAPI(error_formatter=_record)

    @api.get("/escape")
    def escape() -> dict:
        raise _Escapes("boom")

    with caplog.at_level(logging.ERROR, logger="django_bolt.error_handlers"), TestClient(api) as client:
        response = client.get("/escape")

    assert response.status_code == 500
    assert response.json()["kind"] == "internal_error"
    assert response.json()["exception"] == "_Escapes"
    assert any(record.name == "django_bolt.error_handlers" for record in caplog.records)


def test_debug_body_of_an_exception_that_reaches_rust_escapes_control_characters(settings):
    """The DEBUG body holds the message. A control character in it stays valid JSON."""
    settings.DEBUG = True
    api = BoltAPI()
    message = "bell\x07 escape\x1b[31m nul\x00 form\x0c"

    @api.get("/escape")
    def escape() -> dict:
        raise _Escapes(message)

    with TestClient(api) as client:
        response = client.get("/escape")

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == f"_Escapes: {message}"
    assert body["extra"]["exception"] == message
    assert message in body["extra"]["traceback"]


class ArchivedQuerySet(QuerySet):
    """Raise an HTTPException when the rows load."""

    def _fetch_all(self):
        raise HTTPException(410, "Archived")


def archived_articles():
    """Return a QuerySet from a helper, so that the sync dispatch defers it to the ORM pool."""
    return ArchivedQuerySet(model=Article)


@pytest.mark.parametrize("formatter", [None, _record], ids=["default", "formatter"])
def test_http_exception_from_a_queryset_on_the_orm_pool(formatter):
    """A sync handler gives a QuerySet, and its rows load on the ORM pool.

    An HTTPException that the rows raise there keeps its status.
    """
    api = BoltAPI(error_formatter=formatter)

    @api.get("/articles")
    def listing(request):
        return archived_articles()

    with TestClient(api) as client:
        response = client.get("/articles")

    assert response.status_code == 410, response.text
    if formatter is None:
        assert response.json() == {"detail": "Archived"}
    else:
        assert response.json()["kind"] == "gone"
        assert response.json()["exception"] == "HTTPException"


@pytest.mark.parametrize("parent_formatter", [None, _record], ids=["parent-without", "parent-same"])
def test_mount_keeps_the_formatter_of_the_mounted_api(parent_formatter):
    """The server has one formatter. The parent takes the formatter of the mounted API."""
    child = BoltAPI(error_formatter=_record)

    @child.get("/conflict")
    async def conflict() -> dict:
        raise HTTPException(409, "Already exists")

    parent = BoltAPI(error_formatter=parent_formatter)
    parent.mount("/v2", child)

    with TestClient(parent) as client:
        mounted = client.get("/v2/conflict")
        miss = client.get("/nowhere")

    assert mounted.status_code == 409
    assert mounted.json()["kind"] == "conflict"
    assert miss.status_code == 404
    assert miss.json()["kind"] == "not_found"


def test_mount_rejects_a_different_formatter():
    parent = BoltAPI(error_formatter=problem_details)

    @parent.get("/parent")
    async def parent_route() -> dict:
        return {}

    child = BoltAPI(error_formatter=_record)

    @child.get("/child")
    async def child_route() -> dict:
        return {}

    with pytest.raises(ValueError, match="error_formatter"):
        parent.mount("/v2", child)

    # The failed mount leaves the parent as it was.
    with TestClient(parent) as client:
        assert client.get("/parent").status_code == 200
        miss = client.get("/v2/child")

    assert miss.status_code == 404
    assert miss.headers["content-type"] == "application/problem+json"


async def _asgi_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"asgi"})


def test_mount_that_fails_late_keeps_the_formatter_of_the_parent():
    """A duplicate ASGI prefix fails after the routes are copied. The parent keeps its formatter."""
    parent = BoltAPI()
    parent.mount_asgi("/v2/legacy", _asgi_app)
    child = BoltAPI(error_formatter=_record)
    child.mount_asgi("/legacy", _asgi_app)

    with pytest.raises(ValueError, match="Duplicate ASGI mount prefix"):
        parent.mount("/v2", child)

    with TestClient(parent) as client:
        response = client.get("/nowhere")

    assert response.json() == {"detail": "Not Found"}


def _raising_asgi_app(scope, receive, send):
    """An ASGI app that raises when Bolt calls it."""
    raise RuntimeError("boom")


def _mount_raising_asgi_app(api: BoltAPI) -> tuple[str, str]:
    api.mount_asgi("/legacy", _raising_asgi_app)
    return "GET", "/legacy/page"


def _mount_guarded_mcp(api: BoltAPI) -> tuple[str, str]:
    mcp = MCP("formatter-test", "1.0")

    @mcp.tool
    async def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    mount_mcp(api, mcp, auth=[JWTAuthentication(secret="formatter-test-secret-0123456789")], guards=[IsAuthenticated()])
    return "POST", "/mcp"


@pytest.mark.parametrize("mount", [_mount_raising_asgi_app, _mount_guarded_mcp], ids=["asgi-raises", "mcp-guard"])
def test_formatter_does_not_change_the_errors_of_a_mount(mount):
    """An ASGI or an MCP mount keeps its own error responses."""
    responses = []
    for formatter in (None, _record):
        api = BoltAPI(error_formatter=formatter)
        method, path = mount(api)
        with TestClient(api) as client:
            responses.append(client.request(method, path, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}))

    default, formatted = responses
    assert default.status_code >= 400
    assert (formatted.status_code, formatted.headers["content-type"], formatted.content) == (
        default.status_code,
        default.headers["content-type"],
        default.content,
    )


def test_without_formatter_the_errors_keep_the_detail_shape():
    with TestClient(_routes(BoltAPI())) as client:
        assert client.get("/private").json() == {"detail": "Authentication required"}
        assert client.get("/nowhere").json() == {"detail": "Not Found"}
        assert client.get("/conflict").json() == {"detail": "Already exists", "extra": {"id": 3}}


def test_error_formatter_must_be_callable():
    with pytest.raises(TypeError, match="error_formatter"):
        BoltAPI(error_formatter="problem_details")


class _AsyncFormatter:
    async def __call__(self, error: BoltError, request) -> JSON:
        return JSON({}, status_code=error.status)


async def _async_formatter(error: BoltError, request) -> JSON:
    return JSON({}, status_code=error.status)


@pytest.mark.parametrize("formatter", [_async_formatter, _AsyncFormatter()], ids=["function", "instance"])
def test_error_formatter_must_not_be_async(formatter):
    """Rust calls the formatter on an error path that cannot await."""
    with pytest.raises(TypeError, match="error_formatter"):
        BoltAPI(error_formatter=formatter)


# --- runbolt merges the APIs of a project -----------------------------------


def _api_with_route(path: str, formatter=None) -> BoltAPI:
    api = BoltAPI(error_formatter=formatter)

    @api.get(path)
    async def view() -> dict:
        return {}

    return api


def test_merge_keeps_the_one_formatter_of_the_project():
    merged = Command().merge_apis([("a.api", _api_with_route("/a", problem_details)), ("b.api", _api_with_route("/b"))])

    with TestClient(merged) as client:
        response = client.get("/nowhere")

    assert response.headers["content-type"] == "application/problem+json"


@dataclass
class _TypedProblems:
    """A formatter with its own settings. A dataclass with ``eq=True`` cannot be hashed."""

    type_base: str

    def __call__(self, error: BoltError, request) -> JSON:
        return JSON({"type": f"{self.type_base}/{error.kind}"}, status_code=error.status)

    def format(self, error: BoltError, request) -> JSON:
        return self(error, request)


_PROBLEMS = _TypedProblems("https://errors.example")


@pytest.mark.parametrize(
    "first,second",
    [
        (_TypedProblems("https://errors.example"), _TypedProblems("https://errors.example")),
        # Each attribute access makes a new bound method. The two are equal, not identical.
        (_PROBLEMS.format, _PROBLEMS.format),
    ],
    ids=["unhashable-equal", "bound-method"],
)
def test_merge_accepts_equal_formatters(first, second):
    merged = Command().merge_apis([("a.api", _api_with_route("/a", first)), ("b.api", _api_with_route("/b", second))])

    with TestClient(merged) as client:
        response = client.get("/nowhere")

    assert response.status_code == 404
    assert response.json() == {"type": "https://errors.example/not_found"}


def test_mount_accepts_an_equal_formatter():
    parent = _api_with_route("/a", _PROBLEMS.format)
    parent.mount("/v2", _api_with_route("/b", _PROBLEMS.format))

    with TestClient(parent) as client:
        assert client.get("/v2/b").status_code == 200
        assert client.get("/nowhere").json() == {"type": "https://errors.example/not_found"}


def test_merge_rejects_two_different_formatters():
    with pytest.raises(CommandError, match="error_formatter"):
        Command().merge_apis(
            [
                ("a.api", _api_with_route("/a", problem_details)),
                ("b.api", _api_with_route("/b", _record)),
            ]
        )
