"""Mount a ``BoltAPI`` from Django's URLconf.

``path("missions/", api.urls)`` puts an API in the URLconf. At startup, Bolt
walks the resolver of ``ROOT_URLCONF`` one time. For each API that it finds,
it records the prefix of the include and the namespaces above it. Bolt then
serves each route of the API at the path that ``reverse()`` gives: ``"/"``,
then the prefix, then the route path without its leading slash.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import URLResolver, get_resolver, include, path
from django.urls.converters import StringConverter
from django.urls.resolvers import LocalePrefixPattern, RegexPattern, RoutePattern
from django.utils.functional import Promise

if TYPE_CHECKING:
    from django_bolt.api import BoltAPI
    from django_bolt.management.commands.runbolt import Command

# The module that gives reverse-only names to the APIs that the URLconf does not mount.
REVERSE_ONLY_URLCONF = "django_bolt.urls"
_INCLUDE_REVERSE_ONLY = f"include({REVERSE_ONLY_URLCONF!r})"

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


class ReverseOnlyURLConf(Sequence):
    """The URLconf in ``django_bolt.urls``: the names of the APIs that no ``api.urls`` mounts.

    ``django_bolt.urls.urlpatterns`` is a list with one include of this object.
    Thus a URLconf can copy that list or join it to another list. Django reads
    the patterns of this object after it imports the whole URLconf, so the
    URLconf can mount an API with ``api.urls`` before or after the include.

    The object is its own list of patterns. Django reads a ``urlpatterns``
    attribute with a default, and that would hide an ``AttributeError`` of
    autodiscovery as a URLconf with no patterns.
    """

    __slots__ = ("_apis", "_command", "_patterns")

    def __init__(self, command: Callable[[], Command]) -> None:
        # runbolt's Command finds and merges the APIs. runbolt imports this module, so the class is an argument.
        self._command = command
        self._apis: list[tuple[str, BoltAPI]] | None = None
        self._patterns: list[Any] | None = None

    def apis(self) -> list[tuple[str, BoltAPI]]:
        """Return the APIs that ``runbolt`` finds and that no ``api.urls`` of ``ROOT_URLCONF`` mounts."""
        if self._apis is None:
            mounted = {id(mount.api) for mount in _walk_root().mounts}
            self._apis = [(label, api) for label, api in self._command().autodiscover_apis() if id(api) not in mounted]
        return self._apis

    def _load(self) -> list[Any]:
        if self._patterns is None:
            apis = self.apis()
            self._patterns = build_urlpatterns(self._command().merge_apis(apis)) if apis else []
        return self._patterns

    def __getitem__(self, index: Any) -> Any:
        return self._load()[index]

    def __len__(self) -> int:
        return len(self._load())

    def __repr__(self) -> str:
        return f"<ReverseOnlyURLConf of {REVERSE_ONLY_URLCONF}>"


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


class _Walk:
    """What one walk of a URLconf finds."""

    __slots__ = ("mounts", "reverse_only", "reverse_only_prefixes")

    def __init__(self) -> None:
        self.mounts: list[UrlconfMount] = []
        # The include of django_bolt.urls, and its prefixes in URLconf order, each one time.
        self.reverse_only: ReverseOnlyURLConf | None = None
        self.reverse_only_prefixes: list[str] = []


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
        ImproperlyConfigured: A prefix above a mounted API or above
            ``django_bolt.urls`` is not a static ``path()`` route that ends
            with a slash, or it has a parameter that Bolt cannot serve.
    """
    return _walk(urlconf).mounts


def _walk(urlconf: str | None) -> _Walk:
    walk = _Walk()
    _collect(get_resolver(urlconf).url_patterns, (), walk)
    return walk


def _walk_root() -> _Walk:
    """Walk ``ROOT_URLCONF``. A project with no ``ROOT_URLCONF`` mounts nothing."""
    if not settings.configured or not getattr(settings, "ROOT_URLCONF", None):
        return _Walk()
    return _walk(None)


