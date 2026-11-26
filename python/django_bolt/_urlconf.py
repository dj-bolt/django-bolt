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

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import URLResolver, get_resolver, include, path
from django.urls.converters import StringConverter
from django.urls.resolvers import RegexPattern, RoutePattern
from django.utils.functional import Promise

if TYPE_CHECKING:
    from django_bolt.api import BoltAPI

# The module that gives reverse-only names to the APIs that the URLconf does not mount.
REVERSE_ONLY_URLCONF = "django_bolt.urls"

# A parameter of a Django route, as Django's RoutePattern parses it.
_ROUTE_PARAMETER = re.compile(r"<(?:(?P<converter>[^>:]+):)?(?P<parameter>[^>]+)>")


# Bolt path params use matchit ``{name}`` syntax. Only ``{name:path}`` is special
# (a catch-all that matches ``/``); the router ignores every other ``:type``, so we
# map catch-alls to Django's ``path`` converter and leave the rest untyped.
_PARAM_RE = re.compile(r"\{([^}:]+)(?::([^}]+))?\}")


def _to_django_route(bolt_path: str) -> str:
    """Convert a Bolt path to a Django route: ``/files/{p:path}`` -> ``files/<path:p>``."""

    def _convert(match: re.Match) -> str:
        name, converter = match.group(1), match.group(2)
        return f"<path:{name}>" if converter == "path" else f"<{name}>"

    return _PARAM_RE.sub(_convert, bolt_path).lstrip("/")


def _reverse_only_view(*args, **kwargs):  # pragma: no cover - never invoked
    raise RuntimeError("django_bolt reverse-only view should never be called")


def _iter_named_routes(api):
    """Yield ``(namespace, name, route, explicit)`` for every named route on ``api``."""
    routes = list(getattr(api, "_routes", []))
    routes += [(None, path, hid, fn) for path, hid, fn in getattr(api, "_websocket_routes", [])]
    for _method, full_path, handler_id, _fn in routes:
        meta = api._handler_meta.get(handler_id) or {}
        name = meta.get("name")
        if not name:
            continue
        yield meta.get("namespace") or "", name, _to_django_route(full_path), bool(meta.get("name_explicit"))


def build_urlpatterns(api, own_namespace: str = "") -> list:
    """Build reverse-only ``urlpatterns`` from a (merged) BoltAPI.

    Flat routes reverse by their bare name; routes whose API set a ``namespace``
    are grouped under it and reverse as ``namespace:name``, like a Django
    ``include`` with an ``app_name``. Routes in ``own_namespace`` are flat:
    the include of ``api.urls`` gives that namespace. Identical targets (e.g. several methods on
    one path) are deduped, an explicit name wins over a derived one, and two
    explicit names mapping to different paths are a configuration error.
    """
    # (namespace, name) -> (route, explicit). Resolve priority/collisions here.
    chosen: dict[tuple[str, str], tuple[str, bool]] = {}
    for namespace, name, route, explicit in _iter_named_routes(api):
        key = (namespace, name)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = (route, explicit)
            continue
        prev_route, prev_explicit = existing
        if prev_route == route:
            continue
        if explicit and prev_explicit:
            label = f"{namespace}:{name}" if namespace else name
            raise ImproperlyConfigured(f"Duplicate route name {label!r} maps to both {prev_route!r} and {route!r}.")
        if explicit and not prev_explicit:
            chosen[key] = (route, True)

    flat: list = []
    namespaced: dict[str, list] = {}
    for (namespace, name), (route, _explicit) in chosen.items():
        pattern = path(route, _reverse_only_view, name=name)
        (namespaced.setdefault(namespace, []) if namespace and namespace != own_namespace else flat).append(pattern)

    urlpatterns = list(flat)
    for namespace, patterns in namespaced.items():
        urlpatterns.append(path("", include((patterns, namespace))))
    return urlpatterns


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
        return build_urlpatterns(self.api, own_namespace=self.api.namespace or "")

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
    resolver = get_resolver(urlconf)
    # django_bolt.urls as the root mounts no API. Its patterns need this walk, so do not read them.
    if getattr(resolver.urlconf_module, "__name__", None) == REVERSE_ONLY_URLCONF:
        return []
    mounts: list[UrlconfMount] = []
    _collect(resolver.url_patterns, (), mounts)
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


def _prefixes_by_api() -> dict[int, tuple[BoltAPI, list[str]]]:
    """Group the prefixes of the mounts of ``ROOT_URLCONF`` by API, in URLconf order."""
    grouped: dict[int, tuple[BoltAPI, list[str]]] = {}
    if not settings.configured or not getattr(settings, "ROOT_URLCONF", None):
        return grouped
    for mount in urlconf_mounts():
        prefixes = grouped.setdefault(id(mount.api), (mount.api, []))[1]
        # One prefix in two namespaces reverses two ways, but serves one time.
        if mount.prefix not in prefixes:
            prefixes.append(mount.prefix)
    return grouped


def _serve(api: BoltAPI, prefixes: list[str]) -> BoltAPI:
    # A mount at the root serves the paths of the API itself.
    return api if set(prefixes) == {""} else api._served_at(prefixes)


def served_api(api: BoltAPI) -> BoltAPI:
    """Return the API that serves the routes of ``api`` as ``runbolt`` does.

    It is ``api`` when ``ROOT_URLCONF`` does not mount it. Otherwise it is an
    API that serves the routes of ``api`` under each prefix of its mounts.
    """
    entry = _prefixes_by_api().get(id(api))
    return api if entry is None else _serve(api, entry[1])


def served_apis(apis: list[tuple[str, BoltAPI]]) -> list[tuple[str, BoltAPI]]:
    """Return ``(label, api)`` for each API that ``runbolt`` serves.

    ``apis`` are the APIs that autodiscovery found. An API that ``ROOT_URLCONF``
    mounts serves under its prefixes only, also when autodiscovery found it.
    The mounted APIs that autodiscovery did not find come last, in URLconf order.
    """
    grouped = _prefixes_by_api()
    served = []
    for label, api in apis:
        entry = grouped.pop(id(api), None)
        served.append((label, api if entry is None else _serve(api, entry[1])))
    for api, prefixes in grouped.values():
        served.append((f"path({prefixes[0]!r}, api.urls)", _serve(api, prefixes)))
    return served
