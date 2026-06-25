import msgspec

from django_bolt import BoltAPI, action
from django_bolt.exceptions import NotFound
from django_bolt.serializers.types import Email, HttpsURL, PositiveInt
from django_bolt.views import ViewSet

from .models import BenchItem

api = BoltAPI(prefix="/bench")


# ============================================================================
# Schemas
# ============================================================================


class BenchItemSchema(msgspec.Struct):
    id: int
    name: str
    value: int
    description: str
    is_active: bool


class BenchItemCreate(msgspec.Struct):
    name: str
    value: int = 0
    description: str = ""
    is_active: bool = True


class BenchItemUpdate(msgspec.Struct):
    name: str | None = None
    value: int | None = None
    description: str | None = None
    is_active: bool | None = None


class SchemaDemo(msgspec.Struct):
    """Demonstrates the OpenAPI schema-accuracy fixes from PR #243.

    Before the fix the codegen-facing schema collapsed these to useless types:
    - ``email``/``age``/``site`` are documented custom types
      (``Annotated[T, Meta(description=...)]``) that rendered as ``object``;
      they now render as ``string``/``integer`` carrying their constraints.
    - ``scores``/``labels`` are typed maps that rendered as
      ``additionalProperties: true`` (TS ``Record<string, unknown>``); they
      now carry the value schema (``additionalProperties: {type: ...}``).
    """

    email: Email  # -> string (maxLength 254, pattern)
    age: PositiveInt  # -> integer (exclusiveMinimum 0)
    site: HttpsURL | None  # -> anyOf [string, null]
    scores: dict[str, int]  # -> additionalProperties: {type: integer}
    labels: dict[str, str]  # -> additionalProperties: {type: string}


# ============================================================================
# Unified ViewSet for Benchmark Items
# ============================================================================


