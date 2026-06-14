"""Internal utility helpers shared across django_bolt modules."""

from __future__ import annotations

import re

from django.utils.text import slugify

# Split CamelCase into words before slugifying, so class/function names become
# Django-style kebab names: GetMission -> get-mission, UserViewSet -> user-view-set.
_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def slugify_route_name(value: str) -> str:
    """Normalize a framework-derived route name to a Django-style slug."""
    snaked = _CAMEL_BOUNDARY_2.sub(r"\1_\2", _CAMEL_BOUNDARY_1.sub(r"\1_\2", value))
    return slugify(snaked.replace("_", "-"))
