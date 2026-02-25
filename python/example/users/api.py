from __future__ import annotations

from typing import Annotated

import msgspec
from msgspec import Meta

from django_bolt import BoltAPI
from django_bolt.pagination import (
    CursorPagination,
    LimitOffsetPagination,
    PageNumberPagination,
    paginate,
)
from django_bolt.serializers import Serializer
from django_bolt.views import APIView, ModelViewSet, ViewSet

from .models import User

api = BoltAPI(prefix="/users")


# ============================================================================
# Schemas (msgspec.Struct - for backward compatibility)
# ============================================================================


class UserFull(msgspec.Struct):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool


class UserMini(msgspec.Struct):
    id: int
    username: str


# ============================================================================
# Serializers (Bolt Serializer - recommended for pagination)
# ============================================================================


class UserListSerializer(msgspec.Struct):
    """
    Serializer for user list view - only includes fields needed for listing.

    Using Bolt Serializer with @paginate enables efficient batch serialization
    via dump_many(), which is faster than converting each item individually.
    """

    id: int
    username: str
    email: str


class UserDetailSerializer(Serializer):
    """Serializer for user detail view - includes all fields."""

    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool


class UserCreate(msgspec.Struct):
    username: str
    email: str
    first_name: str = ""
    last_name: str = ""


class UserUpdate(msgspec.Struct):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    is_active: bool | None = None


# ============================================================================
# Function-Based Views (Original, for benchmarking)
# ============================================================================


@api.get("/")
async def users_root():
    return {"ok": True}


@api.get("/full10")
async def list_full_10() -> list[UserFull]:
    # Optimized: only fetch needed fields instead of all()
    return User.objects.only("id", "username", "email", "first_name", "last_name", "is_active")[:10]


@api.get("/sync-full10")
def list_full_10_sync() -> list[UserFull]:
    # Optimized: only fetch needed fields instead of all()
    return User.objects.only("id", "username", "email", "first_name", "last_name", "is_active")[:10]


@api.get("/sync-mini10")
def list_mini_10_sync() -> list[UserMini]:
    # Already optimized: only() fetches just id and username
    users = User.objects.only("id", "username")[:10]
    # evaludate query inside of sync context
    users = list(users)
    return users


@api.get("/mini10")
async def list_mini_10() -> list[UserMini]:
    # Already optimized: only() fetches just id and username
    return User.objects.only("id", "username")[:10]


@api.get("/seed")
async def seed_users(count: int = 1000) -> dict:
    """Create test users for benchmarking."""
    # Delete existing users first
    await User.objects.all().adelete()

    # Create users in bulk for performance
    users_to_create = [
        User(
            username=f"user{i}",
            email=f"user{i}@example.com",
            first_name=f"First{i}",
            last_name=f"Last{i}",
            is_active=True,
        )
        for i in range(count)
    ]

    created_users = await User.objects.abulk_create(users_to_create)
    return {"created": len(created_users), "count": count}


@api.post("/delete")
async def delete_all_users() -> dict:
    """Delete all users (for cleanup after benchmarking)."""
    count, _ = await User.objects.all().adelete()
    return {"deleted": count}


# ============================================================================
# Serializer Benchmark Endpoints - Raw msgspec
# ============================================================================


