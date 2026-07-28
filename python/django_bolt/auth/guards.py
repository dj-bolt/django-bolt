"""
Permission/guard system for Django-Bolt.

There are exactly three guards:

- ``IsAuthenticated()`` — the request must carry a valid credential (401).
- ``AllowAny()`` — explicitly public; overrides global default guards.
- ``Requires(claim, *values, all_of=..., none_of=..., message=...)`` — THE
  permission check. One primitive for roles, permissions, tenancy, feature
  flags — anything the token carries.

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

# How a guard quantifies its expected values over the claim. Sent to Rust as
# an integer and parsed into `permissions::Quantifier`; a single tri-state
# rather than a pair of booleans, so "all of these AND none of these" — which
# has no meaning — cannot be represented.
QUANT_ANY = 0
QUANT_ALL = 1
QUANT_NONE = 2


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
        Requires("role", none_of=["banned", "suspended"])  # none of

    Semantics:

    - Positional ``values`` are OR — the claim must match at least one.
    - ``all_of`` is AND — the claim (a list, e.g. ``permissions``) must
      contain every value.
    - ``none_of`` is NOR — the claim must match none of the values. Use it to
      exclude a few values instead of listing every allowed one.
    - The three are mutually exclusive: they are the same quantifier slot.
      Guards are cumulative, so "has X but not Y" is two guards:
      ``[Requires("permissions", "blog.add"), Requires("permissions", none_of=["blog.admin"])]``
    - A scalar claim matches by equality; a list claim matches by membership.
    - No values at all means "the claim must be present and non-null". For a
      boolean claim, present means true — ``Requires("is_admin")`` rejects a
      token carrying ``is_admin: false``.
    - The ``permissions`` claim is special-cased to the unified permission
      set, so it also covers ``key_permissions`` from API-key auth.
    - An unauthenticated request fails *every* ``Requires``, including a
      ``none_of`` one, with a 401. A request carrying no claims matches
      nothing, so without this it would satisfy an exclusion and walk
      straight through the guard.

    ``message`` sets the ``detail`` returned with the 403. It is serialized
    into the response body once at registration, so a custom message costs
    nothing per request. It is never used for the 401: an unauthenticated
    caller is not told why it would have been denied.

    Name reusable checks by assignment — no subclassing:

        IsClient = Requires("role", "client")

    Compiled to a native Rust check at registration — the claim name and
    values are extracted from Python exactly once, and request-time
    evaluation never touches the GIL. Returns 401 when unauthenticated,
    403 when the claim is missing or doesn't match.
    """

    __slots__ = ("claim", "values", "quantifier", "message")

    def __init__(
        self,
        claim: str,
        *values: str | int | float | bool,
        all_of: list[str | int | float | bool] | tuple[str | int | float | bool, ...] | None = None,
        none_of: list[str | int | float | bool] | tuple[str | int | float | bool, ...] | None = None,
        message: str | None = None,
    ):
        if not isinstance(claim, str) or not claim:
            raise ImproperlyConfigured(f"Requires() claim must be a non-empty string, got {claim!r}.")

        # Positional values, all_of and none_of are the same quantifier slot,
        # so exactly one may be given. Guards are cumulative, which is how
        # "has X but not Y" is spelled: two guards.
        if sum((bool(values), all_of is not None, none_of is not None)) > 1:
            raise ImproperlyConfigured(
                "Requires() takes positional values (any-of), all_of=[...] (all-of), or "
                "none_of=[...] (none-of) — not both. They are the same quantifier; "
                "guards are cumulative, so combine them as separate guards."
            )

        quantifier = QUANT_ANY
        keyword, supplied = ("all_of", all_of) if all_of is not None else ("none_of", none_of)
        if supplied is not None:
            if isinstance(supplied, (str, bytes)):
                raise ImproperlyConfigured(f"Requires() {keyword} must be a list/tuple of values, got {supplied!r}.")
            values = tuple(supplied)
            if not values:
                raise ImproperlyConfigured(f"Requires() {keyword} must not be empty — omit it for a presence check.")
            quantifier = QUANT_ALL if all_of is not None else QUANT_NONE

        if message is not None and (not isinstance(message, str) or not message):
            raise ImproperlyConfigured(f"Requires() message must be a non-empty string, got {message!r}.")

        for value in values:
            if not isinstance(value, _CLAIM_VALUE_TYPES):
                raise ImproperlyConfigured(
                    f"Requires({claim!r}) got value {value!r} ({type(value).__name__}); "
                    f"claim values must be str, int, float, or bool so they can be "
                    f"compiled into the Rust guard at registration."
                )

        self.claim = claim
        self.values = values
        self.quantifier = quantifier
        self.message = message

    def to_metadata(self) -> dict[str, Any]:
        # Every key is always present: Rust reads them by direct access, and a
        # conditional key here would be a registration-time hole there.
        return {
            "type": "requires",
            "claim": self.claim,
            "values": list(self.values),
            "quantifier": self.quantifier,
            "message": self.message,
        }

    def __repr__(self) -> str:
        if self.quantifier == QUANT_ALL:
            args = f"{self.claim!r}, all_of={list(self.values)!r}"
        elif self.quantifier == QUANT_NONE:
            args = f"{self.claim!r}, none_of={list(self.values)!r}"
        else:
            args = ", ".join(repr(v) for v in (self.claim, *self.values))
        if self.message is not None:
            args = f"{args}, message={self.message!r}"
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