@api.viewset("/items")
class BenchItemViewSet(ViewSet):
    """
    Complete CRUD ViewSet for benchmark items.

    Auto-generates routes:
    - GET    /bench/items              -> list()
    - POST   /bench/items              -> create()
    - GET    /bench/items/{id}         -> retrieve()
    - PUT    /bench/items/{id}         -> update()
    - PATCH  /bench/items/{id}         -> partial_update()
    - DELETE /bench/items/{id}         -> destroy()

    Custom actions:
    - POST   /bench/items/{id}/increment    -> increment()
    - POST   /bench/items/{id}/toggle       -> toggle()
    - GET    /bench/items/search            -> search()
    - GET    /bench/items/active            -> active()
    """

    queryset = BenchItem.objects.all()
    serializer_class = BenchItemSchema
    lookup_field = "id"

    async def list(self, request, active: bool | None = None, limit: int = 100):
        """GET /bench/items - List items with optional filtering."""
        queryset = BenchItem.objects.all()

        if active is not None:
            queryset = queryset.filter(is_active=active)

        queryset = queryset[:limit]

        items = []
        async for item in queryset:
            items.append(
                BenchItemSchema(
                    id=item.id, name=item.name, value=item.value, description=item.description, is_active=item.is_active
                )
            )

        return {"count": len(items), "items": items}

    async def retrieve(self, request, id: int) -> BenchItemSchema:
        """GET /bench/items/{id} - Retrieve a single item by ID."""
        try:
            item = await BenchItem.objects.aget(id=id)
            return BenchItemSchema(
                id=item.id, name=item.name, value=item.value, description=item.description, is_active=item.is_active
            )
        except BenchItem.DoesNotExist:
            raise NotFound(detail=f"BenchItem {id} not found") from None

    async def create(self, item: BenchItemCreate) -> BenchItemSchema:
        """POST /bench/items - Create a new item."""
        print("create", item)
        item = await BenchItem.objects.acreate(
            name=item.name, value=item.value, description=item.description, is_active=item.is_active
        )

        return BenchItemSchema(
            id=item.id, name=item.name, value=item.value, description=item.description, is_active=item.is_active
        )

    async def update(self, request, id: int, data: BenchItemUpdate) -> BenchItemSchema:
        """PUT /bench/items/{id} - Update an item (full update)."""
        try:
            item = await BenchItem.objects.aget(id=id)
        except BenchItem.DoesNotExist:
            raise NotFound(detail=f"BenchItem {id} not found") from None

        # Update all fields
        if data.name is not None:
            item.name = data.name
        if data.value is not None:
            item.value = data.value
        if data.description is not None:
            item.description = data.description
        if data.is_active is not None:
            item.is_active = data.is_active

        await item.asave()

        return BenchItemSchema(
            id=item.id, name=item.name, value=item.value, description=item.description, is_active=item.is_active
        )

    async def partial_update(self, request, id: int, data: BenchItemUpdate) -> BenchItemSchema:
        """PATCH /bench/items/{id} - Partially update an item."""
        try:
            item = await BenchItem.objects.aget(id=id)
        except BenchItem.DoesNotExist:
            raise NotFound(detail=f"BenchItem {id} not found") from None

        # Only update provided fields
        if data.name is not None:
            item.name = data.name
        if data.value is not None:
            item.value = data.value
        if data.description is not None:
            item.description = data.description
        if data.is_active is not None:
            item.is_active = data.is_active

        await item.asave()

        return BenchItemSchema(
            id=item.id, name=item.name, value=item.value, description=item.description, is_active=item.is_active
        )

    async def destroy(self, request, id: int):
        """DELETE /bench/items/{id} - Delete an item."""
        try:
            item = await BenchItem.objects.aget(id=id)
            await item.adelete()
            return {"deleted": True, "item_id": id}
        except BenchItem.DoesNotExist:
            raise NotFound(detail=f"BenchItem {id} not found") from None

    # ========================================================================
    # Custom Actions
    # ========================================================================

    @action(methods=["POST"], detail=True)
    async def increment(self, request, id: int):
        """POST /bench/items/{id}/increment - Increment item value."""
        try:
            item = await BenchItem.objects.aget(id=id)
            item.value += 1
            await item.asave()
            return {"item_id": id, "value": item.value, "incremented": True}
        except BenchItem.DoesNotExist:
            raise NotFound(detail=f"BenchItem {id} not found") from None

    @action(methods=["POST"], detail=True)
    async def toggle(self, request, id: int):
        """POST /bench/items/{id}/toggle - Toggle is_active status."""
        try:
            item = await BenchItem.objects.aget(id=id)
            item.is_active = not item.is_active
            await item.asave()
            return {"item_id": id, "is_active": item.is_active, "toggled": True}
        except BenchItem.DoesNotExist:
            raise NotFound(detail=f"BenchItem {id} not found") from None

    @action(methods=["GET"], detail=False)
    async def search(self, request, query: str):
        """GET /bench/items/search?query=xxx - Search items by name."""
        items = []
        async for item in BenchItem.objects.filter(name__icontains=query)[:10]:
            items.append(
                BenchItemSchema(
                    id=item.id, name=item.name, value=item.value, description=item.description, is_active=item.is_active
                )
            )
        return {"query": query, "count": len(items), "results": items}

    @action(methods=["GET"], detail=False)
    async def active(self, request):
        """GET /bench/items/active - Get all active items."""
        items = []
        async for item in BenchItem.objects.filter(is_active=True)[:100]:
            items.append(
                BenchItemSchema(
                    id=item.id, name=item.name, value=item.value, description=item.description, is_active=item.is_active
                )
            )
        return {"count": len(items), "items": items}


# ============================================================================
# OpenAPI schema-accuracy demo (PR #243)
# ============================================================================


@api.get("/schema-demo")
async def schema_demo() -> SchemaDemo:
    """GET /bench/schema-demo - Documented custom types + typed dicts in OpenAPI.

    Open the Swagger "Schema" tab for the 200 response and confirm:
    - ``email``/``age``/``site`` are ``string``/``integer`` (not ``object``)
    - ``scores``/``labels`` show ``additionalProperties`` typed as integer/string
    """
    return SchemaDemo(
        email="alice@example.com",
        age=30,
        site="https://example.com",
        scores={"cpu": 80, "mem": 60},
        labels={"env": "prod", "region": "us-east"},
    )
