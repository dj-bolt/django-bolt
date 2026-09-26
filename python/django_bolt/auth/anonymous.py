"""
Async user fallback for requests without Django authentication middleware.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import AnonymousUser

from .user_loader import aload_bolt_user


async def auser_fallback(user: Any = None) -> Any:
    """
    Return the Bolt user, or an anonymous user if no user is set.

    A user that Bolt authenticated loads with its async loader, on the lane
    of the request or on the ORM pool, and does not block the event loop.
    ``request.user`` then shares the loaded result.
    """
    if user is None:
        return AnonymousUser()
    return await aload_bolt_user(user)


__all__ = ["auser_fallback"]
