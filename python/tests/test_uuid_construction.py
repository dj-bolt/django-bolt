"""The uuid.UUID that Rust builds for a typed parameter is a complete UUID.

Rust builds the object without ``uuid.UUID.__init__``. These tests make sure
that the object behaves the same as one that Python builds from the string.
"""

from __future__ import annotations

import copy
import pickle
import uuid
from typing import Annotated

import pytest

from django_bolt import BoltAPI
from django_bolt.param_functions import Header, Query
from django_bolt.testing import TestClient

VALUES = [
    "5cd2b626-39a7-4893-b8ca-7a1c3478d754",
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "0190a8b2-7c3e-7def-8abc-0123456789ab",
]


def _describe(value: uuid.UUID, raw: str) -> dict:
    expected = uuid.UUID(raw)
    immutable = False
    try:
        value.int = 0  # type: ignore[misc]
    except TypeError:
        immutable = True
    return {
        "type": type(value).__name__,
        "equal": value == expected,
        "int": value.int == expected.int,
        "hash": hash(value) == hash(expected),
        "str": str(value) == str(expected),
        "version": value.version == expected.version,
        "is_safe": value.is_safe is uuid.SafeUUID.unknown,
        "immutable": immutable,
        "pickle": pickle.loads(pickle.dumps(value)) == expected,
        "copy": copy.deepcopy(value) == expected,
        "ordering": (value <= expected) and not (value < expected),
    }


@pytest.fixture(scope="module")
def client():
    api = BoltAPI()

    @api.get("/path/{value}")
    async def path_uuid(value: uuid.UUID, raw: Annotated[str, Query()]):
        return _describe(value, raw)

    @api.get("/query")
    def query_uuid(value: Annotated[uuid.UUID, Query()], raw: Annotated[str, Query()]):
        return _describe(value, raw)

    @api.get("/header")
    async def header_uuid(x_id: Annotated[uuid.UUID, Header()], raw: Annotated[str, Query()]):
        return _describe(x_id, raw)

    with TestClient(api) as test_client:
        yield test_client


ALL_TRUE = {
    "type": "UUID",
    **dict.fromkeys(
        ["equal", "int", "hash", "str", "version", "is_safe", "immutable", "pickle", "copy", "ordering"], True
    ),
}


@pytest.mark.parametrize("raw", VALUES)
def test_path_uuid_is_complete(client, raw):
    response = client.get(f"/path/{raw}?raw={raw}")
    assert response.status_code == 200, response.text
    assert response.json() == ALL_TRUE


@pytest.mark.parametrize("raw", VALUES)
def test_query_uuid_is_complete(client, raw):
    response = client.get(f"/query?value={raw.upper()}&raw={raw}")
    assert response.status_code == 200, response.text
    assert response.json() == ALL_TRUE


@pytest.mark.parametrize("raw", VALUES)
def test_header_uuid_is_complete(client, raw):
    response = client.get(f"/header?raw={raw}", headers={"x-id": raw})
    assert response.status_code == 200, response.text
    assert response.json() == ALL_TRUE
