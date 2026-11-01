"""
Parameter markers and validation constraints for Django-Bolt.

Provides explicit parameter source annotations and validation metadata.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Param",
    "Query",
    "Path",
    "Body",
    "Header",
    "Cookie",
    "Form",
    "File",
    "Depends",
]


@dataclass(frozen=True)
class Param:
    """
    Base parameter marker with validation constraints.

    Used internally by Query, Path, Body, etc. markers.
    """

    source: str
    """Parameter source: 'query', 'path', 'body', 'header', 'cookie', 'form', 'file'"""

    alias: str | None = None
    """Alternative name for the parameter in the request"""

    embed: bool | None = None
    """Whether to embed body parameter in wrapper object"""

    # Numeric constraints
    gt: float | None = None
    """Greater than (exclusive minimum)"""

    ge: float | None = None
    """Greater than or equal (inclusive minimum)"""

    lt: float | None = None
    """Less than (exclusive maximum)"""

    le: float | None = None
    """Less than or equal (inclusive maximum)"""

    multiple_of: float | None = None
    """Value must be multiple of this number"""

    # String/collection constraints
    min_length: int | None = None
    """Minimum length for strings or collections"""

    max_length: int | None = None
    """Maximum length for strings or collections"""

    pattern: str | None = None
    """Regex pattern for string validation"""

    # Metadata
    description: str | None = None
    """Parameter description for documentation"""

    example: Any = None
    """Example value for documentation"""

    deprecated: bool = False
    """Mark parameter as deprecated"""

    # File upload constraints
    max_size: int | None = None
    """Maximum file size in bytes"""

    min_size: int | None = None
    """Minimum file size in bytes"""

    allowed_types: tuple[str, ...] | None = None
    """Allowed MIME types (supports wildcards like 'image/*')"""

    max_files: int | None = None
    """Maximum number of files for list[UploadFile] parameters"""

    # Last, so that a Param made with positional arguments keeps its field order.
    default: Any = ...
    """Value of a missing parameter. ``...`` makes the parameter required."""


def Query(
    default: Any = ...,
    *,
    alias: str | None = None,
    gt: float | None = None,
    ge: float | None = None,
    lt: float | None = None,
    le: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    description: str | None = None,
    example: Any = None,
    deprecated: bool = False,
) -> Any:
    """
    Mark parameter as query parameter.

    Args:
        default: Default value (... for required)
        alias: Alternative parameter name in URL
        gt: Value must be greater than this
        ge: Value must be greater than or equal to this
        lt: Value must be less than this
        le: Value must be less than or equal to this
        min_length: Minimum string/collection length
        max_length: Maximum string/collection length
        pattern: Regex pattern to match
        description: Parameter description
        example: Example value
        deprecated: Mark as deprecated

    Returns:
        Param marker instance
    """
    return Param(
        source="query",
        default=default,
        alias=alias,
        gt=gt,
        ge=ge,
        lt=lt,
        le=le,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        description=description,
        example=example,
        deprecated=deprecated,
    )


def Path(
    default: Any = ...,
    *,
    alias: str | None = None,
    gt: float | None = None,
    ge: float | None = None,
    lt: float | None = None,
    le: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    description: str | None = None,
    example: Any = None,
    deprecated: bool = False,
) -> Any:
    """
    Mark parameter as path parameter.

    Args:
        default: Must be ... (path params are always required)
        alias: Alternative parameter name
        gt: Value must be greater than this
        ge: Value must be greater than or equal to this
        lt: Value must be less than this
        le: Value must be less than or equal to this
        min_length: Minimum string length
        max_length: Maximum string length
        pattern: Regex pattern to match
        description: Parameter description
        example: Example value
        deprecated: Mark as deprecated

    Returns:
        Param marker instance
    """
    if default is not ...:
        raise ValueError("Path parameters cannot have default values")

    return Param(
        source="path",
        default=default,
        alias=alias,
        gt=gt,
        ge=ge,
        lt=lt,
        le=le,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        description=description,
        example=example,
        deprecated=deprecated,
    )


def Body(
    default: Any = ...,
    *,
    alias: str | None = None,
    embed: bool = False,
    description: str | None = None,
    example: Any = None,
) -> Any:
    """
    Mark parameter as request body.

    Args:
        default: Default value (... for required)
        alias: Alternative parameter name
        embed: Whether to wrap in {<alias>: <value>}
        description: Parameter description
        example: Example value

    Returns:
        Param marker instance
    """
    return Param(
        source="body",
        default=default,
        alias=alias,
        embed=embed,
        description=description,
        example=example,
    )


def Header(
    default: Any = ...,
    *,
    alias: str | None = None,
    description: str | None = None,
    example: Any = None,
    deprecated: bool = False,
) -> Any:
    """
    Mark parameter as HTTP header.

    Args:
        default: Default value (... for required)
        alias: Alternative header name
        description: Parameter description
        example: Example value
        deprecated: Mark as deprecated

    Returns:
        Param marker instance
    """
    return Param(
        source="header",
        default=default,
        alias=alias,
        description=description,
        example=example,
        deprecated=deprecated,
    )


def Cookie(
    default: Any = ...,
    *,
    alias: str | None = None,
    description: str | None = None,
    example: Any = None,
    deprecated: bool = False,
) -> Any:
    """
    Mark parameter as cookie value.

    Args:
        default: Default value (... for required)
        alias: Alternative cookie name
        description: Parameter description
        example: Example value
        deprecated: Mark as deprecated

    Returns:
        Param marker instance
    """
    return Param(
        source="cookie",
        default=default,
        alias=alias,
        description=description,
        example=example,
        deprecated=deprecated,
    )


def Form(
    default: Any = ...,
    *,
    alias: str | None = None,
    description: str | None = None,
    example: Any = None,
) -> Any:
    """
    Mark parameter as form data field.

    Args:
        default: Default value (... for required)
        alias: Alternative form field name
        description: Parameter description
        example: Example value

    Returns:
        Param marker instance
    """
    return Param(
        source="form",
        default=default,
        alias=alias,
        description=description,
        example=example,
    )


def _file_limit(name: str, value: Any, minimum: int) -> int | None:
    """Return a limit of File(). Raise for a value that is not None or an int of the minimum or more."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"File {name} must be an int, got {type(value).__name__} {value!r}")
    if value < minimum:
        raise ValueError(f"File {name} must be {minimum} or more, got {value}")
    # Rust reads a limit as a usize and ignores a larger int. sys.maxsize fits a usize.
    if value > sys.maxsize:
        raise ValueError(f"File {name} must be {sys.maxsize} or less, got {value}")
    return value


