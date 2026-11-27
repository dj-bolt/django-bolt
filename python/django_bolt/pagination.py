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
from collections.abc import Callable, Sequence
from functools import cache, wraps
from typing import Any, Generic, TypeVar, get_args, get_origin

import msgspec
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import connections
from django.db.models import CompositePrimaryKey, F, Field, IntegerField
from django.db.models.fields.tuple_lookups import Tuple, TupleGreaterThan, TupleLessThan

from . import _json, async_orm
from ._kwargs import StructRelationLoader, compile_struct_relation_loader
from .concurrency import run_in_orm_executor
from .exceptions import ResponseValidationError, UnloadedRelationError
from .middleware.middleware import note_config_decorator
from .typing import REQUEST_PARAM_NAMES

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
# A cursor uses the URL-safe base64 alphabet. A cursor from an earlier release has "+" and "/".
_URL_SAFE_TO_STANDARD = str.maketrans("-_", "+/")


def _cursor_field(model: Any, name: str) -> Any:
    """Return the model field that a cursor value must fit. Return None when ``name`` is not a model field."""
    if name == "pk":
        return model._meta.pk
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    # A reverse relation is not a Field and has no get_prep_value().
    return field if isinstance(field, Field) else None


def _field_accepts(field: Any, value: Any, queryset: Any) -> bool:
    """Return True when a lookup on ``field`` can use ``value``. A cursor value comes from the client.

    ``queryset`` gives the database. An integer must be in the range of its column in that database.
    """
    if field is None:
        return True
    if isinstance(field, CompositePrimaryKey):
        # The cursor holds a list with one value for each column of the key.
        columns = field.fields
        return (
            isinstance(value, list)
            and len(value) == len(columns)
            and all(_field_accepts(column, item, queryset) for column, item in zip(columns, value, strict=True))
        )
    try:
        # The lookup makes the same call. Without this check, its error is a 500.
        prepared = field.get_prep_value(value)
    except (ValueError, TypeError, ValidationError):
        return False
    if isinstance(prepared, int):
        # A ForeignKey has the column type of its target.
        while field.is_relation:
            field = field.target_field
        if isinstance(field, IntegerField):
            # The row comparison of the tie-breaker sends the integer as it is. Out of the range,
            # SQLite raises OverflowError and other databases raise a data error.
            low, high = connections[queryset.db].ops.integer_field_range(field.get_internal_type())
            return (low is None or prepared >= low) and (high is None or prepared <= high)
    return True


