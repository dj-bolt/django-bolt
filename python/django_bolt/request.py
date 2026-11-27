"""
Request Protocol for Django-Bolt.

Defines the interface for request objects. At runtime, handlers receive
PyRequest from Rust (src/request.rs). This Protocol provides type hints
and IDE autocomplete.
"""

from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from django.contrib.sessions.backends.base import SessionBase


@runtime_checkable
class Request(Protocol):
    """
    Request protocol - the interface for request objects.

    At runtime, handlers receive PyRequest from Rust (src/request.rs).
    This Protocol defines the interface for type checking and IDE support.

    Examples:
        @api.get("/profile")
        async def profile(request: Request) -> dict:
            user = await request.auser()
            return {"user": user.username}
    """

    @property
    def method(self) -> str:
        """HTTP method (GET, POST, etc.)"""
        ...

    @property
    def path(self) -> str:
        """Request path. With ``FORCE_SCRIPT_NAME``, the script name comes first, as in Django."""
        ...

    @property
    def path_info(self) -> str:
        """Request path without the script name. The router matches it."""
        ...

    @property
    def body(self) -> bytes:
        """Request body as bytes"""
        ...

    @property
    def headers(self) -> dict[str, str]:
        """Request headers"""
        ...

    @property
    def cookies(self) -> dict[str, str]:
        """Request cookies"""
        ...

    @property
    def query(self) -> dict[str, str]:
        """Query parameters"""
        ...

    @property
    def params(self) -> dict[str, str]:
        """Path parameters"""
        ...

    @property
    def user(self) -> Any:
        """The user of the request, or Django's AnonymousUser when there is none."""
        ...

    @user.setter
    def user(self, value: Any) -> None: ...

    @property
    def context(self) -> Any:
        """Auth context (JWT claims, API key info, etc.)"""
        ...

    @property
    def state(self) -> dict[str, Any]:
        """Middleware state dict"""
        ...

    @property
    def auser(self) -> Any:
        """Async user getter (Django-style)"""
        ...

    @auser.setter
    def auser(self, value: Any) -> None:
        """Set async user callable (used by Django's alogin/alogout)"""
        ...

    @property
    def session(self) -> "SessionBase":
        """Django session (requires SessionMiddleware)"""
        ...

    @property
    def scheme(self) -> str:
        """The scheme of the request: "http" or "https", as in ``build_absolute_uri()``."""
        ...

    def is_secure(self) -> bool:
        """True when ``scheme`` is "https"."""
        ...

    def __getitem__(self, name: str, /) -> Any:
        """Support `request["args"]`"""
        ...


__all__ = [
    "Request",
]
