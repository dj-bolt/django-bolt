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
from typing import Any

import msgspec

from .exceptions import HTTPException


class APIView:
    """
    Base class for class-based views in Django-Bolt.

    Attributes:
        http_method_names: List of supported HTTP methods (lowercase)
        guards: List of guard/permission classes to apply to all methods
        auth: List of authentication backends to apply to all methods
        status_code: Default status code for responses (can be overridden per-method)
    """

    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    # Class-level defaults (can be overridden by subclasses)
    guards: list[Any] | None = None
    auth: list[Any] | None = None
    status_code: int | None = None
    validate_response: bool | None = None

    def __init__(self, **kwargs):
        """
        Initialize the view instance.

        Args:
            **kwargs: Additional instance attributes
        """
        for key, value in kwargs.items():
            setattr(self, key, value)

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
        action_name = None

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

        # Set action name once at registration time
        if hasattr(view_instance, "action"):
            view_instance.action = action_name

        # Bind the method once to eliminate lookup overhead
        bound_method = method_handler.__get__(view_instance, cls)

        # Create handler wrapper based on whether method is async or sync
        is_async_method = inspect.iscoroutinefunction(method_handler)

        if is_async_method:
            # Create async wrapper for async methods
            async def view_handler(*args, **kwargs):
                """Auto-generated async view handler that calls bound method directly."""
                # Inject request object into view instance for pagination/filtering
                # Request is typically the first positional arg or named 'request'
                if args and isinstance(args[0], dict) and "method" in args[0]:
                    view_instance.request = args[0]
                elif "request" in kwargs:
                    view_instance.request = kwargs["request"]

                return await bound_method(*args, **kwargs)

        else:
            # Create sync wrapper for sync methods
            def view_handler(*args, **kwargs):
                """Auto-generated sync view handler that calls bound method directly."""
                # Inject request object into view instance for pagination/filtering
                # Request is typically the first positional arg or named 'request'
                if args and isinstance(args[0], dict) and "method" in args[0]:
                    view_instance.request = args[0]
                elif "request" in kwargs:
                    view_instance.request = kwargs["request"]

                return bound_method(*args, **kwargs)

        view_handler.__wrapped__ = method_handler

        # Attach the signature (for parameter extraction)
        view_handler.__signature__ = new_sig
        view_handler.__annotations__ = {k: v for k, v in inspect.get_annotations(method_handler).items() if k != "self"}

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