class PaginatedResponse(msgspec.Struct, Generic[T], omit_defaults=True):  # noqa: UP046 — PEP 695 params are unresolvable under PEP 563
    """
    Standard paginated response structure.

    Uses omit_defaults=True so only relevant fields are included in response:
    - PageNumberPagination: page, page_size, total_pages, next_page, previous_page
    - LimitOffsetPagination: limit, offset
    - CursorPagination: page_size, next_cursor

    Attributes:
        items: List of paginated items
        total: Total number of items across all pages
        has_next: Whether there is a next page (always included)
        has_previous: Whether there is a previous page (always included).
            For CursorPagination, it is True when the request has a valid cursor.
            A cursor page gives no cursor to go back.
        page: Current page number (for PageNumber pagination)
        page_size: Number of items per page
        total_pages: Total number of pages
        next_page: Next page number (None if no next page)
        previous_page: Previous page number (None if no previous page)
        limit: Number of items requested (for LimitOffset pagination)
        offset: Position of the first item (for LimitOffset pagination)
        next_cursor: Cursor of the next page (for Cursor pagination, None if no next page)
        previous_cursor: Not set by the built-in schemes. A custom scheme can set it.
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

    # Maximum allowed page size (prevents abuse). It also limits the default page size.
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
        next_page/previous_page, limit/offset, page_size/next_cursor).
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

        Args:
            queryset: Django QuerySet, list, tuple or other sized sequence.
                It can also be a collection of the application that can be sliced
                and has an async ``acount()`` or a sync ``count()`` with no argument.

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
            if async_orm.enabled:
                # With BOLT_ASYNC_ORM_ON_LOOP, the event loop sends the statement.
                total = await async_orm.count(queryset)
                if total is not None:
                    return total
            return await run_in_orm_executor(queryset.count)
        # Non-Django async collections that expose acount
        if hasattr(queryset, "acount"):
            return await queryset.acount()
        # A sequence has count(value), which counts one value. Its size is len().
        if isinstance(queryset, Sequence):
            return len(queryset)
        # Other sync collections with a count() method
        if hasattr(queryset, "count") and callable(queryset.count):
            return await run_in_orm_executor(queryset.count)
        # For other iterables
        else:
            return len(queryset)

    async def _serialize_items(self, items: list[Any]) -> list[Any]:
        """
        Serialize items using efficient batch serialization.

        Supports:
        - Bolt Serializer: from_model() + dump_many() (fastest). A dict is
          validated, as a ``list[Serializer]`` response type with no ``@paginate``
        - msgspec.Struct: one msgspec.convert() for the page, as a
          ``list[Struct]`` response type with no ``@paginate``
        - Fallback: _model_to_dict() for backwards compatibility

        Args:
            items: List of raw items (Django models, dicts or Structs)

        Returns:
            List of Struct instances for a plain msgspec.Struct item type,
            else a list of dictionaries. Both are ready for JSON serialization.

        Raises:
            ResponseValidationError: An item does not fit the Struct or the
                Serializer item type.
        """
        if not items:
            return []

        if self.serializer_class is None:
            # Backwards compatibility: use _model_to_dict fallback
            return [self._model_to_dict(item) for item in items]

        convert_page = _struct_page_converter(self.serializer_class)
        if convert_page is not None:
            relation_loader = _struct_relation_loader(self.serializer_class)
            if relation_loader is not None:
                # After the cursor: a CursorPagination reads it from the instances.
                items = relation_loader.rows(items)
            return convert_page(items)

        if getattr(self.serializer_class, "__is_bolt_serializer__", False):
            return await _dump_serializer_page(self.serializer_class, items)

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
        relation_loader = _struct_relation_loader(self.serializer_class)
        if hasattr(queryset, "_iterable_class") and hasattr(queryset, "model"):
            serializer_class = self.serializer_class
            if getattr(serializer_class, "__is_bolt_serializer__", False):
                # Pre-load what the item serializer reads (select_related /
                # prefetch_related / annotate) so a page never costs N+1.
                queryset = serializer_class._prepare_queryset(queryset)
            elif relation_loader is not None:
                # Join the relations of a nested Struct item type, in the same hop.
                return await run_in_orm_executor(relation_loader.evaluate, queryset)
            # With BOLT_ASYNC_ORM_ON_LOOP, the event loop sends the statement.
            if async_orm.enabled and await async_orm.load(queryset):
                return list(queryset)
            return await run_in_orm_executor(list, queryset)
        # Non-QuerySet async iterables (has __aiter__)
        if hasattr(queryset, "__aiter__"):
            async for item in queryset:
                items.append(item)
        # Regular iterable or list
        else:
            items = list(queryset)
        if relation_loader is not None:
            await relation_loader.aload(items)
        return items

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
                        return min(self.page_size, self.max_page_size)
                    return page_size
                except (ValueError, TypeError):
                    pass

        # max_page_size also limits the default. A subclass can set only max_page_size.
        return min(self.page_size, self.max_page_size)


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

        # A page after the last page is the last page. With no rows, it is page 1.
        if page_number > total_pages:
            page_number = max(total_pages, 1)

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
            except (ValueError, TypeError):
                limit = self.page_size
        # max_page_size also limits the default. A subclass can set only max_page_size.
        if limit > self.max_page_size:
            limit = self.max_page_size

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

        # An offset at or after the end selects no row, so it needs no query.
        # The database also refuses an offset that does not fit its integer type.
        if offset >= total:
            raw_items = []
        else:
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

    More efficient than offset-based pagination for large datasets
    as it doesn't require counting all records or scanning through skipped records.

    The handler must return a Django QuerySet. The cursor is a filter on the database.
    A list raises TypeError.

    Query parameters:
        - cursor: Opaque cursor string (optional)
        - page_size: Items per page (optional)

    Example:
        /api/users?cursor=eyJpZCI6MTAwfQ==&page_size=20

    Attributes:
        page_size: Default number of items per page (default: 100)
        max_page_size: Maximum allowed page size (default: 1000)
        page_size_query_param: Query param name for page size (default: "page_size")
        ordering: Field to order by (default: "-id" for descending ID).
            The primary key breaks ties, so the field does not have to be unique.
            A values() queryset must include the primary key.
            The field must not be null: a null value cannot make a cursor.
            For a ForeignKey, the cursor holds the key. Use the key name ("author_id")
            when the related model has Meta.ordering.
    """

    page_size: int = 100
    max_page_size: int = 1000
    page_size_query_param: str = "page_size"
    ordering: str = "-id"  # Default ordering field

    def _encode_cursor(self, value: Any, pk: Any = None) -> str:
        """
        Encode cursor position to a URL-safe base64 string.

        Args:
            value: Ordering value of the last row
            pk: Primary key of the last row. None when the ordering field is the primary key.

        Returns:
            Cursor string. A client can put it into a query string with no percent-encoding.
        """
        cursor_data = _json.encode({"v": value} if pk is None else {"v": value, "pk": pk})
        return base64.urlsafe_b64encode(cursor_data).decode("ascii")

    def _decode_cursor(self, cursor: str) -> tuple[Any, Any]:
        """
        Decode cursor string to position.

        Args:
            cursor: Cursor string in the URL-safe or the standard base64 alphabet

        Returns:
            (ordering value, primary key). Each is None when the cursor does not hold it.
            A cursor that is not base64 of a JSON object gives (None, None).
        """
        try:
            data = msgspec.json.decode(base64.b64decode(cursor.translate(_URL_SAFE_TO_STANDARD)))
        except (ValueError, RecursionError):
            # ValueError includes binascii.Error, UnicodeError and msgspec.DecodeError.
            # JSON with too many levels raises RecursionError.
            return None, None
        if not isinstance(data, dict):
            return None, None
        return data.get("v"), data.get("pk")

    async def get_page_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """Extract cursor and page_size from request."""
        query = request.get("query", {})

        # Get cursor (optional)
        cursor_str = query.get("cursor")
        cursor_value, cursor_pk = self._decode_cursor(cursor_str) if cursor_str else (None, None)

        # Get page size
        page_size = self._get_page_size(request)

        return {"cursor": cursor_value, "cursor_pk": cursor_pk, "page_size": page_size}

    async def _row_ordering_value(self, row: Any, name: str) -> Any:
        """
        Read the ordering value of a row that is not a dict.

        Args:
            row: Model instance, or a named row of values_list()
            name: Ordering field with no "-"

        Returns:
            The value for the cursor. For a ForeignKey, it is the key and not the related object.
        """
        state = getattr(row, "__dict__", None)
        meta = getattr(row, "_meta", None)
        if state is None or meta is None or name in state:
            # A loaded field, an annotation, or a row that is not a model instance.
            return getattr(row, name, None)
        field = next((f for f in meta.concrete_fields if name in (f.name, f.attname)), None)
        if field is None:
            return getattr(row, name, None)
        if field.attname in state:
            # A ForeignKey name. The attribute is the related object, and to read it can run a query.
            related_model = field.related_model
            if related_model is not None and related_model._meta.ordering:
                raise ValueError(
                    f"CursorPagination ordering {self.ordering!r} names a ForeignKey to a model that has "
                    f"Meta.ordering, so the rows are not in the order of the key. Order by {field.attname!r}."
                )
            return state[field.attname]
        # The queryset defers the field. Load it on the ORM executor, not on the event loop.
        return await run_in_orm_executor(getattr, row, field.attname)

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
        # An override of get_page_params from before the tie-breaker returns no "cursor_pk".
        cursor_pk = page_params.get("cursor_pk")
        page_size = page_params["page_size"]

        # Apply ordering
        ordering = self.ordering
        is_descending = ordering.startswith("-")
        ordering_field = ordering.lstrip("-")
        try:
            pk_field = queryset.model._meta.pk
        except AttributeError:
            # A cursor is a filter on the database. A list has no such filter.
            raise TypeError(
                f"CursorPagination needs a Django QuerySet, and the handler returned {type(queryset).__name__}. "
                f"Use PageNumberPagination or LimitOffsetPagination for a list."
            ) from None

        # Rows can share an ordering value. Order by the primary key too, so that
        # a page that ends inside such a group continues at the next row.
        tie_break = ordering_field not in ("pk", pk_field.name, pk_field.attname)
        if tie_break:
            ordered_qs = queryset.order_by(ordering, "-pk" if is_descending else "pk")
        else:
            ordered_qs = queryset.order_by(ordering)

        # A value that its field cannot use means no cursor, as a cursor that does not decode.
        if cursor_value is not None and not (
            _field_accepts(_cursor_field(queryset.model, ordering_field), cursor_value, queryset)
            and (cursor_pk is None or not tie_break or _field_accepts(pk_field, cursor_pk, queryset))
        ):
            cursor_value = None

        # Apply cursor filter if present
        if cursor_value is not None:
            # Descending order continues with smaller values, ascending with larger values.
            lookup = "lt" if is_descending else "gt"
            # A cursor from before the tie-breaker holds no primary key. It keeps the old filter.
            if tie_break and cursor_pk is not None:
                # A row comparison, (field, pk) > (value, pk), lets an index on (field, pk) seek.
                # A composite primary key adds each of its columns, and the cursor holds a list.
                if isinstance(pk_field, CompositePrimaryKey):
                    pk_columns = [F(name) for name in pk_field.field_names]
                    pk_values = tuple(cursor_pk)
                else:
                    pk_columns = [F("pk")]
                    pk_values = (cursor_pk,)
                after = TupleLessThan if is_descending else TupleGreaterThan
                ordered_qs = ordered_qs.filter(after(Tuple(F(ordering_field), *pk_columns), (cursor_value, *pk_values)))
            else:
                ordered_qs = ordered_qs.filter(**{f"{ordering_field}__{lookup}": cursor_value})

        # Fetch page_size + 1 items to determine if there's a next page
        raw_items = await self._evaluate_queryset_slice(ordered_qs[: page_size + 1])

        # Check if there are more items
        has_next = len(raw_items) > page_size
        if has_next:
            raw_items = raw_items[:page_size]  # Trim to page_size

        # Generate next cursor from last raw item (before serialization)
        next_cursor = None
        if has_next and raw_items:
            last_item = raw_items[-1]
            # Items are raw (Django models or dicts from values()), get cursor value
            if isinstance(last_item, dict):
                value_key = ordering_field
                if not tie_break and ordering_field not in last_item:
                    # The ordering field is the primary key. The row can have it as the column name or as "pk".
                    value_key = pk_field.attname if pk_field.attname in last_item else "pk"
                if value_key not in last_item:
                    raise ValueError(
                        f"CursorPagination ordering {ordering!r} needs {ordering_field!r} in values() "
                        f"to make the next cursor."
                    )
                last_value = last_item[value_key]
                last_pk = last_item.get(pk_field.attname, last_item.get("pk"))
            else:
                last_value = await self._row_ordering_value(last_item, ordering_field)
                last_pk = getattr(last_item, "pk", None)

            if last_value is None:
                # A cursor of null means "first page", and SQL compares no value with null.
                # Without this error, the page says has_next but gives no cursor to get it.
                raise ValueError(
                    f"CursorPagination cannot continue after a row whose {ordering_field!r} is null. "
                    f"Order by a field that is never null, or exclude the null values."
                )
            if not tie_break:
                next_cursor = self._encode_cursor(last_value)
            elif last_pk is None:
                raise ValueError(
                    f"CursorPagination ordering {ordering!r} needs the primary key "
                    f"{pk_field.attname!r} in values() to continue after rows with equal values."
                )
            else:
                next_cursor = self._encode_cursor(last_value, last_pk)

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