def _collect(patterns: Any, parents: tuple[URLResolver, ...], walk: _Walk) -> None:
    for entry in patterns:
        if not isinstance(entry, URLResolver):
            continue
        chain = (*parents, entry)
        module = entry.urlconf_module
        if isinstance(module, ApiURLConf):
            namespace = ":".join(resolver.namespace for resolver in chain if resolver.namespace)
            where = f"The URLconf mounts a BoltAPI at {_describe(chain, 'api.urls')}."
            walk.mounts.append(UrlconfMount(module.api, _bolt_prefix(chain, where, refuse_kwargs=True), namespace))
        elif isinstance(module, ReverseOnlyURLConf):
            # The last entry of the chain is in django_bolt.urls itself, so the message names its parents.
            where = f"The URLconf includes {REVERSE_ONLY_URLCONF} at {_describe(parents, _INCLUDE_REVERSE_ONLY)}."
            # Bolt ignored the keyword arguments of this include before, so it does not refuse them.
            prefix = _bolt_prefix(chain, where, refuse_kwargs=False)
            if walk.reverse_only is None:
                walk.reverse_only = module
            if prefix not in walk.reverse_only_prefixes:
                walk.reverse_only_prefixes.append(prefix)
        else:
            _collect(entry.url_patterns, chain, walk)


def _entry(pattern: Any, target: str) -> str:
    """One URLconf entry as the URLconf writes it, for example ``path('org/', include(...))``."""
    if isinstance(pattern, LocalePrefixPattern):
        return f"i18n_patterns({target})"
    if isinstance(pattern, RoutePattern):
        return f"path({str(pattern)!r}, {target})"
    if isinstance(pattern, RegexPattern):
        return f"re_path({str(pattern)!r}, {target})"
    return f"{type(pattern).__name__}({str(pattern)!r}, {target})"


def _describe(chain: tuple[URLResolver, ...], target: str) -> str:
    """Name the URLconf entries of ``chain``, for example ``path('org/', include(...)) > path('', api.urls)``."""
    if not chain:
        return target
    *outer, last = chain
    return " > ".join([*(_entry(resolver.pattern, "include(...)") for resolver in outer), _entry(last.pattern, target)])


def _is_empty(pattern: Any) -> bool:
    """Whether ``pattern`` adds nothing to the path: ``path("")``, ``re_path("")`` or ``re_path("^")``."""
    if isinstance(pattern, RoutePattern):
        return str(pattern) == ""
    return isinstance(pattern, RegexPattern) and str(pattern) in ("", "^")


