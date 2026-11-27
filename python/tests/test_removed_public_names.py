"""Public names that no Bolt code called are removed (issue #873). These tests keep them out."""

from __future__ import annotations

import importlib

import pytest

from django_bolt import BoltAPI
from django_bolt.admin.routes import AdminRouteRegistrar
from django_bolt.responses import HTML, JSON, PlainText, Response

REMOVED = [
    ("django_bolt.auth", "register_auth_backend"),
    ("django_bolt.auth", "get_registered_backend"),
    ("django_bolt.auth", "load_user"),
    ("django_bolt.auth.user_loader", "register_auth_backend"),
    ("django_bolt.auth.user_loader", "get_registered_backend"),
    ("django_bolt.auth.user_loader", "load_user"),
    ("django_bolt.auth.user_loader", "load_user_sync"),
    ("django_bolt.auth.user_loader", "default_django_user_loader"),
    ("django_bolt.auth.user_loader", "default_django_user_aloader"),
    ("django_bolt.auth.user_loader", "DEFAULT_USER_LOADERS"),
    ("django_bolt.websocket", "run_websocket_handler"),
    ("django_bolt.websocket", "is_websocket_handler"),
    ("django_bolt.websocket.handlers", "run_websocket_handler"),
    ("django_bolt.websocket.handlers", "is_websocket_handler"),
    ("django_bolt.admin", "get_admin_route_patterns"),
    ("django_bolt.admin", "get_admin_info"),
    ("django_bolt.admin.admin_detection", "get_admin_route_patterns"),
    ("django_bolt.admin.admin_detection", "get_admin_info"),
    ("django_bolt.admin.admin_detection", "get_static_url_prefix"),
    ("django_bolt.request", "is_request"),
    ("django_bolt.openapi.spec.schema", "SchemaDataContainer"),
]


@pytest.mark.parametrize(("module_name", "name"), REMOVED)
def test_the_name_is_not_in_the_module(module_name, name):
    module = importlib.import_module(module_name)

    assert name not in getattr(module, "__all__", ())
    assert not hasattr(module, name)


def test_response_has_no_to_bytes():
    assert not hasattr(Response({"a": 1}), "to_bytes")


def test_the_serialized_response_types_keep_to_bytes():
    assert JSON({"a": 1}).to_bytes() == b'{"a":1}'
    assert PlainText("a").to_bytes() == b"a"
    assert HTML("<p>a</p>").to_bytes() == b"<p>a</p>"


@pytest.mark.parametrize("kwargs", [{"host": "127.0.0.1"}, {"port": 8000}])
def test_register_routes_takes_no_host_or_port(kwargs):
    with pytest.raises(TypeError):
        AdminRouteRegistrar(BoltAPI()).register_routes(**kwargs)
