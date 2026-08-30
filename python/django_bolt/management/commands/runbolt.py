from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import importlib.metadata
import importlib.util
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.template.autoreload import get_template_directories
from django.utils.autoreload import iter_all_python_module_files

from django_bolt import _core
from django_bolt._bridge import make_bound_dispatch
from django_bolt.api import BoltAPI, _validate_asgi_mount_conflicts, _validate_mcp_mount_conflicts, serve_with_lifespan
from django_bolt.workers import MIB, WorkerSupervisor

try:
    from django_bolt.logging.config import setup_django_logging
    from django_bolt.responses import initialize_file_response_settings
except ImportError:
    setup_django_logging = None
    initialize_file_response_settings = None

try:
    from django_bolt.admin.admin_detection import detect_admin_url_prefix
except ImportError:
    detect_admin_url_prefix = None


_ENV_DEV_WORKER = "DJANGO_BOLT_DEV_WORKER"
_ENV_DEV_RELOAD_COUNT = "DJANGO_BOLT_DEV_RELOAD_COUNT"
_ENV_DEV_SPAWN_MS = "DJANGO_BOLT_DEV_SPAWN_UNIX_MS"
DEV_RELOAD_DEBOUNCE_MS = 50


class _Ansi:
    """Minimal ANSI palette; every attribute is '' when colors are disabled."""

    __slots__ = ("bold", "dim", "cyan", "green", "reset")

    def __init__(self, enabled: bool) -> None:
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.cyan = "\033[36m" if enabled else ""
        self.green = "\033[32m" if enabled else ""
        self.reset = "\033[0m" if enabled else ""


_GLYPHS_UNICODE = {"bolt": "⚡ ", "arrow": "➜", "check": "✓", "sep": " · "}
_GLYPHS_ASCII = {"bolt": "", "arrow": "->", "check": "*", "sep": " - "}


