"""The parameters of a dependency bind like the parameters of a handler.

Each case runs with an async dependency on an async handler, and with a sync
dependency on a sync handler (the sync injector). The route also documents
the parameters and the body of its dependencies in its OpenAPI schema.
"""

from __future__ import annotations

from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI, Depends, UploadFile
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.param_functions import Body, Cookie, File, Form, Header, Query
from django_bolt.params import Param
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


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/sync/nested-async")
    def sync_nested_async_route(outer=Depends(sync_outer_of_async)):
        return outer

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
        headers={"X-Session-Token": "abc", "X-Api-Token": "t-1", "X-Api-Version": "v2", "Cookie": "theme=dark"},
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


class Item(msgspec.Struct):
    name: str
    count: int


def sync_item(item: Item) -> dict:
    return {"name": item.name, "count": item.count}


async def async_item(item: Item) -> dict:
    return {"name": item.name, "count": item.count}


def sync_form(name: Annotated[str, Form()], count: Annotated[int, Form()] = 0) -> dict:
    return {"name": name, "count": count}


async def async_form(name: Annotated[str, Form()], count: Annotated[int, Form()] = 0) -> dict:
    return {"name": name, "count": count}


UPLOADS: list[UploadFile] = []


def sync_upload(upload: Annotated[UploadFile, File(max_size=16)]) -> dict:
    UPLOADS.append(upload)
    return {"filename": upload.filename, "size": upload.size}


async def async_upload(upload: Annotated[UploadFile, File(max_size=16)]) -> dict:
    UPLOADS.append(upload)
    return {"filename": upload.filename, "size": upload.size}


def _body_api() -> BoltAPI:
    api = BoltAPI()

    @api.post("/async/body")
    async def async_body_route(item=Depends(async_item)):
        return item

    @api.post("/sync/body")
    def sync_body_route(item=Depends(sync_item)):
        return item

    @api.post("/async/form")
    async def async_form_route(values=Depends(async_form)):
        return values

    @api.post("/sync/form")
    def sync_form_route(values=Depends(sync_form)):
        return values

    @api.post("/async/upload")
    async def async_upload_route(values=Depends(async_upload)):
        return values

    @api.post("/sync/upload")
    def sync_upload_route(values=Depends(sync_upload)):
        return values

    @api.get("/documented")
    async def documented_route(
        values=Depends(async_values),
        outer=Depends(async_outer),
        tenant=Depends(async_required),
        same_tenant=Depends(sync_required),
    ):
        return values

    return api


@pytest.fixture(scope="module")
def body_client():
    with TestClient(_body_api()) as c:
        yield c


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_gets_the_json_body(body_client, mode):
    response = body_client.post(f"/{mode}/body", json={"name": "bolt", "count": 2})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "bolt", "count": 2}
    assert body_client.post(f"/{mode}/body", json={"name": "bolt"}).status_code == 422


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_gets_typed_form_fields(body_client, mode):
    response = body_client.post(f"/{mode}/form", data={"name": "bolt", "count": "5"})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "bolt", "count": 5}
    assert body_client.post(f"/{mode}/form", data={"count": "5"}).status_code == 422


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_gets_an_upload_within_its_constraints(body_client, mode):
    UPLOADS.clear()
    response = body_client.post(f"/{mode}/upload", files={"upload": ("a.txt", b"hello", "text/plain")})
    assert response.status_code == 200, response.text
    assert response.json() == {"filename": "a.txt", "size": 5}
    # Bolt closes the upload after the response, as for an upload of a handler.
    assert UPLOADS and UPLOADS[0]._file.closed is True

    too_big = body_client.post(f"/{mode}/upload", files={"upload": ("b.txt", b"x" * 64, "text/plain")})
    assert too_big.status_code == 422, too_big.text


