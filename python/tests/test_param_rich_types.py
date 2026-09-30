"""Parameters with a sequence type, a NewType, a type alias, or msgspec.Meta constraints.

Each value arrives with its declared type, and a bad value gives a 422:

- A ``list``, ``set``, ``frozenset`` or ``tuple`` query parameter takes each value of
  its repeated key (``?tag=a&tag=b``), also on a WebSocket route. A form field takes each value of its name. A
  header or a cookie gives one item.
- A ``NewType`` or a ``type`` alias converts as the type it names.
- ``msgspec.Meta`` constraints (``ge``, ``max_length``, ``pattern``) apply to the value.
"""

# No ``from __future__ import annotations``: the tests declare the handlers
# with local types, and the handlers must resolve them.
from decimal import Decimal
from typing import Annotated, NewType
from urllib.parse import parse_qsl

import msgspec
import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.middleware import DjangoMiddlewareStack
from django_bolt.param_functions import Cookie, File, Form, Header, Path, Query
from django_bolt.serializers.types import PositiveInt
from django_bolt.testing import TestClient, WebSocketTestClient

UserId = NewType("UserId", int)
type Page = int
type MaybeUserId = UserId | None


def _show(value):
    """Describe a value and its type for a JSON response."""
    if isinstance(value, (set, frozenset)):
        return {"value": sorted(value), "type": type(value).__name__}
    if isinstance(value, tuple):
        return {"value": list(value), "type": "tuple"}
    if isinstance(value, list):
        return {"value": value, "type": "list", "item_types": sorted({type(item).__name__ for item in value})}
    return {"value": value, "type": type(value).__name__}


# --- Sequences in the query ---------------------------------------------------------


@pytest.fixture(scope="module")
def query_client():
    api = BoltAPI()

    @api.get("/list")
    async def as_list(tag: Annotated[list[int], Query()]):
        return _show(tag)

    @api.get("/set")
    async def as_set(tag: Annotated[set[int], Query()]):
        return _show(tag)

    @api.get("/frozenset")
    async def as_frozenset(tag: Annotated[frozenset[str], Query()]):
        return _show(tag)

    @api.get("/tuple")
    async def as_tuple(tag: Annotated[tuple[int, ...], Query()]):
        return _show(tag)

    @api.get("/pair")
    async def as_pair(point: Annotated[tuple[int, str], Query()]):
        return _show(point)

    @api.get("/optional")
    async def as_optional(tag: Annotated[list[int] | None, Query()] = None):
        return _show(tag)

    @api.get("/default")
    def as_default(tag: Annotated[list[int], Query()] = [7]):  # noqa: B006 - the default is only read
        return _show(tag)

    @api.get("/alias")
    async def as_alias(ids: Annotated[list[UserId], Query(alias="id")]):
        return _show(ids)

    with TestClient(api) as client:
        yield client


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/list?tag=1&tag=2&tag=1", {"value": [1, 2, 1], "type": "list", "item_types": ["int"]}),
        ("/list?tag=5", {"value": [5], "type": "list", "item_types": ["int"]}),
        ("/set?tag=1&tag=2&tag=1", {"value": [1, 2], "type": "set"}),
        ("/frozenset?tag=b&tag=a", {"value": ["a", "b"], "type": "frozenset"}),
        ("/tuple?tag=3&tag=4", {"value": [3, 4], "type": "tuple"}),
        ("/pair?point=1&point=x", {"value": [1, "x"], "type": "tuple"}),
        ("/optional", {"value": None, "type": "NoneType"}),
        ("/optional?tag=2&tag=3", {"value": [2, 3], "type": "list", "item_types": ["int"]}),
        ("/default", {"value": [7], "type": "list", "item_types": ["int"]}),
        ("/alias?id=4&id=5", {"value": [4, 5], "type": "list", "item_types": ["int"]}),
    ],
)
def test_a_sequence_query_parameter_takes_each_value_of_its_key(query_client, url, expected):
    response = query_client.get(url)
    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    "url",
    ["/list?tag=1&tag=x", "/set?tag=x", "/tuple?tag=1.5", "/pair?point=1", "/pair?point=1&point=a&point=b", "/list"],
)
def test_a_bad_or_missing_sequence_query_parameter_is_a_422(query_client, url):
    response = query_client.get(url)
    assert response.status_code == 422, response.text


