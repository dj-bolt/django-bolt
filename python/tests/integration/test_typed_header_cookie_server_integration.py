"""Integration tests for typed header and cookie values over real TCP.

These exercise the production request path in `src/handler.rs` (async and
sync dispatch) and the production WebSocket upgrade in
`crates/bolt-websocket/src/handler.rs::build_scope`. The in-process clients
reach only `src/testing.rs`.
"""

from __future__ import annotations

import pytest

from .apps import app_module
from .helpers import SimpleWebSocketClient, attempt_ws_upgrade

pytestmark = pytest.mark.server_integration


@pytest.mark.parametrize("path", ["/typed/async", "/typed/sync"])
def test_http_typed_header_and_cookie(make_server_project, path):
    """Typed values arrive as ints, and bad values give 422 naming the source."""
    project = make_server_project(api_module=app_module("typed_header_cookie"))

    with project.start() as server:
        response = server.get(path, headers={"X-Count": "5", "Cookie": "page=3"})
        assert response.status_code == 200, response.text
        assert response.json() == {"x_count": 5, "page": 3, "types": ["int", "int"]}

        response = server.get(path, headers={"X-Count": "abc"})
        assert response.status_code == 422, response.text
        assert "Header 'x-count'" in response.json()["detail"]

        response = server.get(path, headers={"X-Count": "1", "Cookie": "page=last"})
        assert response.status_code == 422, response.text
        assert "Cookie 'page'" in response.json()["detail"]


def test_websocket_typed_header_and_cookie(make_server_project):
    """Typed values arrive as ints, and bad values reject the upgrade with 400."""
    project = make_server_project(api_module=app_module("typed_header_cookie"))

    with project.start() as server:
        headers = {"X-Count": "5", "Cookie": "page=3"}
        with SimpleWebSocketClient(server.host, server.port, "/ws/typed", headers=headers) as websocket:
            assert websocket.receive_text() == "int:5 int:3"

        status_line, body = attempt_ws_upgrade(server.host, server.port, "/ws/typed", headers={"X-Count": "abc"})
        assert "400" in status_line, f"status={status_line!r} body={body!r}"
        assert "Header 'x-count'" in body, f"body={body!r}"

        status_line, body = attempt_ws_upgrade(
            server.host, server.port, "/ws/typed", headers={"X-Count": "1", "Cookie": "page=last"}
        )
        assert "400" in status_line, f"status={status_line!r} body={body!r}"
        assert "Cookie 'page'" in body, f"body={body!r}"


def test_websocket_list_item_is_refused_as_a_scalar_is(make_server_project):
    """A bad item of a list rejects the upgrade with 400, as a bad scalar value does.

    Rust refuses a scalar in build_scope. A parameter extractor refuses a list
    item, before accept. Master gave 101 for `5.0`, as msgspec read it, and 500
    for `abc`.
    """
    project = make_server_project(api_module=app_module("typed_header_cookie"))

    with project.start() as server:
        with SimpleWebSocketClient(server.host, server.port, "/ws/list?ids=1&ids=%2B2") as websocket:
            assert websocket.receive_text() == "[1, 2] None"

        for text in ["5.0", "abc"]:
            status_line, body = attempt_ws_upgrade(server.host, server.port, f"/ws/list?ids=1&ids={text}")
            assert "400" in status_line, f"status={status_line!r} body={body!r}"
            assert f"Query parameter 'ids': Invalid integer '{text}'" in body, f"body={body!r}"
            assert "at `$[1]`" in body, f"body={body!r}"

        status_line, body = attempt_ws_upgrade(server.host, server.port, "/ws/list?ids=1", headers={"X-Ids": "5.0"})
        assert "400" in status_line, f"status={status_line!r} body={body!r}"
        assert "Header 'x-ids': Invalid integer '5.0'" in body, f"body={body!r}"

        # A bad scalar gives the same status and the same form of detail.
        status_line, body = attempt_ws_upgrade(server.host, server.port, "/ws/typed", headers={"X-Count": "5.0"})
        assert "400" in status_line, f"status={status_line!r} body={body!r}"
        assert "Invalid request: ValueError: Header 'x-count': Invalid integer '5.0'" in body, f"body={body!r}"


def test_websocket_validation_error_with_no_message_refuses_the_upgrade_with_400(make_server_project):
    """An error item with no "msg" gives 400 at once, as WebSocketTestClient raises a ValueError."""
    project = make_server_project(api_module=app_module("typed_header_cookie"))

    with project.start() as server:
        status_line, body = attempt_ws_upgrade(server.host, server.port, "/ws/validation-error")
        assert "400" in status_line, f"status={status_line!r} body={body!r}"
        assert "Query parameter 'x': missing" in body, f"body={body!r}"


def test_a_cookie_is_read_as_django_reads_it(make_server_project):
    """A quoted cookie loses its quotes, and spaces around `=` do not count."""
    project = make_server_project(api_module=app_module("typed_header_cookie"))

    with project.start() as server:
        for cookie in ['page="7"', "theme=dark;  page = 7"]:
            response = server.get("/typed/async", headers={"X-Count": "5", "Cookie": cookie})
            assert response.status_code == 200, response.text
            assert response.json()["page"] == 7

            headers = {"X-Count": "5", "Cookie": cookie}
            with SimpleWebSocketClient(server.host, server.port, "/ws/typed", headers=headers) as websocket:
                assert websocket.receive_text() == "int:5 int:7"


def test_repeated_cookie_lines_are_read_as_django_reads_them(make_server_project):
    """Django joins the `Cookie` lines of a request with "; ", so each line counts."""
    project = make_server_project(api_module=app_module("typed_header_cookie"))
    lines = ["theme=dark", "page=7", "lang=en"]

    with project.start() as server:
        response = server.get("/typed/async", headers=[("X-Count", "5"), *(("Cookie", line) for line in lines)])
        assert response.status_code == 200, response.text
        assert response.json()["page"] == 7

        # Header names are not case-sensitive, so these three keys send three `Cookie` lines.
        headers = {"X-Count": "5", "Cookie": lines[0], "cookie": lines[1], "COOKIE": lines[2]}
        with SimpleWebSocketClient(server.host, server.port, "/ws/typed", headers=headers) as websocket:
            assert websocket.receive_text() == "int:5 int:7"
