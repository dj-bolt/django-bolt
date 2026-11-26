"""Public names that no Bolt code calls warn on use, and still work (issue #873)."""

from __future__ import annotations

import asyncio

import pytest

from django_bolt.auth import APIKeyAuthentication, get_registered_backend, load_user, register_auth_backend, user_loader
from django_bolt.auth.user_loader import default_django_user_aloader, default_django_user_loader, load_user_sync

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