def test_the_request_query_holds_each_value_of_a_sequence_key():
    api = BoltAPI()

    @api.get("/items")
    async def items(request, tag: Annotated[list[int], Query()]):
        return {"query": request.query["tag"]}

    with TestClient(api) as client:
        response = client.get("/items?tag=1&tag=2")

    assert response.status_code == 200, response.text
    assert response.json() == {"query": ["1", "2"]}


# --- Sequences in a form, a header and a cookie --------------------------------------


@pytest.fixture(scope="module")
def form_client():
    api = BoltAPI()

    @api.post("/list")
    async def as_list(tag: Annotated[list[int], Form()]):
        return _show(tag)

    @api.post("/set")
    async def as_set(tag: Annotated[set[int], Form()]):
        return _show(tag)

    @api.post("/tuple")
    async def as_tuple(tag: Annotated[tuple[int, ...], Form()]):
        return _show(tag)

    @api.get("/header")
    async def header(x_tag: Annotated[list[int], Header()]):
        return _show(x_tag)

    @api.get("/cookie")
    async def cookie(tags: Annotated[set[str], Cookie()]):
        return _show(tags)

    with TestClient(api) as client:
        yield client


def test_a_sequence_form_field_converts_each_value(form_client):
    assert form_client.post("/list", data={"tag": ["1", "2"]}).json() == {
        "value": [1, 2],
        "type": "list",
        "item_types": ["int"],
    }
    assert form_client.post("/set", data={"tag": ["1", "1", "2"]}).json() == {"value": [1, 2], "type": "set"}
    assert form_client.post("/tuple", data={"tag": ["3"]}).json() == {"value": [3], "type": "tuple"}


def test_a_bad_item_of_a_sequence_form_field_is_a_422(form_client):
    response = form_client.post("/list", data={"tag": ["1", "x"]})
    assert response.status_code == 422, response.text


def test_a_sequence_header_or_cookie_gives_one_item(form_client):
    assert form_client.get("/header", headers={"X-Tag": "4"}).json() == {
        "value": [4],
        "type": "list",
        "item_types": ["int"],
    }
    assert form_client.get("/header", headers={"X-Tag": "x"}).status_code == 422
    assert form_client.get("/cookie", cookies={"tags": "a"}).json() == {"value": ["a"], "type": "set"}


@pytest.mark.parametrize(
    ("route", "annotation"),
    [
        ("/value", Annotated[tuple[int, str], Header()]),
        ("/value", Annotated[tuple[int, str], Cookie()]),
        ("/value", Annotated[list[int], msgspec.Meta(min_length=2), Header()]),
        ("/value/{value}", Annotated[tuple[int, int], Path()]),
    ],
    ids=["header_pair", "cookie_pair", "header_min_two", "path_pair"],
)
def test_a_single_value_source_rejects_a_type_that_cannot_hold_one_item(route, annotation):
    """A path, a header or a cookie gives one value. A type that needs more fails when the route registers."""
    api = BoltAPI()

    async def handler(value: annotation):
        return {"value": list(value)}

    with pytest.raises(TypeError, match="one item"):
        api.get(route)(handler)


