"""App under test for Serializer validator error shape over real HTTP.

A ``@field_validator`` (plain and ``@classmethod`` form) on a body serializer
must produce one 422 entry per failing field on create (POST), full update
(PUT), partial update (PATCH) and list bodies, and nested serializers must keep
the container path in ``loc``.
"""

from __future__ import annotations

from typing import Annotated

from django_bolt import BoltAPI
from django_bolt.params import Body
from django_bolt.serializers import Serializer, field_validator

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


class RegisterRequest(Serializer):
    collage_code: str
    phone_number: str

    @field_validator("collage_code")
    def validate_collage(cls, v: str) -> str:
        if v != "ok":
            raise ValueError("collage Code is Not Valid")
        return v

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if v != "ok":
            raise ValueError("The phone number entered is not valid.")
        return v


class RegisterPatch(Serializer):
    collage_code: str | None = None
    phone_number: str | None = None

    @field_validator("collage_code")
    def validate_collage(cls, v: str | None) -> str | None:
        if v is not None and v != "ok":
            raise ValueError("collage Code is Not Valid")
        return v

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if v is not None and v != "ok":
            raise ValueError("The phone number entered is not valid.")
        return v


class Profile(Serializer):
    register: RegisterRequest


class Account(Serializer):
    profile: Profile


@api.post("/register")
async def create(payload: RegisterRequest):
    return {"ok": True}


@api.put("/register/{item_id}")
async def update(item_id: int, payload: RegisterRequest):
    return {"ok": item_id}


@api.patch("/register/{item_id}")
async def partial_update(item_id: int, payload: RegisterPatch):
    return {"ok": item_id}


@api.post("/register/bulk")
async def bulk_create(payload: Annotated[list[RegisterRequest], Body()]):
    return {"ok": len(payload)}


@api.post("/profile")
async def create_profile(payload: Profile):
    return {"ok": True}


@api.post("/account")
async def create_account(payload: Account):
    return {"ok": True}


@api.post("/profile/bulk")
async def bulk_profiles(payload: Annotated[list[Profile], Body()]):
    return {"ok": len(payload)}
