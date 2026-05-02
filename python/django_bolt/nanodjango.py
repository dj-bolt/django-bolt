"""
django-bolt plugin for nanodjango single-file apps.

Provides a BoltAPI subclass that auto-configures Django settings so you can
write a production-ready single-file app without manual settings wrangling:

    from nanodjango import Django
    from django_bolt.nanodjango import BoltAPI

    app = Django()
    bolt = BoltAPI()

    @bolt.get('/hello')
    async def hello(request):
        return {'message': 'hello'}

Run with: python myapp.py runbolt --port 8001

Install: pip install "django-bolt[nanodjango]"
"""

from __future__ import annotations

import ast
import inspect
import logging
import sys
from typing import Any

from nanodjango import Django, defer, hookimpl
from nanodjango.convert.converter import Converter, Resolver
from nanodjango.convert.utils import collect_references, get_decorators

from .api import BoltAPI as _RealBoltAPI

logger = logging.getLogger(__name__)

_BOLT_DECORATOR_NAMES: tuple[str, ...] = (
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "websocket",
)
_BOLT_MOUNT_NAMES: tuple[str, ...] = ("mount_django", "mount")


class BoltAPI(_RealBoltAPI):
    """
    BoltAPI subclass for nanodjango single-file apps.

    Must be instantiated after ``app = Django()``, which is where
    settings.configure() is called (same requirement as the real BoltAPI).

    Auto-configures:
    - ``django_bolt`` added to ``INSTALLED_APPS``
    - ``settings.BOLT_API`` set to ``["<module>:<varname>"]`` on first route

    This subclass passes ``isinstance(bolt, django_bolt.api.BoltAPI)`` so
    django-bolt's ``runbolt`` autodiscovery finds it correctly.
    """

    def __init__(self, *, module: str | None = None, **kwargs: Any) -> None:
        if module is None:
            frame = inspect.currentframe()
            try:
                module = frame.f_back.f_globals.get("__name__", "__main__")
            finally:
                # Drop the frame ref before super().__init__ to avoid a cycle.
                del frame
        self._module_name: str = module
        self._bolt_api_configured: bool = False

        super().__init__(**kwargs)

        # Inline import: this module is loaded by nanodjango's plugin manager
        # during ``Django()`` construction, before ``settings.configure()``.
        # A module-top ``from django.conf import settings`` would expose the
        # lazy ``settings`` proxy to pluggy's attribute walk and trigger
        # ``ImproperlyConfigured`` via ``inspect.isroutine``.
        from django.conf import settings  # noqa: PLC0415

        if "django_bolt" not in settings.INSTALLED_APPS:
            settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + ["django_bolt"]

    def _configure_bolt_api(self) -> None:
        """
        Scan the calling module's globals to find our variable name, then
        set settings.BOLT_API so runbolt autodiscovery can find this instance.

        Called lazily on the first route decorator so the variable is guaranteed
        to be in the module namespace by then.
        """
        if self._bolt_api_configured:
            return
        self._bolt_api_configured = True

        from django.conf import settings  # noqa: PLC0415

        module = sys.modules.get(self._module_name)
        if module is None:
            self._warn_autoconfig_failed(f"module {self._module_name!r} is not in sys.modules")
            return

        for name, val in vars(module).items():
            if val is self:
                entry = f"{self._module_name}:{name}"
                existing = list(getattr(settings, "BOLT_API", []))
                if entry not in existing:
                    existing.append(entry)
                    settings.BOLT_API = existing
                return

        self._warn_autoconfig_failed(
            f"BoltAPI instance not found in globals of module {self._module_name!r} "
            "(usually means BoltAPI() was constructed inside a function rather than "
            "at module top-level)"
        )

    def _warn_autoconfig_failed(self, reason: str) -> None:
        logger.warning(
            "django_bolt.nanodjango: could not auto-configure BOLT_API — %s. "
            "Pass module=__name__ to BoltAPI() or set settings.BOLT_API manually.",
            reason,
        )

    def _route_decorator(self, *args: Any, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super()._route_decorator(*args, **kwargs)

    def _websocket_decorator(self, *args: Any, **kwargs: Any) -> Any:
        self._configure_bolt_api()
        return super()._websocket_decorator(*args, **kwargs)


# ---------------------------------------------------------------------------
# Plugin hooks - auto-loaded by nanodjango via setuptools entry point
# ---------------------------------------------------------------------------


@hookimpl
def django_pre_setup(app: Django) -> None:
    """
    Add django_bolt to INSTALLED_APPS when the package is installed.

    This covers the edge case where django_bolt is installed but BoltAPI()
    is never instantiated (e.g. the app only uses runbolt management command
    without registering routes via this wrapper).
    """
    from django.conf import settings  # noqa: PLC0415

    if not defer.is_installed("django_bolt"):
        return

    if "django_bolt" not in settings.INSTALLED_APPS:
        settings.INSTALLED_APPS = list(settings.INSTALLED_APPS) + ["django_bolt"]


@hookimpl
def convert_build_settings(converter: Converter, resolver: Resolver, settings_ast: ast.AST) -> None:
    """
    Add ``django_bolt`` to INSTALLED_APPS in the generated settings.py.
    """
    for node in settings_ast.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "INSTALLED_APPS" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            already_present = any(
                isinstance(elt, ast.Constant) and elt.value == "django_bolt" for elt in node.value.elts
            )
            if not already_present:
                node.value.elts.append(ast.Constant(value="django_bolt"))
            break


def _is_bolt_assignment(node: ast.AST, api_objs: set[str], resolver: Resolver) -> bool:
    """Detect ``bolt = BoltAPI(...)`` or ``bolt = something.BoltAPI(...)``."""
    if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
        return False
    func = node.value.func
    if isinstance(func, ast.Name):
        func_name = func.id
    elif isinstance(func, ast.Attribute):
        func_name = func.attr
    else:
        return False
    if func_name != "BoltAPI":
        return False
    for target in node.targets:
        if isinstance(target, ast.Name):
            api_objs.add(target.id)
            resolver.add_object(target.id)
    return True


def _is_bolt_decorated_function(node: ast.AST, api_objs: set[str], resolver: Resolver) -> bool:
    """Detect ``@bolt.get(...)`` / ``@bolt.websocket(...)`` etc. on a function def."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for decorator in get_decorators(node):
        if isinstance(decorator, ast.Call):
            decorator = decorator.func
        if (
            isinstance(decorator, ast.Attribute)
            and isinstance(decorator.value, ast.Name)
            and decorator.value.id in api_objs
            and decorator.attr in _BOLT_DECORATOR_NAMES
        ):
            resolver.add_object(node.name)
            return True
    return False


def _is_bolt_mount_call(node: ast.AST, api_objs: set[str]) -> bool:
    """Detect ``bolt.mount_django(...)`` or ``bolt.mount(...)`` expression statements."""
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in api_objs
        and func.attr in _BOLT_MOUNT_NAMES
    )


@hookimpl
def convert_build_app_api(converter: Converter, resolver: Resolver, extra_src: list[str]) -> tuple[Resolver, list[str]]:
    """
    During ``nanodjango convert``, move BoltAPI instances and their route
    handlers into ``app/api.py``.

    Detects:
    - ``bolt = BoltAPI(...)``  (from django_bolt.nanodjango or django_bolt)
    - ``bolt = django_bolt.nanodjango.BoltAPI(...)``
    - ``@bolt.get(...)``, ``@bolt.post(...)``, etc. on async/sync functions
    - ``bolt.mount_django(...)`` and ``bolt.mount(...)`` calls
    """
    api_objs: set[str] = set()

    for node in converter.ast.body:
        is_bolt = (
            _is_bolt_assignment(node, api_objs, resolver)
            or _is_bolt_decorated_function(node, api_objs, resolver)
            or _is_bolt_mount_call(node, api_objs)
        )
        if is_bolt:
            extra_src.append(ast.unparse(node))
            resolver.add_references(collect_references(node))

    return resolver, extra_src
