"""The arguments of a parameter marker (``Query()``, ``Path()``, ``Header()``, ``Cookie()``, ``Form()``).

A marker can be the default of a parameter (``page: int = Query(ge=1, default=1)``)
or an ``Annotated`` item (``page: Annotated[int, Query(ge=1)] = 1``). In both forms:

- ``default`` is the value of a missing parameter. ``...`` (the default) makes it required.
- The constraint arguments (``gt``, ``ge``, ``lt``, ``le``, ``min_length``,
  ``max_length``, ``pattern``) apply to the value, and a bad value gives a 422.
- ``description``, ``example`` and ``deprecated`` go into the OpenAPI schema.
"""

# No ``from __future__ import annotations``: the tests declare the handlers
# inside functions, and the handlers must resolve their types.
from typing import Annotated

import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI, WebSocket
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.param_functions import Cookie, Form, Header, Path, Query
from django_bolt.params import Param
from django_bolt.testing import TestClient, WebSocketTestClient


@pytest.fixture(scope="module")
def api():
    api = BoltAPI()

    @api.get("/search")
    async def search(
        name: str = Query(min_length=3),
        category: str = Query(default="all"),
        page: int = Query(ge=1, default=1),
    ):
        return {"name": name, "category": category, "page": page}

    # A sync handler with scalar parameters binds its arguments in Rust.
    @api.get("/sync-search")
    def sync_search(category: str = Query(default="all"), page: int = Query(ge=1, default=1)):
        return {"category": category, "page": page}

    @api.get("/annotated")
    async def annotated(
        name: Annotated[str, Query(min_length=3, max_length=5, pattern="^[a-z]+$")],
        category: Annotated[str, Query(default="all")],
        page: Annotated[int, Query(gt=0, lt=100)] = 1,
    ):
        return {"name": name, "category": category, "page": page}

    # No constraint, so this route also binds its arguments in Rust.
    @api.get("/annotated-default")
    async def annotated_default(category: Annotated[str, Query(default="all")]):
        return {"category": category}

    @api.get("/optional")
    async def optional(q: str | None = Query(None, min_length=2)):
        return {"q": q}

    @api.get("/items/{item_id}")
    async def item(item_id: int = Path(ge=1, le=10)):
        return {"item_id": item_id}

    @api.get("/client")
    async def client_info(mode: str = Header(default="fast"), theme: str = Cookie(default="dark")):
        return {"mode": mode, "theme": theme}

    @api.post("/note")
    async def note(title: str = Form(), tag: str = Form(default="none")):
        return {"title": title, "tag": tag}

    return api


@pytest.fixture(scope="module")
def client(api):
    with TestClient(api) as client:
        yield client


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/search?name=abc", {"name": "abc", "category": "all", "page": 1}),
        ("/search?name=abc&category=books&page=3", {"name": "abc", "category": "books", "page": 3}),
        ("/sync-search", {"category": "all", "page": 1}),
        ("/sync-search?category=books&page=2", {"category": "books", "page": 2}),
        ("/annotated?name=abc", {"name": "abc", "category": "all", "page": 1}),
        ("/annotated?name=abcde&page=99", {"name": "abcde", "category": "all", "page": 99}),
        ("/annotated-default", {"category": "all"}),
        ("/annotated-default?category=books", {"category": "books"}),
        ("/optional", {"q": None}),
        ("/optional?q=ab", {"q": "ab"}),
        ("/items/10", {"item_id": 10}),
    ],
)
def test_a_marker_gives_its_default_and_accepts_a_valid_value(client, url, expected):
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    "url",
    [
        "/search",  # name is required
        "/search?name=ab",  # min_length=3
        "/search?name=abc&page=0",  # ge=1
        "/sync-search?page=0",  # ge=1
        "/annotated?category=x",  # name is required
        "/annotated?category=x&name=ab",  # min_length=3
        "/annotated?category=x&name=abcdef",  # max_length=5
        "/annotated?category=x&name=ABC",  # pattern
        "/annotated?category=x&name=abc&page=0",  # gt=0
        "/annotated?category=x&name=abc&page=100",  # lt=100
        "/optional?q=a",  # min_length=2
        "/items/0",  # ge=1
        "/items/11",  # le=10
    ],
)
def test_a_missing_required_value_or_a_value_outside_the_marker_constraints_is_a_422(client, url):
    response = client.get(url)
    assert response.status_code == 422, response.text