def test_a_bare_collection_parameter_takes_each_value_of_its_key():
    """msgspec reads a bare ``set`` as ``set[Any]``. It takes each value, and each item stays a string."""
    api = BoltAPI()

    @api.get("/bare")
    async def bare(tag: Annotated[set, Query()], ids: Annotated[tuple, Query()], x_names: Annotated[list, Header()]):
        return {"tag": _show(tag), "ids": _show(ids), "x_names": _show(x_names)}

    @api.post("/bare-form")
    async def bare_form(tag: Annotated[frozenset, Form()]):
        return _show(tag)

    class Prefs(msgspec.Struct):
        themes: list = []

    @api.get("/bare-cookie-struct")
    async def bare_cookie_struct(prefs: Annotated[Prefs, Cookie()]):
        return _show(prefs.themes)

    with TestClient(api) as client:
        response = client.get("/bare?tag=b&tag=a&tag=b&ids=1&ids=2", headers={"X-Names": "n"})
        form_response = client.post("/bare-form", data={"tag": ["b", "a", "b"]})
        cookie_response = client.get("/bare-cookie-struct", cookies={"themes": "dark"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "tag": {"value": ["a", "b"], "type": "set"},
        "ids": {"value": ["1", "2"], "type": "tuple"},
        "x_names": {"value": ["n"], "type": "list", "item_types": ["str"]},
    }
    assert form_response.status_code == 200, form_response.text
    assert form_response.json() == {"value": ["a", "b"], "type": "frozenset"}
    assert cookie_response.status_code == 200, cookie_response.text
    assert cookie_response.json() == {"value": ["dark"], "type": "list", "item_types": ["str"]}


def test_a_one_item_or_variadic_tuple_header_takes_the_value():
    api = BoltAPI()

    @api.get("/ids")
    async def ids(x_id: Annotated[tuple[int], Header()], x_ids: Annotated[tuple[int, ...], Header()]):
        return {"one": _show(x_id), "many": _show(x_ids)}

    with TestClient(api) as client:
        response = client.get("/ids", headers={"X-Id": "4", "X-Ids": "5"})

    assert response.status_code == 200, response.text
    assert response.json() == {"one": {"value": [4], "type": "tuple"}, "many": {"value": [5], "type": "tuple"}}


# --- NewType and type aliases ----------------------------------------------------------


@pytest.fixture(scope="module")
def alias_client():
    api = BoltAPI()

    @api.get("/path/{user_id}")
    async def by_path(user_id: UserId):
        return _show(user_id)

    @api.get("/query")
    async def by_query(user_id: UserId, page: Page = 1):
        return {"user_id": _show(user_id), "page": _show(page)}

    @api.get("/maybe")
    async def maybe(user_id: MaybeUserId = None):
        return _show(user_id)

    @api.get("/header")
    async def by_header(x_user_id: Annotated[UserId, Header()]):
        return _show(x_user_id)

    @api.get("/cookie")
    async def by_cookie(page: Annotated[Page, Cookie()]):
        return _show(page)

    @api.post("/form")
    async def by_form(user_id: Annotated[UserId, Form()]):
        return _show(user_id)

    with TestClient(api) as client:
        yield client


def test_a_newtype_or_type_alias_parameter_converts_as_its_base_type(alias_client):
    assert alias_client.get("/path/5").json() == {"value": 5, "type": "int"}
    assert alias_client.get("/query?user_id=6&page=2").json() == {
        "user_id": {"value": 6, "type": "int"},
        "page": {"value": 2, "type": "int"},
    }
    assert alias_client.get("/maybe").json() == {"value": None, "type": "NoneType"}
    assert alias_client.get("/maybe?user_id=3").json() == {"value": 3, "type": "int"}
    assert alias_client.get("/header", headers={"X-User-Id": "7"}).json() == {"value": 7, "type": "int"}
    assert alias_client.get("/cookie", cookies={"page": "8"}).json() == {"value": 8, "type": "int"}
    assert alias_client.post("/form", data={"user_id": "9"}).json() == {"value": 9, "type": "int"}


@pytest.mark.parametrize(
    ("method", "url", "kwargs"),
    [
        ("get", "/path/x", {}),
        ("get", "/query?user_id=x", {}),
        ("get", "/query?user_id=1&page=x", {}),
        ("get", "/maybe?user_id=x", {}),
        ("get", "/header", {"headers": {"X-User-Id": "x"}}),
        ("get", "/cookie", {"cookies": {"page": "x"}}),
        ("post", "/form", {"data": {"user_id": "x"}}),
    ],
)
def test_a_bad_newtype_or_type_alias_value_is_a_422(alias_client, method, url, kwargs):
    response = getattr(alias_client, method)(url, **kwargs)
    assert response.status_code == 422, response.text


# --- msgspec.Meta constraints ----------------------------------------------------------


@pytest.fixture(scope="module")
def constraint_client():
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def by_path(item_id: Annotated[int, msgspec.Meta(ge=1)]):
        return _show(item_id)

    @api.get("/page")
    async def page(page: Annotated[int, msgspec.Meta(ge=1, le=100)] = 1):
        return _show(page)

    @api.get("/marker")
    async def marker(code: Annotated[str, msgspec.Meta(pattern=r"^[A-Z]{3}$"), Query()]):
        return _show(code)

    @api.get("/positive")
    async def positive(count: PositiveInt):
        return _show(count)

    @api.get("/header")
    async def header(x_name: Annotated[str, msgspec.Meta(max_length=3), Header()]):
        return _show(x_name)

    @api.get("/cookie")
    async def cookie(size: Annotated[float, msgspec.Meta(gt=0), Cookie()]):
        return _show(size)

    @api.post("/form")
    async def form(tags: Annotated[list[Annotated[int, msgspec.Meta(ge=0)]], msgspec.Meta(max_length=2), Form()]):
        return _show(tags)

    with TestClient(api) as client:
        yield client


@pytest.mark.parametrize(
    ("method", "url", "kwargs", "expected"),
    [
        ("get", "/items/3", {}, {"value": 3, "type": "int"}),
        ("get", "/page?page=100", {}, {"value": 100, "type": "int"}),
        ("get", "/page", {}, {"value": 1, "type": "int"}),
        ("get", "/marker?code=ABC", {}, {"value": "ABC", "type": "str"}),
        ("get", "/positive?count=2", {}, {"value": 2, "type": "int"}),
        ("get", "/header", {"headers": {"X-Name": "abc"}}, {"value": "abc", "type": "str"}),
        ("get", "/cookie", {"cookies": {"size": "0.5"}}, {"value": 0.5, "type": "float"}),
        ("post", "/form", {"data": {"tags": ["0", "4"]}}, {"value": [0, 4], "type": "list", "item_types": ["int"]}),
    ],
)
def test_a_value_within_its_constraints_passes(constraint_client, method, url, kwargs, expected):
    response = getattr(constraint_client, method)(url, **kwargs)
    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    ("method", "url", "kwargs"),
    [
        ("get", "/items/0", {}),
        ("get", "/page?page=0", {}),
        ("get", "/page?page=101", {}),
        ("get", "/marker?code=abc", {}),
        ("get", "/positive?count=0", {}),
        ("get", "/header", {"headers": {"X-Name": "abcd"}}),
        ("get", "/cookie", {"cookies": {"size": "0"}}),
        ("post", "/form", {"data": {"tags": ["-1"]}}),
        ("post", "/form", {"data": {"tags": ["1", "2", "3"]}}),
    ],
)
def test_a_value_outside_its_constraints_is_a_422(constraint_client, method, url, kwargs):
    response = getattr(constraint_client, method)(url, **kwargs)
    assert response.status_code == 422, response.text