class ViewSet(APIView):
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
                users = await self.get_queryset()
                return [UserSchema.from_model(u) async for u in users]

            async def retrieve(self, request, pk: int):
                user = await self.get_object(pk)
                return UserSchema.from_model(user)
    """

    # ViewSet configuration
    queryset: Any | None = None
    serializer_class: type | None = None
    list_serializer_class: type | None = None  # Optional: override serializer for list operations
    lookup_field: str = "pk"  # Field to use for object lookup (default: 'pk')
    pagination_class: Any | None = None  # Optional: pagination class to use

    # Action name for current request (set automatically)
    action: str | None = None

    # Request object (set automatically during dispatch)
    request: dict[str, Any] | None = None

    def __init_subclass__(cls, **kwargs):
        """
        Hook called when a subclass is created.

        Converts class-level queryset to instance-level _base_queryset
        to enable proper cloning on each access (Litestar pattern).
        """
        super().__init_subclass__(**kwargs)

        # If subclass defines queryset as class attribute, store it separately
        if "queryset" in cls.__dict__ and cls.__dict__["queryset"] is not None:
            # Store the base queryset for cloning
            cls._base_queryset = cls.__dict__["queryset"]
            # Remove the class attribute so property works
            delattr(cls, "queryset")

    def _get_base_queryset(self):
        """
        Get the base queryset defined on the class.

        Returns None if no queryset is defined.
        """
        # Check instance attribute first (for dynamic assignment)
        if hasattr(self, "_instance_queryset"):
            return self._instance_queryset

        # Check class attribute (set via __init_subclass__)
        if hasattr(self.__class__, "_base_queryset"):
            return self.__class__._base_queryset

        # Check if there's a class attribute 'queryset' (shouldn't happen after __init_subclass__)
        return getattr(self.__class__, "queryset", None)

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

        # Always return a fresh clone to prevent state leakage
        # Django QuerySets are lazy, so .all() creates a new QuerySet instance
        if hasattr(queryset, "_clone"):
            # Use Django's internal _clone() for true deep copy
            return queryset._clone()
        elif hasattr(queryset, "all"):
            # Fallback to .all() which also creates a new QuerySet
            return queryset.all()

        # Not a QuerySet, return as-is
        return queryset

    @property
    def queryset(self):  # noqa: F811
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
    def queryset(self, value):
        """
        Setter for queryset attribute.

        Stores the base queryset that will be cloned on each access.

        Args:
            value: Base queryset to store
        """
        self._instance_queryset = value

    async def get_queryset(self):
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

    async def filter_queryset(self, queryset):
        """
        Given a queryset, filter it with whichever filter backends are enabled.

        This method provides a hook for filtering, searching, and ordering.
        Override this method to implement custom filtering logic.

        Note: Pagination is handled separately via paginate_queryset().

        Example:
            async def filter_queryset(self, queryset):
                # Apply filters from query params
                status = self.request.get('query', {}).get('status')
                if status:
                    queryset = queryset.filter(status=status)

                # Apply ordering
                ordering = self.request.get('query', {}).get('ordering')
                if ordering:
                    queryset = queryset.order_by(ordering)

                # Apply search
                search = self.request.get('query', {}).get('search')
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

    async def paginate_queryset(self, queryset):
        """
        Paginate a queryset if pagination is enabled.

        This method checks if self.pagination_class is set and applies
        pagination if available. If no pagination is configured, returns
        the queryset unchanged.

        Args:
            queryset: The queryset to paginate

        Returns:
            PaginatedResponse if pagination enabled, otherwise queryset
        """
        if self.pagination_class is None:
            return queryset

        if self.request is None:
            raise ValueError(
                f"Cannot paginate in {self.__class__.__name__}: request object not available. "
                f"Ensure request parameter is passed to the handler."
            )

        # Create paginator instance
        paginator = self.pagination_class()

        # Apply pagination
        return await paginator.paginate_queryset(queryset, self.request)

    def get_pagination_class(self):
        """
        Get the pagination class for this viewset.

        Override this method to dynamically select pagination class
        based on action or other criteria.

        Returns:
            Pagination class or None
        """
        return self.pagination_class

    async def get_object(self, pk: Any = None, **lookup_kwargs):
        """
        Get a single object by lookup field.

        Args:
            pk: Primary key value (if using default lookup_field)
            **lookup_kwargs: Additional lookup parameters

        Returns:
            Model instance

        Raises:
            HTTPException: If object not found (404)
        """
        queryset = await self.get_queryset()

        # Build lookup kwargs
        if pk is not None and not lookup_kwargs:
            lookup_kwargs = {self.lookup_field: pk}

        try:
            # Use aget for async retrieval
            obj = await queryset.aget(**lookup_kwargs)
            return obj
        except Exception as e:
            # Django raises DoesNotExist, but we convert to HTTPException
            raise HTTPException(status_code=404, detail="Not found") from e

    def get_serializer_class(self, action: str | None = None):
        """
        Get the serializer class for this viewset.

        Override to customize serializer selection based on action.

        Args:
            action: The action being performed ('list', 'retrieve', 'create', etc.)

        Returns:
            Serializer class
        """
        # Use instance action if not provided
        if action is None:
            action = self.action

        # Use list_serializer_class for list actions if defined
        if action == "list" and self.list_serializer_class is not None:
            return self.list_serializer_class

        if self.serializer_class is None:
            raise ValueError(
                f"'{self.__class__.__name__}' should either include a `serializer_class` attribute, "
                f"or override the `get_serializer_class()` method."
            )
        return self.serializer_class

    def get_serializer(self, instance=None, data=None, many=False):
        """
        Get a serializer instance.

        Args:
            instance: Model instance to serialize (for reading)
            data: Data to validate/deserialize (for writing)
            many: Whether serializing multiple instances

        Returns:
            Serializer instance or list of serialized data
        """
        serializer_class = self.get_serializer_class()

        # If it's a msgspec.Struct, handle conversion
        if hasattr(serializer_class, "__struct_fields__"):
            if instance is not None:
                if many:
                    # Serialize multiple instances
                    if hasattr(serializer_class, "from_model"):
                        return [serializer_class.from_model(obj) for obj in instance]
                    else:
                        # Manual mapping
                        fields = getattr(serializer_class, "__annotations__", {})
                        return [
                            msgspec.convert({name: getattr(obj, name, None) for name in fields}, serializer_class)
                            for obj in instance
                        ]
                else:
                    # Serialize single instance
                    if hasattr(serializer_class, "from_model"):
                        return serializer_class.from_model(instance)
                    else:
                        fields = getattr(serializer_class, "__annotations__", {})
                        mapped = {name: getattr(instance, name, None) for name in fields}
                        return msgspec.convert(mapped, serializer_class)
            elif data is not None:
                # Data is already validated by msgspec at parameter binding
                return data

        return serializer_class


