"""The OpenAPI schema describes the error body of BoltAPI(error_formatter=...).

Each test reads the schema from the docs route, as a client generator does,
and compares it with the error response that a client gets.
"""

from __future__ import annotations

import http.client

import msgspec
import pytest
from openapi_spec_validator import validate

from django_bolt import BoltAPI
from django_bolt.error_handlers import BoltError, ProblemDetails, error_schema, problem_details
from django_bolt.exceptions import HTTPException
from django_bolt.openapi import OpenAPIConfig
from django_bolt.openapi.schema_generator import OpenAPIStrictError, SchemaGenerator
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
