"""
Pagination utilities for Django-Bolt.

Provides Django Paginator-based pagination that works with both functional
and class-based views, leveraging Django's built-in pagination while integrating
with Bolt's parameter extraction and serialization systems.

Example (Functional View):
    @api.get("/users")
    @paginate(PageNumberPagination)
    async def list_users():
        return User.objects.all()

Example (Class-Based View):
    @api.viewset("/articles")
    class ArticleViewSet(ModelViewSet):
        queryset = Article.objects.all()
        serializer_class = ArticleSchema
        pagination_class = PageNumberPagination
"""

from __future__ import annotations

import asyncio
import base64
import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import wraps
from typing import Any, Generic, NamedTuple, TypeVar, get_args, get_origin

import msgspec
from django.core.exceptions import FieldDoesNotExist, FieldError, ValidationError
from django.db import connections
from django.db.models import F, Q, Value
from django.db.models.fields.tuple_lookups import (
    Tuple,
    TupleGreaterThan,
    TupleLessThan,
)
from django.db.models.query import (
    FlatValuesListIterable,
    NamedValuesListIterable,
    ValuesIterable,
    ValuesListIterable,
)
from django.db.models.utils import create_namedtuple_class

from . import _json
from .concurrency import run_in_orm_executor
from .exceptions import UnloadedRelationError

__all__ = [
    "PaginationBase",
    "PageNumberPagination",
    "LimitOffsetPagination",
    "CursorPagination",
    "PaginatedResponse",
    "paginate",
    "extract_pagination_item_type",
]

T = TypeVar("T")
_PAGINATION_UNSET = object()
_CURSOR_NULL = object()
_CURSOR_JSON_NULL = object()


class _CompositeCursor(NamedTuple):
    value: Any
    pk: Any


class PaginatedResponse(msgspec.Struct, Generic[T], omit_defaults=True):  # noqa: UP046 — PEP 695 params are unresolvable under PEP 563
    """
    Standard paginated response structure.

    Uses omit_defaults=True so only relevant fields are included in response:
    - PageNumberPagination: page, page_size, total_pages, next_page, previous_page
    - LimitOffsetPagination: limit, offset
    - CursorPagination: next_cursor, previous_cursor

    Attributes:
        items: List of paginated items
        total: Total number of items across all pages
        has_next: Whether there is a next page (always included)
        has_previous: Whether there is a previous page (always included)
        page: Current page number (for PageNumber pagination)
        page_size: Number of items per page
        total_pages: Total number of pages
        next_page: Next page number (None if no next page)
        previous_page: Previous page number (None if no previous page)
    """

    # Required fields (always included in response)
    items: list[T]
    total: int
    has_next: bool  # No default = always included
    has_previous: bool  # No default = always included

    # PageNumber pagination fields (included when set)
    page: int | None = None
    page_size: int | None = None
    total_pages: int | None = None
    next_page: int | None = None
    previous_page: int | None = None

    # LimitOffset pagination fields (included when set)
    limit: int | None = None
    offset: int | None = None

    # Cursor pagination fields (included when set)
    next_cursor: str | None = None
    previous_cursor: str | None = None


def extract_pagination_item_type(response_type: Any) -> type | None:
    """
    Extract item type from list[T] or PaginatedResponse[T] for pagination.

    This is used by the route decorator to determine the serializer class
    when a handler is decorated with @paginate and has a response_model
    like list[UserSerializer] or PaginatedResponse[UserSerializer].

    Args:
        response_type: Type annotation (e.g., list[UserSerializer])

    Returns:
        The item type (e.g., UserSerializer), or None if not extractable
    """
    origin = get_origin(response_type)
    args = get_args(response_type)

    if not args:
        return None

    # Handle list[T]
    if origin is list:
        return args[0]

    # Handle PaginatedResponse[T]
    if origin is PaginatedResponse:
        return args[0]

    return None