def _to_serializer(serializer_class: Any, item: Any) -> Any:
    """Make the Serializer instance of one item of a page, from the kind of that item."""
    if isinstance(item, dict):
        return msgspec.convert(item, serializer_class)
    if getattr(item.__class__, "__is_bolt_serializer__", False):
        return item
    return serializer_class.from_model(item)


async def _to_serializer_async(serializer_class: Any, item: Any) -> Any:
    try:
        return _to_serializer(serializer_class, item)
    except UnloadedRelationError:
        pass
    return await serializer_class.afrom_model(item)


async def _dump_serializer_page(serializer_class: Any, items: list[Any]) -> list[dict]:
    """Dump one page for a Bolt Serializer item type.

    Each item goes through the Serializer as the same route with no
    ``@paginate`` sends it: a dict is validated, an instance stays, and each
    other object is read by ``from_model()``. Thus the page has the same
    items, and an item that does not fit gives the same response validation
    error.
    """
    serializer_class._ensure_from_model_ready()
    try:
        try:
            instances = [_to_serializer(serializer_class, item) for item in items]
        except UnloadedRelationError:
            instances = list(await asyncio.gather(*(_to_serializer_async(serializer_class, item) for item in items)))
        return serializer_class.dump_many(instances)
    except msgspec.ValidationError as exc:
        raise ResponseValidationError([exc]) from exc
    except Exception as exc:
        raise ResponseValidationError([{"loc": ["response"], "msg": str(exc), "type": "validation_error"}]) from exc


