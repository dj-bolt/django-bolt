"""Tests for django-bolt testing utilities.

This file tests the TestClient (V2) that routes through Rust with per-instance state.
"""

import asyncio
from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI, _core
from django_bolt.param_functions import Header
from django_bolt.testing import AsyncTestClient, TestClient


def test_simple_get_request():
    """Test basic GET request with TestClient."""
    api = BoltAPI()

    @api.get("/hello")
    async def hello():
        return {"message": "world"}

    with TestClient(api) as client:
        response = client.get("/hello")
        assert response.status_code == 200
        assert response.json() == {"message": "world"}


def test_path_parameters():
    """Test path parameters extraction."""
    api = BoltAPI()

    @api.get("/users/{user_id}")
    async def get_user(user_id: int):
        return {"id": user_id, "name": f"User {user_id}"}

    with TestClient(api) as client:
        response = client.get("/users/123")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 123
        assert data["name"] == "User 123"


def test_query_parameters():
    """Test query parameters extraction."""
    api = BoltAPI()

    @api.get("/search")
    async def search(q: str, limit: int = 10):
        return {"query": q, "limit": limit}

    with TestClient(api) as client:
        response = client.get("/search?q=test&limit=20")
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "test"
        assert data["limit"] == 20


def test_post_with_body():
    """Test POST with JSON body."""

    class UserCreate(msgspec.Struct):
        name: str
        email: str

    api = BoltAPI()

    @api.post("/users")
    async def create_user(user: UserCreate):
        return {"id": 1, "name": user.name, "email": user.email}

    with TestClient(api) as client:
        response = client.post("/users", json={"name": "John", "email": "john@example.com"})
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 1
        assert data["name"] == "John"
        assert data["email"] == "john@example.com"


def test_404_not_found():
    """Test 404 for non-existent route."""
    api = BoltAPI()

    @api.get("/hello")
    async def hello():
        return {"message": "world"}

    with TestClient(api) as client:
        response = client.get("/nonexistent")
        assert response.status_code == 404


def test_multiple_methods():
    """Test different HTTP methods on same path."""
    api = BoltAPI()

    @api.get("/resource")
    async def get_resource():
        return {"method": "GET"}

    @api.post("/resource")
    async def create_resource():
        return {"method": "POST"}

    @api.put("/resource")
    async def update_resource():
        return {"method": "PUT"}

    @api.delete("/resource")
    async def delete_resource():
        return {"method": "DELETE"}

    with TestClient(api) as client:
        assert client.get("/resource").json() == {"method": "GET"}
        assert client.post("/resource").json() == {"method": "POST"}
        assert client.put("/resource").json() == {"method": "PUT"}
        assert client.delete("/resource").json() == {"method": "DELETE"}


def test_headers():
    """Test request headers."""
    api = BoltAPI()

    @api.get("/with-header")
    async def with_header(x_custom: Annotated[str, Header()]):
        return {"header_value": x_custom}

    with TestClient(api) as client:
        response = client.get("/with-header", headers={"X-Custom": "test-value"})
        assert response.status_code == 200
        assert response.json() == {"header_value": "test-value"}


def test_status_code():
    """Test custom status codes."""
    api = BoltAPI()

    @api.post("/created", status_code=201)
    async def create():
        return {"created": True}

    with TestClient(api) as client:
        response = client.post("/created")
        assert response.status_code == 201
        assert response.json() == {"created": True}


def _percent_encoded_path_api():
    api = BoltAPI()

    @api.get("/int/{value}")
    async def get_int(value: int):
        return {"value": value}

    @api.get("/str/{value}")
    async def get_str(value: str):
        return {"value": value}

    return api


def test_percent_encoded_path_params():
    """The client sends the encoded path, and Bolt decodes path params as in production."""
    with TestClient(_percent_encoded_path_api()) as client:
        response = client.get("/int/%0A")
        assert response.status_code == 422

        response = client.get("/str/hello%20world")
        assert response.status_code == 200
        assert response.json() == {"value": "hello world"}

        response = client.get("/str/a%2Fb")
        assert response.status_code == 200
        assert response.json() == {"value": "a/b"}


def test_percent_encoded_path_params_async_client():
    """AsyncTestClient sends the encoded path too."""

    async def run():
        async with AsyncTestClient(_percent_encoded_path_api()) as client:
            bad = await client.get("/int/%09")
            spaced = await client.get("/str/hello%20world")
            return bad.status_code, spaced.status_code, spaced.json()

    assert asyncio.run(run()) == (422, 200, {"value": "hello world"})


def test_invalid_uri_raises_value_error():
    """The Rust test backend rejects a URI that is not valid, and does not panic."""
    with TestClient(_percent_encoded_path_api()) as client, pytest.raises(ValueError, match="Invalid request URI"):
        _core.test_request(
            app_id=client.app_id,
            method="GET",
            path="/int/\n",
            headers=[],
            body=b"",
            query_string=None,
        )