# Mixins for common CRUD operations


class ListMixin:
    """
    Mixin that provides a list() method for GET requests on collections.

    Automatically implements:
        async def get(self, request) -> list

    Requires:
        - queryset attribute
        - serializer_class attribute (optional, returns raw queryset if not provided)
    """

    async def get(self, request):
        """
        List all objects in the queryset.

        Note: This evaluates the entire queryset. For large datasets,
        consider implementing pagination or filtering via filter_queryset().
        """
        queryset = await self.get_queryset()

        # Optional: Apply filtering if filter_queryset is available
        if hasattr(self, "filter_queryset"):
            queryset = await self.filter_queryset(queryset)

        # Convert queryset to list (evaluates database query here)
        results = []
        async for obj in queryset:
            # If serializer_class is defined, use it
            if hasattr(self, "serializer_class") and self.serializer_class:
                # Get serializer class (use method if available, otherwise direct attribute)
                if hasattr(self, "get_serializer_class"):
                    serializer_class = self.get_serializer_class()
                else:
                    serializer_class = self.serializer_class

                if hasattr(serializer_class, "from_model"):
                    results.append(serializer_class.from_model(obj))
                else:
                    # Assume it's a msgspec.Struct, use convert
                    fields = getattr(serializer_class, "__annotations__", {})
                    mapped = {name: getattr(obj, name, None) for name in fields}
                    results.append(msgspec.convert(mapped, serializer_class))
            else:
                results.append(obj)

        return results


class RetrieveMixin:
    """
    Mixin that provides a retrieve() method for GET requests on single objects.

    Automatically implements:
        async def get(self, request, pk: int) -> object

    Requires:
        - queryset attribute
        - get_object(pk) method
        - serializer_class attribute (optional, returns raw object if not provided)
    """

    async def get(self, request, pk: int):
        """Retrieve a single object by primary key."""
        obj = await self.get_object(pk)

        # If serializer_class is defined, use it
        if hasattr(self, "serializer_class") and self.serializer_class:
            # Get serializer class (use method if available, otherwise direct attribute)
            if hasattr(self, "get_serializer_class"):
                serializer_class = self.get_serializer_class()
            else:
                serializer_class = self.serializer_class

            if hasattr(serializer_class, "from_model"):
                return serializer_class.from_model(obj)
            else:
                # Assume it's a msgspec.Struct, use convert
                fields = getattr(serializer_class, "__annotations__", {})
                mapped = {name: getattr(obj, name, None) for name in fields}
                return msgspec.convert(mapped, serializer_class)

        return obj


