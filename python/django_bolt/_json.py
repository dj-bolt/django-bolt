"""Fast JSON helpers backed by cached msgspec Encoder/Decoder.

This module provides optimized JSON encoding/decoding using msgspec with:
- Thread-local cached encoder/decoder instances (thread-safe buffer reuse)
- Support for standard types (see DEFAULT_TYPE_ENCODERS)
- Automatic Serializer.dump() for write_only, computed_field support
- Type-safe decoding with validation
- Custom encoder/decoder hooks

Inspired by Litestar's serialization approach.
"""

from __future__ import annotations

import re
from array import array
from collections import ChainMap, UserDict, UserList, UserString, deque
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import Path, PurePath
from types import GeneratorType, MappingProxyType
from typing import Any
from uuid import UUID

import msgspec

# Default type encoders for non-JSON-native types
# Maps type -> encoder function
DEFAULT_TYPE_ENCODERS: dict[type, Callable[[Any], Any]] = {
    # msgspec delegates str subclasses (for example Django's SafeString) to
    # enc_hook. Call the base descriptor to guarantee an exact str --
    # SafeString.__str__ returns itself. Ordinary strings stay on msgspec's
    # native fast path and never reach this mapping.
    str: str.__str__,
    # Enum members -> primitive value
    Enum: lambda v: v.value,
    # Paths
    Path: str,
    PurePath: str,
    # msgspec encodes an exact datetime, time or Decimal itself. Only a
    # subclass comes here. Return the exact type, and msgspec encodes it as
    # it encodes the base type: "Z" for UTC, a string for a Decimal.
    datetime: lambda v: datetime(
        v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond, v.tzinfo, fold=v.fold
    ),
    date: lambda v: v.isoformat(),
    time: lambda v: time(v.hour, v.minute, v.second, v.microsecond, v.tzinfo, fold=v.fold),
    Decimal: Decimal,
    # The same rule for a subclass of int, float, bytes or timedelta.
    # The base descriptor returns the exact type for each of the first three.
    int: int.__int__,
    float: float.__float__,
    bytes: bytes.__bytes__,
    # msgspec encodes an exact bytearray as base64 text. bytes() copies a subclass.
    bytearray: bytes,
    timedelta: lambda v: timedelta(days=v.days, seconds=v.seconds, microseconds=v.microseconds),
    # IP addresses
    IPv4Address: str,
    IPv4Interface: str,
    IPv4Network: str,
    IPv6Address: str,
    IPv6Interface: str,
    IPv6Network: str,
    # UUID
    UUID: str,
    # A deque or a generator is a JSON array. The items go through the encoder again.
    deque: list,
    GeneratorType: list,
    # A Fraction is text, as a Decimal is: "1/3", or "2" for an integer value.
    Fraction: str,
    # A compiled regular expression is its pattern text.
    re.Pattern: lambda v: v.pattern,
    # A mapping is a JSON object. A sequence, a dict view or an iterator is a JSON array.
    # The items go through the encoder again.
    ChainMap: dict,
    MappingProxyType: dict,
    UserDict: dict,
    UserList: list,
    UserString: lambda v: v.data,
    range: list,
    array: array.tolist,
    type({}.keys()): list,
    type({}.values()): list,
    type({}.items()): list,
    map: list,
    zip: list,
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


__all__ = ["encode", "decode", "DEFAULT_TYPE_ENCODERS", "default_serializer"]