def test_a_constraint_error_names_the_parameter(constraint_client):
    response = constraint_client.get("/page?page=0")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["query", "page"]
    assert ">= 1" in detail[0]["msg"]


def test_path_alias_with_constraint():
    api = BoltAPI()

    @api.get("/items/{id}")
    async def by_path(item_id: Annotated[int, msgspec.Meta(ge=1), Path(alias="id")]):
        return _show(item_id)

    with TestClient(api) as client:
        assert client.get("/items/2").json() == {"value": 2, "type": "int"}
        assert client.get("/items/0").status_code == 422


# --- msgspec.Meta on an optional type, on a bad type, and on a file ---------------------


@pytest.fixture(scope="module")
def optional_constraint_client():
    api = BoltAPI()

    @api.get("/marker")
    async def marker(page: Annotated[int | None, msgspec.Meta(ge=1), Query()]):
        return _show(page)

    @api.get("/default")
    async def default(page: Annotated[int | None, msgspec.Meta(ge=1)] = None):
        return _show(page)

    @api.get("/header")
    async def header(x_limit: Annotated[int | None, msgspec.Meta(le=10), Header()] = None):
        return _show(x_limit)

    @api.get("/tags")
    async def tags(tag: Annotated[list[int] | None, msgspec.Meta(max_length=2), Query()] = None):
        return _show(tag)

    with TestClient(api) as client:
        yield client


