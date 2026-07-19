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
from typing import Any, TypeVar, get_args, get_origin

import msgspec

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


class PaginatedResponse[T](msgspec.Struct, omit_defaults=True):
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
    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> PaginatedResponse:
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
            can_use_sync_from_model = not any(
                spec.nested is not None or (spec.source is not None and "." in spec.source)
                for spec in serializer_class.__model_field_specs__
            )

            if can_use_sync_from_model:
                try:
                    serializer_instances = [serializer_class.from_model(item) for item in items]
                except Exception:
                    serializer_instances = None
                else:
                    return serializer_class.dump_many(serializer_instances)

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

    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> PaginatedResponse:
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
        return PaginatedResponse(
            items=items,
            total=total,
            page=page_number,
            page_size=page_size,
            total_pages=total_pages,
            has_next=page_number < total_pages,
            has_previous=page_number > 1,
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

    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> PaginatedResponse:
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

        return PaginatedResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_next=has_next,
            has_previous=has_previous,
        )


class CursorPagination(PaginationBase):
    """
    Cursor-based pagination for large datasets.

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

    def _encode_cursor(self, value: Any) -> str:
        """
        Encode cursor value to base64 string.

        Args:
            value: Cursor value (typically an ID)

        Returns:
            Base64-encoded cursor string
        """
        cursor_data = _json.encode({"v": value})
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
            return data.get("v")
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

    async def paginate_queryset(self, queryset: Any, request: dict[str, Any], **params: Any) -> PaginatedResponse:
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

        # Apply ordering to queryset
        ordered_qs = queryset.order_by(ordering)

        # Apply cursor filter if present
        if cursor_value is not None:
            if is_descending:
                # For descending order, we want items less than cursor
                filter_kwargs = {f"{ordering_field}__lt": cursor_value}
            else:
                # For ascending order, we want items greater than cursor
                filter_kwargs = {f"{ordering_field}__gt": cursor_value}

            ordered_qs = ordered_qs.filter(**filter_kwargs)

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
                last_value = last_item.get(ordering_field)
            else:
                last_value = getattr(last_item, ordering_field, None)

            if last_value is not None:
                next_cursor = self._encode_cursor(last_value)

        # Serialize items using efficient batch serialization
        items = await self._serialize_items(raw_items)

        return PaginatedResponse(
            items=items,
            total=0,  # Cursor pagination doesn't provide total count for efficiency
            page_size=page_size,
            has_next=has_next,
            has_previous=cursor_value is not None,
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