def test_the_schema_documents_the_parameters_and_body_of_dependencies():
    schema = SchemaGenerator(_body_api(), OpenAPIConfig(title="Test", version="1")).generate()

    documented = schema.paths["/documented"].get
    locations = {(p.param_in, p.name) for p in documented.parameters}
    # From async_values, and once only although async_outer depends on it too.
    assert {("header", "X-Api-Token"), ("cookie", "theme"), ("query", "page")} <= locations
    # Two dependencies read the same header: it is documented one time.
    assert [p.name for p in documented.parameters].count("x-tenant") == 1
    assert len(documented.parameters) == len(locations)

    body = schema.paths["/async/body"].post.request_body
    assert body is not None and "application/json" in body.content

    form = schema.paths["/async/form"].post.request_body
    assert form is not None
    form_schema = form.content["multipart/form-data"].schema
    assert set(form_schema.properties) == {"name", "count"}
    assert form_schema.required == ["name"]

    upload = schema.paths["/async/upload"].post.request_body
    assert upload is not None and "upload" in upload.content["multipart/form-data"].schema.properties


def test_the_schema_names_a_header_as_bolt_reads_it():
    api = BoltAPI()

    @api.get("/headers")
    async def headers_route(x_request_id: Annotated[str, Header()], token: Annotated[str, Header(alias="X-Token")]):
        return {}

    parameters = (
        SchemaGenerator(api, OpenAPIConfig(title="Test", version="1")).generate().paths["/headers"].get.parameters
    )
    assert {(p.param_in, p.name) for p in parameters} == {("header", "x-request-id"), ("header", "X-Token")}


class ItemName(msgspec.Struct):
    name: str


def sync_item_object(item: Item) -> Item:
    return item


async def async_item_object(item: Item) -> Item:
    return item


def sync_item_name(item: Annotated[ItemName, Body()]) -> str:
    return item.name


async def async_item_name(item: Annotated[ItemName, Body()]) -> str:
    return item.name


@pytest.fixture(scope="module")
def shared_body_client():
    api = BoltAPI()

    @api.post("/async/same-type")
    async def async_same_type(item: Item, from_dependency=Depends(async_item_object)):
        return {"same_object": item is from_dependency, "count": item.count}

    @api.post("/sync/same-type")
    def sync_same_type(item: Item, from_dependency=Depends(sync_item_object)):
        return {"same_object": item is from_dependency, "count": item.count}

    @api.post("/async/other-type")
    async def async_other_type(item: Item, name=Depends(async_item_name)):
        return {"name": name, "count": item.count}

    @api.post("/sync/other-type")
    def sync_other_type(item: Item, name=Depends(sync_item_name)):
        return {"name": name, "count": item.count}

    with TestClient(api) as c:
        yield c


@pytest.mark.parametrize("mode", MODES)
def test_a_handler_and_a_dependency_share_one_decode_of_the_same_body_type(shared_body_client, mode):
    response = shared_body_client.post(f"/{mode}/same-type", json={"name": "bolt", "count": 3})
    assert response.status_code == 200, response.text
    assert response.json() == {"same_object": True, "count": 3}
    assert shared_body_client.post(f"/{mode}/same-type", json={"name": "bolt"}).status_code == 422


@pytest.mark.parametrize("mode", MODES)
def test_a_handler_and_a_dependency_decode_other_body_types_separately(shared_body_client, mode):
    response = shared_body_client.post(f"/{mode}/other-type", json={"name": "bolt", "count": 3})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "bolt", "count": 3}


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_binds_by_the_source_of_each_route(mode):
    """One dependency on two routes: a path parameter on one, a query parameter on the other."""
    api = BoltAPI()

    def sync_item_id(item_id: int) -> int:
        return item_id

    async def async_item_id(item_id: int) -> int:
        return item_id

    dependency = async_item_id if mode == "async" else sync_item_id

    @api.get("/by-query")
    async def by_query(resolved=Depends(dependency)):
        return {"item_id": resolved}

    @api.get("/by-path/{item_id}")
    async def by_path(resolved=Depends(dependency)):
        return {"item_id": resolved}

    with TestClient(api) as client:
        assert client.get("/by-query?item_id=7").json() == {"item_id": 7}
        response = client.get("/by-path/8")
        assert response.status_code == 200, response.text
        assert response.json() == {"item_id": 8}


