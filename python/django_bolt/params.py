"""
Parameter markers and validation constraints for Django-Bolt.

Provides explicit parameter source annotations and validation metadata.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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

    default: Any = ...
    """Value of a missing parameter. ``...`` makes the parameter required."""

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


def File(
    default: Any = ...,
    *,
    alias: str | None = None,
    description: str | None = None,
    max_size: int | None = None,
    min_size: int | None = None,
    allowed_types: Sequence[str] | None = None,
    max_files: int | None = None,
) -> Any:
    """
    Mark parameter as file upload with optional validation.

    Args:
        default: Default value (... for required)
        alias: Alternative form field name
        description: Parameter description
        max_size: Maximum file size in bytes (e.g., 2_000_000 for 2MB)
        min_size: Minimum file size in bytes
        allowed_types: Allowed MIME types (e.g., ["image/*", "application/pdf"])
        max_files: Maximum number of files for list[UploadFile] parameters

    Returns:
        Param marker instance

    Example:
        @api.post("/avatar")
        async def upload(
            avatar: Annotated[UploadFile, File(max_size=2_000_000, allowed_types=["image/*"])]
        ):
            content = await avatar.read()
    """
    return Param(
        source="file",
        default=default,
        alias=alias,
        description=description,
        max_size=max_size,
        min_size=min_size,
        allowed_types=tuple(allowed_types) if allowed_types else None,
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
