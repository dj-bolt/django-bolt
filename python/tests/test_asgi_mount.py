"""Tests for HTTP ASGI mounts."""

import asyncio
import json

import pytest
from django.core.management.base import CommandError

from django_bolt import BoltAPI
from django_bolt.api import (
    _rewrite_django_mount_redirect_message,
    _rewrite_scope_for_django_mount,
)
from django_bolt.management.commands.runbolt import Command
from django_bolt.testing import TestClient


def test_mount_asgi_rejects_dynamic_path():
    api = BoltAPI()

    async def asgi_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    with pytest.raises(ValueError, match="static"):
        api.mount_asgi("/django/{path}", asgi_app)


def test_mount_asgi_scope_root_path_and_subpath():
    api = BoltAPI()
    captured = []

    async def asgi_app(scope, receive, send):
        event = await receive()
        captured.append(
            {
                "root_path": scope["root_path"],
                "path": scope["path"],
                "query_string": scope["query_string"],
                "event_type": event["type"],
            }
        )

        payload = json.dumps({"path": scope["path"], "root_path": scope["root_path"]}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})

    api.mount_asgi("/django", asgi_app)

    with TestClient(api) as client:
        root_response = client.get("/django")
        assert root_response.status_code == 200
        assert root_response.json() == {"path": "/", "root_path": "/django"}

        nested_response = client.get("/django/accounts/login/?next=%2Fadmin%2F")
        assert nested_response.status_code == 200
        assert nested_response.json() == {
            "path": "/accounts/login/",
            "root_path": "/django",
        }

    assert captured[0]["root_path"] == "/django"
    assert captured[0]["path"] == "/"
    assert captured[0]["event_type"] == "http.request"
    assert captured[1]["path"] == "/accounts/login/"
    assert captured[1]["query_string"] == b"next=%2Fadmin%2F"


def test_mount_asgi_scope_server_defaults_to_443_on_https():
    api = BoltAPI()
    captured = {}

    async def asgi_app(scope, receive, send):
        captured["server"] = scope["server"]
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    api.mount_asgi("/secure", asgi_app)

    with TestClient(api) as client:
        response = client.get(
            "/secure",
            headers={
                "Host": "example.com",
                "X-Forwarded-Proto": "https",
            },
        )

    assert response.status_code == 204
    assert captured["server"][0] == "example.com"
    assert captured["server"][1] == 443


def test_mount_asgi_longest_prefix_wins():
    api = BoltAPI()

    async def short_app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"source":"short"}', "more_body": False})

    async def long_app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"source":"long"}', "more_body": False})

    api.mount_asgi("/d", short_app)
    api.mount_asgi("/django", long_app)

    with TestClient(api) as client:
        response = client.get("/django/admin/")
        assert response.status_code == 200
        assert response.json() == {"source": "long"}


def test_mount_asgi_chunked_response_body_is_combined():
    api = BoltAPI()

    async def asgi_app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"hello ", "more_body": True})
        await send({"type": "http.response.body", "body": b"world", "more_body": False})

    api.mount_asgi("/stream", asgi_app)

    with TestClient(api) as client:
        response = client.get("/stream")
        assert response.status_code == 200
        assert response.text == "hello world"


def test_mount_asgi_post_body_is_buffered_single_event():
    api = BoltAPI()
    captured_body = {}

    async def asgi_app(scope, receive, send):
        event = await receive()
        captured_body["event_type"] = event["type"]
        captured_body["body"] = event.get("body", b"")
        captured_body["more_body"] = event.get("more_body")

        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": False})

    api.mount_asgi("/upload", asgi_app)

    with TestClient(api) as client:
        response = client.post("/upload", content=b"abc123")
        assert response.status_code == 201
        assert response.json() == {"ok": True}

    assert captured_body == {"event_type": "http.request", "body": b"abc123", "more_body": False}


def test_mount_asgi_enforces_max_payload_size(settings):
    settings.BOLT_MAX_UPLOAD_SIZE = 4
    api = BoltAPI()
    captured = {"called": False}

    async def asgi_app(scope, receive, send):
        captured["called"] = True
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    api.mount_asgi("/upload", asgi_app)

    with TestClient(api) as client:
        response = client.post("/upload", content=b"12345")

    assert response.status_code == 413
    assert captured["called"] is False


def test_mount_asgi_timeout_returns_504(settings):
    settings.BOLT_ASGI_MOUNT_TIMEOUT = 0.05
    api = BoltAPI()

    async def hanging_asgi(scope, receive, send):
        await receive()
        await asyncio.Event().wait()

    api.mount_asgi("/hang", hanging_asgi)

    with TestClient(api) as client:
        response = client.get("/hang")

    assert response.status_code == 504


