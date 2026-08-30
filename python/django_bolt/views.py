"""
Class-based views for Django-Bolt.

Provides Django-style class-based views that integrate seamlessly with
Bolt's routing, dependency injection, guards, and authentication.

Example:
    api = BoltAPI()

    @api.view("/hello")
    class HelloView(APIView):
        guards = [IsAuthenticated()]

        async def get(self, request, current_user=Depends(get_current_user)) -> dict:
            return {"user": current_user.id}
"""

import inspect
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from django.db.models import Model, ObjectDoesNotExist, QuerySet

from ._json import decode
from ._view_context import _current_action, _current_request
from .exceptions import HTTPException
from .pagination import PaginationBase
from .request import Request
from .serializers.base import Serializer


class APIView:
    """
    Base class for class-based views in Django-Bolt.

    Attributes:
        http_method_names: Tuple of supported HTTP methods (lowercase)
        guards: List of guard/permission classes to apply to all methods
        auth: List of authentication backends to apply to all methods
        status_code: Default status code for responses (can be overridden per-method)
    """

    http_method_names = ("get", "post", "put", "patch", "delete", "head", "options", "query")

    # Class-level defaults (can be overridden by subclasses)
    guards: list[Any] | None = None
    auth: list[Any] | None = None
    status_code: int | None = None
    validate_response: bool | None = None
    include_in_schema: bool | None = None

    def __init__(self, **kwargs):
        """
        Initialize the view instance.

        Args:
            **kwargs: Additional instance attributes
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def request(self) -> Request:
        return _current_request.get()

    @classmethod
    def as_view(cls, method: str, action: str | None = None) -> Callable:
        """
        Create a handler callable for a specific HTTP method.

        This method:
        1. Validates that the HTTP method is supported
        2. Creates a wrapper that instantiates the view and calls the method handler
        3. Preserves the method signature for parameter extraction and dependency injection
        4. Attaches class-level metadata (guards, auth) for middleware compilation
        5. Maps DRF-style action names (list, retrieve, etc.) to HTTP methods
        6. Supports both sync and async handler methods

        Args:
            method: HTTP method name (lowercase, e.g., "get", "post")
            action: Optional action name for DRF-style methods (e.g., "list", "retrieve")

        Returns:
            Handler function compatible with BoltAPI routing (sync or async)

        Raises:
            ValueError: If method is not supported by this view
        """
        method_lower = method.lower()

        if method_lower not in cls.http_method_names:
            raise ValueError(f"Method '{method}' not allowed. Allowed methods: {cls.http_method_names}")

        # DRF-style action mapping: try action name first, then HTTP method
        # Actions: list, retrieve, create, update, partial_update, destroy
        method_handler = None
        action_name = method_lower

        if action:
            # Try the action name first (e.g., "list", "retrieve")
            method_handler = getattr(cls, action, None)
            action_name = action

        # Fall back to HTTP method name
        if method_handler is None:
            method_handler = getattr(cls, method_lower, None)
            action_name = method_lower

        if method_handler is None:
            raise ValueError(f"View class {cls.__name__} does not implement method '{action or method_lower}'")

        # Handlers can be sync or async
        # Sync handlers will be executed via spawn_blocking or inline mode
        # This is determined at registration time based on the 'inline' parameter

        # Create wrapper that preserves signature for parameter extraction
        # The wrapper's signature matches the method handler (excluding 'self')
        sig = inspect.signature(method_handler)
        params = list(sig.parameters.values())[1:]  # Skip 'self' parameter

        # Build new signature without 'self'
        new_sig = sig.replace(parameters=params)

        # Create single view instance at registration time (not per-request)
        # This eliminates the per-request instantiation overhead (~40% faster)
        view_instance = cls()

        # Bind the method once to eliminate lookup overhead
        bound_method = method_handler.__get__(view_instance, cls)

        # Create handler wrapper based on whether method is async or sync
        is_async_method = inspect.iscoroutinefunction(method_handler)

        if is_async_method:
            # Create async wrapper for async methods
            async def view_handler(*args, _action=action_name, **kwargs):
                """Auto-generated async view handler that calls bound method directly."""
                _current_action.set(_action)
                _current_request.set(args[0])
                return await bound_method(*args, **kwargs)

        else:
            # Create sync wrapper for sync methods
            def view_handler(*args, _action=action_name, **kwargs):
                """Auto-generated sync view handler that calls bound method directly."""
                _current_action.set(_action)
                _current_request.set(args[0])
                return bound_method(*args, **kwargs)

        view_handler.__wrapped__ = method_handler

        # Attach the signature (for parameter extraction)
        view_handler.__signature__ = new_sig
        view_handler.__annotations__ = {k: v for k, v in inspect.get_annotations(method_handler).items() if k != "self"}

        # Resolve the request body type for write actions only when the
        # ViewSet configures a serializer class. ViewSets that don't (because
        # each action declares its own typed body parameter, or it is a plain
        # ViewSet without a default serializer) are left alone — compile_binder
        # picks up the handler-declared body type via the function signature.
        if action_name in {"create", "update", "partial_update"} and any(
            getattr(cls, attr, None) is not None
            for attr in ("serializer_class", "create_serializer_class", "update_serializer_class")
        ):
            view_handler.__bolt_body_struct_type__ = cls._get_default_serializer_class(action_name)

        # Preserve docstring and name
        view_handler.__name__ = f"{cls.__name__}.{action_name}"
        view_handler.__doc__ = method_handler.__doc__
        view_handler.__module__ = cls.__module__

        # Attach class-level metadata for middleware compilation
        # These will be picked up by BoltAPI._route_decorator
        if cls.guards is not None:
            view_handler.__bolt_guards__ = cls.guards
        if cls.auth is not None:
            view_handler.__bolt_auth__ = cls.auth
        if cls.status_code is not None:
            view_handler.__bolt_status_code__ = cls.status_code
        if cls.validate_response is not None:
            view_handler.__bolt_validate_response__ = cls.validate_response

        if getattr(method_handler, "validate_response", None) is not None:
            view_handler.validate_response = method_handler.validate_response

        # Copy pagination attributes from method handler (for @paginate decorator)
        if hasattr(method_handler, "__paginated__"):
            view_handler.__paginated__ = method_handler.__paginated__
        if hasattr(method_handler, "__pagination_class__"):
            view_handler.__pagination_class__ = method_handler.__pagination_class__
        # Store reference to the paginate wrapper so serializer_class can be propagated
        view_handler.__original_handler__ = method_handler

        return view_handler

    def initialize(self, request: dict[str, Any]) -> None:
        """
        Hook called before the method handler is invoked.

        Override this to perform per-request initialization.

        Args:
            request: The request dictionary
        """
        pass

    @classmethod
    def get_allowed_methods(cls) -> set[str]:
        """
        Get the set of HTTP methods that this view implements.

        Returns:
            Set of uppercase HTTP method names (e.g., {"GET", "POST"})
        """
        allowed = set()
        for method in cls.http_method_names:
            if hasattr(cls, method) and callable(getattr(cls, method)):
                allowed.add(method.upper())
        return allowed


class ViewSet[T: Model](APIView):
    """
    ViewSet for CRUD operations on resources.

    Provides a higher-level abstraction for common REST patterns.
    Subclasses can implement standard methods: list, retrieve, create, update, partial_update, destroy.

    Example:
        @api.viewset("/users")
        class UserViewSet(ViewSet):
            queryset = User.objects.all()
            serializer_class = UserSchema
            list_serializer_class = UserMiniSchema  # Optional: different serializer for lists
            pagination_class = PageNumberPagination  # Optional: enable pagination

            async def list(self, request):
                return await self.get_queryset()

            async def retrieve(self, request):
                return await self.get_object()
    """

    # ViewSet configuration
    queryset: QuerySet[T] = None
    serializer_class: type[Serializer] = None

    # Optional: separate serializer for list/create/update operations
    list_serializer_class: type[Serializer] | None = None
    create_serializer_class: type[Serializer] | None = None
    update_serializer_class: type[Serializer] | None = None

    lookup_field: str = "pk"  # Field to use for object lookup (default: 'pk')
    pagination_class: type[PaginationBase] | None = None  # Optional: pagination class to use

    @property
    def action(self) -> str:
        return _current_action.get()

    def __init_subclass__(cls, **kwargs):
        """
        Hook called when a subclass is created.

        Capture an explicitly declared class-level queryset once so runtime
        lookup can stay simple and avoid descriptor/MRO fallbacks.
        """
        super().__init_subclass__(**kwargs)

        declared_queryset = cls.__dict__.get("queryset")
        if declared_queryset is not None:
            cls._base_queryset = declared_queryset
            delattr(cls, "queryset")

    def _get_base_queryset(self) -> QuerySet[T]:
        """
        Get the base queryset defined on the class.

        Returns None if no queryset is defined.
        """
        # Check instance attribute first (for dynamic assignment)
        if hasattr(self, "_instance_queryset"):
            return self._instance_queryset

        return getattr(self.__class__, "_base_queryset", None)

    def _clone_queryset(self, queryset):
        """
        Clone a queryset to ensure isolation between requests.

        Args:
            queryset: The queryset to clone

        Returns:
            Fresh QuerySet clone
        """
        if queryset is None:
            return None

        # Django QuerySets are lazy, and .all() returns a fresh queryset.
        if hasattr(queryset, "all") and callable(queryset.all):
            return queryset.all()

        # Not a QuerySet, return as-is
        return queryset

    @property
    def queryset(self) -> QuerySet[T]:  # noqa: F811
        """
        Property that returns a fresh queryset clone on each access.

        This ensures queryset isolation between requests while maintaining
        single-instance performance (Litestar pattern).

        Returns:
            Fresh QuerySet clone or None if not set
        """
        base_qs = self._get_base_queryset()
        return self._clone_queryset(base_qs)

    @queryset.setter
    def queryset(self, value: QuerySet[T]):
        """
        Setter for queryset attribute.

        Stores the base queryset that will be cloned on each access.

        Args:
            value: Base queryset to store
        """
        self._instance_queryset = value

    queryset: QuerySet[T]  # make pyright happy when inheriting

    async def get_queryset(self) -> QuerySet[T]:
        """
        Get the queryset for this viewset.

        This method returns a fresh queryset clone on each call, ensuring
        no state leakage between requests (following Litestar's pattern).
        Override to customize queryset filtering.

        Returns:
            Django QuerySet
        """
        base_qs = self._get_base_queryset()

        if base_qs is None:
            raise ValueError(
                f"'{self.__class__.__name__}' should either include a `queryset` attribute, "
                f"or override the `get_queryset()` method."
            )

        # Return a fresh clone
        return self._clone_queryset(base_qs)

    async def _apply_filter_queryset(self, queryset: QuerySet[T]) -> QuerySet[T]:
        """
        Apply filter_queryset() only when a subclass actually overrides it.

        The default implementation is a no-op, so skipping the await saves a
        small but common hot-path call for unfiltered list/detail routes.
        """
        if type(self).filter_queryset is ViewSet.filter_queryset:
            return queryset
        return await self.filter_queryset(queryset)

    async def filter_queryset(self, queryset: QuerySet[T]) -> QuerySet[T]:
        """
        Given a queryset, filter it with whichever filter backends are enabled.

        This method provides a hook for filtering, searching, and ordering.
        Override this method to implement custom filtering logic.
        Access the current request via ``self.request``.

        Note: Pagination is handled separately via `pagination_class` or `@paginate(...)`.

        Example:
            async def filter_queryset(self, queryset):
                request = self.request
                # Apply filters from query params
                status = request.query.get('status')
                if status:
                    queryset = queryset.filter(status=status)

                # Apply ordering
                ordering = request.query.get('ordering')
                if ordering:
                    queryset = queryset.order_by(ordering)

                # Apply search
                search = request.query.get('search')
                if search:
                    queryset = queryset.filter(name__icontains=search)

                return queryset

        Args:
            queryset: The base queryset to filter

        Returns:
            Filtered queryset (still lazy, not evaluated)
        """
        # Default implementation: return queryset unchanged
        # Subclasses should override this method to add filtering logic
        return queryset

    async def get_object(self, *args: Any, **lookup_kwargs: Any) -> T:
        """
        Get a single object by lookup field.

        When called without arguments, reads the lookup value from
        ``self.request.params``.  Explicit forms such as ``get_object(pk)``
        or ``get_object(id=id)`` are also supported.

        Returns:
            Model instance

        Raises:
            HTTPException: If object not found (404)
        """
        queryset = await self.get_queryset()
        queryset = await self._apply_filter_queryset(queryset)

        if args:
            if len(args) != 1 or lookup_kwargs:
                raise TypeError("get_object() accepts either one positional lookup value or keyword lookups")
            lookup_kwargs = {self.lookup_field: args[0]}
        elif not lookup_kwargs:
            params = self.request.params
            if self.lookup_field not in params:
                raise HTTPException(status_code=400, detail=f"Missing lookup field: {self.lookup_field}")
            lookup_kwargs = {self.lookup_field: params[self.lookup_field]}
        try:
            # Use aget for async retrieval
            obj = await queryset.aget(**lookup_kwargs)
            return obj
        except ObjectDoesNotExist as e:
            # Django raises DoesNotExist, but we convert to HTTPException
            raise HTTPException(status_code=404, detail="Not found") from e

    @classmethod
    def _get_default_serializer_class(cls, action: str | None = None) -> type[Serializer]:
        """
        Resolve the default serializer class for this viewset.

        Supports action-specific serializer classes:
        - list: list_serializer_class or serializer_class
        - create
            - validate: create_serializer_class or serializer_class
            - return: serializer_class
        - update/partial_update
            - validate: update_serializer_class or create_serializer_class or serializer_class
            - return: serializer_class
        - retrieve/destroy: serializer_class

        Override to customize serializer selection based on action.

        Args:
            action: The action being performed ('list', 'retrieve', 'create', etc.)

        Returns:
            Serializer class
        """

        # Action-specific serializer classes
        if action == "list" and cls.list_serializer_class is not None:
            return cls.list_serializer_class
        elif action == "create" and cls.create_serializer_class is not None:
            return cls.create_serializer_class
        elif action in ("update", "partial_update"):
            if cls.update_serializer_class is not None:
                return cls.update_serializer_class
            elif cls.create_serializer_class is not None:
                return cls.create_serializer_class

        if cls.serializer_class is None:
            raise ValueError(
                f"'{cls.__name__}' should either include a `serializer_class` attribute, "
                f"or override the `get_serializer_class()` method."
            )

        return cls.serializer_class

    def get_serializer_class(self, action: str | None = None) -> type[Serializer]:
        """
        Get the serializer class for this viewset.

        Subclasses can override this and use ``self.action`` / ``self.request``
        for runtime decisions.  When not overridden, class-level action-specific
        serializer attributes provide the default behavior.
        """
        if action is None:
            with suppress(LookupError):
                action = _current_action.get()
        return self.__class__._get_default_serializer_class(action)

    async def perform_create(self, obj: T):
        await obj.asave()

    async def perform_update(self, obj: T):
        await obj.asave()

    async def perform_destroy(self, obj: T):
        await obj.adelete()

    async def perform_destory(self, obj: T):
        # Backward-compatible alias for the misspelled hook introduced on this branch.
        await self.perform_destroy(obj)

    async def _perform_destroy(self, obj: T):
        # If a subclass only overrides the misspelled hook, keep honoring it so
        # existing branch consumers don't silently lose their custom behavior.
        if (
            type(self).perform_destory is not ViewSet.perform_destory
            and type(self).perform_destroy is ViewSet.perform_destroy
        ):
            await self.perform_destory(obj)
            return

        await self.perform_destroy(obj)


# Mixins for common CRUD operations
# Runtime mixins inherit from `object` to avoid generic MRO issues, while
# type checkers still see the full ViewSet API during development.
if TYPE_CHECKING:
    _ViewSet = ViewSet
else:
    _ViewSet = object


class ListMixin(_ViewSet):
    """
    Mixin that provides a list() method for GET requests on collections.

    Automatically implements:
        async def list(self, request: Request) -> Queryset
    """

    async def list(self, request: Request):
        """
        List objects in the queryset.

        Note: This evaluates the entire queryset. For large datasets,
        consider setting pagination_class or filtering via filter_queryset().
        """
        queryset = await self.get_queryset()
        queryset = await self._apply_filter_queryset(queryset)
        return queryset


class RetrieveMixin(_ViewSet):
    """
    Mixin that provides a retrieve() method for GET requests on single objects.

    Automatically implements:
        async def retrieve(self, request: Request) -> object
    """

    async def retrieve(self, request: Request):
        """Retrieve a single object by primary key."""
        return await self.get_object()


class CreateMixin(_ViewSet):
    """
    Mixin that provides a create() method for POST requests.

    Automatically implements:
        async def post(self, request, data: SerializerClass) -> object
    """

    async def create(self, request: Request):
        """
        Create a new object.

        The `data` parameter should be a msgspec.Struct with the fields to create.
        Uses create_serializer_class if defined, otherwise serializer_class.
        """
        serializer_class = self.get_serializer_class("create")
        validated_obj = serializer_class.model_validate_json(request.body)

        # Get the model class
        base_queryset = self._get_base_queryset()
        model = base_queryset.model if base_queryset is not None else (await self.get_queryset()).model

        # Create object
        obj = validated_obj.to_model(model)
        await self.perform_create(obj)

        return obj


class UpdateMixin(_ViewSet):
    """
    Mixin that provides an update() method for PUT requests.

    Automatically implements:
        async def update(self, request, data: SerializerClass) -> object
    """

    async def update(self, request: Request):
        """Update an object (full update)."""
        obj = await self.get_object()

        serializer_class = self.get_serializer_class("update")
        validated_obj = serializer_class.model_validate_json(request.body)

        # Update object fields
        if validated_obj.__struct_config__.omit_defaults:
            for key, value in validated_obj.to_dict().items():
                setattr(obj, key, value)
        else:
            validated_obj.update_instance(obj)

        # Save object
        await self.perform_update(obj)

        return obj


class PartialUpdateMixin(_ViewSet):
    """
    Mixin that provides a partial_update() method for PATCH requests.

    Automatically implements:
        async def partial_update(self, request: Request, data: SerializerClass) -> object
    """

    async def partial_update(self, request: Request):
        """Update an object (partial update)."""
        data = decode(request.body)
        obj = await self.get_object()

        serializer_class = self.get_serializer_class("update")
        rename_map = getattr(serializer_class, "__rename_map__", {})
        input_key_to_field = {field_name: field_name for field_name in serializer_class.__struct_fields__}
        input_key_to_field.update({alias: field_name for field_name, alias in rename_map.items()})

        patch_data = {}
        for key, value in data.items():
            field_name = input_key_to_field.get(key)
            if field_name is not None:
                patch_data[field_name] = value

        if not patch_data:
            return obj

        current_state = await serializer_class.afrom_model(obj)
        merged_data = current_state.to_dict()
        merged_data.update(patch_data)
        validated_obj = serializer_class.model_validate(merged_data)

        # Apply only the patched fields, but use the validated values.
        for field_name in patch_data:
            setattr(obj, field_name, getattr(validated_obj, field_name))

        # Save object
        await self.perform_update(obj)

        return obj


class DestroyMixin(_ViewSet):
    """
    Mixin that provides a destroy() method for DELETE requests.

    Automatically implements:
        async def delete(self, request: Request)
    """

    async def destroy(self, request: Request):
        """Delete an object."""
        obj = await self.get_object()

        # Delete object
        await self._perform_destroy(obj)

        # Return 204 response
        return None


# Convenience ViewSet classes (like Django REST Framework)


class ReadOnlyModelViewSet(
    ListMixin,
    RetrieveMixin,
    ViewSet,
):
    """
    A viewset base class for read-only operations.

    Provides `get_queryset()`, `get_object()`, and `get_serializer_class()` methods.
    You implement the HTTP method handlers with proper type annotations.

    Example:
        @api.viewset("/articles")
        class ArticleListViewSet(ReadOnlyModelViewSet):
            queryset = Article.objects.all()
            serializer_class = ArticleSchema
            pagination_class = PageNumberPagination
    """

    pass


class ModelViewSet(
    ListMixin,
    RetrieveMixin,
    CreateMixin,
    UpdateMixin,
    PartialUpdateMixin,
    DestroyMixin,
    ViewSet,
):
    """
    A viewset base class that provides helpers for full CRUD operations.

    Similar to Django REST Framework's ModelViewSet, but adapted for Django-Bolt's
    type-based parameter binding. You set `queryset` and `serializer_class`, then
    implement DRF-style action methods (list, retrieve, create, update, etc.).

    Example:
        from django_bolt import BoltAPI, ModelViewSet, Serializer
        from django_bolt.serializers import Serializer
        from myapp.models import Article

        api = BoltAPI()

        class ArticleSchema(Serializer):
            id: int
            title: str
            content: str

        class ArticleCreateSchema(Serializer):
            title: str
            content: str

        @api.viewset("/articles")
        class ArticleViewSet(ModelViewSet):
            queryset = Article.objects.all()
            serializer_class = ArticleSchema
            create_serializer_class = ArticleCreateSchema
            pagination_class = PageNumberPagination

    This provides full CRUD operations with Django ORM integration, just like DRF.
    The difference is that Django-Bolt requires explicit type annotations for
    parameter binding and validation. Routes are automatically generated based on
    implemented action methods.
    """

    pass