class BenchUser(msgspec.Struct):
    """Benchmark user with msgspec only (no custom validators)."""

    id: int
    username: Annotated[str, Meta(min_length=2, max_length=150)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+\.[^@]+$")]
    bio: str = ""


@api.post("/bench/msgspec")
async def bench_msgspec_serializer(user: BenchUser) -> BenchUser:
    """
    Benchmark endpoint using raw msgspec Struct.
    Tests deserialization (JSON -> Object) and serialization (Object -> JSON).
    """
    return user


# ============================================================================
# Unified ViewSet (DRF-style with api.viewset())
# ============================================================================


@api.view("/cbv-mini10")
class UserBenchViewSet(APIView):
    """Benchmarking endpoints using class-based views."""

    async def get(self, request):
        """List first 10 users (CBV version for benchmarking)."""
        users = []
        async for user in User.objects.only("id", "username")[:10]:
            users.append(UserMini(id=user.id, username=user.username))
        return users


@api.view("/cbv-full10")
class UserFull10ViewSet(APIView):
    """List first 10 users (CBV version for benchmarking)."""

    async def get(self, request):
        """List first 10 users (CBV version for benchmarking)."""
        users = []
        print("get", dir(request), request)
        async for user in User.objects.only("id", "username", "email", "first_name", "last_name", "is_active")[:10]:
            users.append(
                UserFull(
                    id=user.id,
                    username=user.username,
                    email=user.email,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    is_active=user.is_active,
                )
            )
        return users


# ============================================================================
# Pagination Examples
# ============================================================================


# Custom pagination class for examples
class SmallPagePagination(PageNumberPagination):
    """Custom pagination with smaller page size."""

    page_size = 10
    max_page_size = 50
    page_size_query_param = "page_size"  # Allow client to customize page size


# ----------------------------------------------------------------------------
# 1. Pagination with Bolt Serializer (RECOMMENDED)
#    - Uses response_model to specify serializer
#    - Automatically uses dump_many() for efficient batch serialization
#    - Only declared fields are included in response
# ----------------------------------------------------------------------------


@api.get(
    "/paginated",
)
@paginate(SmallPagePagination)
async def list_users_paginated(request):
    """
    List users with page number pagination and Bolt Serializer.

    The serializer automatically filters fields - only id, username, email
    are included in the response, even though the queryset fetches all fields.

    Query params:
        - page: Page number (default: 1)
        - page_size: Items per page (default: 10, max: 50)

    Example: GET /users/paginated?page=2&page_size=20

    Response:
        {
            "items": [{"id": 1, "username": "john", "email": "john@example.com"}, ...],
            "total": 100,
            "page": 2,
            "page_size": 20,
            "total_pages": 5,
            "has_next": true,
            "has_previous": true,
            "next_page": 3,
            "previous_page": 1
        }
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 2. Pagination with return type annotation (alternative syntax)
# ----------------------------------------------------------------------------


@api.get("/paginated-typed")
@paginate(SmallPagePagination)
async def list_users_typed(request) -> list[UserListSerializer]:
    """
    Same as above but using return type annotation instead of response_model.

    Both approaches work identically - choose based on preference.
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 3. LimitOffset Pagination with Serializer
# ----------------------------------------------------------------------------


@api.get("/paginated-offset", response_model=list[UserListSerializer])
@paginate(LimitOffsetPagination)
async def list_users_offset(request):
    """
    List users with limit-offset pagination.

    Query params:
        - limit: Number of items (default: 100, max: 1000)
        - offset: Starting position (default: 0)

    Example: GET /users/paginated-offset?limit=20&offset=40

    Response:
        {
            "items": [...],
            "total": 100,
            "limit": 20,
            "offset": 40,
            "has_next": true,
            "has_previous": true
        }
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 4. Cursor Pagination with Serializer
# ----------------------------------------------------------------------------


class UserCursorPagination(CursorPagination):
    """Cursor pagination ordered by creation date."""

    page_size = 20
    ordering = "-id"  # Most recent first


@api.get("/paginated-cursor", response_model=list[UserListSerializer])
@paginate(UserCursorPagination)
async def list_users_cursor(request):
    """
    List users with cursor-based pagination.

    Cursor pagination is ideal for infinite scroll or real-time feeds
    because it's stable even when new items are added.

    Query params:
        - cursor: Opaque cursor string (optional, omit for first page)
        - page_size: Items per page (default: 20)

    Example: GET /users/paginated-cursor?cursor=eyJ2IjoxMDB9

    Response:
        {
            "items": [...],
            "page_size": 20,
            "has_next": true,
            "has_previous": true,
            "next_cursor": "eyJ2Ijo4MH0=",
            "previous_cursor": null
        }
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 5. Pagination with msgspec.Struct (backward compatible)
#    - Works with plain msgspec.Struct (not just Bolt Serializer)
#    - Fields are extracted based on struct field declarations
# ----------------------------------------------------------------------------


@api.get("/paginated-msgspec", response_model=list[UserMini])
@paginate(SmallPagePagination)
async def list_users_msgspec(request):
    """
    Pagination also works with plain msgspec.Struct.

    Only id and username are included (as declared in UserMini).
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 6. Pagination without serializer (backward compatible fallback)
#    - When no response_model is specified, all model fields are dumped
#    - Not recommended for production (may expose sensitive fields)
# ----------------------------------------------------------------------------


@api.get("/paginated-legacy")
@paginate(SmallPagePagination)
async def list_users_legacy(request):
    """
    Pagination without serializer - all model fields are included.

    WARNING: This may expose sensitive fields. Always use a serializer
    in production to control which fields are returned.
    """
    return User.objects.only("id", "username")


# ----------------------------------------------------------------------------
# 7. Class-Based View (ViewSet) with Pagination
# ----------------------------------------------------------------------------


@api.viewset("/api-paginated")
class UserPaginatedViewSet(ViewSet):
    """
    ViewSet with pagination using @paginate decorator on list method.

    Routes:
        - GET /users/api-paginated -> list (paginated)
        - GET /users/api-paginated/{id} -> retrieve (not paginated)
    """

    queryset = User.objects.all()

    @paginate(SmallPagePagination)
    async def list(self, request) -> list[UserListSerializer]:
        """
        List all users with pagination.

        Using @paginate decorator with return type annotation
        automatically handles serialization.
        """
        return await self.get_queryset()

    async def retrieve(self, request, id: int) -> UserDetailSerializer:
        """Retrieve a single user by ID (not paginated)."""
        user = await self.get_object(id=id)
        return UserDetailSerializer.from_model(user)


# ----------------------------------------------------------------------------
# 8. ModelViewSet with Pagination
# ----------------------------------------------------------------------------


@api.viewset("/model-paginated")
class UserModelPaginatedViewSet(ModelViewSet):
    """
    Full CRUD ModelViewSet with pagination on list action.

    Routes:
        - GET /users/model-paginated -> list (paginated)
        - POST /users/model-paginated -> create
        - GET /users/model-paginated/{id} -> retrieve
        - PUT /users/model-paginated/{id} -> update
        - PATCH /users/model-paginated/{id} -> partial_update
        - DELETE /users/model-paginated/{id} -> destroy

    Query params for list:
        - page: Page number (default: 1)
        - page_size: Items per page (default: 10)
    """

    queryset = User.objects.all()
    serializer_class = UserFull  # For detail/create/update views
    list_serializer_class = UserMini  # For list view
    pagination_class = SmallPagePagination

    # list(), retrieve(), create(), update(), partial_update(), destroy()
    # are all automatically implemented by ModelViewSet
    # Pagination is automatically applied to list() action