def _pick_glyphs(stream) -> dict[str, str]:
    """Unicode glyphs when the output encoding supports them, ASCII otherwise."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "⚡➜✓·".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return _GLYPHS_ASCII
    return _GLYPHS_UNICODE


def _format_elapsed(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.1f} s"


DEV_RELOAD_IGNORE_DIRS = (
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".tox",
    ".venv",
    ".vscode",
    ".nox",
    ".cache",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "node_modules",
    "target",
)

_DEV_BROWSER_HOST = "127.0.0.1"
_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0"})


def _get_dev_reload_count() -> int:
    try:
        return int(os.environ.get(_ENV_DEV_RELOAD_COUNT, "0"))
    except ValueError:
        return 0


def _is_dev_reload_restart() -> bool:
    return os.environ.get(_ENV_DEV_WORKER) == "1" and _get_dev_reload_count() > 0


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _collapse_watch_paths(paths: set[Path]) -> list[Path]:
    collapsed: list[Path] = []

    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        if any(_path_within(path, existing) for existing in collapsed):
            continue
        collapsed.append(path)

    return collapsed


_UNSET = object()


def _dev_worker_main_module(main_module=_UNSET) -> str | None:
    """Return the ``-m`` module the process was started with, or ``None``.

    ``python -m pkg.manage`` rewrites ``sys.argv[0]`` to manage.py's resolved
    file path, so the ``-m`` context survives only on ``__main__.__spec__``.
    Plain ``python manage.py`` runs leave ``__spec__`` as ``None`` (some
    environments omit the attribute entirely), which means script mode.
    """
    if main_module is _UNSET:
        main_module = sys.modules.get("__main__")

    spec = getattr(main_module, "__spec__", None)
    if spec is None:
        return None

    name = getattr(spec, "name", None) or ""
    parent = getattr(spec, "parent", None) or ""

    # `python -m pkg` executes pkg/__main__.py; respawn the package, not the file.
    if (name == "__main__" or name.endswith(".__main__")) and parent:
        return parent

    return name or None


def _build_dev_worker_command(
    argv: list[str] | None = None,
    executable: str | None = None,
    main_module: str | None = None,
) -> list[str]:
    """Rebuild this process's command line for the dev worker.

    ``main_module`` is the ``-m`` module name from
    :func:`_dev_worker_main_module` (``None`` for script invocations). It must be
    preserved: re-running a ``-m``-launched manage.py as a script puts the
    script's own directory at the head of ``sys.path`` instead of the cwd, so a
    project whose manage.py lives inside the package it imports from (a "src
    layout", ``src/manage.py`` with ``src.project.settings``) can no longer
    import its own settings module.
    """
    argv = sys.argv if argv is None else argv
    executable = sys.executable if executable is None else executable

    command = [executable, "-m", main_module] if main_module else [executable, *argv[:1]]
    saw_processes = False
    it = iter(argv[1:])

    for arg in it:
        if arg == "--dev" or arg.startswith("--dev="):
            continue

        if arg == "--processes":
            next(it, None)
            saw_processes = True
            command.extend(["--processes", "1"])
            continue

        if arg.startswith("--processes="):
            saw_processes = True
            command.append("--processes=1")
            continue

        command.append(arg)

    if not saw_processes:
        command.extend(["--processes", "1"])

    return command


def _coerce_path(value) -> Path | None:
    try:
        return Path(os.fspath(value)).resolve()
    except (TypeError, ValueError, OSError):
        return None


def _venv_prefix_paths() -> set[Path]:
    paths: set[Path] = set()
    for prefix in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        if not prefix:
            continue
        path = _coerce_path(prefix)
        if path is not None and path.exists():
            paths.add(path)
    return paths


def _collect_loaded_python_paths() -> set[Path]:
    paths: set[Path] = set()
    prefix_roots = _venv_prefix_paths()

    for module_path in iter_all_python_module_files():
        path = _coerce_path(module_path)
        if path is None or not path.exists():
            continue

        if any(_path_within(path, prefix_root) for prefix_root in prefix_roots):
            continue

        paths.add(path)

    return paths


def _module_spec_watch_paths(module_name: str) -> set[Path]:
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return set()

    if spec is None:
        return set()

    watch_paths: set[Path] = set()

    if spec.submodule_search_locations:
        for search_location in spec.submodule_search_locations:
            path = _coerce_path(search_location)
            if path is not None and path.exists():
                watch_paths.add(path)
        return watch_paths

    path = _coerce_path(spec.origin)
    if path is not None and path.exists():
        watch_paths.add(path)

    return watch_paths


def _collect_autodiscovery_python_paths() -> set[Path]:
    module_names: set[str] = set()

    for api_path in getattr(settings, "BOLT_API", None) or ():
        if ":" in api_path:
            module_names.add(api_path.split(":", 1)[0])

    settings_module = getattr(settings, "SETTINGS_MODULE", None)
    if settings_module:
        module_names.add(settings_module)

    root_urlconf = getattr(settings, "ROOT_URLCONF", None)
    if root_urlconf:
        module_names.add(root_urlconf)
        module_names.update(_project_api_module_names(root_urlconf))

    if apps.ready:
        for app_config in apps.get_app_configs():
            if app_config.name == "django_bolt":
                continue

            if hasattr(app_config, "bolt_api") and ":" in app_config.bolt_api:
                module_names.add(app_config.bolt_api.split(":", 1)[0])
            else:
                module_names.add(f"{app_config.name}.api")
                module_names.add(f"{app_config.name}.bolt_api")

    project_paths: set[Path] = set()
    for module_name in module_names:
        project_paths.update(_module_spec_watch_paths(module_name))

    manage_py = Path.cwd().resolve() / "manage.py"
    if manage_py.exists():
        project_paths.add(manage_py)

    return project_paths


def _autodiscovery_import_module_names() -> list[str]:
    """Module names that autodiscover_apis() will import at worker startup.

    Explicit BOLT_API entries win, mirroring autodiscovery. Otherwise the
    project-level and per-app api module candidates are gated by the same AST
    scan autodiscovery uses, so modules without a BoltAPI assignment are never
    imported here either.
    """
    bolt_api_paths = getattr(settings, "BOLT_API", None)
    if bolt_api_paths:
        return [api_path.split(":", 1)[0] for api_path in bolt_api_paths if ":" in api_path]

    module_names: list[str] = []

    root_urlconf = getattr(settings, "ROOT_URLCONF", None)
    if root_urlconf:
        for module_name in _project_api_module_names(root_urlconf):
            if find_bolt_api_names(module_name):
                module_names.append(module_name)

    if apps.ready:
        for app_config in apps.get_app_configs():
            if app_config.name == "django_bolt":
                continue
            if hasattr(app_config, "bolt_api") and ":" in app_config.bolt_api:
                module_names.append(app_config.bolt_api.split(":", 1)[0])
                continue
            for module_suffix in ("api", "bolt_api"):
                module_name = f"{app_config.name}.{module_suffix}"
                if find_bolt_api_names(module_name):
                    module_names.append(module_name)

    return module_names


def _prime_dev_watch_imports() -> None:
    """Import discovered API modules so their transitive imports get watched.

    The dev supervisor never runs autodiscovery itself, so sys.modules only
    holds what django.setup() loaded — without these imports the watch list
    misses everything api.py imports (helpers, service layers) and edits to
    those files never trigger a reload. Import failures are non-fatal: the
    worker surfaces the real error on its own startup, and the api module file
    itself stays watched (via its module spec) so fixing it triggers a reload.
    """
    for module_name in _autodiscovery_import_module_names():
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - user code can raise anything at import
            print(
                f"[bolt] Warning: failed to import {module_name!r} while building the "
                f"dev watch list ({exc}); files it imports won't trigger auto-reload",
                file=sys.stderr,
            )


def _collect_dev_watch_paths(reload_dirs: list[str] | None = None) -> list[str]:
    _prime_dev_watch_imports()

    watch_paths: set[Path] = set(_collect_loaded_python_paths())
    watch_paths.update(_collect_autodiscovery_python_paths())

    for template_dir in get_template_directories():
        path = _coerce_path(template_dir)
        if path is not None and path.exists():
            watch_paths.add(path)

    # Escape hatch for code outside both the project root and the import
    # graph (e.g. a shared library next to the project).
    for reload_dir in reload_dirs or ():
        path = _coerce_path(reload_dir)
        if path is not None and path.exists():
            watch_paths.add(path)
        else:
            print(
                f"[bolt] Warning: --reload-dir {str(reload_dir)!r} "
                "does not exist; it will not be watched for auto-reload",
                file=sys.stderr,
            )

    # Uvicorn-style default: always watch the project root recursively so
    # newly created modules and packages reload without a dev-server restart.
    # Import-graph entries inside the root collapse into it; entries outside
    # (PYTHONPATH apps, editable installs) stay individually watched.
    project_root = _coerce_path(getattr(settings, "BASE_DIR", None))
    if project_root is None or not project_root.exists():
        project_root = Path.cwd().resolve()
    watch_paths.add(project_root)

    return [str(path) for path in _collapse_watch_paths(watch_paths)]


def _collect_dev_ignore_paths() -> list[str]:
    return [str(path) for path in sorted(_venv_prefix_paths(), key=str)]


def _display_host(host: str, *, dev_mode: bool) -> str:
    """Return the browser-friendly host to show in banner URLs."""
    if dev_mode and host in _WILDCARD_BIND_HOSTS:
        return _DEV_BROWSER_HOST
    return host


def _build_display_url(host: str, port: int, *, dev_mode: bool, path: str = "") -> str:
    """Build a user-facing URL for startup banner output."""
    return f"http://{_display_host(host, dev_mode=dev_mode)}:{port}{path}"


def _is_django_bolt_attribute(node: ast.Attribute) -> bool:
    """Check if an attribute access chain refers to django_bolt (e.g. django_bolt.api.BoltAPI)."""
    parts: list[str] = [node.attr]
    current = node.value
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    # Reconstruct dotted name (reversed) and check for django_bolt prefix
    dotted = ".".join(reversed(parts))
    return dotted.startswith("django_bolt.")


def find_bolt_api_names(module_name: str) -> list[str]:
    """Use AST to find variable names assigned to BoltAPI() calls in a module.

    Parses the module source without importing it, so no side effects are triggered.
    Handles ``BoltAPI()``, import aliases (``from django_bolt.api import BoltAPI as X``),
    and attribute-style calls (``django_bolt.api.BoltAPI()``).
    Only top-level assignments are considered.
    """
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        return []

    try:
        with open(spec.origin) as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Track which local names map to BoltAPI via imports from django_bolt.
    # Only populated from actual django_bolt imports to avoid false positives
    # from other libraries that happen to define a class named BoltAPI.
    bolt_api_aliases: set[str] = set()
    # Track module aliases for `import django_bolt.api as bolt` style imports
    # so that `bolt.BoltAPI()` is recognised via the attribute-style check.
    module_aliases: set[str] = set()

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "django_bolt" in node.module:
            for alias in node.names:
                if alias.name == "BoltAPI":
                    bolt_api_aliases.add(alias.asname or alias.name)
                elif alias.name == "*":
                    # `from django_bolt.api import *` brings BoltAPI into scope
                    bolt_api_aliases.add("BoltAPI")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "django_bolt" in alias.name and alias.asname:
                    # `import django_bolt.api as bolt` → track the alias
                    module_aliases.add(alias.asname)

    # Find top-level assignments where RHS is a call to a BoltAPI alias
    names: list[str] = []

    for node in ast.iter_child_nodes(tree):
        call_node = None
        targets: list[str] = []

        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call_node = node.value
            for target in node.targets:
                if isinstance(target, ast.Name):
                    targets.append(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value and isinstance(node.value, ast.Call):
            call_node = node.value
            if isinstance(node.target, ast.Name):
                targets.append(node.target.id)

        if call_node is None:
            continue

        func = call_node.func
        is_bolt = (isinstance(func, ast.Name) and func.id in bolt_api_aliases) or (
            isinstance(func, ast.Attribute)
            and func.attr == "BoltAPI"
            and (
                _is_django_bolt_attribute(func)
                # Handle `import django_bolt.api as bolt; app = bolt.BoltAPI()`
                or (isinstance(func.value, ast.Name) and func.value.id in module_aliases)
            )
        )

        if is_bolt:
            names.extend(targets)

    return names


def _project_api_module_names(root_urlconf: str) -> list[str]:
    """Return project-level API module candidates for a Django URL config."""
    project_name = root_urlconf.split(".")[0]
    package_module_names = [f"{project_name}.{suffix}" for suffix in ("api", "bolt_api")]

    # Dotted URLConfs already imply a containing project package.
    if "." in root_urlconf:
        return package_module_names

    spec = importlib.util.find_spec(root_urlconf)

    # A bare ROOT_URLCONF like "urls" can refer to either:
    # - a package (`urls/__init__.py`), where the project API lives at `urls.api`
    # - a plain module (`urls.py`), where the sibling API lives at top-level `api`
    if spec is not None and spec.submodule_search_locations is None:
        return ["api", "bolt_api"]

    return package_module_names


class Command(BaseCommand):
    help = "Run Django-Bolt server with autodiscovered APIs"

    # Like runserver: handle() runs the checks itself, at the right point.
    # The dev supervisor skips them; each spawned dev worker runs them.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--skip-checks", action="store_true", help="Skip system checks.")
        parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
        parser.add_argument("--port", type=int, default=8000, help="Port to bind to (default: 8000)")
        parser.add_argument("--processes", type=int, default=1, help="Number of processes (default: 1)")
        parser.add_argument(
            "--no-admin",
            action="store_true",
            help="Disable Django admin integration (admin enabled by default)",
        )
        parser.add_argument("--dev", action="store_true", help="Enable auto-reload on file changes (development mode)")
        parser.add_argument(
            "--reload-dir",
            action="append",
            default=[],
            dest="reload_dirs",
            metavar="PATH",
            help=(
                "Watch this extra directory (or file) for --dev auto-reload; "
                "repeatable. The project root and the app's import graph are "
                "watched by default, so this is only needed for code outside "
                "both, e.g. a shared library next to the project."
            ),
        )
        parser.add_argument("--backlog", type=int, default=1024, help="Socket listen backlog size (default: 1024)")
        parser.add_argument(
            "--keep-alive", type=int, default=None, help="HTTP keep-alive timeout in seconds (default: OS setting)"
        )
        parser.add_argument(
            "--max-rss",
            type=int,
            default=0,
            help=(
                "Recycle a worker once its resident memory exceeds this many MiB "
                "(0 = disabled). Guards long-running servers against slow Python "
                "memory leaks; recycling is graceful (spawn-first, WebSockets "
                "closed with 1012 Service Restart)."
            ),
        )
        parser.add_argument(
            "--workers-lifetime",
            type=int,
            default=0,
            help="Recycle each worker after this many seconds of uptime (0 = disabled)",
        )
        parser.add_argument(
            "--respawn-failed-workers",
            action="store_true",
            help="Respawn workers that exit unexpectedly instead of letting the fleet shrink",
        )
        parser.add_argument(
            "--workers-kill-timeout",
            type=int,
            default=30,
            help=(
                "Seconds a worker gets to shut down gracefully (finish in-flight "
                "requests, close WebSockets) before it is SIGKILLed (default: 30)"
            ),
        )

    @staticmethod
    def _supervision_requested(options) -> bool:
        """True when any worker-recycling option is enabled."""
        return bool(options["max_rss"] or options["workers_lifetime"] or options["respawn_failed_workers"])

    def handle(self, *args, **options):
        self._handle_started = time.monotonic()
        processes = options["processes"]
        dev_mode = options.get("dev", False)
        dev_worker_mode = os.environ.get(_ENV_DEV_WORKER) == "1"
        effective_dev_mode = dev_mode or dev_worker_mode

        if options["reload_dirs"] and not effective_dev_mode:
            self.stdout.write(self.style.WARNING("  Warning: --reload-dir is ignored without --dev"))

        # Dev mode: force single process + enable auto-reload
        if dev_mode and not dev_worker_mode:
            if processes > 1:
                self.stdout.write(self.style.WARNING("  Warning: dev mode forces --processes=1 for auto-reload"))
                options["processes"] = 1
            if self._supervision_requested(options):
                self.stdout.write(self.style.WARNING("  Warning: worker recycling options are ignored in dev mode"))

            self.run_with_autoreload(options)
        else:
            # Mirror runserver: run the checks before the port is bound.
            # Multi-process mode runs them once here, before workers fork.
            # In dev mode each spawned worker gets here, so a reload runs
            # the checks again, like runserver's autoreload.
            self.run_startup_checks(options)

            # Production mode. Worker recycling needs the supervising parent
            # even for a single worker, so route through start_multiprocess.
            if processes > 1 or (self._supervision_requested(options) and not dev_worker_mode):
                self.start_multiprocess(options)
            else:
                self.start_single_process(options, dev_mode=effective_dev_mode)

    def run_startup_checks(self, options) -> None:
        """Run Django's system checks and the unapplied-migration check.

        A check error raises SystemCheckError, which stops startup with a
        non-zero exit code, like runserver.
        """
        if not options.get("skip_checks"):
            self.check(display_num_errors=True)
        self.check_migrations()
        # Close the connections that the migration check opened. Handlers
        # and forked workers must open their own.
        for connection in connections.all(initialized_only=True):
            connection.close()

    def run_with_autoreload(self, options):
        """Run the server behind the native dev supervisor."""
        worker_command = _build_dev_worker_command(main_module=_dev_worker_main_module())
        watch_paths = _collect_dev_watch_paths(options["reload_dirs"])
        ignore_paths = _collect_dev_ignore_paths()
        force_polling = getattr(settings, "BOLT_DEV_FORCE_POLLING", False)

        exit_code = _core.run_dev_reloader(
            worker_command,
            watch_paths,
            list(DEV_RELOAD_IGNORE_DIRS),
            ignore_paths,
            DEV_RELOAD_DEBOUNCE_MS,
            force_polling,
        )

        if exit_code:
            sys.exit(exit_code)

    def start_multiprocess(self, options):
        """Start worker processes with SO_REUSEPORT under a supervisor.

        Prints the startup banner once from the parent process, then forks
        children that each run start_single_process (which skips the banner
        when process_id is set). The parent supervises the workers: it
        recycles them on --max-rss / --workers-lifetime limits (spawn-first,
        graceful drain), respawns crashed workers when
        --respawn-failed-workers is set, and coordinates graceful shutdown
        on SIGTERM/SIGINT.
        """
        if not hasattr(os, "fork"):
            raise CommandError(
                "--processes > 1 and worker recycling require os.fork(), which is unavailable on this platform"
            )

        processes = options["processes"]
        kill_timeout = options["workers_kill_timeout"]

        # Run autodiscovery + banner once in the parent before forking so the
        # user sees a single clean banner instead of N copies.
        self._print_multiprocess_banner(options)

        # Give Actix the same graceful-shutdown window the supervisor allows
        # before escalating to SIGKILL (user-set env takes precedence).
        os.environ.setdefault("DJANGO_BOLT_SHUTDOWN_TIMEOUT", str(kill_timeout))

        def spawn_worker(slot: int) -> int:
            pid = os.fork()
            if pid != 0:
                return pid
            # Child: reset inherited handlers so only the supervising parent
            # reacts to terminal signals; the Rust server installs its own
            # SIGTERM/SIGINT handling for graceful drain.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            # Always enable SO_REUSEPORT: during recycling the replacement
            # worker binds the port while the old worker is still draining.
            os.environ["DJANGO_BOLT_REUSE_PORT"] = "1"
            os.environ["DJANGO_BOLT_PROCESS_ID"] = str(slot)
            exit_code = 0
            try:
                self.start_single_process(options, process_id=slot)
            except BaseException:
                traceback.print_exc()
                exit_code = 1
            finally:
                os._exit(exit_code)
            return 0  # unreachable: os._exit never returns

        supervisor = WorkerSupervisor(
            processes=processes,
            spawn=spawn_worker,
            max_rss_bytes=options["max_rss"] * MIB if options["max_rss"] else None,
            lifetime_seconds=float(options["workers_lifetime"]) if options["workers_lifetime"] else None,
            respawn_failed=options["respawn_failed_workers"],
            kill_timeout=float(kill_timeout),
            log=self._runtime_log,
        )

        def signal_handler(signum, frame):
            supervisor.request_shutdown()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        supervisor.run()

    def _print_multiprocess_banner(self, options):
        """Perform a dry-run of autodiscovery to collect route/feature info,
        then print the startup banner once from the parent process."""
        # Setup logging/file-response just like start_single_process
        if setup_django_logging is not None:
            setup_django_logging()
        if initialize_file_response_settings is not None:
            initialize_file_response_settings()
        banner_base_url = _build_display_url(options["host"], options["port"], dev_mode=False)

        apis = self.autodiscover_apis()
        if not apis:
            self.stdout.write(
                self.style.WARNING(
                    "No BoltAPI instances found. Create api.py files with a top-level BoltAPI() assignment, e.g., api = BoltAPI()"
                )
            )
            return

        merged_api = self.merge_apis(apis)
        _user_route_count = len(merged_api._routes)

        features: list[tuple[str, str]] = []

        # Check OpenAPI
        openapi_config = None
        for _api_path, api in apis:
            if api._openapi_config:
                openapi_config = api._openapi_config
                if openapi_config.enabled:
                    merged_api._openapi_config = openapi_config
                    merged_api._register_openapi_routes()
                    features.append(("Docs", f"{banner_base_url}{openapi_config.path}"))
                break

        # Check admin
        admin_enabled = not options.get("no_admin", False)
        if admin_enabled and detect_admin_url_prefix is not None:
            merged_api._register_admin_routes(options["host"], options["port"])
            if merged_api._admin_routes_registered:
                admin_prefix = detect_admin_url_prefix() or "admin"
                features.append(("Admin", f"{banner_base_url}/{admin_prefix}/"))

        _total_route_count = len(merged_api._routes)
        _framework_route_count = _total_route_count - _user_route_count

        ws_count = len(merged_api._websocket_routes)
        asgi_count = len(merged_api._asgi_mounts)
        middleware_count = len(merged_api._handler_middleware)

        # Check compression
        compression_config = None
        if hasattr(settings, "BOLT_COMPRESSION"):
            if settings.BOLT_COMPRESSION is not None and settings.BOLT_COMPRESSION is not False:
                compression_config = settings.BOLT_COMPRESSION.to_rust_config()
        else:
            for _api_path, api in apis:
                if api._compression is not None:
                    compression_config = api._compression.to_rust_config()
                    break

        if compression_config is not None:
            features.append(("Compression", "enabled"))
        if middleware_count:
            features.append(("Middleware", f"{middleware_count} handlers"))

        recycling_bits = []
        if options["max_rss"]:
            recycling_bits.append(f"max-rss {options['max_rss']}MiB")
        if options["workers_lifetime"]:
            recycling_bits.append(f"lifetime {options['workers_lifetime']}s")
        if options["respawn_failed_workers"]:
            recycling_bits.append("respawn-on-failure")
        if recycling_bits:
            features.append(("Recycling", ", ".join(recycling_bits)))

        self._print_startup_banner(
            options=options,
            dev_mode=False,
            api_routes=_user_route_count,
            framework_routes=_framework_route_count,
            ws_routes=ws_count,
            asgi_mounts=asgi_count,
            api_count=len(apis),
            features=features,
        )

    def start_single_process(self, options, process_id=None, dev_mode=False):
        """Start a single process server"""
        is_dev_reload_restart = _is_dev_reload_restart()

        # Setup Django logging once at server startup (one-shot, respects existing LOGGING)
        if setup_django_logging is not None:
            setup_django_logging()

        # Initialize FileResponse settings cache once at server startup
        if initialize_file_response_settings is not None:
            initialize_file_response_settings()

        # Autodiscover BoltAPI instances
        apis = self.autodiscover_apis()

        if not apis:
            self.stdout.write(
                self.style.WARNING(
                    "No BoltAPI instances found. Create api.py files with a top-level BoltAPI() assignment"
                )
            )
            return

        # Merge all APIs and collect routes FIRST
        merged_api = self.merge_apis(apis)

        # Snapshot user route count before framework routes are added
        _user_route_count = len(merged_api._routes)

        # --- Collect startup info for banner ---
        features: list[tuple[str, str]] = []
        banner_base_url = _build_display_url(options["host"], options["port"], dev_mode=dev_mode)

        # Register OpenAPI routes AFTER merging (so schema includes all routes)
        openapi_enabled = False
        openapi_config = None

        # Find first API with OpenAPI config (project-level API takes priority)
        # Respect enabled=False from the first API - don't let other APIs override it
        for _api_path, api in apis:
            if api._openapi_config:
                openapi_config = api._openapi_config
                openapi_enabled = api._openapi_config.enabled
                break

        # Register OpenAPI routes on merged API if any API had OpenAPI enabled
        if openapi_enabled and openapi_config:
            # Transfer OpenAPI config to merged API
            merged_api._openapi_config = openapi_config
            merged_api._register_openapi_routes()
            features.append(("Docs", f"{banner_base_url}{openapi_config.path}"))

        # Register Django admin routes if not disabled
        # Admin is controlled solely by --no-admin command-line flag
        admin_enabled = not options.get("no_admin", False)

        if admin_enabled and detect_admin_url_prefix is not None:
            # Register admin routes
            merged_api._register_admin_routes(options["host"], options["port"])

            if merged_api._admin_routes_registered:
                admin_prefix = detect_admin_url_prefix() or "admin"
                features.append(("Admin", f"{banner_base_url}/{admin_prefix}/"))

        # Compute route counts
        _total_route_count = len(merged_api._routes)
        _framework_route_count = _total_route_count - _user_route_count

        # Validate ASGI mount conflicts after all framework/admin/docs routes are added.
        self.validate_asgi_mount_conflicts(merged_api._routes, merged_api._asgi_mounts)
        self.validate_mcp_mount_conflicts(
            merged_api._routes,
            merged_api._asgi_mounts,
            merged_api._mcp_mounts,
        )

        # Register routes with Rust. Each route carries per-route bound
        # dispatch callables (partial binding handler + handler_id) so the
        # per-request Rust→Python call passes ONLY the request object —
        # single-arg vectorcall instead of a 3-tuple of conversions.
        rust_routes = []
        for method, path, handler_id, handler in merged_api._routes:
            # Ensure matchit path syntax
            convert = getattr(merged_api, "_convert_path", None)
            norm_path = convert(path) if callable(convert) else path
            rust_routes.append(
                (
                    method,
                    norm_path,
                    handler_id,
                    handler,
                    make_bound_dispatch(merged_api._dispatch, handler, handler_id),
                    make_bound_dispatch(merged_api._dispatch_sync, handler, handler_id),
                )
            )

        _core.register_routes(rust_routes)

        # Register HTTP ASGI mounts with Rust
        if merged_api._asgi_mounts:
            _core.register_asgi_mounts(merged_api._asgi_mounts)

        # Register MCP mounts with Rust (rmcp protocol core)
        if merged_api._mcp_mounts:
            _core.register_mcp_mounts(merged_api._mcp_mounts)

        # Register WebSocket routes with Rust (including pre-compiled injectors)
        ws_routes = []
        for path, handler_id, handler in merged_api._websocket_routes:
            convert = getattr(merged_api, "_convert_path", None)
            norm_path = convert(path) if callable(convert) else path
            # Get pre-compiled injector from handler metadata (handler_id is already in the tuple)
            meta = merged_api._handler_meta.get(handler_id, {})
            injector = meta.get("injector")
            ws_routes.append((norm_path, handler_id, handler, injector))

        if ws_routes:
            _core.register_websocket_routes(ws_routes)

        # Register middleware metadata if present
        middleware_count = 0
        if merged_api._handler_middleware:
            middleware_data = [(handler_id, meta) for handler_id, meta in merged_api._handler_middleware.items()]
            _core.register_middleware_metadata(middleware_data)
            middleware_count = len(middleware_data)

        # Set environment variables for Rust
        os.environ["DJANGO_BOLT_WORKERS"] = "1"
        os.environ["DJANGO_BOLT_BACKLOG"] = str(options["backlog"])
        if options.get("keep_alive") is not None:
            os.environ["DJANGO_BOLT_KEEP_ALIVE"] = str(options["keep_alive"])

        # Determine compression config (server-level in Actix)
        # Priority: Django setting > first API with compression config
        compression_config = None
        if hasattr(settings, "BOLT_COMPRESSION"):
            # Use Django setting if provided (highest priority)
            if settings.BOLT_COMPRESSION is not None and settings.BOLT_COMPRESSION is not False:
                compression_config = settings.BOLT_COMPRESSION.to_rust_config()
        else:
            # Check if any API has compression configured
            for _api_path, api in apis:
                if api._compression is not None:
                    compression_config = api._compression.to_rust_config()
                    break

        if compression_config is not None:
            features.append(("Compression", "enabled"))

        if middleware_count:
            features.append(("Middleware", f"{middleware_count} handlers"))

        # Register authentication backends for user resolution (request.user loading)
        # CRITICAL: Must be called BEFORE starting server so backends are available for user loading
        merged_api._register_auth_backends()

        # Print structured startup banner (only for main process or single-process mode)
        if process_id is None:
            if is_dev_reload_restart:
                c = self._ansi()
                g = _pick_glyphs(self.stdout)
                elapsed = self._startup_elapsed_ms()
                timing = f" {c.dim}in {_format_elapsed(elapsed)}{c.reset}" if elapsed is not None else ""
                self.stdout.write(
                    f"  {c.green}{g['check']}{c.reset} {c.bold}Reloaded{c.reset}{timing}"
                    f"  {c.cyan}{banner_base_url}{c.reset}"
                )
            else:
                self._print_startup_banner(
                    options=options,
                    dev_mode=dev_mode,
                    api_routes=_user_route_count,
                    framework_routes=_framework_route_count,
                    ws_routes=len(ws_routes),
                    asgi_mounts=len(merged_api._asgi_mounts),
                    api_count=len(apis),
                    features=features,
                )

        # Collect lifecycle contexts (merged APIs store them, single APIs check directly)
        source_lifespans = merged_api._source_lifespans
        if source_lifespans is None and merged_api._has_lifespan:
            source_lifespans = [(merged_api, merged_api._lifespan_context)]

        # Start the server (all handlers go through async dispatch with thread pool for sync)
        if source_lifespans:

            def server_fn():
                _core.start_server(
                    merged_api._dispatch,
                    options["host"],
                    options["port"],
                    compression_config,
                )

            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(serve_with_lifespan(source_lifespans, server_fn))
        else:
            _core.start_server(
                merged_api._dispatch,
                options["host"],
                options["port"],
                compression_config,
            )

    # ------------------------------------------------------------------
    # Startup banner
    # ------------------------------------------------------------------

    @staticmethod
    def _get_version() -> str:
        """Return the installed django-bolt version."""
        try:
            return importlib.metadata.version("django-bolt")
        except importlib.metadata.PackageNotFoundError:
            return "dev"

    def _ansi(self) -> _Ansi:
        """Palette derived from Django's own color handling (--no-color, --force-color)."""
        cached = getattr(self, "_ansi_cache", None)
        if cached is None:
            enabled = self.style.SUCCESS("x") != "x" and "NO_COLOR" not in os.environ
            cached = self._ansi_cache = _Ansi(enabled)
        return cached

    def _startup_elapsed_ms(self) -> float | None:
        """Wall time since the process was spawned (dev worker) or handle() began."""
        spawn_ms = os.environ.get(_ENV_DEV_SPAWN_MS)
        if spawn_ms:
            try:
                elapsed = time.time() * 1000.0 - float(spawn_ms)
            except ValueError:
                elapsed = -1.0
            # Guard against clock skew between the supervisor and worker.
            if 0 <= elapsed < 600_000:
                return elapsed
        started = getattr(self, "_handle_started", None)
        if started is None:
            return None
        return (time.monotonic() - started) * 1000.0

    def _runtime_log(self, message: str) -> None:
        """Timestamped post-startup log line (worker recycling, shutdown, ...)."""
        c = self._ansi()
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.stdout.write(f"  {c.dim}{timestamp}{c.reset} {c.cyan}[bolt]{c.reset} {message}")

    def _print_startup_banner(
        self,
        *,
        options: dict,
        dev_mode: bool,
        api_routes: int,
        framework_routes: int,
        ws_routes: int,
        asgi_mounts: int,
        api_count: int,
        features: list[tuple[str, str]],
    ) -> None:
        """Print a Vite-style startup banner: brand + timing, URL rows, dim meta rows."""
        version = self._get_version()
        c = self._ansi()
        g = _pick_glyphs(self.stdout)
        sep = g["sep"]
        processes = options["processes"]
        url = _build_display_url(options["host"], options["port"], dev_mode=dev_mode)

        # Split features into URL rows (arrow lines) and meta rows (dim lines).
        links: list[tuple[str, str]] = [("Local", url)]
        meta_rows: list[tuple[str, str]] = []
        for label, value in features:
            if value.startswith("http"):
                links.append((label, value))
            else:
                meta_rows.append((label, value))

        mode = "development" if dev_mode else "production"
        if dev_mode:
            mode += f"{sep}auto-reload"

        route_bits = [f"{api_routes} API"]
        if ws_routes:
            route_bits.append(f"{ws_routes} WebSocket")
        if asgi_mounts:
            route_bits.append(f"{asgi_mounts} ASGI")
        if framework_routes:
            route_bits.append(f"+{framework_routes} framework")
        route_bits.append(f"{api_count} app{'s' if api_count != 1 else ''}")

        meta_rows = [
            ("Mode", mode),
            ("Routes", sep.join(route_bits)),
            ("Workers", f"{processes} process{'es' if processes != 1 else ''}"),
            *meta_rows,
        ]

        header = f"  {g['bolt']}{c.bold}{c.cyan}Django Bolt{c.reset} {c.cyan}v{version}{c.reset}"
        elapsed = self._startup_elapsed_ms()
        if elapsed is not None:
            header += f"  {c.dim}ready in{c.reset} {c.bold}{_format_elapsed(elapsed)}{c.reset}"

        lines: list[str] = ["", header, ""]

        link_w = max(len(label) for label, _ in links) + 2
        for label, value in links:
            lines.append(
                f"  {c.green}{g['arrow']}{c.reset}  {c.bold}{label + ':':<{link_w}}{c.reset}{c.cyan}{value}{c.reset}"
            )
        lines.append("")

        meta_w = max(len(label) for label, _ in meta_rows) + 4
        for label, value in meta_rows:
            lines.append(f"  {c.dim}{label:<{meta_w}}{c.reset}{value}")
        lines.append("")

        lines.append(f"  {c.dim}press CTRL+C to stop{c.reset}")

        # OutputWrapper adds the final newline; the extra "" keeps one blank
        # line between the banner and whatever the app logs next.
        lines.append("")
        self.stdout.write("\n".join(lines) + "\n")

    def autodiscover_apis(self):
        """Discover BoltAPI instances from installed apps.

        Deduplicates by object identity to ensure each handler uses the FIRST
        API instance created (with correct config), not duplicates from re-imports.
        """
        apis = []

        # Check explicit settings first
        if hasattr(settings, "BOLT_API"):
            for api_path in settings.BOLT_API:
                api = self.import_api(api_path, required=True)
                if api:
                    apis.append((api_path, api))
            return self._deduplicate_apis(apis)

        # Try project-level API first (common pattern)
        # Autodiscovery takes the first BoltAPI instance found.
        # Sub-APIs intended for mounting (e.g. files_api mounted via
        # api.mount("/files", files_api)) must not be discovered as
        # standalone instances.  Place the primary BoltAPI assignment
        # before any sub-API assignments in your api.py file.
        project_found = False

        for module_name in _project_api_module_names(settings.ROOT_URLCONF):
            if project_found:
                break
            attr_names = find_bolt_api_names(module_name)
            for attr_name in attr_names:
                candidate = f"{module_name}:{attr_name}"
                api = self.import_api(candidate)
                if api:
                    apis.append((candidate, api))
                    project_found = True
                    break

        # Track which apps we've already imported (to avoid duplicates)
        imported_apps = {api_path.split(":")[0].split(".")[0] for api_path, _ in apis}

        # Autodiscover from installed apps
        for app_config in apps.get_app_configs():
            # Skip django_bolt itself
            if app_config.name == "django_bolt":
                continue

            # Skip if we already imported this app at project level
            app_base = app_config.name.split(".")[0]
            if app_base in imported_apps:
                continue

            # Check if app config has bolt_api hint
            if hasattr(app_config, "bolt_api"):
                api = self.import_api(app_config.bolt_api)
                if api:
                    apis.append((app_config.bolt_api, api))
                continue

            # Try standard locations using AST discovery
            app_name = app_config.name
            found = False
            for module_suffix in ("api", "bolt_api"):
                if found:
                    break
                module_name = f"{app_name}.{module_suffix}"
                attr_names = find_bolt_api_names(module_name)
                for attr_name in attr_names:
                    candidate = f"{module_name}:{attr_name}"
                    api = self.import_api(candidate)
                    if api:
                        apis.append((candidate, api))
                        found = True
                        break  # First BoltAPI per module

        return self._deduplicate_apis(apis)

    def _deduplicate_apis(self, apis):
        """Deduplicate APIs by object identity.

        This ensures each handler uses the FIRST API instance created (with original
        config), not duplicates from module re-imports. Critical for preserving
        per-API logging, auth, and middleware configs.
        """
        seen_ids = set()
        deduplicated = []
        for api_path, api in apis:
            api_id = id(api)
            if api_id not in seen_ids:
                seen_ids.add(api_id)
                deduplicated.append((api_path, api))
            else:
                self.stdout.write(f"  Skipped duplicate API: {api_path}")
        return deduplicated

    def import_api(self, dotted_path, *, required: bool = False):
        """Import a BoltAPI instance from dotted path like 'myapp.api:api'"""
        if ":" not in dotted_path:
            if required:
                raise CommandError(f"BOLT_API entry {dotted_path!r} could not be imported: expected 'module:attr' path")
            return None

        module_path, attr_name = dotted_path.split(":", 1)

        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as e:
            # Check if the error is for the target module itself (optional)
            # or for a dependency within the module (fatal error)
            if e.name == module_path or e.name == module_path.split(".")[0]:
                if required:
                    raise CommandError(f"BOLT_API entry {dotted_path!r} could not be imported: {e}") from e
                # Target module doesn't exist - this is fine, api.py is optional
                return None
            else:
                # Module exists but has a missing dependency - let the original error bubble up
                # This preserves the full traceback for debugging
                raise

        # If attribute doesn't exist, return None (not an error, just doesn't have that attr)
        if not hasattr(module, attr_name):
            if required:
                raise CommandError(
                    f"BOLT_API entry {dotted_path!r} could not be imported: "
                    f"module {module_path!r} has no attribute {attr_name!r}"
                )
            return None

        api = getattr(module, attr_name)

        # Verify it's a BoltAPI instance
        if isinstance(api, BoltAPI):
            return api

        if required:
            raise CommandError(
                f"BOLT_API entry {dotted_path!r} could not be imported: "
                f"{module_path!r}.{attr_name} is not a BoltAPI instance"
            )

        return None

    def merge_apis(self, apis):
        """Merge multiple BoltAPI instances into one, preserving per-API context.

        Uses Litestar-style approach: each handler maintains reference to its original
        API instance, allowing it to use that API's logging, auth, and middleware config.
        """
        if len(apis) == 1:
            return apis[0][1]  # Return the single API

        # Create a new merged API without logging (handlers will use their original APIs)
        merged = BoltAPI(enable_logging=False)
        # Preserve trailing_slash from first API (routes are pre-normalized by their original API)
        merged.trailing_slash = apis[0][1].trailing_slash
        route_map = {}  # Track conflicts

        # Map handler_id -> original API instance (preserves per-API context)
        merged._handler_api_map = {}

        # Track next available handler_id to avoid collisions
        next_handler_id = 0

        for api_path, api in apis:
            for method, path, old_handler_id, handler in api._routes:
                # Keep routes with their original trailing slash (based on each API's setting)
                # Redirect-on-mismatch handles both URLs at runtime (Starlette-style)
                route_key = f"{method} {path}"

                if route_key in route_map:
                    raise CommandError(
                        f"Route conflict: {route_key} defined in both {route_map[route_key]} and {api_path}"
                    )

                # CRITICAL: Assign NEW unique handler_id to avoid collisions
                # Each API starts handler_ids at 0, so we must renumber during merge
                new_handler_id = next_handler_id
                next_handler_id += 1

                route_map[route_key] = api_path
                merged._routes.append((method, path, new_handler_id, handler))
                merged._handlers[new_handler_id] = handler

                # CRITICAL: Store reference to original API for this handler
                # For mounted sub-apps, preserve the sub-app reference (has its own middleware)
                # Otherwise use the API that owns this route
                if hasattr(api, "_handler_api_map") and old_handler_id in api._handler_api_map:
                    # This route was mounted from a sub-app - preserve that reference
                    merged._handler_api_map[new_handler_id] = api._handler_api_map[old_handler_id]
                else:
                    # Route belongs directly to this API
                    merged._handler_api_map[new_handler_id] = api

                # Merge handler metadata (use handler_id as key, old_handler_id is already in the tuple)
                if old_handler_id in api._handler_meta:
                    merged._handler_meta[new_handler_id] = api._handler_meta[old_handler_id]

                # Merge middleware metadata (use NEW handler_id)
                if old_handler_id in api._handler_middleware:
                    merged._handler_middleware[new_handler_id] = api._handler_middleware[old_handler_id]

            # Merge WebSocket routes from this API
            for path, old_ws_handler_id, ws_handler in api._websocket_routes:
                # Keep routes with their original trailing slash (based on each API's setting)
                ws_route_key = f"WS {path}"

                if ws_route_key in route_map:
                    raise CommandError(
                        f"WebSocket route conflict: {ws_route_key} defined in both "
                        f"{route_map[ws_route_key]} and {api_path}"
                    )

                # Assign new unique handler_id for WebSocket route
                new_ws_handler_id = next_handler_id
                next_handler_id += 1

                route_map[ws_route_key] = api_path
                merged._websocket_routes.append((path, new_ws_handler_id, ws_handler))
                merged._handlers[new_ws_handler_id] = ws_handler

                # Store reference to original API (or sub-app for mounted routes)
                if hasattr(api, "_handler_api_map") and old_ws_handler_id in api._handler_api_map:
                    merged._handler_api_map[new_ws_handler_id] = api._handler_api_map[old_ws_handler_id]
                else:
                    merged._handler_api_map[new_ws_handler_id] = api

                # Merge handler metadata for WebSocket (use handler_id as key, old_ws_handler_id is already in the tuple)
                if old_ws_handler_id in api._handler_meta:
                    merged._handler_meta[new_ws_handler_id] = api._handler_meta[old_ws_handler_id]

                # Merge middleware metadata for WebSocket
                if old_ws_handler_id in api._handler_middleware:
                    merged._handler_middleware[new_ws_handler_id] = api._handler_middleware[old_ws_handler_id]

            # Merge HTTP ASGI mounts from this API
            for asgi_prefix, asgi_app in getattr(api, "_asgi_mounts", []):
                asgi_key = f"ASGI {asgi_prefix}"
                if asgi_key in route_map:
                    raise CommandError(
                        f"ASGI mount conflict: {asgi_prefix} defined in both {route_map[asgi_key]} and {api_path}"
                    )

                route_map[asgi_key] = api_path
                merged._asgi_mounts.append((asgi_prefix, asgi_app))

            # Merge MCP mounts from this API
            for mcp_mount in getattr(api, "_mcp_mounts", []):
                mcp_key = f"MCP {mcp_mount['path']}"
                if mcp_key in route_map:
                    raise CommandError(
                        f"MCP mount conflict: {mcp_mount['path']} defined in both {route_map[mcp_key]} and {api_path}"
                    )
                route_map[mcp_key] = api_path
                merged._mcp_mounts.append(mcp_mount)

        _validate_mcp_mount_conflicts(
            merged._routes,
            merged._asgi_mounts,
            merged._mcp_mounts,
            error_cls=CommandError,
        )

        # Update next handler ID
        merged._next_handler_id = next_handler_id

        # Collect lifecycle contexts from source APIs
        merged._source_lifespans = [(api, api._lifespan_context) for _, api in apis if api._has_lifespan]

        return merged

    def validate_asgi_mount_conflicts(self, routes, asgi_mounts):
        """Validate exact-path conflicts for ASGI mounts."""
        _validate_asgi_mount_conflicts(routes, asgi_mounts, error_cls=CommandError)

    def validate_mcp_mount_conflicts(self, routes, asgi_mounts, mcp_mounts):
        """Validate MCP paths against routes and ASGI mounts."""
        _validate_mcp_mount_conflicts(routes, asgi_mounts, mcp_mounts, error_cls=CommandError)