class CreateMixin:
    """
    Mixin that provides a create() method for POST requests.

    Automatically implements:
        async def post(self, request, data: SerializerClass) -> object

    Requires:
        - queryset attribute (to determine model)
        - serializer_class attribute (for input validation)
    """

    async def post(self, request, data):
        """Create a new object."""
        # Get the model class without evaluating queryset
        base_qs = self._get_base_queryset()
        if base_qs is None:
            raise ValueError(f"'{self.__class__.__name__}' should include a `queryset` attribute.")
        model = base_qs.model

        # Extract data from msgspec.Struct to dict
        if hasattr(data, "__struct_fields__"):
            # It's a msgspec.Struct
            fields = data.__struct_fields__
            data_dict = {field: getattr(data, field) for field in fields}
        elif isinstance(data, dict):
            data_dict = data
        else:
            raise ValueError(f"Cannot extract data from {type(data)}")

        # Create object using async ORM
        obj = await model.objects.acreate(**data_dict)

        # Serialize response
        if hasattr(self, "serializer_class") and self.serializer_class:
            # Get serializer class (use method if available, otherwise direct attribute)
            if hasattr(self, "get_serializer_class"):
                serializer_class = self.get_serializer_class()
            else:
                serializer_class = self.serializer_class

            if hasattr(serializer_class, "from_model"):
                return serializer_class.from_model(obj)
            else:
                fields = getattr(serializer_class, "__annotations__", {})
                mapped = {name: getattr(obj, name, None) for name in fields}
                return msgspec.convert(mapped, serializer_class)

        return obj


class UpdateMixin:
    """
    Mixin that provides an update() method for PUT requests.

    Automatically implements:
        async def put(self, request, pk: int, data: SerializerClass) -> object

    Requires:
        - queryset attribute
        - get_object(pk) method
        - serializer_class attribute
    """

    async def put(self, request, pk: int, data):
        """Update an object (full update)."""
        obj = await self.get_object(pk)

        # Extract data from msgspec.Struct to dict
        if hasattr(data, "__struct_fields__"):
            fields = data.__struct_fields__
            data_dict = {field: getattr(data, field) for field in fields}
        elif isinstance(data, dict):
            data_dict = data
        else:
            raise ValueError(f"Cannot extract data from {type(data)}")

        # Update object fields
        for key, value in data_dict.items():
            setattr(obj, key, value)

        # Save using async ORM
        await obj.asave()

        # Serialize response
        if hasattr(self, "serializer_class") and self.serializer_class:
            # Get serializer class (use method if available, otherwise direct attribute)
            if hasattr(self, "get_serializer_class"):
                serializer_class = self.get_serializer_class()
            else:
                serializer_class = self.serializer_class

            if hasattr(serializer_class, "from_model"):
                return serializer_class.from_model(obj)
            else:
                fields = getattr(serializer_class, "__annotations__", {})
                mapped = {name: getattr(obj, name, None) for name in fields}
                return msgspec.convert(mapped, serializer_class)

        return obj


class PartialUpdateMixin:
    """
    Mixin that provides a partial_update() method for PATCH requests.

    Automatically implements:
        async def patch(self, request, pk: int, data: SerializerClass) -> object

    Requires:
        - queryset attribute
        - get_object(pk) method
        - serializer_class attribute
    """

    async def patch(self, request, pk: int, data):
        """Update an object (partial update)."""
        obj = await self.get_object(pk)

        # Extract data from msgspec.Struct to dict
        if hasattr(data, "__struct_fields__"):
            fields = data.__struct_fields__
            data_dict = {field: getattr(data, field) for field in fields}
        elif isinstance(data, dict):
            data_dict = data
        else:
            raise ValueError(f"Cannot extract data from {type(data)}")

        # Update only provided fields
        for key, value in data_dict.items():
            if value is not None:  # Skip None values in PATCH
                setattr(obj, key, value)

        # Save using async ORM
        await obj.asave()

        # Serialize response
        if hasattr(self, "serializer_class") and self.serializer_class:
            # Get serializer class (use method if available, otherwise direct attribute)
            if hasattr(self, "get_serializer_class"):
                serializer_class = self.get_serializer_class()
            else:
                serializer_class = self.serializer_class

            if hasattr(serializer_class, "from_model"):
                return serializer_class.from_model(obj)
            else:
                fields = getattr(serializer_class, "__annotations__", {})
                mapped = {name: getattr(obj, name, None) for name in fields}
                return msgspec.convert(mapped, serializer_class)

        return obj


