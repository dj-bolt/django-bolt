"""Route metadata that django_bolt.middleware.compiler gives to Rust.

Each test sends requests with TestClient and checks the response. A test
reads route metadata only when the response cannot show the difference.
"""

from __future__ import annotations

import time
from typing import Annotated

import jwt
import msgspec
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_bolt import BoltAPI, UploadFile
from django_bolt.auth import JWTAuthentication, Requires
from django_bolt.middleware import no_compress
from django_bolt.middleware.compiler import _build_policy_without_nonce
from django_bolt.param_functions import File, Header, Query
from django_bolt.testing import TestClient

try:
    from django.utils.csp import CSP, build_policy
except ImportError:  # Django 5.2 has no django.utils.csp
    CSP = build_policy = None


class _Tags(msgspec.Struct):
    tag: list[str] = []


class _BoundedTags(msgspec.Struct):
    tag: Annotated[list[int], msgspec.Meta(max_length=3)] = []


class _Page(msgspec.Struct):
    page: int = 1


# --- A sequence parameter gets each value of its key ---------------------------


def test_a_constrained_list_query_parameter_takes_each_value():
    api = BoltAPI()

    @api.get("/tags")
    async def tags(tag: Annotated[list[int], Query(max_length=3)]):
        return {"tag": tag}

    with TestClient(api) as client:
        response = client.get("/tags?tag=1&tag=2")

    assert response.status_code == 200, response.text
    assert response.json() == {"tag": [1, 2]}


def test_a_list_field_of_a_query_struct_takes_each_value():
    api = BoltAPI()

    @api.get("/tags")
    async def tags(filters: Annotated[_Tags, Query()]):
        return {"tag": filters.tag}

    with TestClient(api) as client:
        response = client.get("/tags?tag=1&tag=2")

    assert response.status_code == 200, response.text
    assert response.json() == {"tag": ["1", "2"]}


def test_a_constrained_list_field_of_a_query_struct_takes_each_value():
    api = BoltAPI()

    @api.get("/tags")
    async def tags(filters: Annotated[_BoundedTags, Query()]):
        return {"tag": filters.tag}

    with TestClient(api) as client:
        response = client.get("/tags?tag=1&tag=2")

    assert response.status_code == 200, response.text
    assert response.json() == {"tag": [1, 2]}


# --- Rust converts a typed value before Python runs ----------------------------

RUST_INT_ERROR = "Query parameter '{key}': Invalid integer 'abc': invalid digit found in string"


def test_rust_converts_a_constrained_int():
    api = BoltAPI()

    @api.get("/count")
    async def count(n: Annotated[int, Query(ge=1)]):
        return {"n": n}

    with TestClient(api) as client:
        response = client.get("/count?n=abc")

    assert response.status_code == 422
    assert response.json() == {"detail": RUST_INT_ERROR.format(key="n")}


def test_rust_converts_an_int_field_of_a_query_struct():
    api = BoltAPI()

    @api.get("/items")
    async def items(paging: Annotated[_Page, Query()]):
        return {"page": paging.page}

    with TestClient(api) as client:
        response = client.get("/items?page=abc")

    assert response.status_code == 422
    assert response.json() == {"detail": RUST_INT_ERROR.format(key="page")}


# --- A guard that cannot compile names itself ------------------------------------


def test_a_guard_class_that_needs_arguments_names_itself():
    api = BoltAPI()

    with pytest.raises(ImproperlyConfigured, match=r"Guard Requires on GET /admin could not be instantiated"):

        @api.get("/admin", guards=[Requires])
        async def admin():
            return {}


def test_a_value_that_is_not_a_guard_names_its_type():
    api = BoltAPI()

    with pytest.raises(
        ImproperlyConfigured, match=r"Guard str on GET /admin is not a guard\..* a named Requires: str = Requires"
    ):

        @api.get("/admin", guards=["IsAuthenticated"])
        async def admin():
            return {}


# --- Rust binds the handler arguments ------------------------------------------


