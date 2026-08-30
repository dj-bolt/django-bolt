from __future__ import annotations

import pytest

from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.testing import TestClient

from .apps import app_module, include_in_schema_layers

HIDDEN = ("/route-hidden", "/view-hidden", "/internal/secret")
SHOWN = ("/public", "/internal/shown")


@pytest.mark.server_integration
def test_layered_include_in_schema_over_real_server(make_server_project):
    project = make_server_project(api_module=app_module("include_in_schema_layers"))

    with project.start() as server:
        spec = server.get("/docs/openapi.json").json()
        served = {path: server.get(path).status_code for path in HIDDEN + SHOWN}

    paths = set(spec["paths"])
    assert set(SHOWN) <= paths
    assert not paths & set(HIDDEN)
    assert served == dict.fromkeys(HIDDEN + SHOWN, 200)


def test_layered_include_in_schema_in_process():
    """The `/docs` routes only exist under `runbolt`; read the schema directly here."""
    api = include_in_schema_layers.api
    paths = set(SchemaGenerator(api, OpenAPIConfig(title="t", version="1")).generate().to_schema()["paths"])
    with TestClient(api) as client:
        served = {path: client.get(path).status_code for path in HIDDEN + SHOWN}

    assert set(SHOWN) <= paths
    assert not paths & set(HIDDEN)
    assert served == dict.fromkeys(HIDDEN + SHOWN, 200)
