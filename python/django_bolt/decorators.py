"""
Decorators for Django-Bolt.

Provides decorators for ViewSet custom actions similar to Django REST Framework's @action decorator.
"""

from collections.abc import Callable
from typing import Any

_RESPONSE_MODEL_UNSET = object()


class ActionHandler:
    """
    Marker class for ViewSet custom actions decorated with @action.

    When @action is used inside a ViewSet class, it returns an ActionHandler instance
    that stores metadata about the action. The api.viewset() method discovers these
    ActionHandler instances and auto-generates routes.

    Similar to Django REST Framework's @action decorator approach.

    Attributes:
        fn: The wrapped function
        methods: List of HTTP methods (e.g., ["GET", "POST"])
        detail: Whether this is a detail (instance-level) or list (collection-level) action
        path: Custom path segment (defaults to function name)
        auth: Optional authentication backends
        guards: Optional permission guards
        response_model: Optional response model for serialization
        status_code: Optional HTTP status code
    """

    __slots__ = (
        "fn",
        "methods",
        "detail",
        "path",
        "auth",
        "guards",
        "response_model",
        "status_code",
        "validate_response",
        "tags",
        "summary",
        "description",
    )

    def __init__(
        self,
        fn: Callable,
        methods: list[str],
        detail: bool,
        path: str | None = None,
        auth: list[Any] | None = None,
        guards: list[Any] | None = None,
        response_model: Any = _RESPONSE_MODEL_UNSET,
        status_code: int | None = None,
        validate_response: bool | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
    ):
        self.fn = fn
        self.methods = [m.upper() for m in methods]  # Normalize to uppercase
        self.detail = detail
        self.path = path or fn.__name__  # Default to function name
        self.auth = auth
        self.guards = guards
        self.response_model = response_model
        self.status_code = status_code
        self.validate_response = validate_response
        self.tags = tags
        self.summary = summary
        self.description = description

    def __call__(self, *args, **kwargs):
        """Make the handler callable (delegates to wrapped function)."""
        return self.fn(*args, **kwargs)

    def __repr__(self):
        methods_str = "|".join(self.methods)
        detail_str = "detail" if self.detail else "list"
        return f"ActionHandler({methods_str}, {detail_str}, path={self.path}, fn={self.fn.__name__})"


def action(
    methods: list[str],
    detail: bool,
    path: str | None = None,
    *,
    auth: list[Any] | None = None,
    guards: list[Any] | None = None,
    response_model: Any = _RESPONSE_MODEL_UNSET,
    status_code: int | None = None,
    validate_response: bool | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
    description: str | None = None,
) -> Callable:
    """
    Decorator for ViewSet custom actions (DRF-style).

    Marks a ViewSet method as a custom action with automatic route generation.

    Auto-generated paths:
    - detail=True (instance-level):  /{resource}/{pk}/{action_name}
      Example: @action(methods=["POST"], detail=True) -> POST /users/{id}/activate

    - detail=False (collection-level): /{resource}/{action_name}
      Example: @action(methods=["GET"], detail=False) -> GET /users/active

    Multiple methods on single action:
    - @action(methods=["GET", "POST"], detail=True, path="preferences")
      Generates both: GET /users/{id}/preferences and POST /users/{id}/preferences

    Args:
        methods: List of HTTP methods (e.g., ["GET"], ["POST"], ["GET", "POST"])
        detail: True for instance-level (requires pk), False for collection-level
        path: Optional custom action name (defaults to function name)
        auth: Optional authentication backends (overrides class-level auth)
        guards: Optional permission guards (overrides class-level guards)
        response_model: Optional response model for serialization
        status_code: Optional HTTP status code

    Returns:
        ActionHandler instance that wraps the function with metadata

    Example:
        @api.viewset("/users")
        class UserViewSet(ViewSet):
            async def list(self, request) -> list[UserMini]:
                return User.objects.all()[:100]

            # Instance-level action: POST /users/{id}/activate
            @action(methods=["POST"], detail=True)
            async def activate(self, request, id: int) -> UserFull:
                user = await User.objects.aget(id=id)
                user.is_active = True
                await user.asave()
                return user

            # Collection-level action: GET /users/active
            @action(methods=["GET"], detail=False)
            async def active(self, request) -> list[UserMini]:
                return User.objects.filter(is_active=True)[:100]

            # Custom path: GET/POST /users/{id}/preferences
            @action(methods=["GET", "POST"], detail=True, path="preferences")
            async def user_preferences(self, request, id: int, data: dict | None = None):
                if data:  # POST
                    # update preferences
                    pass
                else:  # GET
                    # return preferences
                    pass

    Notes:
        - Actions inherit class-level auth and guards unless explicitly overridden
        - The function must be async
        - Path parameters are automatically extracted from the route
        - For detail=True actions, the lookup field parameter (e.g., 'id', 'pk') is required
    """

    def decorator(fn: Callable) -> ActionHandler:
        """Wrap the function with ActionHandler metadata."""
        # Validate methods
        valid_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        for method in methods:
            if method.upper() not in valid_methods:
                raise ValueError(f"Invalid HTTP method '{method}'. Valid methods: {', '.join(sorted(valid_methods))}")

        # Create and return ActionHandler
        return ActionHandler(
            fn=fn,
            methods=methods,
            detail=detail,
            path=path,
            auth=auth,
            guards=guards,
            response_model=response_model,
            status_code=status_code,
            validate_response=validate_response,
            tags=tags,
            summary=summary,
            description=description,
        )

    return decorator
