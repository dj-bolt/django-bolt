"""
Serializer validation errors must stay structured, and validators must run.

Two defects reported in issue #280, both in the custom-validator path:

1. A serializer's own validators raise `RequestValidationError` from
   `__post_init__`, which msgspec catches during decoding and re-raises as a
   plain `msgspec.ValidationError` whose message is `str(exc)` — every field's
   message joined with "; ". The per-field `loc`, `msg`, and `type` are gone by
   the time the 422 is built, leaving one entry with `loc: ["body"]`.

2. Pylance rejects the documented validator form, because an undecorated
   `def validate(cls, v)` is inferred as an instance method (`cls: Self`) and
   the decorator declared `type[Serializer]`. The obvious fix — and the form
   Pydantic requires — is `@classmethod`, which here silently dropped the
   validator: `classmethod` is a descriptor, not callable, so the collector's
   `callable(value)` filter skipped it and the validator never ran.
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer, field_validator, model_validator
from django_bolt.testing import TestClient


class RegisterRequest(Serializer):
    name: str
    collage_code: str
    phone_number: str

    @field_validator("collage_code")
    def validate_collage(cls, v: str) -> str:
        if v != "ok":
            raise ValueError("collage Code is Not Valid")
        return v

    @field_validator("phone_number")
    def validate_phone(cls, v: str) -> str:
        if v != "ok":
            raise ValueError("The phone number entered is not valid.")
        return v


@pytest.fixture
def client():
    api = BoltAPI()

    @api.post("/register")
    async def register(payload: RegisterRequest):
        return {"ok": True}

    with TestClient(api) as test_client:
        yield test_client


def _register(client, **overrides):
    body = {"name": "n", "collage_code": "bad", "phone_number": "bad"}
    body.update(overrides)
    return client.post("/register", json=body)


def test_each_failing_field_gets_its_own_error(client):
    """One entry per field, not one entry holding every message."""
    response = _register(client)
    assert response.status_code == 422

    errors = response.json()["detail"]
    assert len(errors) == 2, f"expected one error per failing field, got {errors}"


def test_errors_carry_the_failing_field_in_loc(client):
    """`loc` must locate the field, which is the whole point of `loc`."""
    errors = _register(client).json()["detail"]

    assert [list(e["loc"]) for e in errors] == [
        ["body", "collage_code"],
        ["body", "phone_number"],
    ]


def test_messages_are_not_concatenated(client):
    """Each `msg` is that field's message alone — no "; "-joined blob."""
    errors = _register(client).json()["detail"]
    by_field = {e["loc"][-1]: e["msg"] for e in errors}

    assert by_field["collage_code"] == "collage Code is Not Valid"
    assert by_field["phone_number"] == "The phone number entered is not valid."
    for msg in by_field.values():
        assert "; " not in msg


def test_error_type_is_per_error(client):
    """Every entry carries its own type, not the first error's type."""
    errors = _register(client).json()["detail"]
    assert [e["type"] for e in errors] == ["value_error", "value_error"]


def test_single_field_failure_still_reports_that_field(client):
    """The single-error case must not regress to the generic body location."""
    errors = _register(client, phone_number="ok").json()["detail"]

    assert len(errors) == 1
    assert list(errors[0]["loc"]) == ["body", "collage_code"]


# ---------------------------------------------------------------------------
# @classmethod validators must not be silently ignored
# ---------------------------------------------------------------------------


def test_classmethod_field_validator_runs():
    """`@field_validator` above `@classmethod` — the form the type checker wants."""

    class WithClassmethod(Serializer):
        a: str

        @field_validator("a")
        @classmethod
        def upper(cls, v: str) -> str:
            return v.upper()

    assert WithClassmethod.__has_field_validators__, "validator was dropped at class creation"
    assert WithClassmethod(a="fine").a == "FINE"


def test_classmethod_field_validator_reports_failures():
    """A dropped validator fails open — the invalid value would be accepted."""

    class WithClassmethod(Serializer):
        a: str

        @field_validator("a")
        @classmethod
        def reject(cls, v: str) -> str:
            raise ValueError(f"nope: {v}")

    with pytest.raises(Exception) as exc_info:
        WithClassmethod(a="anything")
    assert "nope: anything" in str(exc_info.value)


def test_classmethod_below_decorator_also_runs():
    """The reverse decorator order must behave identically."""

    class ReverseOrder(Serializer):
        a: str

        @classmethod
        @field_validator("a")
        def upper(cls, v: str) -> str:
            return v.upper()

    assert ReverseOrder(a="fine").a == "FINE"


def test_classmethod_model_validator_is_rejected_not_ignored():
    """A model validator takes the instance, so `@classmethod` cannot work.

    It must say so at decoration time. Silently collecting nothing would leave
    a serializer that looks validated and is not — the failure mode this whole
    issue is about.
    """
    with pytest.raises(TypeError, match="classmethod"):

        class Broken(Serializer):
            a: str

            @model_validator
            @classmethod
            def check(cls, instance):
                return instance
