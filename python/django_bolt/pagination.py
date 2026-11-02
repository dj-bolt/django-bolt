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
_CURSOR_JSON_NULL = object()


class _CompositeCursor(NamedTuple):
    value: Any
    pk: Any


class _KeysetCursor(NamedTuple):
    values: tuple[Any, ...]
    json_nulls: tuple[int, ...] = ()


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
    """Keyset pagination with automatic model-PK ties or explicit ordering.

    A string ordering adds a PK tie-breaker for ordinary model rows.
    A tuple ordering uses exactly the fields supplied by the developer.
    Cursors use the existing base64-JSON API; historical scalar cursors
    and earlier composite cursors remain readable.
    """

    page_size: int = 100
    max_page_size: int = 1000
    page_size_query_param: str = "page_size"
    ordering: str | tuple[str, ...] = "-id"

    def _encode_cursor(self, value: Any, *, kind: str | None = None, json_nulls: tuple[int, ...] = ()) -> str:
        data = {"v": value}
        if kind is not None:
            data["t"] = kind
        if json_nulls:
            data["j"] = list(json_nulls)
        return base64.b64encode(_json.encode(data)).decode("utf-8")

    def _decode_cursor(self, cursor: str) -> Any:
        try:
            data = msgspec.json.decode(base64.b64decode(cursor.encode("utf-8"), validate=True))
            if not isinstance(data, dict) or "v" not in data:
                return None
            value, kind = data["v"], data.get("t")
            if kind == "keyset-v1":
                if not isinstance(value, list) or not value:
                    return None
                marks = data.get("j", [])
                if (
                    not isinstance(marks, list)
                    or any(type(i) is not int or i < 0 or i >= len(value) for i in marks)
                    or len(set(marks)) != len(marks)
                    or any(value[i] is not None for i in marks)
                ):
                    return None
                return _KeysetCursor(tuple(value), tuple(marks))
            if kind in {"composite-v2", "composite-json-null-v1"}:
                if not isinstance(value, dict) or set(value) != {"value", "pk"}:
                    return None
                if kind == "composite-json-null-v1" and value["value"] is not None:
                    return None
                if kind == "composite-json-null-v1":
                    return _KeysetCursor((None, value["pk"]), (0,))
                return _CompositeCursor(value["value"], value["pk"])
            if kind == "null-v2":
                return _KeysetCursor((None,), ()) if value is None else None
            return value
        except (ValueError, TypeError, UnicodeError, msgspec.DecodeError):
            return None

    async def get_page_params(self, request: dict[str, Any]) -> dict[str, Any]:
        query = request.get("query", {})
        cursor = query.get("cursor")
        return {
            "cursor": self._decode_cursor(cursor) if cursor else None,
            "page_size": self._get_page_size(request),
        }

    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> Any:
        page_params = await self.get_page_params(request)
        cursor = page_params["cursor"]
        page_size = page_params["page_size"]
        configured = self.ordering
        explicit = isinstance(configured, tuple)
        if not isinstance(configured, (str, tuple)) or (explicit and not configured):
            raise ValueError("CursorPagination ordering must be a field string or a nonempty tuple of fields")
        configured_terms = configured if explicit else (configured,)
        if queryset.query.combinator:
            # Django forbids filtering a combined QuerySet after union/intersection/difference.
            raise ValueError("CursorPagination cannot seek through combined QuerySets (union/intersection/difference)")

        pk_field = queryset.model._meta.pk
        terms = []
        for token in configured_terms:
            if not isinstance(token, str) or not token or token in {"-", "?"}:
                raise ValueError("CursorPagination requires concrete field or annotation ordering")
            descending = token.startswith("-")
            name = token[1:] if descending else token
            if "__" in name:
                raise ValueError("CursorPagination ordering does not support related-field traversals")
            if name == "pk":
                name = pk_field.attname
            try:
                field = pk_field if name == "pk" else queryset.model._meta.get_field(name)
            except FieldDoesNotExist:
                field = None
            if field is None:
                annotation = queryset.query.annotations.get(name)
                if annotation is None:
                    raise ValueError(f"Unknown cursor ordering field: {name}")
                try:
                    output_field = annotation.output_field
                except FieldError as exc:
                    raise ValueError(f"Cannot resolve ordering annotation: {name}") from exc
            else:
                if not field.concrete:
                    raise ValueError(f"Cursor ordering field must be concrete: {name}")
                if field.is_relation:
                    if not (field.many_to_one or field.one_to_one):
                        raise ValueError("CursorPagination cannot order by a multi-valued relation")
                    if name == field.name and field.remote_field.model._meta.ordering:
                        raise ValueError(f"Use '{field.attname}' for a stable foreign-key ordering")
                    name = field.attname
                output_field = field
            is_json = output_field.get_internal_type() == "JSONField"
            terms.append((name, token.lstrip("-"), descending, output_field, is_json))

        iterable = getattr(queryset, "_iterable_class", None)
        is_values = iterable is ValuesIterable
        is_flat = iterable is FlatValuesListIterable
        is_named = iterable is NamedValuesListIterable
        is_values_list = iterable in (ValuesListIterable, FlatValuesListIterable, NamedValuesListIterable)
        projected = is_values or is_values_list
        selected = tuple(getattr(queryset, "_fields", None) or ())
        grouped = projected and bool(queryset.query.distinct or queryset.query.group_by)
        single_pk = len(terms) == 1 and terms[0][0] in {pk_field.name, pk_field.attname}
        auto_pk = not explicit and not single_pk and not grouped

        if grouped and not explicit and not single_pk:
            # A single projected/grouping key uniquely identifies its result.
            if selected != (terms[0][1],) and selected != (terms[0][0],):
                raise ValueError("DISTINCT/GROUP BY needs explicit tuple ordering for multiple result keys")
            if queryset.query.group_by:
                group_keys = queryset.query.group_by
                resolved = queryset.query.resolve_ref(terms[0][0])
                if group_keys is True or len(group_keys) != 1 or (
                    group_keys[0] != resolved and getattr(group_keys[0], "refs", None) != terms[0][0]
                ):
                    raise ValueError("GROUP BY ordering must identify each projected group")
            elif (set(queryset.query.annotation_select) | set(queryset.query.extra_select)) - {terms[0][0], terms[0][1]}:
                raise ValueError("DISTINCT with additional selected expressions needs explicit tuple ordering")
        if grouped and any(t[4] for t in terms):
            # Neither SQL NULL nor JSON null has a row PK for a grouped result.
            raise ValueError("JSONField grouping needs an explicit non-JSON unique ordering key")

        if auto_pk:
            terms.append((pk_field.attname, pk_field.attname, terms[0][2], pk_field, False))
        term_names = tuple(t[0] for t in terms)
        ordering = tuple(("-" if t[2] else "") + t[0] for t in terms)
        if projected and selected:
            missing = [
                t for t in terms
                if t[0] not in selected and t[1] not in selected
                and not (t[0] in {pk_field.attname, pk_field.name} and (auto_pk or single_pk))
            ]
            if missing:
                raise ValueError("CursorPagination projection must include all explicit ordering fields")

        pk_projected = not selected or bool({pk_field.name, pk_field.attname, "pk"}.intersection(selected))
        needs_hidden_pk = projected and (auto_pk or single_pk) and not pk_projected and not grouped
        hidden_pk = "_bolt_cursor_pk"
        if needs_hidden_pk:
            while hidden_pk in selected or hidden_pk in queryset.query.annotations:
                hidden_pk += "_"
            queryset = queryset.annotate(**{hidden_pk: F("pk")})
            if is_flat or is_named:
                queryset._iterable_class = ValuesListIterable

        def read_value(row: Any, name: str, alias: str, result_fields: tuple[str, ...]) -> Any:
            if is_values:
                return row[name] if name in row else row[alias]
            if is_values_list:
                if is_flat and not needs_hidden_pk:
                    return row
                key = next((x for x in (name, alias, "pk" if name == pk_field.attname else "") if x in result_fields), None)
                if key is None:
                    raise ValueError(f"Ordering field {name} is not in values_list()")
                return row[result_fields.index(key)]
            return getattr(row, name)

        async def is_json_null_at(pk: Any, field_name: str, output_field: Any) -> bool:
            if pk is None:
                raise ValueError("JSONField cursor cannot distinguish SQL NULL from JSON null without a row key")
            if output_field.concrete and not output_field.null and field_name == output_field.attname:
                return True
            return await run_in_orm_executor(
                lambda: queryset.filter(
                    pk=pk, **{field_name: Value(None, output_field=output_field)}
                ).exists()
            )

        if isinstance(cursor, _CompositeCursor):
            if len(terms) != 2 or not auto_pk:
                cursor = None
            else:
                val = cursor.value
                if val is None and terms[0][4] and await is_json_null_at(cursor.pk, terms[0][0], terms[0][3]):
                    cursor = _KeysetCursor((None, cursor.pk), (0,))
                else:
                    cursor = _KeysetCursor((val, cursor.pk), ())
        if isinstance(cursor, _KeysetCursor):
            try:
                if len(cursor.values) != len(terms) or any(
                    not terms[i][4] for i in cursor.json_nulls
                ):
                    raise ValueError("Wrong cursor ordering shape")
                values = list(cursor.values)
                for i, term in enumerate(terms):
                    if i in cursor.json_nulls:
                        values[i] = _CURSOR_JSON_NULL
                    elif values[i] is not None and term[3].concrete:
                        values[i] = term[3].to_python(values[i])
                cursor = _KeysetCursor(tuple(values), cursor.json_nulls)
            except (ValueError, TypeError, OverflowError, ValidationError):
                cursor = None

        ordered = queryset.order_by(*ordering)
        if isinstance(cursor, _KeysetCursor):
            values = cursor.values
            features = connections[queryset.db].features
            null_parts = []
            prefix = Q()
            parts = []
            for term, value in zip(terms, values):
                name, alias, descending, output, is_json = term
                null_first = features.nulls_order_largest == descending
                sql_null = value is None
                bound = Value(None, output_field=output) if value is _CURSOR_JSON_NULL else value
                same = Q(**{f"{name}__isnull": True}) if sql_null else Q(**{name: bound})
                if sql_null:
                    after = Q(**{f"{name}__isnull": False}) if null_first else None
                else:
                    after = Q(**{f"{name}__{'lt' if descending else 'gt'}": bound})
                    if (output.null or name in queryset.query.annotations) and not null_first:
                        after |= Q(**{f"{name}__isnull": True})
                if after is not None:
                    parts.append(prefix & after)
                prefix &= same
            if not parts:
                ordered = ordered.none()
            elif (
                connections[queryset.db].vendor == "postgresql"
                and len(terms) > 1
                and len({t[2] for t in terms}) == 1
                and all(v is not None and v is not _CURSOR_JSON_NULL for v in values)
                and all((not t[3].null and t[0] not in queryset.query.annotations) or features.nulls_order_largest == t[2] for t in terms)
            ):
                # B-tree-friendly row comparison for non-null, equal-direction keys.
                row = Tuple(*(F(t[0]) for t in terms))
                comparator = TupleLessThan if terms[0][2] else TupleGreaterThan
                ordered = ordered.filter(comparator(row, values))
            else:
                after_any = parts[0]
                for part in parts[1:]:
                    after_any |= part
                ordered = ordered.filter(after_any)
        elif cursor is not None:
            # Historical scalar cursors cannot identify the exact row in a tie.
            name, _, descending, output, _ = terms[0]
            try:
                if output.concrete:
                    cursor = output.to_python(cursor)
                ordered = ordered.filter(**{f"{name}__{'lt' if descending else 'gt'}": cursor})
            except (ValueError, TypeError, OverflowError, ValidationError):
                cursor = None
                ordered = queryset.order_by(*ordering)

        raw = await self._evaluate_queryset_slice(ordered[: page_size + 1])
        has_next = len(raw) > page_size
        if has_next:
            raw = raw[:page_size]

        next_cursor = None
        if has_next and raw:
            last = raw[-1]
            result_fields = selected or (
                tuple(queryset.query.extra_select)
                + tuple(f.attname for f in queryset.model._meta.concrete_fields)
                + tuple(queryset.query.annotation_select)
            )
            values = [
                read_value(last, t[0], t[1], result_fields)
                for t in terms
            ]
            last_pk = (
                last.get(pk_field.attname, last.get(hidden_pk))
                if is_values else
                last[-1] if is_values_list and needs_hidden_pk else
                read_value(last, pk_field.attname, pk_field.name, result_fields)
                if is_values_list and pk_projected else
                last.pk if not projected else None
            )
            json_nulls = []
            for i, term in enumerate(terms):
                if term[4] and values[i] is None and await is_json_null_at(last_pk, term[0], term[3]):
                    json_nulls.append(i)
            if not explicit and len(terms) == 1 and values[0] is not None and not json_nulls:
                next_cursor = self._encode_cursor(values[0])
            else:
                next_cursor = self._encode_cursor(values, kind="keyset-v1", json_nulls=tuple(json_nulls))

        if needs_hidden_pk:
            if is_values:
                for row in raw:
                    row.pop(hidden_pk, None)
            elif is_flat:
                raw = [row[0] for row in raw]
            elif is_named:
                named_row = create_namedtuple_class(*selected)
                raw = [named_row(*row[:-1]) for row in raw]
            elif is_values_list:
                raw = [row[:-1] for row in raw]

        return self.build_response(
            await self._serialize_items(raw),
            total=0,
            request=request,
            has_next=has_next,
            has_previous=cursor is not None,
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
