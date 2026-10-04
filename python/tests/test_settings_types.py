"""Django settings with the wrong type stop startup.

Bolt reads its settings once at startup. A missing setting gives the default.
A present setting with the wrong type raises ``ImproperlyConfigured`` that
names the setting. Before, the wrong type silently became the default: for
example, ``BOLT_MAX_UPLOAD_SIZE = "10485760"`` gave a 413 for a 2 MB upload.

``TestClient`` reads the settings with the reader of ``runbolt``, so these
tests cover the production reader.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_bolt import BoltAPI, FileSize, UploadFile
from django_bolt.params import File
from django_bolt.testing import TestClient

TWO_MB = 2 * 1024 * 1024


def _upload_api() -> BoltAPI:
    api = BoltAPI()

    @api.post("/upload")
    async def upload(avatar: UploadFile = File()):
        return {"size": avatar.size}

    return api


def _post_two_mb(client: TestClient):
    return client.post("/upload", files={"avatar": ("a.bin", b"x" * TWO_MB, "application/octet-stream")})


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("BOLT_MAX_UPLOAD_SIZE", "10", "an int of 0 or more"),
        ("BOLT_MAX_UPLOAD_SIZE", True, "an int of 0 or more"),
        ("BOLT_MAX_UPLOAD_SIZE", -1, "an int of 0 or more"),
        ("BOLT_MAX_UPLOAD_SIZE", 1.5, "an int of 0 or more"),
        ("BOLT_MAX_HEADER_SIZE", "8192", "an int of 0 or more"),
        ("DEBUG", "False", "a bool"),
        ("DEBUG", 1, "a bool"),
        ("BOLT_ASGI_MOUNT_TIMEOUT", "30", "a number more than 0"),
        ("BOLT_ASGI_MOUNT_TIMEOUT", True, "a number more than 0"),
        ("BOLT_ASGI_MOUNT_TIMEOUT", 0, "a number more than 0"),
        ("BOLT_ASGI_MOUNT_TIMEOUT", float("nan"), "a number more than 0"),
        ("CORS_ALLOWED_ORIGINS", "https://a.example", "a list of str"),
        ("CORS_ALLOWED_ORIGINS", ["https://a.example", 1], "a list of str"),
        ("CORS_ALLOWED_ORIGIN_REGEXES", r"^https://.*\.example$", "a list of str"),
        ("CORS_ALLOW_ALL_ORIGINS", "True", "a bool"),
        ("CORS_ALLOW_CREDENTIALS", 1, "a bool"),
        ("CORS_ALLOW_METHODS", "GET", "a list of str"),
        ("CORS_ALLOW_HEADERS", "x-token", "a list of str"),
        ("CORS_EXPOSE_HEADERS", "x-total", "a list of str"),
        ("CORS_PREFLIGHT_MAX_AGE", "3600", "an int from 0 to 4294967295"),
        ("CORS_PREFLIGHT_MAX_AGE", -1, "an int from 0 to 4294967295"),
        ("BOLT_MAX_SYNC_STREAMING_THREADS", "10", "an int of 1 or more"),
        ("BOLT_MAX_SYNC_STREAMING_THREADS", 0, "an int of 1 or more"),
        ("BOLT_WS_MAX_CONNECTIONS", "2", "an int of 0 or more"),
        ("BOLT_WS_CHANNEL_SIZE", "100", "an int of 1 or more"),
        ("BOLT_WS_CHANNEL_SIZE", 0, "an int of 1 or more"),
        ("BOLT_WS_HEARTBEAT_INTERVAL", "5", "an int of 1 or more"),
        ("BOLT_WS_HEARTBEAT_INTERVAL", 0, "an int of 1 or more"),
        ("BOLT_WS_CLIENT_TIMEOUT", 2.5, "an int of 0 or more"),
        ("BOLT_WS_MAX_MESSAGE_SIZE", "1048576", "an int of 0 or more"),
    ],
)
def test_wrong_type_stops_startup(name, value, expected):
    # Define the routes before the override: the route decorator reads
    # BOLT_MAX_UPLOAD_SIZE too, and this test is about the server reader.
    api = _upload_api()

    with override_settings(**{name: value}), pytest.raises(ImproperlyConfigured) as excinfo:
        TestClient(api)

    message = str(excinfo.value)
    assert message.startswith(f"{name} must be {expected}, got {type(value).__name__}")


@pytest.mark.parametrize("name", ["STATIC_URL", "MEDIA_URL"])
def test_error_inside_a_setting_is_not_a_missing_setting(name):
    """Django adds the script prefix to STATIC_URL and MEDIA_URL with str methods.

    For an int, Django raises AttributeError while it reads the setting. That
    error is not a missing setting, so it must not turn into the default.
    """
    api = _upload_api()

    with override_settings(**{name: 8000}), pytest.raises(ImproperlyConfigured) as excinfo:
        TestClient(api)

    assert str(excinfo.value).startswith(f"Cannot read the Django setting {name}: ")
    assert isinstance(excinfo.value.__cause__, AttributeError)


@pytest.mark.parametrize("name", ["BOLT_MAX_UPLOAD_SIZE", "BOLT_MEMORY_SPOOL_THRESHOLD"])
def test_wrong_type_stops_route_registration(name):
    """The route decorator copies the upload settings into the route metadata."""
    api = BoltAPI()

    with override_settings(**{name: "10"}), pytest.raises(ImproperlyConfigured) as excinfo:

        @api.post("/upload")
        async def upload(avatar: UploadFile = File()):
            return {"size": avatar.size}

    assert str(excinfo.value).startswith(f"{name} must be an int of 0 or more, got str")


@pytest.mark.parametrize("key", ["max_upload_size", "memory_spool_threshold"])
def test_wrong_type_in_route_metadata_stops_registration(key):
    """Rust checks the upload sizes that the route metadata carries to it."""
    api = _upload_api()
    handler_id = next(handler_id for _method, path, handler_id, _fn in api._routes if path == "/upload")
    api._handler_middleware[handler_id][key] = "10"

    # Registration can wrap the TypeError of the parser in a ValueError.
    with pytest.raises((TypeError, ValueError), match=rf"'{key}' must be an int of 0 or more, got str"):
        TestClient(api)


def test_int_enum_upload_size_is_used():
    """An IntEnum such as FileSize is an int, so it is a valid size."""
    with override_settings(BOLT_MAX_UPLOAD_SIZE=FileSize.MB_10):
        api = _upload_api()
        with TestClient(api) as client:
            response = _post_two_mb(client)

    assert response.status_code == 200
    assert response.json() == {"size": TWO_MB}


@pytest.mark.parametrize("deleted", [False, True], ids=["never-set", "deleted"])
def test_missing_upload_size_uses_the_default(settings, deleted):
    """Without BOLT_MAX_UPLOAD_SIZE the limit is 1 MB, so 2 MB gets 413.

    A setting that a test deletes with override_settings is missing too.
    """
    assert not hasattr(settings, "BOLT_MAX_UPLOAD_SIZE")
    if deleted:
        del settings.BOLT_MAX_UPLOAD_SIZE
    api = _upload_api()

    with TestClient(api) as client:
        response = _post_two_mb(client)

    assert response.status_code == 413


ENV_CASES = [
    ("DJANGO_BOLT_MAX_PARAM_LENGTH", "abc", "an int from 1 to 1048576"),
    ("DJANGO_BOLT_MAX_PARAM_LENGTH", "0", "an int from 1 to 1048576"),
    ("DJANGO_BOLT_MAX_PARAM_LENGTH", "2000000", "an int from 1 to 1048576"),
    ("DJANGO_BOLT_MAX_SYNC_STREAMING_THREADS", "0", "an int of 1 or more"),
    ("DJANGO_BOLT_STREAM_SYNC_BATCH_SIZE", "0", "an int of 1 or more"),
    ("DJANGO_BOLT_STREAM_CHANNEL_CAPACITY", "lots", "an int of 1 or more"),
    ("DJANGO_BOLT_WS_MAX_CONNECTIONS", "-1", "an int of 0 or more"),
    ("DJANGO_BOLT_WS_CHANNEL_SIZE", "0", "an int of 1 or more"),
    ("DJANGO_BOLT_WS_HEARTBEAT_INTERVAL", "0", "an int of 1 or more"),
    ("DJANGO_BOLT_WS_CLIENT_TIMEOUT", "1.5", "an int of 0 or more"),
    ("DJANGO_BOLT_WS_MAX_MESSAGE_SIZE", "1MB", "an int of 0 or more"),
    ("DJANGO_BOLT_LANE_IDLE_SECONDS", "0", "a number more than 0"),
    ("DJANGO_BOLT_LANE_IDLE_SECONDS", "soon", "a number more than 0"),
    ("DJANGO_BOLT_EXECUTOR_THREADS", "0", "an int of 1 or more"),
    ("DJANGO_BOLT_ORM_THREADS", "many", "an int of 1 or more"),
]


@pytest.mark.parametrize(("name", "value", "expected"), ENV_CASES)
def test_invalid_environment_variable_stops_startup(monkeypatch, name, value, expected):
    api = _upload_api()
    monkeypatch.setenv(name, value)

    with pytest.raises(ImproperlyConfigured) as excinfo:
        TestClient(api)

    assert str(excinfo.value) == f"{name} must be {expected}, got '{value}'."


def test_empty_environment_variable_gives_the_default(monkeypatch):
    """An empty variable is not set, as in a template that leaves a value out."""
    for name in {name for name, _value, _expected in ENV_CASES}:
        monkeypatch.setenv(name, "")
    api = _upload_api()

    with TestClient(api) as client:
        response = client.post("/upload", files={"avatar": ("a.bin", b"x", "application/octet-stream")})

    assert response.status_code == 200


def test_wrong_runtime_debug_is_reported(capfd):
    """A request reads DEBUG again when Rust builds an error response.

    Startup rejects a wrong DEBUG. A test can still change DEBUG later, so the
    request writes a warning and keeps the DEBUG value of startup.
    """
    api = BoltAPI()

    def broken(scope, receive, send):
        # A plain function: the call fails before Bolt gets a coroutine.
        raise RuntimeError("boom")

    api.mount_asgi("/broken", broken)

    with override_settings(DEBUG=False):
        client = TestClient(api)

    with client, override_settings(DEBUG="yes"):
        response = client.get("/broken")

    assert response.status_code == 500
    assert "DEBUG must be a bool, got str" in capfd.readouterr().err