def _bolt_prefix(chain: tuple[URLResolver, ...], where: str, *, refuse_kwargs: bool) -> str:
    """Return the prefix of the resolvers in ``chain`` in Bolt path syntax.

    ``refuse_kwargs`` refuses the keyword arguments of an include. A Bolt handler does not receive them.
    """
    routes: list[str] = []
    parts: list[str] = []
    for resolver in chain:
        pattern = resolver.pattern
        if refuse_kwargs and resolver.default_kwargs:
            raise ImproperlyConfigured(
                f"{where} The include gives the keyword arguments {sorted(resolver.default_kwargs)!r}. "
                "A Bolt handler does not receive them. Remove them from the include."
            )
        if isinstance(pattern, LocalePrefixPattern):
            raise ImproperlyConfigured(
                f"{where} The prefix of i18n_patterns() changes with the language, and Bolt serves "
                "routes at static paths. Move the entry out of i18n_patterns()."
            )
        if _is_empty(pattern):
            continue
        if not isinstance(pattern, RoutePattern):
            raise ImproperlyConfigured(
                f"{where} Bolt serves routes at static paths, so it cannot use the prefix {str(pattern)!r}. "
                "Use path() with a static prefix, not re_path()."
            )
        if isinstance(pattern._route, Promise):
            raise ImproperlyConfigured(
                f"{where} The prefix {str(pattern)!r} is translated. Bolt serves routes at static paths. "
                "Use a prefix that is not translated."
            )
        routes.append(str(pattern))
        parts.append(_bolt_route(str(pattern), pattern, where))
    prefix = "".join(parts)
    if prefix and not prefix.endswith("/"):
        raise ImproperlyConfigured(
            f"{where} End the prefix with a slash, for example {''.join(routes) + '/'!r}. "
            "Django joins the prefix and the route paths as text."
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

    bolt_route = _ROUTE_PARAMETER.sub(convert, route)
    for segment in bolt_route.split("/"):
        if segment.count("{") > 1:
            raise ImproperlyConfigured(
                f"{where} The Bolt router allows one parameter in a path segment, and {segment!r} has more. "
                "Put each parameter in its own segment."
            )
    return bolt_route


def _prefixes_by_api(walk: _Walk) -> dict[int, tuple[BoltAPI, list[str]]]:
    """Group the prefixes of the ``api.urls`` mounts by API, in URLconf order."""
    grouped: dict[int, tuple[BoltAPI, list[str]]] = {}
    for mount in walk.mounts:
        prefixes = grouped.setdefault(id(mount.api), (mount.api, []))[1]
        # One prefix in two namespaces reverses two ways, but serves one time.
        if mount.prefix not in prefixes:
            prefixes.append(mount.prefix)
    return grouped


def _reverse_only_prefixes(walk: _Walk) -> list[str] | None:
    """The prefixes of ``django_bolt.urls``, or None when its APIs serve at their own paths."""
    prefixes = walk.reverse_only_prefixes
    return None if prefixes in ([], [""]) else prefixes


def _serve(api: BoltAPI, prefixes: list[str]) -> BoltAPI:
    # A mount at the root serves the paths of the API itself.
    return api if set(prefixes) == {""} else api._served_at(prefixes)


def served_api(api: BoltAPI) -> BoltAPI:
    """Return the API that serves the routes of ``api`` as ``runbolt`` does.

    It is an API that serves the routes of ``api`` under each prefix of its
    ``api.urls`` mounts. When no ``api.urls`` mounts ``api`` and
    ``django_bolt.urls`` names it, it serves under the prefixes of that include.
    Otherwise it is ``api``.
    """
    walk = _walk_root()
    entry = _prefixes_by_api(walk).get(id(api))
    if entry is not None:
        return _serve(api, entry[1])
    moved = _reverse_only_prefixes(walk)
    # Autodiscovery runs only when django_bolt.urls has a prefix, to know if it names the API.
    if (
        moved is not None
        and walk.reverse_only is not None
        and any(named is api for _label, named in walk.reverse_only.apis())
    ):
        return _serve(api, moved)
    return api


def served_apis(apis: list[tuple[str, BoltAPI]]) -> list[tuple[str, BoltAPI]]:
    """Return ``(label, api)`` for each API that ``runbolt`` serves.

    ``apis`` are the APIs that autodiscovery found. An API that an
    ``api.urls`` of ``ROOT_URLCONF`` mounts serves under its prefixes only, also
    when autodiscovery found it. The other APIs are the APIs that
    ``django_bolt.urls`` names, so they serve under the prefixes of its include.
    The mounted APIs that autodiscovery did not find come last, in URLconf order.
    """
    walk = _walk_root()
    grouped = _prefixes_by_api(walk)
    moved = _reverse_only_prefixes(walk)
    served = []
    for label, api in apis:
        entry = grouped.pop(id(api), None)
        if entry is not None:
            served.append((label, _serve(api, entry[1])))
        elif moved is not None:
            served.append((label, _serve(api, moved)))
        else:
            served.append((label, api))
    for api, prefixes in grouped.values():
        served.append((f"path({prefixes[0]!r}, api.urls)", _serve(api, prefixes)))
    return served
