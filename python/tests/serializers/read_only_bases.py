"""Base serializers for the ``Config.read_only`` tests of a field of a base class.

The subclasses are in another module. The annotations here are strings, and
``Tag`` is not a name in the module of the subclasses.
"""

from __future__ import annotations

import msgspec

from django_bolt.serializers import Serializer


class Tag(Serializer):
    label: str = ""


class TaggedBase(Serializer):
    id: int = 0
    tag: Tag | None = None
    tags: list[str] = []
    user_id: int = msgspec.field(name="userId", default=0)
    name: str


class AuthoredBase(Serializer):
    author: Author | None = None


class AuthoredChild(AuthoredBase):
    """``Author`` is not defined yet, and the class does not forbid unknown fields."""

    class Config:
        read_only = {"author"}


class Author(Serializer):
    name: str = ""
