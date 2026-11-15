"""Reusable validated types for Serializer fields.

These types provide validation for common data formats used in web APIs.
All validation happens during deserialization (parsing), not serialization.

Usage:
    from django_bolt.serializers import Serializer, Email, URL, Slug

    class UserSerializer(Serializer):
        email: Email
        website: URL | None = None
        slug: Slug

Type Categories:
    - String lengths: Char50, Char100, Char255, Text
    - Validated strings: Email, URL, Slug, UUID
    - Integers: SmallInt, Int, BigInt (and Positive variants)
    - Floats: Float, PositiveFloat
    - Network: IPv4, IPv6, Port, HttpStatus
    - Geo: Latitude, Longitude
    - Auth: Username, Password
    - Misc: Phone, HexColor, CurrencyCode, CountryCode, LanguageCode, Timezone
    - Constraints: NonEmptyStr, PositiveInt, NonNegativeInt, Percentage, Rating
"""

from __future__ import annotations

from typing import Annotated

from msgspec import Meta

# Python reads `$` as the end of the string, and also as the position before a
# line break at the end. Each pattern ends with `$(?!\n)`, so a value with a
# line break after it does not match. A JSON Schema validator (ECMA 262) reads
# `$(?!\n)` as the end of the string.

# =============================================================================
# String Length Types (Django CharField equivalents)
# =============================================================================

Char50 = Annotated[
    str,
    Meta(max_length=50, description="String with max 50 chars"),
]
"""String with max 50 characters."""

Char100 = Annotated[
    str,
    Meta(max_length=100, description="String with max 100 chars"),
]
"""String with max 100 characters."""

Char150 = Annotated[
    str,
    Meta(max_length=150, description="String with max 150 chars"),
]
"""String with max 150 characters. Default for Django auth username."""

Char200 = Annotated[
    str,
    Meta(max_length=200, description="String with max 200 chars"),
]
"""String with max 200 characters."""

Char255 = Annotated[
    str,
    Meta(max_length=255, description="String with max 255 chars"),
]
"""String with max 255 characters. Common varchar default."""

Char500 = Annotated[
    str,
    Meta(max_length=500, description="String with max 500 chars"),
]
"""String with max 500 characters."""

Char1000 = Annotated[
    str,
    Meta(max_length=1000, description="String with max 1000 chars"),
]
"""String with max 1000 characters."""

Text = Annotated[
    str,
    Meta(description="Unlimited text"),
]
"""Unlimited text string."""

# =============================================================================
# Validated String Types
# =============================================================================

Email = Annotated[
    str,
    Meta(
        max_length=254,
        pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$(?!\n)",
        description="Valid email address",
        examples=["user@example.com"],
    ),
]
"""Email address with validation. Max 254 chars."""

URL = Annotated[
    str,
    Meta(
        max_length=200,
        pattern=r"^https?://[^\s/$.?#].[^\s]*$(?!\n)",
        description="HTTP or HTTPS URL",
        examples=["https://example.com"],
    ),
]
"""HTTP/HTTPS URL with validation. Max 200 chars."""

HttpsURL = Annotated[
    str,
    Meta(
        max_length=200,
        pattern=r"^https://[^\s/$.?#].[^\s]*$(?!\n)",
        description="HTTPS-only URL",
        examples=["https://example.com"],
    ),
]
"""HTTPS-only URL with validation. Max 200 chars."""

Slug = Annotated[
    str,
    Meta(
        max_length=50,
        pattern=r"^[-a-zA-Z0-9_]+$(?!\n)",
        description="URL-safe identifier",
        examples=["my-blog-post", "article_123"],
    ),
]
"""URL-safe slug. Max 50 chars, alphanumeric + hyphens/underscores."""

Slug100 = Annotated[
    str,
    Meta(
        max_length=100,
        pattern=r"^[-a-zA-Z0-9_]+$(?!\n)",
        description="URL-safe identifier (100 chars)",
        examples=["my-long-blog-post-title"],
    ),
]
"""URL-safe slug with max 100 chars."""

Slug200 = Annotated[
    str,
    Meta(
        max_length=200,
        pattern=r"^[-a-zA-Z0-9_]+$(?!\n)",
        description="URL-safe identifier (200 chars)",
        examples=["my-very-long-blog-post-title"],
    ),
]
"""URL-safe slug with max 200 chars."""

