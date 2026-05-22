from __future__ import annotations

from typing import Union, Optional

import msgspec

from django_bolt import BoltAPI
from django_bolt.serializers import Serializer
from django_bolt.testing import TestClient


class Cat(msgspec.Struct, tag=True, tag_field="_type"):
    name: str
    meows: bool = True


class Dog(Serializer, tag=True, tag_field="_type"):
    name: str
    barks: bool = True


def test_union_response_model():
    api = BoltAPI()

    @api.get("/pet/{pet_type}", response_model=Cat | Dog)
    async def get_pet(pet_type: str):
        if pet_type == "cat":
            return {"name": "Whiskers", "meows": True, "type": "cat", "_type": "Cat"}
        return {"name": "Fido", "barks": True, "type": "dog", "_type": "Dog"}

    with TestClient(api) as client:
        # Test cat response
        resp = client.get("/pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        # Test dog response
        resp = client.get("/pet/dog")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


def test_union_return_annotation():
    api = BoltAPI()

    @api.get("/pet/{pet_type}")
    async def get_pet(pet_type: str) -> Cat | Dog:
        if pet_type == "cat":
            return Cat(name="Whiskers")
        return Dog(name="Fido")

    with TestClient(api) as client:
        resp = client.get("/pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        resp = client.get("/pet/dog")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


def test_union_validation_error():
    api = BoltAPI(validate_response=True)

    @api.get("/bad-pet", response_model=Cat | Dog)
    async def get_bad_pet():
        return {"name": "Bird", "flies": True}

    with TestClient(api) as client:
        resp = client.get("/bad-pet")
        assert resp.status_code == 500
        assert b"Response validation error" in resp.content


def test_optional_response():
    api = BoltAPI()

    @api.get("/maybe-pet/{pet_type}", response_model=Cat | None)
    async def get_maybe_pet(pet_type: str):
        if pet_type == "cat":
            return Cat(name="Whiskers")
        return None

    with TestClient(api) as client:
        resp = client.get("/maybe-pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        resp = client.get("/maybe-pet/none")
        assert resp.status_code == 200
        assert resp.json() is None


def test_typing_union_compatibility():
    api = BoltAPI()

    @api.get("/typing-union", response_model=Cat | Dog)
    async def get_typing_union():
        return Dog(name="Fido")

    with TestClient(api) as client:
        resp = client.get("/typing-union")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}
