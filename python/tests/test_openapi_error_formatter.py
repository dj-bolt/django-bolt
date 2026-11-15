"""The OpenAPI schema describes the error body of BoltAPI(error_formatter=...).

Each test reads the schema from the docs route, as a client generator does,
and compares it with the error response that a client gets.
"""

from __future__ import annotations

import http.client
from typing import Annotated

import msgspec
import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

from django_bolt import BoltAPI
from django_bolt.error_handlers import BoltError, ProblemDetails, error_schema, problem_details
from django_bolt.exceptions import HTTPException
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import OpenAPIStrictError, SchemaGenerator
from django_bolt.param_functions import Cookie, Form, Header, Query
from django_bolt.responses import PlainText, Response
from django_bolt.testing import TestClient


class ItemIn(msgspec.Struct):
    name: str


class Item(msgspec.Struct):
    id: int
    name: str


class Conflict(msgspec.Struct):
    reason: str


class Rejected(msgspec.Struct):
    reason: str


class ErrorBody(msgspec.Struct):
    """The body of a vendor error."""

    code: str
    message: str


@error_schema(ErrorBody, media_type="application/vnd.error+json")
def vendor_error(error: BoltError, request) -> Response:
    body = {"code": error.kind, "message": error.detail}
    return Response(body, status_code=error.status, media_type="application/vnd.error+json")


@error_schema(ErrorBody)
def json_error(error: BoltError, request) -> Response:
    return Response({"code": error.kind, "message": error.detail}, status_code=error.status)


def plain_error(error: BoltError, request) -> PlainText:
    return PlainText(error.detail, status_code=error.status)


# The 422 body without a formatter: {"detail": [...]}.
DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "detail": {
            "type": "array",
            "description": "List of validation errors",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": "Error type",
                        "example": "validation_error",
                    },
                    "loc": {
                        "type": "array",
                        "description": "Location of the error (field path)",
                        "items": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                        "example": ["body", "is_active"],
                    },
                    "msg": {
                        "type": "string",
                        "description": "Error message",
                        "example": "Expected `bool`, got `int`",
                    },
                    "input": {"description": "The input value that caused the error (optional)"},
                },
                "required": ["type", "loc", "msg"],
            },
        }
    },
    "required": ["detail"],
}
# The 422 body of a route with a parameter or a form field: the detail is a list of errors or a text.
PARAMETER_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "detail": {
            "anyOf": [
                DETAIL_SCHEMA["properties"]["detail"],
                {"type": "string", "description": "Validation error message"},
            ]
        }
    },
    "required": ["detail"],
}
REJECTED_REF = {"$ref": "#/components/schemas/Rejected"}
PROBLEM_REF = {"$ref": "#/components/schemas/ProblemDetails"}


def _api(formatter=None, *, strict: bool = False) -> BoltAPI:
    api = BoltAPI(
        error_formatter=formatter,
        openapi_config=OpenAPIConfig(title="Errors", version="1.0.0", strict=strict),
    )

    @api.post("/items", response_model={200: Item, 409: Conflict})
    async def create_item(data: ItemIn):
        if data.name == "taken":
            raise HTTPException(409, body=Conflict(reason="taken"))
        return Item(id=1, name=data.name)

    @api.post("/reviews", response_model={200: Item, 422: Rejected})
    async def create_review(data: ItemIn):
        if data.name == "spam":
            raise HTTPException(422, body=Rejected(reason="spam"))
        return Item(id=1, name=data.name)

    api._register_openapi_routes()
    return api


