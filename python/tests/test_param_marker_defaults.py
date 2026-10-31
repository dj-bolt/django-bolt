"""The arguments of a parameter marker (``Query()``, ``Path()``, ``Header()``, ``Cookie()``, ``Form()``).

A marker can be the default of a parameter (``page: int = Query(ge=1, default=1)``)
or an ``Annotated`` item (``page: Annotated[int, Query(ge=1)] = 1``). In both forms:

- The default of the parameter is the value of a missing parameter. No default makes it required.
  An ``Annotated`` marker cannot hold the default, as in FastAPI. Write it after ``=``.
- The constraint arguments (``gt``, ``ge``, ``lt``, ``le``, ``min_length``,
  ``max_length``, ``pattern``) apply to the value, and a bad value gives a 422.
- ``description``, ``example`` and ``deprecated`` go into the OpenAPI schema.
"""

# No ``from __future__ import annotations``: the tests declare the handlers
# inside functions, and the handlers must resolve their types.
from typing import Annotated

import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI, UploadFile, WebSocket
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import SchemaGenerator
from django_bolt.param_functions import Cookie, File, Form, Header, Path, Query
from django_bolt.params import Param
from django_bolt.testing import TestClient, WebSocketTestClient


# Function scope: each test registers its routes, so a mutation tool sees
# each test reach the request-time closures of the extractors.
@pytest.fixture
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
        category: Annotated[str, Query()] = "all",
        page: Annotated[int, Query(gt=0, lt=100)] = 1,
    ):
        return {"name": name, "category": category, "page": page}

    # No constraint, so this route also binds its arguments in Rust.
    @api.get("/annotated-default")
    async def annotated_default(category: Annotated[str, Query()] = "all"):
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


@pytest.fixture
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


def test_a_default_in_an_annotated_marker_fails_at_registration():
    """``Form("tag")`` sets the default, not the alias. In ``Annotated``, it fails as in FastAPI."""
    api = BoltAPI()
    with pytest.raises(TypeError, match="'page'.*Set the default value with `=` instead"):

        @api.get("/annotated-default")
        async def annotated_default(page: Annotated[int, Query(default=1)]):
            return {}

    with pytest.raises(TypeError, match="'page'"):

        @api.get("/twice")
        async def twice(page: Annotated[int, Query(default=1)] = 2):
            return {}

    with pytest.raises(TypeError, match="'tag'"):

        @api.post("/positional")
        async def positional(tag: Annotated[str, Form("tag")]):
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


# Each test below builds its routes inside the test. So each test runs the
# marker function that it checks.


def _register(api: BoltAPI, method: str, path: str, annotation) -> None:
    async def handler(value):
        return {"value": value}

    handler.__annotations__ = {"value": annotation}
    # The name of the handler is the operation ID in the schema, so it must be unique.
    handler.__name__ = path.strip("/").split("/")[0]
    getattr(api, method)(path)(handler)


_CONSTRAINTS = [
    ("gt", 5, int, "6", "5"),
    ("ge", 5, int, "5", "4"),
    ("lt", 5, int, "4", "5"),
    ("le", 5, int, "5", "6"),
    ("min_length", 2, str, "ab", "a"),
    ("max_length", 2, str, "ab", "abc"),
    ("pattern", "^a+$", str, "aa", "b"),
]


@pytest.mark.parametrize("marker", [Query, Path], ids=["query", "path"])
@pytest.mark.parametrize(("name", "limit", "kind", "good", "bad"), _CONSTRAINTS, ids=[c[0] for c in _CONSTRAINTS])
def test_each_constraint_argument_of_a_query_and_a_path_marker_applies(marker, name, limit, kind, good, bad):
    api = BoltAPI()
    is_path = marker is Path
    _register(api, "get", "/items/{value}" if is_path else "/items", Annotated[kind, marker(**{name: limit})])

    with TestClient(api) as client:

        def get(value: str):
            return client.get(f"/items/{value}" if is_path else f"/items?value={value}")

        response = get(good)
        assert response.status_code == 200, response.text
        assert response.json() == {"value": kind(good)}
        assert get(bad).status_code == 422


def test_a_path_marker_with_a_default_fails():
    with pytest.raises(ValueError, match="^Path parameters cannot have default values$"):
        Path(default=1)


@pytest.mark.parametrize("source", ["query", "path", "header", "cookie", "form"])
def test_the_alias_of_a_marker_is_the_name_on_the_wire(source):
    markers = {"query": Query, "path": Path, "header": Header, "cookie": Cookie, "form": Form}
    api = BoltAPI()
    method = "post" if source == "form" else "get"
    path = "/items/{wire}" if source == "path" else "/items"
    _register(api, method, path, Annotated[str, markers[source](alias="wire")])

    with TestClient(api) as client:
        if source == "query":
            response = client.get("/items?wire=x")
        elif source == "path":
            response = client.get("/items/x")
        elif source == "header":
            response = client.get("/items", headers={"wire": "x"})
        elif source == "cookie":
            response = client.get("/items", headers={"cookie": "wire=x"})
        else:
            response = client.post("/items", data={"wire": "x"})
        assert response.status_code == 200, response.text
        assert response.json() == {"value": "x"}

        if source != "path":
            # The name of the parameter is not its name on the wire.
            if source == "query":
                other = client.get("/items?value=x")
            elif source == "header":
                other = client.get("/items", headers={"value": "x"})
            elif source == "cookie":
                other = client.get("/items", headers={"cookie": "value=x"})
            else:
                other = client.post("/items", data={"value": "x"})
            assert other.status_code == 422, other.text


@pytest.mark.parametrize("marker", [Query, Path, Header, Cookie], ids=["query", "path", "header", "cookie"])
def test_the_docs_arguments_of_a_parameter_marker_reach_the_schema(marker):
    where = {Query: "query", Path: "path", Header: "header", Cookie: "cookie"}[marker]
    api = BoltAPI()
    _register(api, "get", "/plain/{value}" if where == "path" else "/plain", Annotated[str, marker()])
    _register(
        api,
        "get",
        "/documented/{value}" if where == "path" else "/documented",
        Annotated[str, marker(description="The value", example="abc", deprecated=True)],
    )
    spec = _spec(api)

    (plain,) = spec["paths"]["/plain/{value}" if where == "path" else "/plain"]["get"]["parameters"]
    assert plain["description"] == "Parameter value"
    assert "example" not in plain
    assert plain.get("deprecated", False) is False

    (documented,) = spec["paths"]["/documented/{value}" if where == "path" else "/documented"]["get"]["parameters"]
    assert documented["in"] == where
    assert documented["description"] == "The value"
    assert documented["example"] == "abc"
    assert documented["deprecated"] is True


def test_a_file_marker_gives_its_default_and_its_description_to_the_schema():
    api = BoltAPI()

    @api.post("/upload")
    async def upload(doc: UploadFile = File(default=None, description="The document")):
        return {"filename": None if doc is None else doc.filename}

    with TestClient(api) as client:
        assert client.post("/upload", files={"other": ("a.txt", b"x", "text/plain")}).json() == {"filename": None}
        response = client.post("/upload", files={"doc": ("a.txt", b"x", "text/plain")})
        assert response.status_code == 200, response.text
        assert response.json() == {"filename": "a.txt"}

    schema = _spec(api)["paths"]["/upload"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["properties"]["doc"]["description"] == "The document"
    assert "doc" not in schema.get("required", [])
