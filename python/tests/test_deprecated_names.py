"""Public names that no Bolt code calls warn on use, and still work (issue #873)."""

from __future__ import annotations

import asyncio

import pytest
from django.test import override_settings

from django_bolt import BoltAPI
from django_bolt.admin import get_admin_info, get_admin_route_patterns
from django_bolt.admin.admin_detection import get_static_url_prefix
from django_bolt.admin.routes import AdminRouteRegistrar
from django_bolt.auth import APIKeyAuthentication, get_registered_backend, load_user, register_auth_backend, user_loader
from django_bolt.auth.user_loader import default_django_user_aloader, default_django_user_loader, load_user_sync
from django_bolt.openapi.spec.schema import SchemaDataContainer
from django_bolt.request import is_request
from django_bolt.responses import Response
from django_bolt.websocket import is_websocket_handler, mark_websocket_handler, run_websocket_handler

REMOVAL = "Bolt removes it in the next minor release"


@pytest.fixture
def backend_registry(monkeypatch):
    """Give the test an empty registry of auth backends, and restore the real one after."""
    monkeypatch.setattr(user_loader, "_auth_backend_registry", {})
    monkeypatch.setattr(user_loader, "_resolved_loader_registry", {})


def test_the_user_loading_functions_warn_and_still_work(backend_registry):
    backend = APIKeyAuthentication(api_keys={"k"})

    with pytest.warns(DeprecationWarning, match=rf"register_auth_backend\(\) is deprecated. {REMOVAL}"):
        register_auth_backend("api_key", backend)
    with pytest.warns(DeprecationWarning, match=rf"get_registered_backend\(\) is deprecated. {REMOVAL}"):
        assert get_registered_backend("api_key") is backend
    with pytest.warns(DeprecationWarning, match=r"load_user\(\) is deprecated.* Use `await request.auser\(\)`"):
        assert asyncio.run(load_user("7", "api_key", {})) is None
    with pytest.warns(DeprecationWarning, match=r"load_user_sync\(\) is deprecated.* Use `request.user`"):
        assert load_user_sync("7", "api_key", {}) is None


@pytest.mark.django_db
def test_the_default_user_loaders_warn_and_still_work():
    with pytest.warns(DeprecationWarning, match=rf"default_django_user_loader\(\) is deprecated. {REMOVAL}"):
        assert default_django_user_loader("999999", None) is None
    with pytest.warns(DeprecationWarning, match=rf"default_django_user_aloader\(\) is deprecated. {REMOVAL}"):
        assert asyncio.run(default_django_user_aloader("999999", None)) is None
    with pytest.warns(DeprecationWarning, match=rf"DEFAULT_USER_LOADERS is deprecated. {REMOVAL}"):
        sync_loader, async_loader = user_loader.DEFAULT_USER_LOADERS
    assert sync_loader("999999", None) is None
    assert asyncio.run(async_loader("999999", None)) is None


def test_an_unknown_attribute_of_the_user_loader_module_raises_attribute_error():
    with pytest.raises(AttributeError, match="NO_SUCH_NAME"):
        _ = user_loader.NO_SUCH_NAME


def test_the_websocket_handler_utilities_warn_and_still_work():
    received = []

    async def handler(websocket, room: str):
        received.append((websocket, room))

    with pytest.warns(DeprecationWarning, match=rf"is_websocket_handler\(\) is deprecated. {REMOVAL}"):
        assert is_websocket_handler(handler) is False
    mark_websocket_handler(handler)
    with pytest.warns(DeprecationWarning, match=rf"is_websocket_handler\(\) is deprecated. {REMOVAL}"):
        assert is_websocket_handler(handler) is True

    with pytest.warns(DeprecationWarning, match=r"run_websocket_handler\(\) is deprecated.* `@api.websocket\(\)`"):
        asyncio.run(run_websocket_handler(handler, "socket", {"room": "blue"}))
    assert received == [("socket", "blue")]


@override_settings(STATIC_URL="/assets/")
def test_the_admin_helpers_warn_and_still_work():
    with pytest.warns(DeprecationWarning, match=rf"get_admin_route_patterns\(\) is deprecated. {REMOVAL}"):
        assert isinstance(get_admin_route_patterns(), list)
    with pytest.warns(DeprecationWarning, match=r"get_admin_info\(\) is deprecated.* Use `is_admin_installed\(\)`"):
        info = get_admin_info()
    assert info["static_url"] == "assets"
    with pytest.warns(DeprecationWarning, match=r"get_static_url_prefix\(\) is deprecated.* `settings.STATIC_URL`"):
        assert get_static_url_prefix() == "assets"


def test_the_host_and_port_of_register_routes_warn():
    with pytest.warns(DeprecationWarning, match=r"The host and port arguments of register_routes\(\) are deprecated"):
        AdminRouteRegistrar(BoltAPI()).register_routes("127.0.0.1", 8000)
    with pytest.warns(DeprecationWarning, match=r"The host and port arguments of register_routes\(\) are deprecated"):
        AdminRouteRegistrar(BoltAPI()).register_routes(port=8000)


def test_register_routes_with_no_arguments_does_not_warn():
    # pytest.ini turns each warning into an error.
    AdminRouteRegistrar(BoltAPI()).register_routes()


def test_is_request_warns_and_still_works():
    with pytest.warns(DeprecationWarning, match=rf"is_request\(\) is deprecated. {REMOVAL}"):
        assert is_request(object()) is False


def test_response_to_bytes_warns_and_still_works():
    with pytest.warns(DeprecationWarning, match=rf"Response.to_bytes\(\) is deprecated. {REMOVAL}"):
        assert Response({"a": 1}).to_bytes() == b'{"a":1}'


def test_schema_data_container_warns_and_still_works():
    with pytest.warns(DeprecationWarning, match=rf"SchemaDataContainer is deprecated. {REMOVAL}"):
        schema = SchemaDataContainer(data_container=dict)
    assert schema.data_container is dict