def File(
    default: Any = ...,
    *,
    alias: str | None = None,
    description: str | None = None,
    max_size: int | None = None,
    min_size: int | None = None,
    allowed_types: Iterable[str] | None = None,
    max_files: int | None = None,
) -> Any:
    """
    Mark parameter as file upload with optional validation.

    Args:
        default: Default value (... for required)
        alias: Alternative form field name
        description: Parameter description
        max_size: Maximum file size in bytes (e.g., 2_000_000 for 2MB). An int of 0 or more.
        min_size: Minimum file size in bytes. An int of 0 or more, and not above max_size.
        allowed_types: Allowed MIME types, as a list, a tuple or another iterable of str
            (e.g., ["image/*", "application/pdf"]). A str raises TypeError.
        max_files: Maximum number of files for list[UploadFile] parameters. An int of 1 or more.

    Raises:
        TypeError: A limit is not an int. Rust ignores such a limit, so the check is here.
        ValueError: A limit is out of range, or min_size is above max_size.

    Returns:
        Param marker instance

    Example:
        @api.post("/avatar")
        async def upload(
            avatar: Annotated[UploadFile, File(max_size=2_000_000, allowed_types=["image/*"])]
        ):
            content = await avatar.read()
    """
    # Check the limits here. Rust ignores a value of the wrong type, so a typo
    # such as max_size="10MB" would remove the limit with no error.
    max_size = _file_limit("max_size", max_size, 0)
    min_size = _file_limit("min_size", min_size, 0)
    max_files = _file_limit("max_files", max_files, 1)
    if max_size is not None and min_size is not None and min_size > max_size:
        raise ValueError(f"File min_size {min_size} must not be above max_size {max_size}")
    if allowed_types is not None:
        # A str is a sequence of characters. "image/*" would give "*", and "*" allows each type.
        # Make the tuple before the item check, so that a generator is read only one time.
        # iter() also reads a class with only __getitem__, which isinstance(..., Iterable) rejects.
        given = allowed_types
        try:
            values = None if isinstance(given, (str, bytes)) else iter(given)
        except TypeError:
            values = None
        allowed_types = () if values is None else tuple(values)
        if values is None or not all(isinstance(value, str) for value in allowed_types):
            raise TypeError(
                f"File(allowed_types=...) takes an iterable of MIME type strings, for example ['image/*'], got {given!r}."
            )
        # A route that allows no file type can take no upload. That is a mistake, so stop here.
        if not allowed_types:
            raise ValueError("File(allowed_types=[]) allows no file type. Remove allowed_types to allow each type.")
    return Param(
        source="file",
        default=default,
        alias=alias,
        description=description,
        max_size=max_size,
        min_size=min_size,
        allowed_types=allowed_types,
        max_files=max_files,
    )


@dataclass(frozen=True)
class Depends:
    """
    Dependency injection marker.

    Marks a parameter as a dependency that will be resolved
    by calling the specified function.
    """

    dependency: Callable[..., Any] | None = None
    """Function to call for dependency resolution"""

    use_cache: bool = True
    """Whether to cache the dependency result per request"""