def test_rust_binds_optional_parameters_by_keyword():
    """A missing optional parameter must not shift the next one into its place."""
    api = BoltAPI()

    @api.get("/pair")
    async def pair(a: int = 1, b: int = 2):
        return {"a": a, "b": b}

    with TestClient(api) as client:
        response = client.get("/pair?b=5")

    assert response.status_code == 200, response.text
    assert response.json() == {"a": 1, "b": 5}


def test_rust_binds_keyword_only_and_positional_only_parameters():
    api = BoltAPI()

    @api.get("/keyword")
    async def keyword(*, a: int):
        return {"a": a}

    @api.get("/positional")
    async def positional(a: int, /):
        return {"a": a}

    with TestClient(api) as client:
        keyword_response = client.get("/keyword?a=3")
        positional_response = client.get("/positional?a=4")

    assert keyword_response.status_code == 200, keyword_response.text
    assert keyword_response.json() == {"a": 3}
    assert positional_response.status_code == 200, positional_response.text
    assert positional_response.json() == {"a": 4}


def test_a_missing_required_parameter_is_a_422_on_a_prebound_route():
    """Rust gives up the plan for a missing required value, and the injector gives the 422.

    Rust refuses a plan with a positional binding that is not required. A route
    with only keyword-only parameters has no positional binding. Thus only the
    required flag stops Rust from calling the handler with no value.
    """
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def item(item_id: int, q: str):
        return {"item_id": item_id, "q": q}

    @api.get("/keyword")
    async def keyword_item(*, q: str):
        return {"q": q}

    with TestClient(api) as client:
        response = client.get("/items/1")
        keyword_response = client.get("/keyword")

    assert response.status_code == 422
    assert keyword_response.status_code == 422, keyword_response.text
    assert keyword_response.json()["detail"] == "Missing required query parameter: q"


def test_rust_gets_the_exact_binding_plan():
    """Precision: a malformed plan makes Rust fall back to the Python injector, with the same response."""
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def item(
        item_id: int,
        x_token: Annotated[str, Header()],
        q: str | None = None,
    ):
        return {}

    assert api._handler_middleware[0]["rust_arg_bindings"] == [
        {
            "source": "path",
            "lookup_key": "item_id",
            "arg_name": "item_id",
            "arg_kind": "keyword",
            "required": True,
            "inject_none": False,
        },
        {
            "source": "header",
            "lookup_key": "x-token",
            "arg_name": "x_token",
            "arg_kind": "keyword",
            "required": True,
            "inject_none": False,
        },
        {
            "source": "query",
            "lookup_key": "q",
            "arg_name": "q",
            "arg_kind": "keyword",
            "required": False,
            "inject_none": False,
        },
    ]


def test_rust_gets_positional_bindings_when_no_parameter_is_optional():
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def item(item_id: int, q: str):
        return {}

    assert [binding["arg_kind"] for binding in api._handler_middleware[0]["rust_arg_bindings"]] == [
        "positional",
        "positional",
    ]


# --- A route with no guard -----------------------------------------------------


def test_a_route_with_auth_and_no_guard_authenticates_the_request():
    api = BoltAPI()

    @api.get("/me", auth=[JWTAuthentication(secret="test-secret")], guards=[])
    async def me(request):
        return {"user_id": request.context.get("user_id")}

    token = jwt.encode({"sub": "42", "exp": int(time.time()) + 60}, "test-secret", algorithm="HS256")
    with TestClient(api) as client:
        response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert response.json() == {"user_id": "42"}