def test_mount_asgi_does_not_hijack_api_trailing_slash_redirects():
    api = BoltAPI()

    @api.get("/items")
    async def items():
        return {"source": "api"}

    async def fallback_asgi(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"source":"asgi"}', "more_body": False})

    api.mount_asgi("/", fallback_asgi)

    with TestClient(api) as client:
        # TestClient follows redirects, so /items/ should resolve to API route /items.
        response = client.get("/items/")
        assert response.status_code == 200
        assert response.json() == {"source": "api"}


def test_mount_django_with_custom_app_forwards_to_mount_asgi():
    api = BoltAPI()

    async def custom_asgi(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"source":"django"}', "more_body": False})

    api.mount_django("/django", app=custom_asgi)

    with TestClient(api) as client:
        response = client.get("/django")
        assert response.status_code == 200
        assert response.json() == {"source": "django"}


def test_mount_django_redirect_keeps_mount_prefix():
    api = BoltAPI()

    async def django_like_app(scope, receive, send):
        await receive()
        path = scope.get("path", "/")

        # Simulate Django APPEND_SLASH behavior.
        if not path.endswith("/"):
            location = f"{path}/"
            await send(
                {
                    "type": "http.response.start",
                    "status": 308,
                    "headers": [(b"location", location.encode("utf-8"))],
                }
            )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        body = json.dumps({"path": path}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    api.mount_django("/django", app=django_like_app)

    with TestClient(api) as client:
        response = client.get("/django/accounts")

    assert response.status_code == 200
    assert response.json() == {"path": "/django/accounts/"}
    assert response.history
    assert response.history[0].headers["location"] == "/django/accounts/"


def test_mount_django_rewrites_root_relative_redirect_location():
    api = BoltAPI()

    async def redirecting_app(scope, receive, send):
        await receive()
        if (scope.get("path") or "").endswith("/"):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [(b"location", b"/accounts/")],
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    api.mount_django("/django", app=redirecting_app)

    with TestClient(api) as client:
        response = client.get("/django/accounts")

    assert response.history
    assert response.history[0].headers["location"] == "/django/accounts/"


@pytest.mark.parametrize(
    ("header_name", "header_value", "expected_value"),
    [
        (b"location", b"/accounts/", b"/django/accounts/"),
        ("location", "/accounts/", "/django/accounts/"),
    ],
)
def test_rewrite_django_mount_redirect_message_handles_location_value_types(header_name, header_value, expected_value):
    message = {
        "type": "http.response.start",
        "status": 302,
        "headers": [(header_name, header_value)],
    }

    rewritten = _rewrite_django_mount_redirect_message(message, "/django")

    assert rewritten["headers"][0][0] == header_name
    assert rewritten["headers"][0][1] == expected_value


def test_rewrite_scope_for_django_mount_prepends_root_path_to_subpath():
    scope = {
        "type": "http",
        "root_path": "/django",
        "path": "/accounts/login/",
        "raw_path": b"/accounts/login/",
    }

    rewritten = _rewrite_scope_for_django_mount(scope)

    assert rewritten["path"] == "/django/accounts/login/"
    assert rewritten["raw_path"] == b"/django/accounts/login/"


def test_testclient_rejects_exact_route_mount_collision():
    api = BoltAPI()

    @api.get("/django")
    async def route():
        return {"ok": True}

    async def asgi_app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    api.mount_asgi("/django", asgi_app)

    with pytest.raises(ValueError, match="exact collision"):
        TestClient(api)


def test_runbolt_rejects_exact_route_mount_collision():
    command = Command()

    routes = [("GET", "/django", 1, lambda: None)]
    asgi_mounts = [("/django", lambda scope, receive, send: None)]

    with pytest.raises(CommandError, match="exact collision"):
        command.validate_asgi_mount_conflicts(routes, asgi_mounts)


def test_runbolt_allows_prefix_overlap_without_exact_collision():
    command = Command()

    routes = [("GET", "/api/items", 1, lambda: None)]
    asgi_mounts = [("/api", lambda scope, receive, send: None)]

    # Exact collisions are forbidden; prefix overlap is allowed.
    command.validate_asgi_mount_conflicts(routes, asgi_mounts)


class _CustomConfigError(Exception):
    pass


@pytest.mark.parametrize(
    ("exc_class", "exc_msg", "expected_fragments"),
    [
        (RuntimeError, "SECRET_KEY must not be empty", ["SECRET_KEY must not be empty"]),
        (_CustomConfigError, "bad config value", ["_CustomConfigError", "bad config value"]),
    ],
    ids=["builtin-exception", "custom-exception-type"],
)
def test_mount_asgi_exception_surfaces_in_response(exc_class, exc_msg, expected_fragments):
    """ASGI app exceptions must appear (type + message) in the HTTP 500 response."""
    api = BoltAPI()

    async def broken_asgi(scope, receive, send):
        raise exc_class(exc_msg)

    api.mount_asgi("/broken", broken_asgi)

    with TestClient(api) as client:
        response = client.get("/broken")

    assert response.status_code == 500
    for fragment in expected_fragments:
        assert fragment in response.text