@pytest.mark.parametrize(
    ("url", "headers", "expected"),
    [
        ("/marker", {}, {"value": None, "type": "NoneType"}),
        ("/marker?page=2", {}, {"value": 2, "type": "int"}),
        ("/default", {}, {"value": None, "type": "NoneType"}),
        ("/default?page=3", {}, {"value": 3, "type": "int"}),
        ("/header", {"X-Limit": "10"}, {"value": 10, "type": "int"}),
        ("/tags", {}, {"value": None, "type": "NoneType"}),
        ("/tags?tag=1&tag=2", {}, {"value": [1, 2], "type": "list", "item_types": ["int"]}),
    ],
)
def test_a_constraint_on_an_optional_type_applies_to_its_value(optional_constraint_client, url, headers, expected):
    """``Annotated[int | None, Meta(ge=1)]`` is optional, and a value must meet the constraint."""
    response = optional_constraint_client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("/marker?page=0", {}),
        ("/default?page=0", {}),
        ("/header", {"X-Limit": "11"}),
        ("/tags?tag=1&tag=2&tag=3", {}),
    ],
)
def test_a_value_outside_the_constraint_of_an_optional_type_is_a_422(optional_constraint_client, url, headers):
    response = optional_constraint_client.get(url, headers=headers)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "annotation",
    [Annotated[Decimal, msgspec.Meta(ge=0)], Annotated[int | str, msgspec.Meta(ge=1)]],
    ids=["decimal", "union"],
)
def test_a_constraint_that_msgspec_cannot_apply_fails_at_registration(annotation):
    """msgspec takes ``ge`` on an ``int`` or a ``float`` only. The route fails when it registers, not at each request."""
    api = BoltAPI()

    async def handler(value: annotation):
        return {"value": str(value)}

    with pytest.raises(TypeError, match="Can only set `ge`"):
        api.get("/value")(handler)


def test_msgspec_meta_does_not_change_how_a_file_parameter_binds():
    """``File()`` takes its constraints (``max_files``) itself. A single file still gives a list."""
    api = BoltAPI()

    @api.post("/marker")
    async def marker(files: Annotated[list[dict], msgspec.Meta(max_length=3), File()]):
        return {"type": type(files).__name__, "count": len(files)}

    @api.post("/default")
    async def default(files: Annotated[list[dict], msgspec.Meta(max_length=3)] = File()):
        return {"type": type(files).__name__, "count": len(files)}

    with TestClient(api) as client:
        for url in ("/marker", "/default"):
            response = client.post(url, files={"files": ("a.txt", b"a", "text/plain")})
            assert response.status_code == 200, response.text
            assert response.json() == {"type": "list", "count": 1}