@cache
def _struct_page_converter(item_type: type) -> Callable[[list[Any]], list[Any]] | None:
    """The conversion of one page for a plain msgspec.Struct item type, else None. Built one time per type.

    The conversion is that of a ``list[Struct]`` response type with no
    ``@paginate``: msgspec reads a dict by key and each other object by
    attribute. Thus the page has the same items, and a value that does not fit
    the Struct gives the same response validation error.
    """
    if not (isinstance(item_type, type) and issubclass(item_type, msgspec.Struct)):
        return None
    if getattr(item_type, "__is_bolt_serializer__", False):
        return None
    page_type = list[item_type]

    def convert_page(items: list[Any]) -> list[Any]:
        try:
            return msgspec.convert(items, page_type, from_attributes=True)
        except msgspec.ValidationError as exc:
            raise ResponseValidationError([exc]) from exc

    return convert_page


@cache
def _struct_relation_loader(item_type: Any) -> StructRelationLoader | None:
    """The relation loader for a plain msgspec.Struct item type with a nested Struct, else None. Built one time per type."""
    if not (isinstance(item_type, type) and issubclass(item_type, msgspec.Struct)):
        return None
    if getattr(item_type, "__is_bolt_serializer__", False):
        return None
    return compile_struct_relation_loader(item_type)