def _spec(api: BoltAPI) -> dict:
    with TestClient(api) as client:
        response = client.get("/docs/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    validate(spec)
    return spec


def _responses(spec: dict, path: str = "/items") -> dict:
    return spec["paths"][path]["post"]["responses"]


def test_without_formatter_the_422_keeps_the_detail_schema():
    assert _responses(_spec(_api()))["422"] == {
        "description": "Validation Error - Request data failed validation",
        "content": {"application/json": {"schema": DETAIL_SCHEMA}},
    }


def test_problem_details_documents_the_422_that_the_client_gets():
    api = _api(problem_details, strict=True)
    spec = _spec(api)
    responses = _responses(spec)

    assert responses["422"]["content"] == {"application/problem+json": {"schema": PROBLEM_REF}}
    problem = spec["components"]["schemas"]["ProblemDetails"]
    assert problem["required"] == ["type", "title", "status", "detail", "instance"]
    assert problem["properties"]["errors"]["items"] == {"$ref": "#/components/schemas/ProblemFieldError"}
    assert spec["components"]["schemas"]["ProblemFieldError"]["required"] == ["loc", "msg", "type"]
    # An error model that the route declares keeps its entry: its typed body skips the formatter.
    assert responses["409"]["content"] == {"application/json": {"schema": {"$ref": "#/components/schemas/Conflict"}}}

    with TestClient(api) as client:
        invalid = client.post("/items", json={"name": 1})
        taken = client.post("/items", json={"name": "taken"})

    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/problem+json"
    body = msgspec.convert(invalid.json(), ProblemDetails)
    assert body.status == 422
    assert [error.loc for error in body.errors] == [["body", "name"]]
    assert taken.status_code == 409
    assert taken.headers["content-type"] == "application/json"
    assert taken.json() == {"reason": "taken"}


def _parameter_api(formatter=None) -> BoltAPI:
    api = BoltAPI(
        error_formatter=formatter,
        openapi_config=OpenAPIConfig(title="Errors", version="1.0.0", strict=True),
    )

    @api.get("/ping")
    async def ping() -> Item:
        return Item(id=1, name="ping")

    @api.get("/query")
    async def by_query(n: Annotated[int, Query(ge=1)]) -> Item:
        return Item(id=n, name="query")

    @api.get("/path/{n}")
    async def by_path(n: int) -> Item:
        return Item(id=n, name="path")

    @api.get("/header")
    async def by_header(x_n: Annotated[int, Header()]) -> Item:
        return Item(id=x_n, name="header")

    @api.get("/cookie")
    async def by_cookie(n: Annotated[int, Cookie()]) -> Item:
        return Item(id=n, name="cookie")

    @api.put("/items/{n}")
    async def replace_item(n: int, data: ItemIn) -> Item:
        return Item(id=n, name=data.name)

    @api.post("/forms")
    async def send_form(n: Annotated[int, Form()]) -> Item:
        return Item(id=n, name="form")

    api._register_openapi_routes()
    return api


# One request for each parameter location, with a value that is not an integer.
_BAD_PARAMETER = {
    "/query": {"url": "/query?n=abc"},
    "/path/{n}": {"url": "/path/abc"},
    "/header": {"url": "/header", "headers": {"x-n": "abc"}},
    "/cookie": {"url": "/cookie", "headers": {"cookie": "n=abc"}},
}


@pytest.mark.parametrize("path", list(_BAD_PARAMETER))
def test_parameter_route_documents_the_422_that_the_client_gets(path):
    """Bolt validates a path, query, header or cookie parameter, so the route has a 422."""
    api = _parameter_api()
    spec = _spec(api)
    request = _BAD_PARAMETER[path]

    with TestClient(api) as client:
        invalid = client.get(request["url"], headers=request.get("headers"))

    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/json"
    responses = spec["paths"][path]["get"]["responses"]
    assert sorted(responses) == ["200", "422"]
    assert responses["422"] == {
        "description": "Validation Error - Request data failed validation",
        "content": {"application/json": {"schema": PARAMETER_DETAIL_SCHEMA}},
    }
    Draft202012Validator(PARAMETER_DETAIL_SCHEMA).validate(invalid.json())


def test_a_422_detail_is_a_text_or_a_list_of_errors():
    """A parameter of the wrong type gives a text. A value out of its limit gives a list of errors."""
    api = _parameter_api()
    validator = Draft202012Validator(PARAMETER_DETAIL_SCHEMA)

    with TestClient(api) as client:
        missing = client.get("/query")
        wrong_type = client.get("/query?n=abc")
        too_small = client.get("/query?n=0")
        missing_form_field = client.post("/forms", data={})
        bad_form_value = client.post("/forms", data={"n": "abc"})
        bad_body = client.put("/items/1", json={"name": 1})

    assert missing.json() == {"detail": "Missing required query parameter: n"}
    assert isinstance(wrong_type.json()["detail"], str)
    assert too_small.json()["detail"][0]["loc"] == ["query", "n"]
    assert missing_form_field.json() == {"detail": "Missing required form field: n"}
    assert bad_form_value.json()["detail"][0]["loc"] == ["body", "n"]
    assert bad_body.json()["detail"][0]["loc"] == ["body", "name"]
    for response in (missing, wrong_type, too_small, missing_form_field, bad_form_value, bad_body):
        assert response.status_code == 422
        validator.validate(response.json())

    spec = _spec(api)
    # A route with a parameter or a form field can send the two forms of the detail.
    for method, path in (("put", "/items/{n}"), ("post", "/forms")):
        content = spec["paths"][path][method]["responses"]["422"]["content"]
        assert content == {"application/json": {"schema": PARAMETER_DETAIL_SCHEMA}}


def test_route_with_no_input_has_no_422():
    assert sorted(_spec(_parameter_api())["paths"]["/ping"]["get"]["responses"]) == ["200"]


def test_parameter_route_documents_the_422_of_the_formatter():
    api = _parameter_api(problem_details)
    spec = _spec(api)

    for path in _BAD_PARAMETER:
        content = spec["paths"][path]["get"]["responses"]["422"]["content"]
        assert content == {"application/problem+json": {"schema": PROBLEM_REF}}
    assert "422" not in spec["paths"]["/ping"]["get"]["responses"]

    with TestClient(api) as client:
        missing = client.get("/query")
        wrong_type = client.get("/query?n=abc")

    for response in (missing, wrong_type):
        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        msgspec.convert(response.json(), ProblemDetails)


def test_parameter_route_has_no_422_without_error_responses():
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Errors", version="1.0.0", include_error_responses=False))

    @api.get("/query")
    async def by_query(n: int) -> Item:
        return Item(id=n, name="query")

    api._register_openapi_routes()
    assert sorted(_spec(api)["paths"]["/query"]["get"]["responses"]) == ["200"]


@pytest.mark.parametrize(
    ("formatter", "content"),
    [
        (None, {"application/json": {"schema": {"anyOf": [REJECTED_REF, DETAIL_SCHEMA]}}}),
        (
            problem_details,
            {"application/json": {"schema": REJECTED_REF}, "application/problem+json": {"schema": PROBLEM_REF}},
        ),
        (plain_error, {"application/json": {"schema": REJECTED_REF}}),
    ],
    ids=["no-formatter", "problem-details", "no-error-schema"],
)
def test_declared_422_keeps_its_body_next_to_the_validation_body(formatter, content):
    """A body route can send its own typed 422 and the validation 422, so the schema shows both."""
    response = _responses(_spec(_api(formatter)), "/reviews")["422"]
    # The route declares this entry, so it keeps its description. The phrase depends on the Python version.
    assert response == {"description": http.client.responses[422], "content": content}


def test_declared_422_and_validation_422_reach_the_client():
    with TestClient(_api(problem_details)) as client:
        spam = client.post("/reviews", json={"name": "spam"})
        invalid = client.post("/reviews", json={"name": 1})

    assert spam.status_code == 422
    assert spam.headers["content-type"] == "application/json"
    msgspec.convert(spam.json(), Rejected)
    assert invalid.status_code == 422
    assert invalid.headers["content-type"] == "application/problem+json"
    msgspec.convert(invalid.json(), ProblemDetails)


def test_problem_details_errors_stay_a_list_of_field_errors():
    """An "errors" key in the extra of the error does not take the place of the field errors."""
    api = BoltAPI(error_formatter=problem_details)

    @api.get("/vote")
    async def vote() -> Item:
        raise HTTPException(422, detail="Vote closed", extra={"errors": "closed", "poll": 7})

    with TestClient(api) as client:
        response = client.get("/vote")

    assert response.status_code == 422
    body = response.json()
    assert "errors" not in body
    assert body["poll"] == 7
    msgspec.convert(body, ProblemDetails)


@pytest.mark.parametrize(
    ("detail", "extra", "members"),
    [
        # The keys of a dict detail are extension members, as the keys of the extra are.
        ({"code": "out_of_stock", "items": [3, 7]}, None, {"code": "out_of_stock", "items": [3, 7]}),
        # A list detail is the "extra" member, as an extra that is a list.
        ([{"item": 3}, {"item": 7}], None, {"extra": [{"item": 3}, {"item": 7}]}),
        # A key does not replace a standard member or a key of the extra, and "errors" is not used.
        (
            {"status": 200, "detail": {"a": 1}, "errors": "x", "code": "detail", "poll": 7},
            {"code": "extra"},
            {"code": "extra", "poll": 7},
        ),
    ],
)
def test_problem_details_detail_is_a_string(detail, extra, members):
    """RFC 9457 section 3.1.4: "detail" is a string. Extension members hold structured data."""
    api = BoltAPI(error_formatter=problem_details)

    @api.get("/order")
    async def order():
        raise HTTPException(400, detail=detail, extra=extra)

    with TestClient(api) as client:
        response = client.get("/order")

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Bad Request",
        "status": 400,
        "detail": "Bad Request",
        "instance": "/order",
        **members,
    }
    msgspec.convert(response.json(), ProblemDetails)