def test_a_route_with_only_a_skip_flag_skips_compression():
    api = BoltAPI()

    @api.get("/raw", guards=[], auth=[])
    @no_compress
    async def raw():
        return {"data": "x" * 5000}

    with TestClient(api) as client:
        response = client.get("/raw", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers


def test_a_default_guard_that_cannot_compile_names_the_route():
    api = BoltAPI()

    with (
        override_settings(BOLT_DEFAULT_PERMISSION_CLASSES=[Requires]),
        pytest.raises(ImproperlyConfigured, match=r"Guard Requires on GET /admin could not be instantiated"),
    ):

        @api.get("/admin")
        async def admin():
            return {}


# --- Size settings and route names -----------------------------------------------


def test_a_spool_threshold_of_zero_writes_each_file_to_disk():
    """0 is a valid size: each uploaded file goes to a temporary file."""
    rolled: list[bool] = []

    with override_settings(BOLT_MEMORY_SPOOL_THRESHOLD=0):
        api = BoltAPI()

        @api.post("/upload")
        async def upload(file: Annotated[UploadFile, File()]):
            rolled.append(file.rolled_to_disk)
            return {"size": file.size}

        with TestClient(api) as client:
            response = client.post("/upload", files={"file": ("a.txt", b"abc", "text/plain")})

    assert response.status_code == 200, response.text
    assert rolled == [True]


def test_a_file_with_constraints_and_no_max_size_gets_the_global_limit():
    with override_settings(BOLT_MAX_UPLOAD_SIZE=2 * 1024 * 1024):
        api = BoltAPI()

        @api.post("/upload")
        async def upload(file: Annotated[UploadFile, File(allowed_types=["text/plain"])]):
            return {"size": file.size}

        with TestClient(api) as client:
            content = b"x" * (1536 * 1024)
            response = client.post("/upload", files={"file": ("a.txt", content, "text/plain")})

    assert response.status_code == 200, response.text
    assert response.json() == {"size": 1536 * 1024}


def test_a_route_with_no_guard_and_no_auth_is_named_in_a_metadata_error():
    """The metadata of a route with no middleware, auth or guard also names the route."""
    api = BoltAPI()

    @api.get("/open", guards=[], auth=[])
    async def open_route():
        return {}

    api._handler_middleware[0]["needs_body"] = None

    with pytest.raises(TypeError, match=r"'needs_body'.*GET /open"):
        TestClient(api)


# --- BOLT_TRUSTED_PROXIES ----------------------------------------------------


def _client_address_api(captured: dict) -> BoltAPI:
    api = BoltAPI()

    async def asgi_app(scope, receive, send):
        captured["client"] = scope["client"]
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    api.mount_asgi("/mounted", asgi_app)
    return api


def test_a_trusted_proxy_entry_with_host_bits_trusts_its_block():
    """`127.0.0.1/8` is the block 127.0.0.0/8, as Python and Rust read it."""
    captured: dict = {}

    with override_settings(BOLT_TRUSTED_PROXIES=["127.0.0.1/8"]), TestClient(_client_address_api(captured)) as client:
        response = client.get("/mounted", headers={"X-Forwarded-For": "203.0.113.10"})

    assert response.status_code == 204
    assert captured["client"][0] == "203.0.113.10"


@pytest.mark.parametrize(
    ("configured", "message"),
    [
        ("10.0.0.0/8", r"must be a list of addresses or CIDR blocks, got str '10.0.0.0/8'"),
        ([10], r"entry 10 is not an address or CIDR block: expected a string, got int"),
    ],
)
def test_a_wrong_trusted_proxy_setting_names_the_type(configured, message):
    with override_settings(BOLT_TRUSTED_PROXIES=configured), pytest.raises(ImproperlyConfigured, match=message):
        TestClient(BoltAPI())


# --- The Django 5.2 copy of build_policy -----------------------------------------


@pytest.mark.skipif(build_policy is None, reason="Django 5.2 has no django.utils.csp to compare with")
@pytest.mark.parametrize(
    "config",
    [
        {"default-src": ["'self'"]},
        {"default-src": ["'self'", "<nonce>"], "script-src": ["<nonce>"]},
        {"upgrade-insecure-requests": True, "block-all-mixed-content": False, "img-src": None},
        {"img-src": None, "default-src": ["'self'"]},
        {"script-src": ["<nonce>"], "default-src": ["'self'"]},
        {"img-src": {"https://b.example", "https://a.example"}},
        {"style-src": ("'self'", "https://cdn.example")},
        {"report-uri": "/csp-report/"},
        {"default-src": ["'self'"], "img-src": ["https://img.example"]},
        {"default-src": []},
    ],
)
def test_the_django_5_2_copy_builds_the_policy_that_django_builds(config):
    config = {
        directive: [CSP.NONCE if value == "<nonce>" else value for value in values]
        if isinstance(values, list)
        else values
        for directive, values in config.items()
    }
    assert _build_policy_without_nonce(config) == build_policy(config)