class PaginationBase(ABC):
    """
    Base class for all pagination schemes.

    Subclasses must implement:
        - get_page_params(): Extract pagination params from request
        - paginate_queryset(): Apply pagination to queryset
    """

    # Default page size
    page_size: int = 100

    # Maximum allowed page size (prevents abuse)
    max_page_size: int = 1000

    # Name of the page size query parameter
    page_size_query_param: str | None = None

    # Serializer class for item serialization (set at runtime by paginate wrapper)
    serializer_class: type | None = None

    # Envelope type: a generic msgspec.Struct/Serializer taking the item type
    # (``response_class[Item]``). Drives the OpenAPI schema and response
    # validation; override together with build_response() for a custom shape.
    response_class: type = PaginatedResponse

    def build_response(
        self,
        items: list[Any],
        *,
        total: int,
        request: Any,
        has_next: bool,
        has_previous: bool,
        **info: Any,
    ) -> Any:
        """Build the envelope for one page.

        ``info`` carries the scheme-specific fields (page/page_size/total_pages/
        next_page/previous_page, limit/offset, next_cursor/previous_cursor).
        Override to return an instance of a custom ``response_class``.
        """
        return self.response_class(items=items, total=total, has_next=has_next, has_previous=has_previous, **info)

    @abstractmethod
    async def get_page_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        Extract pagination parameters from request.

        Args:
            request: Request dictionary

        Returns:
            Dictionary of pagination parameters
        """
        raise NotImplementedError

    @abstractmethod
    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> Any:
        """
        Apply pagination to a queryset and return paginated response.

        Args:
            queryset: Django QuerySet to paginate
            request: Request dictionary
            **params: Additional parameters

        Returns:
            PaginatedResponse with items and metadata
        """
        raise NotImplementedError

    async def _get_queryset_count(self, queryset: Any) -> int:
        """
        Get total count of queryset items.

        Handles both sync and async querysets.

        Args:
            queryset: Django QuerySet or list

        Returns:
            Total count
        """
        # Check if it's a plain list first (Python list.count() requires an argument)
        if isinstance(queryset, (list, tuple)):
            return len(queryset)
        # Django QuerySet: run COUNT(*) in the parallel default thread pool.
        # Django's acount() is sync_to_async(thread_sensitive=True) internally,
        # which funnels every concurrent request through ONE shared thread.
        if hasattr(queryset, "_iterable_class") and hasattr(queryset, "model"):
            return await run_in_orm_executor(queryset.count)
        # Non-Django async collections that expose acount
        if hasattr(queryset, "acount"):
            return await queryset.acount()
        # Other sync collections with a count() method
        if hasattr(queryset, "count") and callable(queryset.count):
            return await run_in_orm_executor(queryset.count)
        # For other iterables
        else:
            return len(queryset)

    async def _serialize_items(self, items: list[Any]) -> list[dict]:
        """
        Serialize items using efficient batch serialization.

        Supports:
        - Bolt Serializer: from_model() + dump_many() (fastest)
        - msgspec.Struct: msgspec.structs.asdict() for each item
        - Fallback: _model_to_dict() for backwards compatibility

        Args:
            items: List of raw items (Django models or dicts)

        Returns:
            List of dictionaries ready for JSON serialization
        """
        if not items:
            return []

        if self.serializer_class is None:
            # Backwards compatibility: use _model_to_dict fallback
            return [self._model_to_dict(item) for item in items]

        # Check if items are already dicts (e.g., from values() queryset)
        # In this case, just extract the fields we need
        first_item = items[0]
        if isinstance(first_item, dict):
            # Items are dicts - extract only the fields declared in serializer
            if hasattr(self.serializer_class, "__struct_fields__"):
                fields = self.serializer_class.__struct_fields__
                return [{field: item.get(field) for field in fields} for item in items]
            return items  # Return dicts as-is if no fields info

        # Bolt Serializer - use from_model + dump_many for efficiency
        if getattr(self.serializer_class, "__is_bolt_serializer__", False):
            serializer_class = self.serializer_class
            serializer_class._ensure_from_model_ready()

            try:
                serializer_instances = [serializer_class.from_model(item) for item in items]
            except UnloadedRelationError:
                pass
            else:
                return serializer_class.dump_many(serializer_instances)

            serializer_instances = list(await asyncio.gather(*(serializer_class.afrom_model(item) for item in items)))
            return serializer_class.dump_many(serializer_instances)

        # Plain msgspec.Struct - extract fields from model instances
        if hasattr(self.serializer_class, "__struct_fields__"):
            fields = self.serializer_class.__struct_fields__
            return [{field: getattr(item, field, None) for field in fields} for item in items]

        # Fallback for unknown serializer types
        return [self._model_to_dict(item) for item in items]

    async def _evaluate_queryset_slice(self, queryset: Any) -> list[Any]:
        """
        Evaluate a queryset slice to a list of raw items.

        Handles both sync and async querysets.
        Serialization is done separately via _serialize_items() for efficiency.

        Args:
            queryset: Django QuerySet or iterable

        Returns:
            List of raw items (Django models or dicts from values())
        """
        items = []

        # Django QuerySet FIRST: evaluate with ONE parallel thread-pool call.
        # Django's `async for` iterates in chunks of 100, each chunk a
        # sync_to_async(thread_sensitive=True) hop onto one shared thread —
        # both per-chunk overhead AND a cross-request serialization point.
        if hasattr(queryset, "_iterable_class") and hasattr(queryset, "model"):
            serializer_class = self.serializer_class
            if getattr(serializer_class, "__is_bolt_serializer__", False):
                # Pre-load what the item serializer reads (select_related /
                # prefetch_related / annotate) so a page never costs N+1.
                queryset = serializer_class._prepare_queryset(queryset)
            return await run_in_orm_executor(list, queryset)
        # Non-QuerySet async iterables (has __aiter__)
        if hasattr(queryset, "__aiter__"):
            async for item in queryset:
                items.append(item)
            return items
        # Regular iterable or list
        else:
            return list(queryset)

    def _model_to_dict(self, item: Any) -> Any:
        """
        Convert Django model instance to dict for serialization.

        Args:
            item: Django model instance or any object

        Returns:
            Dict if item is a Django model, otherwise returns item unchanged
        """
        # Check if it's a Django model instance
        if hasattr(item, "_meta") and hasattr(item, "_state"):
            # It's a Django model - convert to dict
            # Get all field values - use __dict__ which is safe in async context
            data = {}
            # Use model's __dict__ to avoid accessing _meta in async context
            for key, value in item.__dict__.items():
                # Skip private attributes and Django internal state
                if not key.startswith("_"):
                    data[key] = value
            return data
        # Not a model, return as-is
        return item

    def _get_page_size(self, request: dict[str, Any]) -> int:
        """
        Get page size from request, with validation.

        Args:
            request: Request dictionary

        Returns:
            Validated page size
        """
        if self.page_size_query_param:
            query = request.get("query", {})
            page_size_str = query.get(self.page_size_query_param)
            if page_size_str:
                try:
                    page_size = int(page_size_str)
                    # Enforce max_page_size limit
                    if page_size > self.max_page_size:
                        return self.max_page_size
                    if page_size < 1:
                        return self.page_size
                    return page_size
                except (ValueError, TypeError):
                    pass

        return self.page_size


class PageNumberPagination(PaginationBase):
    """
    Page number-based pagination.

    Query parameters:
        - page: Page number (default: 1)
        - page_size: Items per page (optional, default: 100)

    Example:
        /api/users?page=2&page_size=20

    Attributes:
        page_size: Default number of items per page (default: 100)
        max_page_size: Maximum allowed page size (default: 1000)
        page_size_query_param: Query param name for page size (default: "page_size")
    """

    page_size: int = 100
    max_page_size: int = 1000
    page_size_query_param: str = "page_size"

    async def get_page_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """Extract page number and page_size from request."""
        query = request.get("query", {})

        # Get page number (default to 1)
        page_str = query.get("page", "1")
        try:
            page = int(page_str)
            if page < 1:
                page = 1
        except (ValueError, TypeError):
            page = 1

        # Get page size
        page_size = self._get_page_size(request)

        return {"page": page, "page_size": page_size}

    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> Any:
        """
        Paginate queryset using page numbers.

        Args:
            queryset: Django QuerySet to paginate
            request: Request dictionary
            **params: Additional parameters (unused)

        Returns:
            PaginatedResponse with pagination metadata
        """
        page_params = await self.get_page_params(request)
        page_number = page_params["page"]
        page_size = page_params["page_size"]

        # Get total count
        total = await self._get_queryset_count(queryset)

        # Calculate total pages
        total_pages = (total + page_size - 1) // page_size if total > 0 else 0

        # Validate page number
        if page_number > total_pages and total_pages > 0:
            page_number = total_pages

        # Calculate offset
        offset = (page_number - 1) * page_size

        # Slice queryset and get raw items
        raw_items = await self._evaluate_queryset_slice(queryset[offset : offset + page_size])

        # Serialize items using efficient batch serialization
        items = await self._serialize_items(raw_items)

        # Build response
        return self.build_response(
            items,
            total=total,
            request=request,
            has_next=page_number < total_pages,
            has_previous=page_number > 1,
            page=page_number,
            page_size=page_size,
            total_pages=total_pages,
            next_page=page_number + 1 if page_number < total_pages else None,
            previous_page=page_number - 1 if page_number > 1 else None,
        )


class LimitOffsetPagination(PaginationBase):
    """
    Limit-offset based pagination.

    Query parameters:
        - limit: Number of items to return (default: 100)
        - offset: Starting position (default: 0)

    Example:
        /api/users?limit=20&offset=40

    Attributes:
        page_size: Default limit when not specified (default: 100)
        max_page_size: Maximum allowed limit (default: 1000)
    """

    page_size: int = 100
    max_page_size: int = 1000

    async def get_page_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """Extract limit and offset from request."""
        query = request.get("query", {})

        # Get limit (default to page_size)
        limit_str = query.get("limit", None)
        if limit_str is None:
            # No limit specified, use default
            limit = self.page_size
        else:
            try:
                limit = int(limit_str)
                if limit < 1:
                    limit = self.page_size
                if limit > self.max_page_size:
                    limit = self.max_page_size
            except (ValueError, TypeError):
                limit = self.page_size

        # Get offset (default to 0)
        offset_str = query.get("offset", "0")
        try:
            offset = int(offset_str)
            if offset < 0:
                offset = 0
        except (ValueError, TypeError):
            offset = 0

        return {"limit": limit, "offset": offset}

    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> Any:
        """
        Paginate queryset using limit/offset.

        Args:
            queryset: Django QuerySet to paginate
            request: Request dictionary
            **params: Additional parameters (unused)

        Returns:
            PaginatedResponse with pagination metadata
        """
        page_params = await self.get_page_params(request)
        limit = page_params["limit"]
        offset = page_params["offset"]

        # Get total count
        total = await self._get_queryset_count(queryset)

        # Slice queryset and get raw items
        raw_items = await self._evaluate_queryset_slice(queryset[offset : offset + limit])

        # Serialize items using efficient batch serialization
        items = await self._serialize_items(raw_items)

        # Calculate page info
        has_next = (offset + limit) < total
        has_previous = offset > 0

        return self.build_response(
            items,
            total=total,
            request=request,
            has_next=has_next,
            has_previous=has_previous,
            limit=limit,
            offset=offset,
        )


class CursorPagination(PaginationBase):
    """
    Cursor-based pagination for large datasets.

    Non-PK ordering uses the primary key to break ties. A single-key
    DISTINCT/GROUP BY projection uses its ordering key as the cursor.

    More efficient than offset-based pagination for large datasets
    as it doesn't require counting all records or scanning through skipped records.

    Query parameters:
        - cursor: Opaque cursor string (optional)
        - page_size: Items per page (optional)

    Example:
        /api/users?cursor=eyJpZCI6MTAwfQ==&page_size=20

    Attributes:
        page_size: Default number of items per page (default: 100)
        max_page_size: Maximum allowed page size (default: 1000)
        page_size_query_param: Query param name for page size (default: "page_size")
        ordering: Field to order by (default: "-id" for descending ID)
    """

    page_size: int = 100
    max_page_size: int = 1000
    page_size_query_param: str = "page_size"
    ordering: str = "-id"  # Default ordering field

    def _encode_cursor(self, value: Any, *, kind: str | None = None) -> str:
        """
        Encode cursor value to base64 string.

        Args:
            value: Cursor value (typically an ID)

        Returns:
            Base64-encoded cursor string
        """
        # Keep legacy scalar wire format; tag new cursor types outside the value.
        data = {"v": value} if kind is None else {"v": value, "t": kind}
        cursor_data = _json.encode(data)
        return base64.b64encode(cursor_data).decode("utf-8")

    def _decode_cursor(self, cursor: str) -> Any:
        """
        Decode cursor string to value.

        Args:
            cursor: Base64-encoded cursor string

        Returns:
            Decoded cursor value
        """
        try:
            cursor_data = base64.b64decode(cursor.encode("utf-8"))
            data = msgspec.json.decode(cursor_data)
            if not isinstance(data, dict):
                return None
            value = data.get("v")
            if data.get("t") in {"composite-v2", "composite-json-null-v1"}:
                if not isinstance(value, dict) or set(value) != {"value", "pk"}:
                    return None
                if data["t"] == "composite-json-null-v1":
                    if value["value"] is not None:
                        return None
                    return _CompositeCursor(_CURSOR_JSON_NULL, value["pk"])
                return _CompositeCursor(value["value"], value["pk"])
            if data.get("t") == "null-v2":
                return _CURSOR_NULL if value is None else None
            return value
        except Exception:
            return None

    async def get_page_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """Extract cursor and page_size from request."""
        query = request.get("query", {})

        # Get cursor (optional)
        cursor_str = query.get("cursor")
        cursor_value = self._decode_cursor(cursor_str) if cursor_str else None

        # Get page size
        page_size = self._get_page_size(request)

        return {"cursor": cursor_value, "page_size": page_size}

    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> Any:
        """
        Paginate queryset using cursor-based pagination.

        Args:
            queryset: Django QuerySet to paginate
            request: Request dictionary
            **params: Additional parameters (unused)

        Returns:
            PaginatedResponse with cursor metadata
        """
        page_params = await self.get_page_params(request)
        cursor_value = page_params["cursor"]
        page_size = page_params["page_size"]

        # Apply ordering
        ordering = self.ordering
        is_descending = ordering.startswith("-")
        ordering_field = ordering.lstrip("-")
        projection_ordering_field = ordering_field
        try:
            sort_field = queryset.model._meta.get_field(ordering_field)
        except FieldDoesNotExist:
            sort_field = None  # Annotation rather than a model field.

        if (
            ordering_field != "pk"
            and ordering_field not in queryset.query.annotation_select
            and (sort_field is None or not sort_field.concrete or "__" in ordering_field)
        ):
            raise ValueError("CursorPagination ordering must be a concrete field or annotation")

        if sort_field is not None and sort_field.concrete and sort_field.is_relation:
            if not (sort_field.many_to_one or sort_field.one_to_one):
                raise ValueError("CursorPagination cannot order by a multi-valued relation")
            if sort_field.name == ordering_field and sort_field.remote_field.model._meta.ordering:
                raise ValueError(
                    "CursorPagination cannot use related-model default ordering; "
                    f"use '{sort_field.attname}' to order by the foreign key"
                )

            # Never fetch a related object to generate a cursor in async code.
            projection_ordering_field = sort_field.name
            ordering_field = sort_field.attname
            ordering = ("-" if is_descending else "") + ordering_field

        pk_field = queryset.model._meta.pk
        is_pk_ordering = ordering_field in {"pk", pk_field.name, pk_field.attname}
        iterable = getattr(queryset, "_iterable_class", None)
        is_values_projection = iterable is ValuesIterable
        is_flat_values_list = iterable is FlatValuesListIterable
        is_named_values_list = iterable is NamedValuesListIterable
        is_values_list_projection = iterable in (ValuesListIterable, FlatValuesListIterable, NamedValuesListIterable)
        projected = is_values_projection or is_values_list_projection
        selected_fields = getattr(queryset, "_fields", None) or ()

        # Adding a PK to DISTINCT/GROUP BY changes the rows themselves.
        # A single projected ordering/grouping key is already unique in
        # these two cases; otherwise fail clearly rather than lose rows.
        grouped_or_distinct = projected and bool(queryset.query.distinct or queryset.query.group_by)

        # Top-level JSON null and SQL NULL both become None in Python.
        # Distinguish them at page boundaries without querying every row.
        ordering_output_field = sort_field
        if ordering_output_field is None:
            annotation = queryset.query.annotations.get(ordering_field)
            if annotation is not None:
                try:
                    ordering_output_field = annotation.output_field
                except FieldError as exc:
                    raise ValueError("CursorPagination cannot resolve annotation ordering type") from exc
        is_json_ordering = (
            ordering_output_field is not None and ordering_output_field.get_internal_type() == "JSONField"
        )
        if grouped_or_distinct and not is_pk_ordering and is_json_ordering:
            raise ValueError("CursorPagination cannot paginate DISTINCT/GROUP BY over a JSONField ordering")

        if grouped_or_distinct and not is_pk_ordering:
            if tuple(selected_fields) not in (
                (ordering_field,),
                (projection_ordering_field,),
            ):
                raise ValueError(
                    "CursorPagination with DISTINCT/GROUP BY needs an ordering "
                    "field that uniquely identifies each projected row"
                )
            if queryset.query.group_by:
                if queryset.query.group_by is True or len(queryset.query.group_by) != 1:
                    raise ValueError("CursorPagination needs one unique GROUP BY ordering key")
                group_key = queryset.query.group_by[0]
                grouped_ordering = queryset.query.resolve_ref(ordering_field)
                if group_key != grouped_ordering and getattr(group_key, "refs", None) != ordering_field:
                    raise ValueError("CursorPagination GROUP BY ordering must match the grouped key")
            elif queryset.query.distinct:
                extra_columns = set(queryset.query.annotation_select) | set(queryset.query.extra_select)
                if extra_columns - {
                    ordering_field,
                    projection_ordering_field,
                }:
                    raise ValueError("CursorPagination cannot paginate DISTINCT with extra selected columns")

        if (
            projected
            and selected_fields
            and ordering_field not in selected_fields
            and projection_ordering_field not in selected_fields
            and not (is_pk_ordering and bool({"pk", pk_field.name, pk_field.attname}.intersection(selected_fields)))
        ):
            raise ValueError("CursorPagination projection must include its ordering field")

        hidden_pk_field = "_bolt_cursor_pk"
        pk_is_projected = (
            not selected_fields  # values() with no arguments includes the PK
            or bool({"pk", pk_field.name, pk_field.attname}.intersection(selected_fields))
        )
        # Django disallows annotate() after union(); keep its legacy first page.
        is_combined = queryset.query.combinator is not None
        needs_hidden_pk = projected and not grouped_or_distinct and not pk_is_projected and not is_combined
        if needs_hidden_pk:
            while hidden_pk_field in selected_fields or hidden_pk_field in queryset.query.annotations:
                hidden_pk_field += "_"
            queryset = queryset.annotate(**{hidden_pk_field: F("pk")})
            if is_flat_values_list or is_named_values_list:
                # These iterable classes discard the hidden column (flat) or
                # cannot build a namedtuple with the extra column (named).
                # Read raw tuples and restore the public shape after slicing.
                queryset._iterable_class = ValuesListIterable

        # Preserve the original -id fast path and avoid changing SQL meaning
        # on aggregated or DISTINCT querysets.
        use_pk_tiebreaker = not is_pk_ordering and not grouped_or_distinct and not is_combined
        ordered_qs = (
            queryset.order_by(ordering, "-pk" if is_descending else "pk")
            if use_pk_tiebreaker
            else queryset.order_by(ordering)
        )
        lookup = "lt" if is_descending else "gt"

        async def is_json_null_at(pk: Any) -> bool:
            # Query the database only for a NULL cursor boundary. Django
            # deserializes both JSON null and SQL NULL to Python None.
            return await run_in_orm_executor(
                lambda: queryset.filter(
                    pk=pk,
                    **{ordering_field: Value(None, output_field=ordering_output_field)},
                ).exists()
            )

        if isinstance(cursor_value, _CompositeCursor):
            try:
                if not use_pk_tiebreaker or (cursor_value.value is _CURSOR_JSON_NULL and not is_json_ordering):
                    raise ValueError("Composite cursor is not valid for this ordering")
                cursor_pk = pk_field.to_python(cursor_value.pk)
                if cursor_pk is None:
                    raise ValueError("Missing cursor primary key")
                value = cursor_value.value
                if (
                    value is not None
                    and value is not _CURSOR_JSON_NULL
                    and sort_field is not None
                    and sort_field.concrete
                    and not is_json_ordering
                ):
                    value = sort_field.to_python(value)
                # Older composite cursors did not distinguish the two NULLs.
                if is_json_ordering and value is None and await is_json_null_at(cursor_pk):
                    value = _CURSOR_JSON_NULL
                cursor_value = _CompositeCursor(value, cursor_pk)
            except (TypeError, ValueError, OverflowError, ValidationError):
                cursor_value = None  # Invalid cursor: behave like the first page.
        elif cursor_value is _CURSOR_NULL and (not grouped_or_distinct or is_pk_ordering):
            cursor_value = None

        if grouped_or_distinct and not is_pk_ordering and cursor_value is not None:
            nulls_first = connections[queryset.db].features.nulls_order_largest == is_descending
            null_lookup = f"{ordering_field}__isnull"
            if cursor_value is _CURSOR_NULL:
                # NULL is a real cursor position, not a missing cursor.
                ordered_qs = ordered_qs.filter(**{null_lookup: False}) if nulls_first else ordered_qs.none()
            else:
                after_cursor = Q(**{f"{ordering_field}__{lookup}": cursor_value})
                try:
                    nullable = queryset.model._meta.get_field(ordering_field).null
                except FieldDoesNotExist:
                    nullable = True
                if nullable and not nulls_first:
                    after_cursor |= Q(**{null_lookup: True})
                ordered_qs = ordered_qs.filter(after_cursor)
        elif isinstance(cursor_value, _CompositeCursor):
            value, cursor_pk = cursor_value
            nulls_first = connections[queryset.db].features.nulls_order_largest == is_descending
            try:
                nullable = queryset.model._meta.get_field(ordering_field).null
            except FieldDoesNotExist:  # annotation/expression, so assume nullable
                nullable = True
            null_lookup = f"{ordering_field}__isnull"
            # A JSON null is a non-SQL-NULL value in database ordering.
            boundary = Value(None, output_field=ordering_output_field) if value is _CURSOR_JSON_NULL else value
            same_value = Q(**{null_lookup: True}) if value is None else Q(**{ordering_field: boundary})
            tie = same_value & Q(**{f"pk__{lookup}": cursor_pk})
            if connections[queryset.db].vendor == "postgresql" and value is not None and (not nullable or nulls_first):
                # A PostgreSQL composite B-tree can use the row comparison as
                # an Index Cond. The equivalent OR predicate can scan every
                # preceding row, making deep pages grow linearly in cost.
                # Preserve the existing NULL-aware Q fallback when needed.
                row = Tuple(F(ordering_field), F(pk_field.attname))
                comparison = TupleLessThan if is_descending else TupleGreaterThan
                ordered_qs = ordered_qs.filter(comparison(row, (boundary, cursor_pk)))
            else:
                if value is None:
                    after_cursor = tie
                    if nulls_first:
                        after_cursor |= Q(**{null_lookup: False})
                else:
                    after_cursor = Q(**{f"{ordering_field}__{lookup}": boundary}) | tie
                    if nullable and not nulls_first:
                        after_cursor |= Q(**{null_lookup: True})
                ordered_qs = ordered_qs.filter(after_cursor)
        elif cursor_value is not None:
            # Scalar legacy cursors retain their previous semantics.
            ordered_qs = ordered_qs.filter(**{f"{ordering_field}__{lookup}": cursor_value})

        # Fetch page_size + 1 items to determine if there's a next page
        raw_items = await self._evaluate_queryset_slice(ordered_qs[: page_size + 1])

        # Check if there are more items
        has_next = len(raw_items) > page_size
        if has_next:
            raw_items = raw_items[:page_size]  # Trim to page_size

        # Generate a cursor from the raw result before removing internal columns.
        next_cursor = None
        if has_next and raw_items:
            last_item = raw_items[-1]
            if is_values_list_projection:
                # Without explicit fields, Django returns extra columns,
                # model fields, then selected annotations in that order.
                result_fields = selected_fields or (
                    tuple(queryset.query.extra_select)
                    + tuple(field.attname for field in queryset.model._meta.concrete_fields)
                    + tuple(queryset.query.annotation_select)
                )

                # "pk" and the actual PK field name refer to the same key.
                pk_key = next(
                    (name for name in ("pk", pk_field.attname, pk_field.name) if name in result_fields),
                    None,
                )

                if is_flat_values_list and not needs_hidden_pk:
                    last_value = last_item
                else:
                    value_key = (
                        pk_key
                        if is_pk_ordering
                        else next(
                            (
                                name
                                for name in (
                                    ordering_field,
                                    projection_ordering_field,
                                )
                                if name in result_fields
                            ),
                            ordering_field,
                        )
                    )
                    last_value = last_item[result_fields.index(value_key)]

                if needs_hidden_pk:
                    last_pk = last_item[-1]
                elif is_flat_values_list and is_pk_ordering:
                    last_pk = last_item
                elif pk_key is not None:
                    last_pk = last_item[result_fields.index(pk_key)]
                else:
                    last_pk = None
            elif is_values_projection:
                last_value = last_item.get(
                    ordering_field,
                    last_item.get(projection_ordering_field),
                )
                last_pk = last_item.get("pk", last_item.get(pk_field.attname, last_item.get(hidden_pk_field)))
            else:
                last_value = getattr(last_item, ordering_field, None)
                last_pk = last_item.pk

            if is_pk_ordering and last_value is None:
                last_value = last_pk

            if (
                is_json_ordering
                and use_pk_tiebreaker
                and last_value is None
                and last_pk is not None
                and await is_json_null_at(last_pk)
            ):
                last_value = _CURSOR_JSON_NULL

            if use_pk_tiebreaker and last_pk is not None:
                # Keep SQL NULL in the old envelope; tag only JSON null.
                json_null = last_value is _CURSOR_JSON_NULL
                next_cursor = self._encode_cursor(
                    {"value": None if json_null else last_value, "pk": last_pk},
                    kind="composite-json-null-v1" if json_null else "composite-v2",
                )
            elif grouped_or_distinct and not is_pk_ordering and last_value is None:
                next_cursor = self._encode_cursor(None, kind="null-v2")
            elif last_value is not None:
                next_cursor = self._encode_cursor(last_value)

        if needs_hidden_pk:
            if is_values_projection:
                for item in raw_items:
                    item.pop(hidden_pk_field, None)
            elif is_values_list_projection:
                if is_flat_values_list:
                    raw_items = [item[0] for item in raw_items]
                elif is_named_values_list:
                    row_type = create_namedtuple_class(*selected_fields)
                    raw_items = [row_type(*item[:-1]) for item in raw_items]
                else:
                    raw_items = [item[:-1] for item in raw_items]

        # Serialize items using efficient batch serialization
        items = await self._serialize_items(raw_items)

        return self.build_response(
            items,
            total=0,  # Cursor pagination doesn't provide total count for efficiency
            request=request,
            has_next=has_next,
            has_previous=cursor_value is not None,
            page_size=page_size,
            next_cursor=next_cursor,
        )


def paginate(pagination_class: type[PaginationBase] = PageNumberPagination):
    """
    Decorator to apply pagination to a route handler.

    The decorated handler should return a Django QuerySet or list.
    The decorator will automatically apply pagination and return a PaginatedResponse.

    When used with response_model or return type annotation like list[UserSerializer],
    the serializer will be used for efficient batch serialization via dump_many().

    Args:
        pagination_class: Pagination class to use (default: PageNumberPagination)

    Example:
        from django_bolt.serializers import Serializer

        class UserSerializer(Serializer):
            id: int
            name: str

        @api.get("/users", response_model=list[UserSerializer])
        @paginate(PageNumberPagination)
        async def list_users():
            return User.objects.all()

        @api.get("/articles")
        @paginate(LimitOffsetPagination)
        async def list_articles(status: str = "published"):
            return Article.objects.filter(status=status)

    Returns:
        Decorated handler function
    """

    def decorator(handler: Callable) -> Callable:
        # Store original handler for introspection
        original_handler = handler
        request_getter = _compile_request_getter(original_handler)
        cached_paginator: PaginationBase | None = None

        @wraps(handler)
        async def wrapper(*args, **kwargs):
            nonlocal cached_paginator

            if cached_paginator is None:
                cached_paginator = pagination_class()
                cached_paginator.serializer_class = getattr(wrapper, "__serializer_class__", None)

            request = request_getter(args, kwargs)

            if request is None:
                raise ValueError(
                    f"Pagination decorator on {handler.__name__} could not find request. "
                    f"Args: {[type(a).__name__ for a in args]}, kwargs: {list(kwargs.keys())}"
                )

            # PyRequest already supports .get() and __getitem__ — no per-request
            # isinstance/hasattr type sniffing (hot-path rule). Anything the
            # request_getter yields is used directly; a genuinely wrong object
            # fails loudly inside paginate_queryset.

            # Call original handler to get queryset
            # Pass all args along (including self for methods)
            queryset = await handler(*args, **kwargs)

            # Apply pagination using dict version
            paginated = await cached_paginator.paginate_queryset(queryset, request)

            return paginated

        # Preserve signature so framework knows to pass request
        wrapper.__signature__ = inspect.signature(original_handler)
        wrapper.__name__ = original_handler.__name__
        wrapper.__doc__ = original_handler.__doc__
        wrapper.__module__ = original_handler.__module__

        # Mark that this handler returns PaginatedResponse for serialization
        wrapper.__paginated__ = True
        wrapper.__pagination_class__ = pagination_class

        return wrapper

    return decorator


def _compile_request_getter(handler: Callable) -> Callable[[tuple[Any, ...], dict[str, Any]], Any]:
    """Compile a lightweight request extractor once per paginated handler."""
    sig = inspect.signature(handler)
    positional_request_index: int | None = None

    for index, param in enumerate(sig.parameters.values()):
        if param.name != "request":
            continue

        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional_request_index = index
        break

    def get_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        request = kwargs.get("request", _PAGINATION_UNSET)
        if request is not _PAGINATION_UNSET:
            return request

        if positional_request_index is not None and positional_request_index < len(args):
            return args[positional_request_index]

        for candidate in args[:2]:
            if isinstance(candidate, dict) or hasattr(candidate, "get") or hasattr(candidate, "__getitem__"):
                return candidate

        return None

    return get_request