class DestroyMixin:
    """
    Mixin that provides a destroy() method for DELETE requests.

    Automatically implements:
        async def delete(self, request, pk: int) -> dict

    Requires:
        - queryset attribute
        - get_object(pk) method
    """

    async def delete(self, request, pk: int):
        """Delete an object."""
        obj = await self.get_object(pk)

        # Delete using async ORM
        await obj.adelete()

        # Return success response
        return {"detail": "Object deleted successfully"}


# Convenience ViewSet classes (like Django REST Framework)


class ReadOnlyModelViewSet(ViewSet):
    """
    A viewset base class for read-only operations.

    Provides `get_queryset()`, `get_object()`, and `get_serializer_class()` methods.
    You implement the HTTP method handlers with proper type annotations.

    Example:
        @api.view("/articles")
        class ArticleListViewSet(ReadOnlyModelViewSet):
            queryset = Article.objects.all()
            serializer_class = ArticleSchema

            async def get(self, request):
                \"\"\"List all articles.\"\"\"
                articles = []
                async for article in await self.get_queryset():
                    articles.append(ArticleSchema.from_model(article))
                return articles

        @api.view("/articles/{pk}")
        class ArticleDetailViewSet(ReadOnlyModelViewSet):
            queryset = Article.objects.all()
            serializer_class = ArticleSchema

            async def get(self, request, pk: int):
                \"\"\"Retrieve a single article.\"\"\"
                article = await self.get_object(pk)
                return ArticleSchema.from_model(article)
    """

    pass


