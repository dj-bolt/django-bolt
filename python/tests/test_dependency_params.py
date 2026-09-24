"""The parameters of a dependency bind like the parameters of a handler.

Each case runs with an async dependency on an async handler, and with a sync
dependency on a sync handler (the sync injector).
"""

from __future__ import annotations

from typing import Annotated

import pytest

from django_bolt import BoltAPI, Depends
from django_bolt.param_functions import Cookie, Header, Query
from django_bolt.testing import TestClient


def _values(
    x_session_token: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Header(alias="X-Api-Token")] = None,
    x_api_version: Annotated[str, Header()] = "v1",
    theme: Annotated[str, Cookie()] = "light",
    page: Annotated[int, Query()] = 1,
) -> dict:
    return {
        "session": x_session_token,
        "token": token,
        "version": x_api_version,
        "theme": theme,
        "page": page,
    }


def sync_values(
    x_session_token: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Header(alias="X-Api-Token")] = None,
    x_api_version: Annotated[str, Header()] = "v1",
    theme: Annotated[str, Cookie()] = "light",
    page: Annotated[int, Query()] = 1,
) -> dict:
    return _values(x_session_token, token, x_api_version, theme, page)


async def async_values(
    x_session_token: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Header(alias="X-Api-Token")] = None,
    x_api_version: Annotated[str, Header()] = "v1",
    theme: Annotated[str, Cookie()] = "light",
    page: Annotated[int, Query()] = 1,
) -> dict:
    return _values(x_session_token, token, x_api_version, theme, page)


def sync_required(x_tenant: Annotated[str, Header()]) -> str:
    return x_tenant


async def async_required(x_tenant: Annotated[str, Header()]) -> str:
    return x_tenant


def sync_outer(values: dict = Depends(sync_values)) -> dict:
    return {"nested": values["version"]}


async def async_outer(values: dict = Depends(async_values)) -> dict:
    return {"nested": values["version"]}


def sync_outer_of_async(values: dict = Depends(async_values)) -> dict:
    return {"nested": values["version"]}


def page_number(page: Annotated[int, Query()] = 1) -> int:
    return page


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/sync/nested-async")
    def sync_nested_async_route(outer=Depends(sync_outer_of_async)):
        return outer

    @api.get("/page-as-text")
    def page_as_text_route(page: str, number=Depends(page_number)):
        return {"page": page, "number": number}

    @api.get("/async/values")
    async def async_values_route(values=Depends(async_values)):
        return values

    @api.get("/sync/values")
    def sync_values_route(values=Depends(sync_values)):
        return values

    @api.get("/async/required")
    async def async_required_route(tenant=Depends(async_required)):
        return {"tenant": tenant}

    @api.get("/sync/required")
    def sync_required_route(tenant=Depends(sync_required)):
        return {"tenant": tenant}

    @api.get("/async/nested")
    async def async_nested_route(outer=Depends(async_outer)):
        return outer

    @api.get("/sync/nested")
    def sync_nested_route(outer=Depends(sync_outer)):
        return outer

    with TestClient(api) as c:
        yield c


MODES = ("async", "sync")
DEFAULTS = {"session": None, "token": None, "version": "v1", "theme": "light", "page": 1}


@pytest.mark.parametrize("mode", MODES)
def test_missing_optional_values_get_their_defaults(client, mode):
    response = client.get(f"/{mode}/values")
    assert response.status_code == 200, response.text
    assert response.json() == DEFAULTS


@pytest.mark.parametrize("mode", MODES)
def test_values_bind_by_header_name_alias_cookie_and_query(client, mode):
    response = client.get(
        f"/{mode}/values?page=3",
        headers={"X-Session-Token": "abc", "X-Api-Token": "t-1", "X-Api-Version": "v2"},
        cookies={"theme": "dark"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"session": "abc", "token": "t-1", "version": "v2", "theme": "dark", "page": 3}


@pytest.mark.parametrize("mode", MODES)
def test_a_missing_required_header_is_a_client_error(client, mode):
    response = client.get(f"/{mode}/required")
    assert response.status_code == 422, response.text
    assert client.get(f"/{mode}/required", headers={"X-Tenant": "acme"}).json() == {"tenant": "acme"}


@pytest.mark.parametrize("mode", MODES)
def test_a_nested_dependency_gets_its_value(client, mode):
    response = client.get(f"/{mode}/nested", headers={"X-Api-Version": "v3"})
    assert response.status_code == 200, response.text
    assert response.json() == {"nested": "v3"}


def test_a_sync_handler_gets_a_sync_dependency_of_an_async_dependency(client):
    response = client.get("/sync/nested-async", headers={"X-Api-Version": "v4"})
    assert response.status_code == 200, response.text
    assert response.json() == {"nested": "v4"}


@pytest.mark.parametrize("mode", MODES)
def test_a_query_value_of_the_wrong_type_is_a_client_error(client, mode):
    assert client.get(f"/{mode}/values?page=abc").status_code == 422


def test_a_handler_field_keeps_its_type_when_a_dependency_has_the_same_name(client):
    response = client.get("/page-as-text?page=7")
    assert response.status_code == 200, response.text
    assert response.json() == {"page": "7", "number": "7"}
