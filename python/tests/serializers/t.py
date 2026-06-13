from __future__ import annotations

from typing import Annotated

from django_bolt.serializers import (
    URL,
    Email,
    Meta,
    NonEmptyStr,
    Percentage,
    PositiveInt,
    Serializer,
    Slug,
    computed_field,
    field,
    field_validator,
    model_validator,
)


class AuthorSerializer(Serializer):
    """Serializer for Author model with computed field."""

    id: int
    name: NonEmptyStr
    email: Email
    bio: str = ""

    @computed_field
    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"


class TagSerializer(Serializer):
    """Simple serializer for Tag model."""

    id: int
    name: str
    description: str = ""


class ProductSerializer(Serializer):
    """Demonstrates all serializer features in a compact form."""

    # Basic types + read_only via field()
    id: int = field(read_only=True)
    name: NonEmptyStr
    slug: Slug

    # Constraints via Meta (validated by msgspec)
    sku: Annotated[str, Meta(pattern=r"^[A-Z]{2}-\d{4}$")]
    description: Annotated[str, Meta(max_length=500)] = ""
    price: Annotated[float, Meta(ge=0.0)]

    # Custom types (have built-in constraints)
    quantity: PositiveInt
    discount: Percentage = 0.0
    email: Email | None = None
    website: URL | None = None

    # field() options: alias, source, write_only, default_factory
    desc: str = field(alias="short_desc", default="")
    category: str = field(source="category.name", default="General")
    secret: str = field(write_only=True, default="")
    tags: list[str] = field(default_factory=list)

    # Nested serializers
    author: AuthorSerializer | None = None
    related: list[TagSerializer] = field(default_factory=list)

    class Config:
        read_only = {"id"}
        write_only = {"secret"}
        field_sets = {
            "list": ["id", "name", "price"],
            "detail": ["id", "name", "slug", "description", "price", "quantity"],
        }

    @field_validator("name")
    def normalize_name(cls, value: str) -> str:
        return value.strip().title()

    @model_validator
    def validate_pricing(self) -> ProductSerializer:
        if self.discount > 0 and self.price <= 0:
            raise ValueError("Cannot discount zero-priced item")
        return self

    @computed_field
    def display_price(self) -> str:
        return f"${self.price:.2f}"

    @computed_field
    def on_sale(self) -> bool:
        return self.discount > 0
