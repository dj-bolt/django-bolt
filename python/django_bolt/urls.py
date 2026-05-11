"""URL reversing for Django-Bolt routes."""

import re

from django.core.exceptions import ImproperlyConfigured

from .api import _BOLT_API_REGISTRY

_NAME_INDE: dict[str, str] | None = None


class BoltNoReverseMatch(ValueError):
    """Raised when reverse() can't find a route by name."""

    pass


def _build_index() -> dict[str, str]:
    """Walk _BOLT_API_REGISTRY, return {name: path_pattern}."""

    index: dict[str, str] = {}

    for api in _BOLT_API_REGISTRY:
        for _, route_path, handler_id, _ in api._routes:
            name = api._handler_meta.get(handler_id, {}).get("name")
            if name is None:
                continue
            if name in index:
                raise ImproperlyConfigured(
                    f"Duplicate Bolt route name {name!r}: "
                    f"already mapped to {index[name]!r}, "
                    f"now seen on {route_path!r}"
                )

            index[name] = route_path
    return index


def _get_index() -> dict[str, str]:
    """Get the URL index, building it if needed."""
    global _NAME_INDE
    if _NAME_INDE is None:
        _NAME_INDE = _build_index()
    return _NAME_INDE


def reverse(name: str, **params) -> str:
    """Resolve a Bolt route name + params to a URL string."""
    index = _get_index()
    if name not in index:
        raise BoltNoReverseMatch(f"No Bolt route named {name!r}. Known names: {sorted(index)}")
    pattern = index[name]

    needed = set(re.findall(r"\{(\w+)\}", pattern))
    provided = set(params.keys())

    missing = needed - provided
    if missing:
        raise BoltNoReverseMatch(f"Missing required parameters for {name!r}: {sorted(missing)}. Got {sorted(provided)}")

    extra = provided - needed
    if extra:
        raise BoltNoReverseMatch(f"Unexpected parameters for {name!r}: {sorted(extra)}. Known params: {sorted(needed)}")

    # Build the URL by replacing placeholders

    return pattern.format(**{k: str(v) for k, v in params.items()})
