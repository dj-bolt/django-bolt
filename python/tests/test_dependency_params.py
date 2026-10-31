"""The parameters of a dependency bind like the parameters of a handler.

Each case runs with an async dependency on an async handler, and with a sync
dependency on a sync handler (the sync injector). The route also documents
the parameters and the body of its dependencies in its OpenAPI schema.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI, Depends, UploadFile, ViewSet
from django_bolt._kwargs.model import compile_binder
from django_bolt.dependencies import compile_dependency
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.param_functions import Body, Cookie, File, Form, Header, Path, Query
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


# Function scope: each test registers its routes, so a mutation tool sees
# each test reach the request-time closures of the extractors.
@pytest.fixture
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


@pytest.fixture
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


@pytest.fixture
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


# A handler with a request parameter and another parameter is analyzed for the
# request parts it reads. A request that the handler hands on (to a callable, a
# name, a container or an attribute) can be read there, so the handler must get
# every part of it. Before, it got an empty body.


def _current_user() -> dict:
    return {"id": 1}


def _read_body(request) -> bytes:
    return request.body


def _read_context_body(context: dict) -> bytes:
    return context["request"].body


def _escape_api() -> BoltAPI:
    api = BoltAPI()

    @api.post("/positional")
    async def positional(request, user: Annotated[dict, Depends(_current_user)]):
        return {"body": _read_body(request).decode(), "user": user["id"]}

    @api.post("/keyword")
    async def keyword(request, user: Annotated[dict, Depends(_current_user)]):
        return {"body": _read_body(request=request).decode(), "user": user["id"]}

    @api.post("/sync")
    def sync(request, user: Annotated[dict, Depends(_current_user)]):
        return {"body": _read_body(request).decode(), "user": user["id"]}

    @api.post("/alias")
    async def alias(request, user: Annotated[dict, Depends(_current_user)]):
        req = request
        return {"body": _read_body(req).decode(), "user": user["id"]}

    @api.post("/container")
    async def container(request, user: Annotated[dict, Depends(_current_user)]):
        return {"body": _read_context_body({"request": request}).decode(), "user": user["id"]}

    @api.post("/attribute")
    async def attribute(request, user: Annotated[dict, Depends(_current_user)]):
        holder = SimpleNamespace()
        holder.request = request
        return {"body": _read_body(holder.request).decode(), "user": user["id"]}

    @api.post("/subscript")
    async def subscript(request, user: Annotated[dict, Depends(_current_user)]):
        # A key known only at run time: the analysis cannot tell the part.
        key = "body"
        return {"body": request[key].decode(), "user": user["id"]}

    @api.post("/get-key")
    async def get_key(request, user: Annotated[dict, Depends(_current_user)]):
        key = "body"
        return {"body": request.get(key).decode(), "user": user["id"]}

    @api.post("/path/{user_id}")
    async def path_param(request, user_id: int):
        req = request
        return {"body": _read_body(req).decode(), "user": user_id}

    class Base(ViewSet):
        async def create(self, request):
            return {"body": request.body.decode()}

    @api.viewset("/items")
    class Items(Base):
        async def create(self, request, user: Annotated[dict, Depends(_current_user)]):
            return await super().create(request)

    return api


@pytest.mark.parametrize(
    "path",
    ["/positional", "/keyword", "/sync", "/alias", "/container", "/attribute", "/subscript", "/get-key", "/path/1"],
)
def test_a_request_handed_on_keeps_its_body(path):
    with TestClient(_escape_api()) as client:
        response = client.post(path, content=b"payload")
    assert response.status_code == 200
    assert response.json() == {"body": "payload", "user": 1}


def test_a_viewset_method_that_delegates_to_super_keeps_the_body():
    with TestClient(_escape_api()) as client:
        response = client.post("/items", content=b"payload")
    assert response.status_code == 201
    assert response.json() == {"body": "payload"}


# The cache of dependencies lives for one request. A dependency that many
# parameters need runs one time in that request, and again in the next request.

_TICKETS = itertools.count(1)
TICKET_CALLS: list[int] = []


def sync_ticket() -> int:
    TICKET_CALLS.append(1)
    return next(_TICKETS)


async def async_ticket() -> int:
    TICKET_CALLS.append(1)
    return next(_TICKETS)


def sync_wrap_a(ticket: int = Depends(sync_ticket)) -> dict:
    return {"who": "a", "ticket": ticket}


def sync_wrap_b(ticket: int = Depends(sync_ticket)) -> dict:
    return {"who": "b", "ticket": ticket}


async def async_wrap_a(ticket: int = Depends(async_ticket)) -> dict:
    return {"who": "a", "ticket": ticket}


async def async_wrap_b(ticket: int = Depends(async_ticket)) -> dict:
    return {"who": "b", "ticket": ticket}


def sync_pair(
    first: int = Depends(sync_ticket, use_cache=False),
    second: int = Depends(sync_ticket, use_cache=False),
) -> dict:
    return {"first": first, "second": second}


async def async_pair(
    first: int = Depends(async_ticket, use_cache=False),
    second: int = Depends(async_ticket, use_cache=False),
) -> dict:
    return {"first": first, "second": second}


@pytest.fixture(scope="module")
def cache_client():
    api = BoltAPI()

    @api.get("/sync/cached")
    def sync_cached(direct=Depends(sync_ticket), a=Depends(sync_wrap_a), b=Depends(sync_wrap_b)):
        return {"direct": direct, "a": a, "b": b}

    @api.get("/async/cached")
    async def async_cached(direct=Depends(async_ticket), a=Depends(async_wrap_a), b=Depends(async_wrap_b)):
        return {"direct": direct, "a": a, "b": b}

    @api.get("/sync/uncached")
    def sync_uncached(
        first=Depends(sync_ticket, use_cache=False),
        second=Depends(sync_ticket),
        third=Depends(sync_ticket),
        fourth=Depends(sync_ticket, use_cache=False),
    ):
        return [first, second, third, fourth]

    @api.get("/async/uncached")
    async def async_uncached(
        first=Depends(async_ticket, use_cache=False),
        second=Depends(async_ticket),
        third=Depends(async_ticket),
        fourth=Depends(async_ticket, use_cache=False),
    ):
        return [first, second, third, fourth]

    @api.get("/sync/nested-uncached")
    def sync_nested_uncached(pair=Depends(sync_pair)):
        return pair

    @api.get("/async/nested-uncached")
    async def async_nested_uncached(pair=Depends(async_pair)):
        return pair

    with TestClient(api) as c:
        yield c


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_runs_one_time_in_a_request_and_again_in_the_next(cache_client, mode):
    TICKET_CALLS.clear()
    first = cache_client.get(f"/{mode}/cached")
    assert first.status_code == 200, first.text
    body = first.json()
    ticket = body["direct"]
    assert body["a"] == {"who": "a", "ticket": ticket}
    assert body["b"] == {"who": "b", "ticket": ticket}
    assert len(TICKET_CALLS) == 1

    second = cache_client.get(f"/{mode}/cached").json()
    assert second["direct"] != ticket
    assert second["a"]["ticket"] == second["b"]["ticket"] == second["direct"]
    assert len(TICKET_CALLS) == 2


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_with_use_cache_false_runs_each_time_and_keeps_no_value(cache_client, mode):
    TICKET_CALLS.clear()
    response = cache_client.get(f"/{mode}/uncached")
    assert response.status_code == 200, response.text
    first, second, third, fourth = response.json()
    # The two cached markers share one run, and the uncached markers do not use it.
    assert second == third
    assert len({first, second, fourth}) == 3
    assert len(TICKET_CALLS) == 3


@pytest.mark.parametrize("mode", MODES)
def test_a_nested_dependency_with_use_cache_false_runs_each_time(cache_client, mode):
    TICKET_CALLS.clear()
    response = cache_client.get(f"/{mode}/nested-uncached")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["first"] != body["second"]
    assert len(TICKET_CALLS) == 2


# Two or more async dependencies of an async handler run at the same time.

STARTED: dict[str, set[str]] = {}


async def _sees_other(rid: str, me: str, other: str) -> bool:
    started = STARTED.setdefault(rid, set())
    started.add(me)
    for _ in range(200):
        if other in started:
            return True
        await asyncio.sleep(0.005)
    return False


async def dep_left(rid: Annotated[str, Query()]) -> bool:
    return await _sees_other(rid, "left", "right")


async def dep_right(rid: Annotated[str, Query()]) -> bool:
    return await _sees_other(rid, "right", "left")


def test_async_dependencies_of_a_handler_run_at_the_same_time():
    """Each dependency waits for the other one to start. In series, the first one times out."""
    api = BoltAPI()

    @api.get("/parallel")
    async def parallel(left=Depends(dep_left), plain=Depends(sync_ticket), right=Depends(dep_right)):
        return {"left": left, "right": right, "plain": plain > 0}

    with TestClient(api) as client:
        response = client.get("/parallel?rid=r1")
    assert response.status_code == 200, response.text
    assert response.json() == {"left": True, "right": True, "plain": True}


# A dependency can take the request with other parameters, keyword-only
# parameters, and a form model that holds uploads.


def sync_request_and_page(request, page: Annotated[int, Query()] = 1) -> dict:
    return {"method": request.method, "page": page}


async def async_request_and_page(request, page: Annotated[int, Query()] = 1) -> dict:
    return {"method": request.method, "page": page}


def sync_keyword_only(*, page: Annotated[int, Query()] = 1, theme: Annotated[str, Cookie()] = "light") -> dict:
    return {"page": page, "theme": theme}


async def async_keyword_only(*, page: Annotated[int, Query()] = 1, theme: Annotated[str, Cookie()] = "light") -> dict:
    return {"page": page, "theme": theme}


class UploadForm(msgspec.Struct):
    title: str
    upload: UploadFile


def sync_upload_form(form: Annotated[UploadForm, Form()]) -> dict:
    return {"title": form.title, "filename": form.upload.filename, "size": form.upload.size}


async def async_upload_form(form: Annotated[UploadForm, Form()]) -> dict:
    return {"title": form.title, "filename": form.upload.filename, "size": form.upload.size}


@pytest.fixture(scope="module")
def argument_client():
    api = BoltAPI()

    @api.post("/async/request-and-page")
    async def async_request_route(values=Depends(async_request_and_page)):
        return values

    @api.post("/sync/request-and-page")
    def sync_request_route(values=Depends(sync_request_and_page)):
        return values

    @api.get("/async/keyword-only")
    async def async_keyword_route(values=Depends(async_keyword_only)):
        return values

    @api.get("/sync/keyword-only")
    def sync_keyword_route(values=Depends(sync_keyword_only)):
        return values

    @api.post("/async/upload-form")
    async def async_upload_form_route(values=Depends(async_upload_form)):
        return values

    @api.post("/sync/upload-form")
    def sync_upload_form_route(values=Depends(sync_upload_form)):
        return values

    with TestClient(api) as c:
        yield c


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_gets_the_request_with_other_parameters(argument_client, mode):
    response = argument_client.post(f"/{mode}/request-and-page?page=4")
    assert response.status_code == 200, response.text
    assert response.json() == {"method": "POST", "page": 4}


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_gets_its_keyword_only_parameters(argument_client, mode):
    response = argument_client.get(f"/{mode}/keyword-only?page=5", headers={"Cookie": "theme=dark"})
    assert response.status_code == 200, response.text
    assert response.json() == {"page": 5, "theme": "dark"}
    assert argument_client.get(f"/{mode}/keyword-only").json() == {"page": 1, "theme": "light"}


@pytest.mark.parametrize("mode", MODES)
def test_a_dependency_gets_a_form_model_with_an_upload(argument_client, mode):
    response = argument_client.post(
        f"/{mode}/upload-form", data={"title": "report"}, files={"upload": ("a.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"title": "report", "filename": "a.txt", "size": 5}
    assert argument_client.post(f"/{mode}/upload-form", data={"title": "report"}).status_code == 422


# An async dependency makes the injector async. The sync dependencies of the
# same handler, and the nested dependencies, still get all request data.


def sync_method(request) -> str:
    return request.method


def sync_inner(request, item_id: Annotated[int, Path()], page: Annotated[int, Query()] = 1) -> dict:
    return {"method": request.method, "item_id": item_id, "page": page}


def sync_outer_of_inner(inner: dict = Depends(sync_inner)) -> dict:
    return {"inner": inner}


async def async_outer_of_inner(inner: dict = Depends(sync_inner)) -> dict:
    return {"inner": inner}


@pytest.fixture(scope="module")
def mixed_client():
    api = BoltAPI()

    @api.get("/mixed")
    async def mixed(
        values=Depends(async_values),
        method=Depends(sync_method),
        keyword_only=Depends(sync_keyword_only),
    ):
        return {"values": values, "method": method, "keyword_only": keyword_only}

    @api.get("/async/nested-request/{item_id}")
    async def async_nested_request(outer=Depends(async_outer_of_inner)):
        return outer

    @api.get("/sync/nested-request/{item_id}")
    def sync_nested_request(outer=Depends(sync_outer_of_inner)):
        return outer

    with TestClient(api) as c:
        yield c


def test_sync_dependencies_next_to_an_async_dependency_get_the_request_and_their_arguments(mixed_client):
    response = mixed_client.get("/mixed?page=2", headers={"Cookie": "theme=dark"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "values": {"session": None, "token": None, "version": "v1", "theme": "dark", "page": 2},
        "method": "GET",
        "keyword_only": {"page": 2, "theme": "dark"},
    }


@pytest.mark.parametrize("mode", MODES)
def test_a_nested_dependency_gets_the_request_and_the_path_and_query_values(mixed_client, mode):
    response = mixed_client.get(f"/{mode}/nested-request/7?page=3")
    assert response.status_code == 200, response.text
    assert response.json() == {"inner": {"method": "GET", "item_id": 7, "page": 3}}


# A dependency can have a sync form (``_bolt_sync_variant``), like ``get_current_user_sync``.
# A sync handler uses the sync form at each depth of its dependencies. An async handler
# uses the async form. The sync form keeps the ``use_cache`` setting of the marker.

VARIANTS: list[str] = []


def sync_form_of_ticket() -> int:
    VARIANTS.append("sync")
    return next(_TICKETS)


async def ticket_with_sync_form() -> int:
    VARIANTS.append("async")
    return next(_TICKETS)


ticket_with_sync_form._bolt_sync_variant = sync_form_of_ticket


def middle_of_ticket(ticket: int = Depends(ticket_with_sync_form)) -> int:
    return ticket


def outer_of_ticket(middle: int = Depends(middle_of_ticket)) -> int:
    return middle


@pytest.fixture(scope="module")
def variant_client():
    api = BoltAPI()

    @api.get("/sync/direct")
    def sync_direct(ticket=Depends(ticket_with_sync_form)):
        return {"ticket": ticket}

    @api.get("/async/direct")
    async def async_direct(ticket=Depends(ticket_with_sync_form)):
        return {"ticket": ticket}

    @api.get("/sync/deep")
    def sync_deep(ticket=Depends(outer_of_ticket)):
        return {"ticket": ticket}

    @api.get("/async/deep")
    async def async_deep(ticket=Depends(outer_of_ticket)):
        return {"ticket": ticket}

    @api.get("/sync/uncached")
    def sync_uncached(
        first=Depends(ticket_with_sync_form, use_cache=False), second=Depends(ticket_with_sync_form, use_cache=False)
    ):
        return [first, second]

    @api.get("/async/uncached")
    async def async_uncached(
        first=Depends(ticket_with_sync_form, use_cache=False), second=Depends(ticket_with_sync_form, use_cache=False)
    ):
        return [first, second]

    @api.get("/sync/cached")
    def sync_cached(first=Depends(ticket_with_sync_form), second=Depends(ticket_with_sync_form)):
        return [first, second]

    @api.get("/async/cached")
    async def async_cached(first=Depends(ticket_with_sync_form), second=Depends(ticket_with_sync_form)):
        return [first, second]

    with TestClient(api) as c:
        yield c


@pytest.mark.parametrize("path", ["direct", "deep"])
@pytest.mark.parametrize("mode", MODES)
def test_each_handler_uses_its_own_form_of_a_dependency_at_each_depth(variant_client, mode, path):
    VARIANTS.clear()
    response = variant_client.get(f"/{mode}/{path}")
    assert response.status_code == 200, response.text
    assert [mode] == VARIANTS


@pytest.mark.parametrize("mode", MODES)
def test_the_sync_form_of_a_dependency_keeps_the_cache_setting(variant_client, mode):
    first, second = variant_client.get(f"/{mode}/uncached").json()
    assert first != second
    first, second = variant_client.get(f"/{mode}/cached").json()
    assert first == second


def test_a_binding_is_compiled_one_time_for_a_route():
    """``compile_dependency`` keeps the binding of a route, and its plan, for each request."""
    handler_meta: dict = {}
    key = (sync_wrap_a, "GET", "/x")

    first = compile_dependency(sync_wrap_a, handler_meta, compile_binder, "GET", "/x")
    plan = first["_dep_arg_plan"]
    nested = first["_dep_nested"]
    assert handler_meta[key] is first

    second = compile_dependency(sync_wrap_a, handler_meta, compile_binder, "GET", "/x")
    assert second is first
    assert second["_dep_arg_plan"] is plan
    assert second["_dep_nested"] is nested
    # Another route gets another binding.
    assert compile_dependency(sync_wrap_a, handler_meta, compile_binder, "POST", "/x") is not first


def sync_item_body(item: Item) -> str:
    return item.name


def outer_of_item_body(name: str = Depends(sync_item_body)) -> str:
    return name


@pytest.mark.parametrize("dependency", [sync_item_body, outer_of_item_body], ids=["direct", "nested"])
def test_a_dependency_with_a_body_fails_on_a_get_route_at_registration(dependency):
    api = BoltAPI()

    with pytest.raises(TypeError, match="GET /with-body cannot have body parameters"):

        @api.get("/with-body")
        def with_body(value=Depends(dependency)):
            return {"value": value}

    @api.post("/with-body")
    def accepted(value=Depends(dependency)):
        return {"value": value}


async def async_name_with_body_form() -> str:
    return "async"


def sync_form_with_body(item: Item) -> str:
    return item.name


async_name_with_body_form._bolt_sync_variant = sync_form_with_body


def outer_of_name_with_body_form(name: str = Depends(async_name_with_body_form)) -> str:
    return name


@pytest.mark.parametrize(
    "dependency", [async_name_with_body_form, outer_of_name_with_body_form], ids=["direct", "nested"]
)
def test_the_sync_form_of_a_dependency_is_checked_for_the_method_of_the_route(dependency):
    """A sync handler uses the sync form, so a body in that form fails on a GET route."""
    api = BoltAPI()

    with pytest.raises(TypeError, match="GET /sync-form cannot have body parameters"):

        @api.get("/sync-form")
        def sync_form_route(value=Depends(dependency)):
            return {"value": value}


def test_a_dependency_used_twice_does_not_hide_the_dependencies_after_it():
    api = BoltAPI()

    @api.get("/twice")
    async def twice(first=Depends(async_required), second=Depends(async_required), values=Depends(async_values)):
        return {"tenants": [first, second], "page": values["page"]}

    with TestClient(api) as client:
        response = client.get("/twice?page=3", headers={"X-Tenant": "acme"})
    assert response.status_code == 200, response.text
    assert response.json() == {"tenants": ["acme", "acme"], "page": 3}

    parameters = (
        SchemaGenerator(api, OpenAPIConfig(title="Test", version="1")).generate().paths["/twice"].get.parameters
    )
    # The header of the repeated dependency, and the fields of the dependency after it.
    assert {("header", "x-tenant"), ("header", "X-Api-Token"), ("cookie", "theme"), ("query", "page")} <= {
        (p.param_in, p.name) for p in parameters
    }
