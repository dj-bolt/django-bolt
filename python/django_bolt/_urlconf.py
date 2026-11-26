"""Mount a ``BoltAPI`` from Django's URLconf.

``path("missions/", api.urls)`` puts an API in the URLconf. At startup, Bolt
walks the resolver of ``ROOT_URLCONF`` one time. For each API that it finds,
it records the prefix of the include and the namespaces above it. Bolt then
serves each route of the API at the path that ``reverse()`` gives: ``"/"``,
then the prefix, then the route path without its leading slash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured
from django.urls import URLResolver, get_resolver
from django.urls.converters import StringConverter
from django.urls.resolvers import RegexPattern, RoutePattern
from django.utils.functional import Promise

if TYPE_CHECKING:
    from django_bolt.api import BoltAPI

# The module that gives reverse-only names to the APIs that the URLconf does not mount.
REVERSE_ONLY_URLCONF = "django_bolt.urls"

# A parameter of a Django route, as Django's RoutePattern parses it.
_ROUTE_PARAMETER = re.compile(r"<(?:(?P<converter>[^>:]+):)?(?P<parameter>[^>]+)>")


class ApiURLConf:
    """The URLconf module of one ``BoltAPI``, as ``api.urls`` gives it.

    Django reads ``urlpatterns`` when it first resolves or reverses a URL, after
    each route of the API is registered. The walk of ``urlconf_mounts`` finds
    the API through this object.
    """

    __slots__ = ("api",)

    def __init__(self, api: BoltAPI) -> None:
        self.api = api

    @property
    def urlpatterns(self) -> list[Any]:
        return []

    def __repr__(self) -> str:
        return f"<ApiURLConf of {self.api!r}>"


@dataclass(frozen=True, slots=True)
class UrlconfMount:
    """One place where the URLconf mounts a ``BoltAPI``.

    ``prefix`` is the Django route of the include in Bolt syntax, for example
    ``"org/{org}/missions/"``. It is empty at the root. ``namespace`` is the
    chain of instance namespaces above the API and of the API, for example
    ``"outer:inner"``.
    """

    api: BoltAPI
    prefix: str
    namespace: str


def join_mount_path(prefix: str, route_path: str) -> str:
    """Return the served path of ``route_path`` under ``prefix``.

    Django's ``reverse()`` joins the routes of the includes and the route of
    the pattern as text. Bolt route paths start with a slash, and Django
    routes do not. Thus ``("missions/", "/{id}")`` gives ``"/missions/{id}"``,
    and the root route ``"/"`` under ``"missions/"`` gives ``"/missions/"``.
    """
    return "/" + prefix + route_path.lstrip("/")


def urlconf_mounts(urlconf: str | None = None) -> list[UrlconfMount]:
    """Find each ``BoltAPI`` that ``urlconf`` mounts, in URLconf order.

    ``urlconf`` defaults to ``ROOT_URLCONF``.

    Raises:
        ImproperlyConfigured: A prefix above a mounted API is not a static
            ``path()`` route that ends with a slash, or it has a parameter
            that is not a ``str`` parameter. Also when ``django_bolt.urls``
            is under a prefix: Bolt serves those routes at their own paths.
    """
    mounts: list[UrlconfMount] = []
    _collect(get_resolver(urlconf).url_patterns, (), mounts)
    return mounts


def _collect(patterns: Any, parents: tuple[URLResolver, ...], mounts: list[UrlconfMount]) -> None:
    for entry in patterns:
        if not isinstance(entry, URLResolver):
            continue
        chain = (*parents, entry)
        module = entry.urlconf_module
        if isinstance(module, ApiURLConf):
            namespace = ":".join(resolver.namespace for resolver in chain if resolver.namespace)
            mounts.append(UrlconfMount(module.api, _bolt_prefix(chain), namespace))
        elif getattr(module, "__name__", None) == REVERSE_ONLY_URLCONF:
            _refuse_reverse_only_prefix(chain)
        else:
            _collect(entry.url_patterns, chain, mounts)


def _describe(chain: tuple[URLResolver, ...]) -> str:
    """The Django route of ``chain``, for an error message."""
    return "".join(str(resolver.pattern) for resolver in chain)


def _bolt_prefix(chain: tuple[URLResolver, ...]) -> str:
    """Return the prefix of the resolvers in ``chain`` in Bolt path syntax."""
    route = _describe(chain)
    where = f"The URLconf mounts a BoltAPI under {route!r}."
    parts: list[str] = []
    for resolver in chain:
        pattern = resolver.pattern
        if not isinstance(pattern, RoutePattern):
            raise ImproperlyConfigured(
                f"{where} Bolt serves an API at one static path, so it cannot use the prefix "
                f"{str(pattern)!r}. Mount the API with path(), not with re_path() or i18n_patterns()."
            )
        if isinstance(pattern._route, Promise):
            raise ImproperlyConfigured(
                f"{where} The prefix {str(pattern)!r} is translated. Bolt serves an API at one path. "
                "Use a prefix that is not translated."
            )
        if resolver.default_kwargs:
            raise ImproperlyConfigured(
                f"{where} The include gives the keyword arguments {sorted(resolver.default_kwargs)!r}. "
                "A Bolt handler does not receive them. Remove them from the include."
            )
        parts.append(_bolt_route(str(pattern), pattern, where))
    prefix = "".join(parts)
    if prefix and not prefix.endswith("/"):
        raise ImproperlyConfigured(
            f"{where} End the prefix with a slash, for example {route + '/'!r}. "
            "Django joins the prefix and the route paths of the API as text."
        )
    if prefix.startswith("/") or "//" in prefix:
        raise ImproperlyConfigured(
            f"{where} The prefix gives an empty path segment. "
            "Start each route of the include without a slash, and end it with one slash."
        )
    return prefix


def _bolt_route(route: str, pattern: RoutePattern, where: str) -> str:
    """Convert one Django route to Bolt syntax: ``org/<str:org>/`` becomes ``org/{org}/``."""
    if "{" in route or "}" in route:
        raise ImproperlyConfigured(
            f"{where} Bolt reads braces in a path as a parameter. Remove the braces from the prefix {route!r}."
        )

    def convert(match: re.Match[str]) -> str:
        name = match.group("parameter")
        # StringConverter matches one segment that is not empty, as a Bolt parameter does.
        if type(pattern.converters[name]) is not StringConverter:
            raise ImproperlyConfigured(
                f"{where} Bolt cannot check the converter of {match.group(0)!r} in the prefix. "
                f"Use <str:{name}>, and check the value in the handler."
            )
        return "{" + name + "}"

    return _ROUTE_PARAMETER.sub(convert, route)


def _is_empty(pattern: Any) -> bool:
    if isinstance(pattern, RoutePattern):
        return str(pattern) == ""
    return isinstance(pattern, RegexPattern) and str(pattern) in ("", "^")


def _refuse_reverse_only_prefix(chain: tuple[URLResolver, ...]) -> None:
    """Refuse ``include("django_bolt.urls")`` under a prefix.

    Those names reverse to paths that Bolt does not serve under the prefix.
    """
    if all(_is_empty(resolver.pattern) for resolver in chain):
        return
    route = _describe(chain)
    raise ImproperlyConfigured(
        f"The URLconf includes {REVERSE_ONLY_URLCONF} under {route!r}. Bolt serves those routes at their "
        "own paths, so reverse() would give paths that Bolt does not serve. "
        f'Include {REVERSE_ONLY_URLCONF} with path(""), or mount each API with path({route!r}, api.urls).'
    )