def test_a_dependency_with_another_type_for_a_handler_wire_key_fails_at_registration():
    """Rust converts one header value one time, so the handler and the dependency cannot both get their type."""
    api = BoltAPI()

    def count_header(x_count: Annotated[int, Header()]) -> int:
        return x_count

    with pytest.raises(TypeError) as exc_info:

        @api.get("/clash")
        async def clash(raw: Annotated[str, Header(alias="x-count")], count=Depends(count_header)):
            return {"raw": raw, "count": count}

    assert "x-count" in str(exc_info.value)


def test_a_dependency_with_the_same_type_for_a_handler_wire_key_is_allowed():
    api = BoltAPI()

    def count_header(x_count: Annotated[int, Header()]) -> int:
        return x_count

    @api.get("/shared")
    async def shared(raw: Annotated[int, Header(alias="x-count")], count=Depends(count_header)):
        return {"raw": raw, "count": count}

    with TestClient(api) as client:
        response = client.get("/shared", headers={"X-Count": "4"})
        assert response.status_code == 200, response.text
        assert response.json() == {"raw": 4, "count": 4}


def test_a_dependency_with_another_type_for_a_handler_name_fails_at_registration():
    """The same Python name in the handler and the dependency is one wire key too."""
    api = BoltAPI()

    def paging(page: Annotated[int, Query()]) -> int:
        return page

    with pytest.raises(TypeError) as exc_info:

        @api.get("/clash")
        async def clash(page: Annotated[str, Query()], paged=Depends(paging)):
            return {"page": page, "paged": paged}

    assert "page" in str(exc_info.value)


def test_a_dependency_with_a_handler_name_in_another_source_gets_its_type():
    """A handler query ``token`` and a dependency header ``token`` are two wire keys."""
    api = BoltAPI()

    def header_token(token: Annotated[int, Header()]) -> int:
        return token

    @api.get("/two-sources")
    async def two_sources(token: Annotated[str, Query()], header=Depends(header_token)):
        return {"query": token, "header": header}

    with TestClient(api) as client:
        response = client.get("/two-sources?token=abc", headers={"token": "7"})
        assert response.status_code == 200, response.text
        assert response.json() == {"query": "abc", "header": 7}

    parameters = (
        SchemaGenerator(api, OpenAPIConfig(title="Test", version="1")).generate().paths["/two-sources"].get.parameters
    )
    assert {(p.param_in, p.name) for p in parameters} == {("query", "token"), ("header", "token")}


def _unsupported_source(value: Annotated[str, Param(source="nowhere")] = "") -> str:
    return value


def _outer_of_unsupported(value: str = Depends(_unsupported_source)) -> str:
    return value


def test_a_nested_dependency_that_fails_to_compile_fails_each_registration():
    """The error leaves no part of the outer binding in the cache of the API.

    Before, the second registration used the plan of the outer dependency
    without its nested binding, and each request answered 500.
    """
    api = BoltAPI()

    for _ in range(2):
        with pytest.raises(TypeError, match="unsupported source"):

            @api.get("/items")
            def items(value: str = Depends(_outer_of_unsupported)):
                return {"value": value}


async def cycle_first(value: Annotated[int, Depends(cycle_second)]) -> int:
    return value


async def cycle_second(value: Annotated[int, Depends(cycle_first)]) -> int:
    return value


def test_a_cycle_of_async_dependencies_fails_at_registration():
    """Before, the route registered, and each request recursed until it failed with a 500."""
    api = BoltAPI()

    with pytest.raises(RecursionError):

        @api.get("/cycle")
        async def cycle(value=Depends(cycle_first)):
            return {"value": value}