UUID = Annotated[
    str,
    Meta(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$(?!\n)",
        description="UUID string format",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    ),
]
"""UUID string format. For native UUID, use uuid.UUID type directly."""

# =============================================================================
# Integer Types
# =============================================================================

SmallInt = Annotated[
    int,
    Meta(
        ge=-32768,
        le=32767,
        description="Small integer (-32768 to 32767)",
    ),
]
"""Small integer. Range: -32768 to 32767."""

Int = Annotated[
    int,
    Meta(
        ge=-2147483648,
        le=2147483647,
        description="Integer (-2147483648 to 2147483647)",
    ),
]
"""Standard integer. Range: -2147483648 to 2147483647."""

BigInt = Annotated[
    int,
    Meta(
        ge=-9223372036854775808,
        le=9223372036854775807,
        description="Big integer (64-bit signed)",
    ),
]
"""Big integer (64-bit signed)."""

PositiveSmallInt = Annotated[
    int,
    Meta(
        ge=0,
        le=32767,
        description="Positive small integer (0 to 32767)",
    ),
]
"""Positive small integer. Range: 0 to 32767."""

PositiveInt = Annotated[
    int,
    Meta(gt=0, description="Positive integer (> 0)"),
]
"""Integer greater than 0."""

PositiveBigInt = Annotated[
    int,
    Meta(
        ge=0,
        le=9223372036854775807,
        description="Positive big integer (0 to 2^63-1)",
    ),
]
"""Positive big integer (64-bit unsigned range)."""

NonNegativeInt = Annotated[
    int,
    Meta(ge=0, description="Non-negative integer (>= 0)"),
]
"""Integer greater than or equal to 0."""

# =============================================================================
# Float Types
# =============================================================================

Float = Annotated[
    float,
    Meta(description="Floating point number"),
]
"""Floating point number."""

PositiveFloat = Annotated[
    float,
    Meta(ge=0, description="Positive float (>= 0)"),
]
"""Float that must be >= 0."""

# =============================================================================
# Network Types
# =============================================================================

IPv4 = Annotated[
    str,
    Meta(
        pattern=r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$(?!\n)",
        description="IPv4 address",
        examples=["192.168.1.1"],
    ),
]
"""IPv4 address."""

IPv6 = Annotated[
    str,
    Meta(
        # Simplified pattern - allows common IPv6 formats
        pattern=r"^(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$(?!\n)|^::(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}$(?!\n)|^(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}$(?!\n)",
        description="IPv6 address",
        examples=["::1", "2001:db8::1"],
    ),
]
"""IPv6 address."""

IP = Annotated[
    str,
    Meta(
        pattern=r"^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$(?!\n)|^(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$(?!\n)|^::(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4}$(?!\n)|^(?:[0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}$(?!\n)",
        description="IPv4 or IPv6 address",
        examples=["192.168.1.1", "::1", "2001:db8::1"],
    ),
]
"""IPv4 or IPv6 address."""

Port = Annotated[
    int,
    Meta(
        ge=1,
        le=65535,
        description="Network port (1-65535)",
    ),
]
"""Network port number. Range: 1 to 65535."""

HttpStatus = Annotated[
    int,
    Meta(
        ge=100,
        le=599,
        description="HTTP status code (100-599)",
    ),
]
"""HTTP status code. Range: 100 to 599."""

# =============================================================================
# File Path
# =============================================================================

FilePath = Annotated[
    str,
    Meta(
        max_length=100,
        description="File system path",
        examples=["/path/to/file.txt"],
    ),
]
"""File system path. Max 100 chars."""

# =============================================================================
# Auth Types
# =============================================================================

Username = Annotated[
    str,
    Meta(
        max_length=150,
        pattern=r"^[\w.@+-]+$(?!\n)",
        description="Username (Django auth compatible)",
        examples=["john_doe", "user@example"],
    ),
]
"""Django auth-compatible username. Max 150 chars, letters/digits/@/./+/-/_."""

Password = Annotated[
    str,
    Meta(
        min_length=8,
        max_length=128,
        description="Password (8-128 chars)",
    ),
]
"""Password. Min 8, max 128 chars."""

# =============================================================================
# Utility Types
# =============================================================================