class ModelViewSet(ViewSet):
    """
    A viewset base class that provides helpers for full CRUD operations.

    Similar to Django REST Framework's ModelViewSet, but adapted for Django-Bolt's
    type-based parameter binding. You set `queryset` and `serializer_class`, then
    implement DRF-style action methods (list, retrieve, create, update, etc.).

    Example:
        from django_bolt import BoltAPI, ModelViewSet
        from myapp.models import Article
        import msgspec

        api = BoltAPI()

        class ArticleSchema(msgspec.Struct):
            id: int
            title: str
            content: str

            @classmethod
            def from_model(cls, obj):
                return cls(id=obj.id, title=obj.title, content=obj.content)

        class ArticleCreateSchema(msgspec.Struct):
            title: str
            content: str

        @api.viewset("/articles")
        class ArticleViewSet(ModelViewSet):
            queryset = Article.objects.all()
            serializer_class = ArticleSchema

            async def list(self, request):
                \"\"\"GET /articles - List all articles.\"\"\"
                articles = []
                async for article in await self.get_queryset():
                    articles.append(ArticleSchema.from_model(article))
                return articles

            async def retrieve(self, request, pk: int):
                \"\"\"GET /articles/{pk} - Retrieve a single article.\"\"\"
                article = await self.get_object(pk=pk)
                return ArticleSchema.from_model(article)

            async def create(self, request, data: ArticleCreateSchema):
                \"\"\"POST /articles - Create a new article.\"\"\"
                article = await Article.objects.acreate(
                    title=data.title,
                    content=data.content
                )
                return ArticleSchema.from_model(article)

            async def update(self, request, pk: int, data: ArticleCreateSchema):
                \"\"\"PUT /articles/{pk} - Update an article.\"\"\"
                article = await self.get_object(pk=pk)
                article.title = data.title
                article.content = data.content
                await article.asave()
                return ArticleSchema.from_model(article)

            async def partial_update(self, request, pk: int, data: ArticleCreateSchema):
                \"\"\"PATCH /articles/{pk} - Partially update an article.\"\"\"
                article = await self.get_object(pk=pk)
                if data.title:
                    article.title = data.title
                if data.content:
                    article.content = data.content
                await article.asave()
                return ArticleSchema.from_model(article)

            async def destroy(self, request, pk: int):
                \"\"\"DELETE /articles/{pk} - Delete an article.\"\"\"
                article = await self.get_object(pk=pk)
                await article.adelete()
                return {"deleted": True}

    This provides full CRUD operations with Django ORM integration, just like DRF.
    The difference is that Django-Bolt requires explicit type annotations for
    parameter binding and validation. Routes are automatically generated based on
    implemented action methods.
    """

    # Optional: separate serializer for create/update operations
    create_serializer_class: type | None = None
    update_serializer_class: type | None = None

    async def list(self, request):
        """
        List all objects in the queryset.

        Uses list_serializer_class if defined, otherwise serializer_class.
        Applies filter_queryset() for filtering, searching, ordering.
        """
        qs = await self.get_queryset()
        qs = await self.filter_queryset(qs)  # Apply filtering (still lazy)
        serializer_class = self.get_serializer_class(action="list")

        # Queryset is evaluated here during iteration
        results = []
        async for obj in qs:
            if hasattr(serializer_class, "from_model"):
                results.append(serializer_class.from_model(obj))
            else:
                # Fallback: manual conversion
                fields = getattr(serializer_class, "__annotations__", {})
                mapped = {name: getattr(obj, name, None) for name in fields}
                results.append(msgspec.convert(mapped, serializer_class))

        return results

    async def retrieve(self, request, **kwargs):
        """
        Retrieve a single object by lookup field.

        The lookup field value is passed as a keyword argument (e.g., pk=1, id=1).
        """
        # Extract lookup value from kwargs
        lookup_value = kwargs.get(self.lookup_field)
        if lookup_value is None:
            raise HTTPException(status_code=400, detail=f"Missing lookup field: {self.lookup_field}")

        obj = await self.get_object(**{self.lookup_field: lookup_value})
        serializer_class = self.get_serializer_class(action="retrieve")

        if hasattr(serializer_class, "from_model"):
            return serializer_class.from_model(obj)
        else:
            fields = getattr(serializer_class, "__annotations__", {})
            mapped = {name: getattr(obj, name, None) for name in fields}
            return msgspec.convert(mapped, serializer_class)

    async def create(self, request, data):
        """
        Create a new object.

        The `data` parameter should be a msgspec.Struct with the fields to create.
        Uses create_serializer_class if defined, otherwise serializer_class.
        """
        # Get the model class without evaluating queryset
        base_qs = self._get_base_queryset()
        if base_qs is None:
            raise ValueError(f"'{self.__class__.__name__}' should include a `queryset` attribute.")
        model = base_qs.model

        # Extract data from msgspec.Struct
        if hasattr(data, "__struct_fields__"):
            fields = data.__struct_fields__
            data_dict = {field: getattr(data, field) for field in fields}
        elif isinstance(data, dict):
            data_dict = data
        else:
            raise ValueError(f"Cannot extract data from {type(data)}")

        # Create object
        obj = await model.objects.acreate(**data_dict)

        # Serialize response
        serializer_class = self.get_serializer_class(action="create")
        if hasattr(serializer_class, "from_model"):
            return serializer_class.from_model(obj)
        else:
            fields = getattr(serializer_class, "__annotations__", {})
            mapped = {name: getattr(obj, name, None) for name in fields}
            return msgspec.convert(mapped, serializer_class)

    async def update(self, request, data, **kwargs):
        """
        Update an object (full update).

        The lookup field value is passed as a keyword argument.
        Uses update_serializer_class if defined, otherwise create_serializer_class or serializer_class.
        """
        lookup_value = kwargs.get(self.lookup_field)
        if lookup_value is None:
            raise HTTPException(status_code=400, detail=f"Missing lookup field: {self.lookup_field}")

        obj = await self.get_object(**{self.lookup_field: lookup_value})

        # Extract data
        if hasattr(data, "__struct_fields__"):
            fields = data.__struct_fields__
            data_dict = {field: getattr(data, field) for field in fields}
        elif isinstance(data, dict):
            data_dict = data
        else:
            raise ValueError(f"Cannot extract data from {type(data)}")

        # Update all fields
        for key, value in data_dict.items():
            setattr(obj, key, value)

        await obj.asave()

        # Serialize response
        serializer_class = self.get_serializer_class(action="update")
        if hasattr(serializer_class, "from_model"):
            return serializer_class.from_model(obj)
        else:
            fields = getattr(serializer_class, "__annotations__", {})
            mapped = {name: getattr(obj, name, None) for name in fields}
            return msgspec.convert(mapped, serializer_class)

    async def partial_update(self, request, data, **kwargs):
        """
        Partially update an object.

        Only updates fields that are not None in the data.
        """
        lookup_value = kwargs.get(self.lookup_field)
        if lookup_value is None:
            raise HTTPException(status_code=400, detail=f"Missing lookup field: {self.lookup_field}")

        obj = await self.get_object(**{self.lookup_field: lookup_value})

        # Extract data
        if hasattr(data, "__struct_fields__"):
            fields = data.__struct_fields__
            data_dict = {field: getattr(data, field) for field in fields}
        elif isinstance(data, dict):
            data_dict = data
        else:
            raise ValueError(f"Cannot extract data from {type(data)}")

        # Update only non-None fields
        for key, value in data_dict.items():
            if value is not None:
                setattr(obj, key, value)

        await obj.asave()

        # Serialize response
        serializer_class = self.get_serializer_class(action="partial_update")
        if hasattr(serializer_class, "from_model"):
            return serializer_class.from_model(obj)
        else:
            fields = getattr(serializer_class, "__annotations__", {})
            mapped = {name: getattr(obj, name, None) for name in fields}
            return msgspec.convert(mapped, serializer_class)

    async def destroy(self, request, **kwargs):
        """
        Delete an object.
        """
        lookup_value = kwargs.get(self.lookup_field)
        if lookup_value is None:
            raise HTTPException(status_code=400, detail=f"Missing lookup field: {self.lookup_field}")

        obj = await self.get_object(**{self.lookup_field: lookup_value})
        await obj.adelete()

        return {"deleted": True}

    def get_serializer_class(self, action: str | None = None):
        """
        Get the serializer class for this viewset.

        Supports action-specific serializer classes:
        - list: list_serializer_class or serializer_class
        - create: create_serializer_class or serializer_class
        - update/partial_update: update_serializer_class or create_serializer_class or serializer_class
        - retrieve/destroy: serializer_class

        Args:
            action: The action being performed ('list', 'retrieve', 'create', etc.)

        Returns:
            Serializer class
        """
        if action is None:
            action = self.action

        # Action-specific serializer classes
        if action == "list" and self.list_serializer_class is not None:
            return self.list_serializer_class
        elif action == "create" and self.create_serializer_class is not None:
            return self.create_serializer_class
        elif action in ("update", "partial_update"):
            if self.update_serializer_class is not None:
                return self.update_serializer_class
            elif self.create_serializer_class is not None:
                return self.create_serializer_class

        if self.serializer_class is None:
            raise ValueError(
                f"'{self.__class__.__name__}' should either include a `serializer_class` attribute, "
                f"or override the `get_serializer_class()` method."
            )
        return self.serializer_class