def test_the_query_string_of_the_request_keeps_each_value_of_a_sequence_key():
    api = BoltAPI()

    @api.get("/items")
    async def items(request, tag: Annotated[list[int], Query()], page: int = 1):
        return {"query_string": request.META["QUERY_STRING"], "full_path": request.get_full_path()}

    with TestClient(api) as client:
        response = client.get("/items?tag=1&page=2&tag=3")

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["query_string"].split("&")) == ["page=2", "tag=1", "tag=3"]
    assert sorted(body["full_path"].removeprefix("/items?").split("&")) == ["page=2", "tag=1", "tag=3"]


class _TagRecordingMiddleware:
    """A Django middleware that copies the ``tag`` values of ``request.GET`` to the request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tags = request.GET.getlist("tag")
        return self.get_response(request)


def test_django_middleware_reads_each_value_of_a_sequence_key_from_request_get():
    api = BoltAPI(middleware=[DjangoMiddlewareStack([_TagRecordingMiddleware])])

    @api.get("/items")
    async def items(request, tag: Annotated[list[int], Query()]):
        return {"middleware": request.state.get("tags"), "handler": tag}

    with TestClient(api) as client:
        response = client.get("/items?tag=1&tag=2")

    assert response.status_code == 200, response.text
    assert response.json() == {"middleware": ["1", "2"], "handler": [1, 2]}


def test_a_sequence_query_value_over_the_length_limit_is_a_422():
    """The limit applies to each value, not only to the last value of the key."""
    api = BoltAPI()

    @api.get("/items")
    async def items(tag: Annotated[list[str], Query()]):
        return {"tag": tag}

    # The default of DJANGO_BOLT_MAX_PARAM_LENGTH is 8192 bytes.
    too_long = "x" * 8193
    with TestClient(api) as client:
        response = client.get(f"/items?tag={too_long}&tag=short")

    assert response.status_code == 422, response.text
    assert "Parameter too long" in response.text


def test_the_query_string_of_the_request_encodes_each_key_and_value():
    """A decoded value with ``&``, ``=`` or a space must not split into other pairs."""
    api = BoltAPI()

    @api.get("/items")
    async def items(request, tag: Annotated[list[str], Query()], q: str = ""):
        return {"query_string": request.META["QUERY_STRING"], "full_path": request.get_full_path()}

    with TestClient(api) as client:
        response = client.get("/items?tag=a%26b&tag=c%3Dd&q=x%20y%25")

    assert response.status_code == 200, response.text
    body = response.json()
    expected = [("q", "x y%"), ("tag", "a&b"), ("tag", "c=d")]
    assert sorted(parse_qsl(body["query_string"])) == expected
    assert sorted(parse_qsl(body["full_path"].removeprefix("/items?"))) == expected


# --- Sequences in the query of a WebSocket route -------------------------------------


@pytest.fixture(scope="module")
def ws_api():
    api = BoltAPI()

    @api.websocket("/ws/tags")
    async def tags(websocket: WebSocket, tag: Annotated[list[int], Query()], page: int = 1):
        await websocket.accept()
        await websocket.send_json({"tag": _show(tag), "page": page})

    @api.websocket("/ws/names")
    async def names(websocket: WebSocket, name: Annotated[set[str], Query()]):
        await websocket.accept()
        await websocket.send_json(_show(name))

    return api


@pytest.mark.asyncio
async def test_a_websocket_sequence_query_parameter_takes_each_value_of_its_key(ws_api):
    async with WebSocketTestClient(ws_api, "/ws/tags", query_string="tag=3&page=2&tag=1&tag=3") as ws:
        response = await ws.receive_json()
    assert response == {"tag": {"value": [3, 1, 3], "type": "list", "item_types": ["int"]}, "page": 2}


@pytest.mark.asyncio
async def test_a_websocket_sequence_query_parameter_decodes_each_value(ws_api):
    async with WebSocketTestClient(ws_api, "/ws/names", query_string="name=a+b&name=c%26d&name=a%20b") as ws:
        response = await ws.receive_json()
    assert response == {"value": ["a b", "c&d"], "type": "set"}