def test_a_header_cookie_or_form_marker_gives_its_default(client):
    assert client.get("/client").json() == {"mode": "fast", "theme": "dark"}
    response = client.get("/client", headers={"mode": "slow", "cookie": "theme=light"})
    assert response.json() == {"mode": "slow", "theme": "light"}

    response = client.post("/note", data={"title": "a"})
    assert response.status_code == 200, response.text
    assert response.json() == {"title": "a", "tag": "none"}
    assert client.post("/note", data={"tag": "x"}).status_code == 422


@pytest.mark.asyncio
async def test_a_websocket_marker_gives_its_default_and_checks_its_constraints():
    api = BoltAPI()

    @api.websocket("/ws")
    async def ws(websocket: WebSocket, page: int = Query(ge=1, default=1)):
        await websocket.accept()
        await websocket.send_json({"page": page})

    async with WebSocketTestClient(api, "/ws") as ws_client:
        assert await ws_client.receive_json() == {"page": 1}
    async with WebSocketTestClient(api, "/ws", query_string="page=4") as ws_client:
        assert await ws_client.receive_json() == {"page": 4}


def test_a_default_in_the_marker_and_after_the_equals_sign_fails_at_registration():
    api = BoltAPI()
    with pytest.raises(TypeError, match="page"):

        @api.get("/twice")
        async def twice(page: Annotated[int, Query(default=1)] = 2):
            return {}


def test_a_constraint_that_does_not_fit_the_type_fails_at_registration():
    api = BoltAPI()
    with pytest.raises(TypeError):

        @api.get("/bad")
        async def bad(page: int = Query(pattern="^[0-9]+$")):
            return {}


def _spec(api: BoltAPI) -> dict:
    spec = SchemaGenerator(api, OpenAPIConfig(title="Test API", version="1.0.0")).generate().to_schema()
    validate(spec)
    return spec


def test_the_openapi_parameter_shows_the_marker_default_constraints_and_docs():
    api = BoltAPI()

    @api.get("/search")
    async def search(
        name: str = Query(min_length=3, description="The name", example="abc", deprecated=True),
        category: str = Query(default="all"),
        page: int = Query(ge=1, default=1),
        mode: str = Header(default="fast", description="The mode"),
    ):
        return {}

    spec = _spec(api)
    parameters = {parameter["name"]: parameter for parameter in spec["paths"]["/search"]["get"]["parameters"]}

    name = parameters["name"]
    assert name["required"] is True
    assert name["description"] == "The name"
    assert name["deprecated"] is True
    assert name["example"] == "abc"
    assert name["schema"] == {"type": "string", "minLength": 3}

    category = parameters["category"]
    assert category.get("required", False) is False
    assert category["schema"] == {"type": "string", "default": "all"}

    page = parameters["page"]
    assert page.get("required", False) is False
    assert page["schema"] == {"type": "integer", "minimum": 1, "default": 1}

    assert parameters["mode"]["description"] == "The mode"
    assert parameters["mode"]["schema"]["default"] == "fast"


def test_the_openapi_form_schema_shows_the_marker_default_and_description():
    api = BoltAPI()

    @api.post("/note")
    async def note(title: str = Form(description="The title"), tag: str = Form(default="none")):
        return {}

    schema = _spec(api)["paths"]["/note"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["required"] == ["title"]
    assert schema["properties"]["title"] == {"type": "string", "description": "The title"}
    assert schema["properties"]["tag"] == {"type": "string", "default": "none"}


def test_param_keeps_its_positional_field_order():
    """``default`` is the last field, so ``Param(source, alias)`` still sets the alias."""
    param = Param("query", "x")
    assert param.alias == "x"
    assert param.default is ...