def test_declared_error_schema_documents_the_422():
    api = _api(vendor_error, strict=True)
    spec = _spec(api)

    assert _responses(spec)["422"]["content"] == {
        "application/vnd.error+json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}
    }
    assert spec["components"]["schemas"]["ErrorBody"]["required"] == ["code", "message"]

    with TestClient(api) as client:
        invalid = client.post("/items", json={})

    assert invalid.headers["content-type"] == "application/vnd.error+json"
    msgspec.convert(invalid.json(), ErrorBody)


def test_error_schema_media_type_is_json_by_default():
    spec = _spec(_api(json_error, strict=True))

    assert _responses(spec)["422"]["content"] == {
        "application/json": {"schema": {"$ref": "#/components/schemas/ErrorBody"}}
    }


def test_formatter_without_error_schema_documents_no_422_body():
    """Bolt cannot know the body of the formatter, so it does not guess one."""
    assert _responses(_spec(_api(plain_error)))["422"] == {
        "description": "Validation Error - Request data failed validation",
    }


def test_strict_mode_reports_a_formatter_without_error_schema():
    api = _api(plain_error, strict=True)
    with pytest.raises(OpenAPIStrictError, match=r"error_formatter plain_error has no error_schema"):
        SchemaGenerator(api, api._openapi_config).generate()