def paginate(pagination_class: type[PaginationBase] = PageNumberPagination):
    """
    Decorator to apply pagination to a route handler.

    The decorated handler should return a Django QuerySet. For PageNumberPagination
    and LimitOffsetPagination, it can also return a list. CursorPagination raises TypeError for a list.
    The decorator will automatically apply pagination and return a PaginatedResponse.

    The handler can be async or sync. Bolt calls a sync handler on the event loop.
    Thus a sync handler must return a lazy QuerySet or a list. If it runs a query,
    Django raises ``SynchronousOnlyOperation``. The pagination runs the queries of
    the QuerySet on the ORM executor.

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
        note_config_decorator(handler, "paginate")
        # The route decorator uses the same test for is_async.
        handler_is_async = inspect.iscoroutinefunction(handler)
        # Store original handler for introspection
        original_handler = handler
        signature, request_getter, added_request_index = _compile_request_getter(original_handler)
        cached_paginator: PaginationBase | None = None

        @wraps(handler)
        async def wrapper(*args, **kwargs):
            nonlocal cached_paginator

            if cached_paginator is None:
                cached_paginator = pagination_class()
                cached_paginator.serializer_class = getattr(wrapper, "__serializer_class__", None)

            request = request_getter(args, kwargs)
            if added_request_index is not None:
                # The handler has no request parameter. Remove the argument that the signature adds.
                args = args[:added_request_index]

            # PyRequest already supports .get() and __getitem__ — no per-request
            # isinstance/hasattr type sniffing (hot-path rule). Anything the
            # request_getter yields is used directly; a genuinely wrong object
            # fails loudly inside paginate_queryset.

            # Call original handler to get queryset
            # Pass all args along (including self for methods)
            queryset = handler(*args, **kwargs)
            if handler_is_async:
                queryset = await queryset

            # Apply pagination using dict version
            paginated = await cached_paginator.paginate_queryset(queryset, request)

            return paginated

        # The framework reads this signature. It has a request parameter, so the framework passes the request.
        wrapper.__signature__ = signature
        wrapper.__name__ = original_handler.__name__
        wrapper.__doc__ = original_handler.__doc__
        wrapper.__module__ = original_handler.__module__

        # Mark that this handler returns PaginatedResponse for serialization
        wrapper.__paginated__ = True
        wrapper.__pagination_class__ = pagination_class

        return wrapper

    return decorator


def _compile_request_getter(
    handler: Callable,
) -> tuple[inspect.Signature, Callable[[tuple[Any, ...], dict[str, Any]], Any], int | None]:
    """Find the request argument of a paginated handler one time, at decoration.

    The framework gives the request to a parameter that has a name in ``REQUEST_PARAM_NAMES``.
    It passes a positional parameter by position and each other parameter by keyword.
    For a handler with no such parameter, the returned signature adds one after the positional parameters.

    Returns:
        The signature for the wrapper, the function that gets the request from
        the arguments, and the index of the added parameter (None if the handler has its own).
    """
    sig = inspect.signature(handler)
    params = list(sig.parameters.values())
    positional_kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    request_param = next((param for param in params if param.name in REQUEST_PARAM_NAMES), None)
    added_index: int | None = None

    if request_param is None:
        added_index = sum(param.kind in positional_kinds for param in params)
        # The default lets the parameter follow one that has a default. The framework always passes the request.
        request_param = inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None)
        params.insert(added_index, request_param)
        sig = sig.replace(parameters=params)

    index = params.index(request_param) if request_param.kind in positional_kinds else None
    name = request_param.name

    def get_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return kwargs[name] if index is None else args[index]

    return sig, get_request, added_index
