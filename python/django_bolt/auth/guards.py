"""
Permission/guard system for Django-Bolt.

There are exactly three guards:

- ``IsAuthenticated()`` — the request must carry a valid credential (401).
- ``AllowAny()`` — explicitly public; overrides global default guards.
- ``Requires(claim, *values, all_of=...)`` — THE permission check. One
  primitive for roles, permissions, tenancy, feature flags — anything the
  token carries.

Every guard compiles to a native Rust check at registration (the claim name
and expected values are extracted from Python exactly once), so request-time
guard evaluation never touches the GIL. Logic that cannot be expressed as a
claim comparison (database lookups, cross-field rules) belongs in a
dependency that raises ``HTTPException(status_code=403)``, not in a guard.
"""

from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# Claim values a guard may match against. These are the JSON scalar types, so
# they can be extracted into Rust once at registration and compared natively
# per request. (bool is accepted implicitly: it is a subclass of int.)
_CLAIM_VALUE_TYPES = (str, int, float)


class BasePermission:
    """
    Internal base for the built-in guards.

    Do not subclass this to build custom checks — declare them with
    ``Requires`` and give them a name by assignment:

        IsClient = Requires("role", "client")
        IsStaff = Requires("is_staff", True)

        @api.get("/orders", auth=[...], guards=[IsAuthenticated(), IsClient])
    """

    __slots__ = ()

    def to_metadata(self) -> dict[str, Any]:
        """
        Compile this guard into metadata for Rust.

        Each built-in guard overrides this. A subclass that doesn't is a
        configuration error caught at registration — guards run natively in
        Rust, so there is nothing a bare subclass could enforce.
        """
        raise ImproperlyConfigured(
            f"{type(self).__name__} is not a usable guard. Custom checks are "
            f"declared with Requires, not by subclassing: "
            f'{type(self).__name__} = Requires("<claim>", <value>, ...)'
        )


class AllowAny(BasePermission):
    """
    Allow any request, authenticated or not.

    This is the default permission when no guards are specified.
    Using this explicitly bypasses any global default permissions.
    """

    __slots__ = ()

    def to_metadata(self) -> dict[str, Any]:
        return {"type": "allow_any"}


class IsAuthenticated(BasePermission):
    """
    Require that the request is authenticated.

    Returns 401 if no authentication was successful.
    """

    __slots__ = ()

    def to_metadata(self) -> dict[str, Any]:
        return {"type": "is_authenticated"}


class Requires(BasePermission):
    """
    The permission check: require a token claim to be present, optionally
    matching expected values.

        Requires("tenant_id")                       # claim must exist
        Requires("role", "client")                  # equals (or list contains)
        Requires("role", "client", "vip")           # any of
        Requires("is_staff", True)                  # boolean claim
        Requires("permissions", "blog.add_article") # Django-style permission
        Requires("permissions", all_of=["blog.add_article", "blog.change_article"])

    Semantics:

    - Positional ``values`` are OR — the claim must match at least one.
    - ``all_of`` is AND — the claim (a list, e.g. ``permissions``) must
      contain every value. Positional values and ``all_of`` are mutually
      exclusive.
    - A scalar claim matches by equality; a list claim matches by membership.
    - No values at all means "the claim must be present and non-null".
    - The ``permissions`` claim is special-cased to the unified permission
      set, so it also covers ``key_permissions`` from API-key auth.

    Name reusable checks by assignment — no subclassing:

        IsClient = Requires("role", "client")

    Compiled to a native Rust check at registration — the claim name and
    values are extracted from Python exactly once, and request-time
    evaluation never touches the GIL. Returns 401 when unauthenticated,
    403 when the claim is missing or doesn't match.
    """

    __slots__ = ("claim", "values", "match_all")

    def __init__(
        self,
        claim: str,
        *values: str | int | float | bool,
        all_of: list[str | int | float | bool] | tuple[str | int | float | bool, ...] | None = None,
    ):
        if not isinstance(claim, str) or not claim:
            raise ImproperlyConfigured(
                f"Requires() claim must be a non-empty string, got {claim!r}."
            )
        if all_of is not None:
            if values:
                raise ImproperlyConfigured(
                    "Requires() takes either positional values (any-of) or "
                    "all_of=[...] (all-of), not both."
                )
            if isinstance(all_of, (str, bytes)):
                raise ImproperlyConfigured(
                    f"Requires() all_of must be a list/tuple of values, got {all_of!r}."
                )
            values = tuple(all_of)
            if not values:
                raise ImproperlyConfigured(
                    "Requires() all_of must not be empty — omit it for a presence check."
                )
        for value in values:
            if not isinstance(value, _CLAIM_VALUE_TYPES):
                raise ImproperlyConfigured(
                    f"Requires({claim!r}) got value {value!r} ({type(value).__name__}); "
                    f"claim values must be str, int, float, or bool so they can be "
                    f"compiled into the Rust guard at registration."
                )

        self.claim = claim
        self.values = values
        self.match_all = all_of is not None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "type": "requires",
            "claim": self.claim,
            "values": list(self.values),
            "match_all": self.match_all,
        }

    def __repr__(self) -> str:
        if self.match_all:
            return f"Requires({self.claim!r}, all_of={list(self.values)!r})"
        args = ", ".join(repr(v) for v in (self.claim, *self.values))
        return f"Requires({args})"


def get_default_permission_classes() -> list[BasePermission]:
    """
    Get default permission classes from Django settings.

    Looks for BOLT_DEFAULT_PERMISSION_CLASSES in settings. If not found,
    returns [AllowAny()] (no restrictions by default).
    """
    try:
        try:
            if hasattr(settings, "BOLT_DEFAULT_PERMISSION_CLASSES"):
                return settings.BOLT_DEFAULT_PERMISSION_CLASSES
        except ImproperlyConfigured:
            # Settings not configured, return default
            pass
    except (ImportError, AttributeError):
        pass

    return [AllowAny()]
