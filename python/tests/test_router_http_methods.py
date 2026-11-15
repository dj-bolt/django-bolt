"""Router registers each HTTP method decorator under its own method."""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, Router
from django_bolt.testing import TestClient

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


@pytest.mark.parametrize("method", METHODS)
def test_router_method_decorator_registers_its_method(method):
    router = Router(prefix="/items")

    @getattr(router, method)("/thing", status_code=202)
    async def handler() -> dict:
        return {"method": method}

    assert [route[0] for route in router.get_all_routes()] == [method.upper()]

    api = BoltAPI()
    api.include_router(router)

    with TestClient(api) as client:
        response = getattr(client, method)("/items/thing")
        assert response.status_code == 202, response.text
        if method != "head":
            assert response.json() == {"method": method}
        else:
            assert response.content == b""