Phone = Annotated[
    str,
    Meta(
        max_length=20,
        pattern=r"^\+?[1-9]\d{1,14}$(?!\n)",
        description="Phone number (E.164 format)",
        examples=["+14155551234", "14155551234"],
    ),
]
"""Phone number in E.164 international format."""

HexColor = Annotated[
    str,
    Meta(
        pattern=r"^#[0-9a-fA-F]{6}$(?!\n)",
        description="Hex color (#RRGGBB)",
        examples=["#FF5733", "#ffffff"],
    ),
]
"""Hex color code (#RRGGBB format)."""

CurrencyCode = Annotated[
    str,
    Meta(
        min_length=3,
        max_length=3,
        pattern=r"^[A-Z]{3}$(?!\n)",
        description="ISO 4217 currency code",
        examples=["USD", "EUR", "GBP"],
    ),
]
"""ISO 4217 3-letter currency code (e.g., USD, EUR)."""

CountryCode = Annotated[
    str,
    Meta(
        min_length=2,
        max_length=2,
        pattern=r"^[A-Z]{2}$(?!\n)",
        description="ISO 3166-1 alpha-2 country code",
        examples=["US", "GB", "DE"],
    ),
]
"""ISO 3166-1 alpha-2 country code (e.g., US, GB)."""

LanguageCode = Annotated[
    str,
    Meta(
        min_length=2,
        max_length=5,
        pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$(?!\n)",
        description="Language code (ISO 639-1)",
        examples=["en", "en-US", "fr-FR"],
    ),
]
"""Language code (e.g., en, en-US, fr-FR)."""

Timezone = Annotated[
    str,
    Meta(
        max_length=50,
        pattern=r"^[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?$(?!\n)|^UTC$(?!\n)",
        description="IANA timezone",
        examples=["America/New_York", "Europe/London", "UTC"],
    ),
]
"""IANA timezone identifier (e.g., America/New_York)."""

# =============================================================================
# Geographic Types
# =============================================================================

Latitude = Annotated[
    float,
    Meta(
        ge=-90,
        le=90,
        description="Latitude (-90 to 90)",
    ),
]
"""Geographic latitude. Range: -90 to 90 degrees."""

Longitude = Annotated[
    float,
    Meta(
        ge=-180,
        le=180,
        description="Longitude (-180 to 180)",
    ),
]
"""Geographic longitude. Range: -180 to 180 degrees."""

# =============================================================================
# Percentage and Rating Types
# =============================================================================

Percentage = Annotated[
    float,
    Meta(
        ge=0,
        le=100,
        description="Percentage (0-100)",
    ),
]
"""Percentage value. Range: 0 to 100."""

Rating = Annotated[
    float,
    Meta(
        ge=0,
        le=5,
        description="Rating (0-5 stars)",
    ),
]
"""Star rating. Range: 0 to 5."""

Rating10 = Annotated[
    float,
    Meta(
        ge=0,
        le=10,
        description="Rating (0-10)",
    ),
]
"""Rating on 0-10 scale."""

# =============================================================================
# Simple Constraint Types
# =============================================================================

NonEmptyStr = Annotated[
    str,
    Meta(min_length=1, description="Non-empty string"),
]
"""String that must have at least 1 character."""

# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # String lengths
    "Char50",
    "Char100",
    "Char150",
    "Char200",
    "Char255",
    "Char500",
    "Char1000",
    "Text",
    # Validated strings
    "Email",
    "URL",
    "HttpsURL",
    "Slug",
    "Slug100",
    "Slug200",
    "UUID",
    # Integers
    "SmallInt",
    "Int",
    "BigInt",
    "PositiveSmallInt",
    "PositiveInt",
    "PositiveBigInt",
    "NonNegativeInt",
    # Floats
    "Float",
    "PositiveFloat",
    # Network
    "IPv4",
    "IPv6",
    "IP",
    "Port",
    "HttpStatus",
    # File path
    "FilePath",
    # Auth
    "Username",
    "Password",
    # Utility
    "Phone",
    "HexColor",
    "CurrencyCode",
    "CountryCode",
    "LanguageCode",
    "Timezone",
    # Geographic
    "Latitude",
    "Longitude",
    # Rating/Percentage
    "Percentage",
    "Rating",
    "Rating10",
    # Simple constraints
    "NonEmptyStr",
]
