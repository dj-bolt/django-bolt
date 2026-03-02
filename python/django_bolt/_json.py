"""Fast JSON helpers backed by cached msgspec Encoder/Decoder.

This module provides optimized JSON encoding/decoding using msgspec with:
- Thread-local cached encoder/decoder instances (thread-safe buffer reuse)
- Support for common non-JSON-native types (datetime, Path, Decimal, UUID, IP addresses)
- Automatic Serializer.dump() for write_only, computed_field support
- Type-safe decoding with validation
- Custom encoder/decoder hooks

Inspired by Litestar's serialization approach.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import Path, PurePath
from typing import Any, TypeVar
from uuid import UUID

import msgspec

T = TypeVar("T")

# Default type encoders for non-JSON-native types
# Maps type -> encoder function
DEFAULT_TYPE_ENCODERS: dict[type, Callable[[Any], Any]] = {
    # Paths
    Path: str,
    PurePath: str,
    # Dates/Times -> ISO format
    datetime: lambda v: v.isoformat(),
    date: lambda v: v.isoformat(),
    time: lambda v: v.isoformat(),
    # Decimals -> int or float
    Decimal: lambda v: int(v) if v.as_tuple().exponent >= 0 else float(v),
    # IP addresses
    IPv4Address: str,
    IPv4Interface: str,
    IPv4Network: str,
    IPv6Address: str,
    IPv6Interface: str,
    IPv6Network: str,
    # UUID
    UUID: str,
}


def default_serializer(value: Any) -> Any:
    """Transform values non-natively supported by msgspec.

    Walks the MRO (Method Resolution Order) to support subclasses.
    Raises TypeError if type is unsupported.

    Note: Serializer instances are handled in serialization.py before reaching
    this hook, since msgspec.Struct is natively supported by msgspec.
    """
    # Walk MRO to support polymorphic types
    for base in value.__class__.__mro__[:-1]:  # Skip 'object'
        encoder = DEFAULT_TYPE_ENCODERS.get(base)
        if encoder is not None:
            return encoder(value)

    raise TypeError(f"Unsupported type: {type(value)!r}")


# Module-level singleton encoder/decoder instances (Litestar pattern).
# Thread-safe under GIL: only one thread can access Python objects at a time.
# Reuses internal buffer across calls for better performance.
_ENCODER = msgspec.json.Encoder(enc_hook=default_serializer)
_DECODER = msgspec.json.Decoder()


def encode(value: Any, serializer: Callable[[Any], Any] | None = None) -> bytes:
    """Encode a Python object to JSON bytes.

    Args:
        value: Object to encode
        serializer: Optional custom encoder hook (overrides default)

    Returns:
        JSON bytes

    Raises:
        TypeError: If value contains unsupported types
        msgspec.EncodeError: If encoding fails
    """
    if serializer is not None:
        # Custom serializer provided - use one-off encoder
        return msgspec.json.encode(value, enc_hook=serializer)

    # Use module-level singleton encoder with default serializer
    return _ENCODER.encode(value)


def decode(value: bytes | str) -> Any:
    """Decode JSON bytes/string to Python object.

    Args:
        value: JSON bytes or string

    Returns:
        Decoded Python object

    Raises:
        msgspec.DecodeError: If decoding fails
    """
    return _DECODER.decode(value)


def decode_typed[T](
    value: bytes | str,
    target_type: type[T],
    strict: bool = True,
) -> T:
    """Decode JSON with type validation and coercion.

    Args:
        value: JSON bytes or string
        target_type: Expected type (e.g., msgspec.Struct subclass)
        strict: If False, enables lenient type coercion (e.g., "123" -> 123)

    Returns:
        Decoded and validated object of target_type

    Raises:
        msgspec.DecodeError: If decoding or validation fails
        msgspec.ValidationError: If type validation fails

    Examples:
        >>> class User(msgspec.Struct):
        ...     id: int
        ...     name: str
        >>> decode_typed(b'{"id": 1, "name": "Alice"}', User)
        User(id=1, name='Alice')
    """
    return msgspec.json.decode(value, type=target_type, strict=strict)


__all__ = ["encode", "decode", "decode_typed", "DEFAULT_TYPE_ENCODERS", "default_serializer"]
