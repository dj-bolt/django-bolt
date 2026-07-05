"""Smoke test for the real-module fixtures under ``apps/`` (issue #218).

Importing every app module on all platforms guards the fixtures that the
subprocess tests can only exercise on Linux (reload, multiprocess) or behind an
artifact path. It catches a broken module (syntax error, bad import, missing
``api``) as a fast in-process failure instead of an opaque subprocess startup
error in CI.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

from django_bolt import BoltAPI

from . import apps
from .apps import app_module

_MODULE_NAMES = sorted(info.name for info in pkgutil.iter_modules(apps.__path__) if not info.name.startswith("_"))


def test_apps_package_has_modules():
    # Guards the discovery itself — an empty list would make the parametrized
    # test below vacuously pass.
    assert _MODULE_NAMES, "no app modules discovered under apps/"


@pytest.mark.parametrize("module_name", _MODULE_NAMES)
def test_app_module_exposes_populated_api(module_name):
    module = importlib.import_module(f"{apps.__name__}.{module_name}")
    assert app_module(module_name).split(":")[0] == module.__name__
    api = getattr(module, "api", None)
    assert isinstance(api, BoltAPI), f"{module_name} must define `api = BoltAPI()`"
    route_count = len(api._routes) + len(getattr(api, "_websocket_routes", []))
    assert route_count >= 2, f"{module_name} must register a health route plus at least one real route"
