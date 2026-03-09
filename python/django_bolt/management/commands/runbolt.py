from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.metadata
import importlib.util
import os
import signal
import sys

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from django_bolt import _core
from django_bolt.api import BoltAPI, _validate_asgi_mount_conflicts

try:
    from django.utils import autoreload
except ImportError:
    autoreload = None

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
                if "django_bolt" in alias.name:
                    # `import django_bolt.api as bolt` → track the alias
                    if alias.asname:
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


class Command(BaseCommand):
    help = "Run Django-Bolt server with autodiscovered APIs"

    def add_arguments(self, parser):
        parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
        parser.add_argument("--port", type=int, default=8000, help="Port to bind to (default: 8000)")
        parser.add_argument("--processes", type=int, default=1, help="Number of processes (default: 1)")
        parser.add_argument(
            "--no-admin",
            action="store_true",
            help="Disable Django admin integration (admin enabled by default)",
        )
        parser.add_argument("--dev", action="store_true", help="Enable auto-reload on file changes (development mode)")
        parser.add_argument("--backlog", type=int, default=1024, help="Socket listen backlog size (default: 1024)")
        parser.add_argument(
            "--keep-alive", type=int, default=None, help="HTTP keep-alive timeout in seconds (default: OS setting)"
        )

    def handle(self, *args, **options):
        processes = options["processes"]
        dev_mode = options.get("dev", False)

        # Dev mode: force single process + enable auto-reload
        if dev_mode:
            if processes > 1:
                self.stdout.write(self.style.WARNING("  Warning: dev mode forces --processes=1 for auto-reload"))
                options["processes"] = 1

            self.run_with_autoreload(options)
        else:
            # Production mode (current logic)
            if processes > 1:
                self.start_multiprocess(options)
            else:
                self.start_single_process(options)

    def run_with_autoreload(self, options):
        """Run server with auto-reload using Django's autoreload system"""
        if autoreload is None:
            self.stdout.write(
                self.style.ERROR("  Error: Django autoreload not available. Upgrade Django or use --no-dev mode.")
            )
            sys.exit(1)

        # Use Django's autoreload system which is optimized
        # It only restarts the Python interpreter when necessary
        # and reuses the same process for faster reloads
        def run_server():
            self.start_single_process(options, dev_mode=True)

        autoreload.run_with_reloader(run_server)

    def start_multiprocess(self, options):
        """Start multiple processes with SO_REUSEPORT.

        Prints the startup banner once from the parent process, then forks
        children that each run start_single_process (which skips the banner
        when process_id is set).
        """
        processes = options["processes"]

        # Run autodiscovery + banner once in the parent before forking so the
        # user sees a single clean banner instead of N copies.
        self._print_multiprocess_banner(options)

        # Store child PIDs for cleanup
        child_pids = []

        def signal_handler(signum, frame):
            self.stdout.write("\n  Shutting down...")
            for pid in child_pids:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
            # Wait for children to exit before the parent exits
            for pid in list(child_pids):
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Fork processes
        for i in range(processes):
            pid = os.fork()
            if pid == 0:
                # Child: reset signals to default so only the parent prints
                # shutdown messages and manages the process group.
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.environ["DJANGO_BOLT_REUSE_PORT"] = "1"
                os.environ["DJANGO_BOLT_PROCESS_ID"] = str(i)
                self.start_single_process(options, process_id=i)
                os._exit(0)
            else:
                # Parent process
                child_pids.append(pid)

        # Parent waits for children
        try:
            while True:
                pid, status = os.wait()
                if pid in child_pids:
                    child_pids.remove(pid)
                if not child_pids:
                    break
        except ChildProcessError:
            pass
        except KeyboardInterrupt:
            pass

    def _print_multiprocess_banner(self, options):
        """Perform a dry-run of autodiscovery to collect route/feature info,
        then print the startup banner once from the parent process."""
        # Setup logging/file-response just like start_single_process
        if setup_django_logging is not None:
            setup_django_logging()
        if initialize_file_response_settings is not None:
            initialize_file_response_settings()

        apis = self.autodiscover_apis()
        if not apis:
            self.stdout.write(
                self.style.WARNING("No BoltAPI instances found. Create api.py files with a top-level BoltAPI() assignment, e.g., api = BoltAPI()")
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
                    features.append(("OpenAPI", f"http://{options['host']}:{options['port']}{openapi_config.path}"))
                break

        # Check admin
        admin_enabled = not options.get("no_admin", False)
        if admin_enabled and detect_admin_url_prefix is not None:
            merged_api._register_admin_routes(options["host"], options["port"])
            if merged_api._admin_routes_registered:
                admin_prefix = detect_admin_url_prefix() or "admin"
                features.append(("Admin", f"http://{options['host']}:{options['port']}/{admin_prefix}/"))
                merged_api._register_static_routes()

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
                self.style.WARNING("No BoltAPI instances found. Create api.py files with a top-level BoltAPI() assignment")
            )
            return

        # Merge all APIs and collect routes FIRST
        merged_api = self.merge_apis(apis)

        # Snapshot user route count before framework routes are added
        _user_route_count = len(merged_api._routes)

        # --- Collect startup info for banner ---
        features: list[tuple[str, str]] = []

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
            features.append(("OpenAPI", f"http://{options['host']}:{options['port']}{openapi_config.path}"))

        # Register Django admin routes if not disabled
        # Admin is controlled solely by --no-admin command-line flag
        admin_enabled = not options.get("no_admin", False)
        admin_prefix = None

        if admin_enabled and detect_admin_url_prefix is not None:
            # Register admin routes
            merged_api._register_admin_routes(options["host"], options["port"])

            if merged_api._admin_routes_registered:
                admin_prefix = detect_admin_url_prefix() or "admin"
                features.append(("Admin", f"http://{options['host']}:{options['port']}/{admin_prefix}/"))

                # Also register static file routes for admin
                merged_api._register_static_routes()

        # Compute route counts
        _total_route_count = len(merged_api._routes)
        _framework_route_count = _total_route_count - _user_route_count

        # Validate ASGI mount conflicts after all framework/admin/docs routes are added.
        self.validate_asgi_mount_conflicts(merged_api._routes, merged_api._asgi_mounts)

        # Register routes with Rust
        rust_routes = []
        for method, path, handler_id, handler in merged_api._routes:
            # Ensure matchit path syntax
            convert = getattr(merged_api, "_convert_path", None)
            norm_path = convert(path) if callable(convert) else path
            rust_routes.append((method, norm_path, handler_id, handler))

        _core.register_routes(rust_routes)

        # Register HTTP ASGI mounts with Rust
        if merged_api._asgi_mounts:
            _core.register_asgi_mounts(merged_api._asgi_mounts)

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

        # Start the server (all handlers go through async dispatch with thread pool for sync)
        _core.start_server(
            merged_api._dispatch,
            options["host"],
            options["port"],
            compression_config,
            merged_api._dispatch_sync,
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
        """Print a clean, structured startup banner."""
        version = self._get_version()
        host = options["host"]
        port = options["port"]
        processes = options["processes"]
        mode = "development" if dev_mode else "production"
        url = f"http://{host}:{port}"

        # Determine label width for alignment
        _LABEL_W = 16

        def _line(label: str, value: str) -> str:
            return f"  {label:<{_LABEL_W}}{value}"

        lines: list[str] = []

        # Header
        lines.append("")
        lines.append(self.style.SUCCESS(f"  Django Bolt v{version}"))
        lines.append("")

        # Server section
        lines.append(self.style.MIGRATE_HEADING("  Server"))
        lines.append(_line("URL", url))
        lines.append(_line("Processes", str(processes)))
        lines.append(_line("Mode", mode))
        lines.append("")

        # Routes section
        lines.append(self.style.MIGRATE_HEADING("  Routes"))
        lines.append(_line("API", f"{api_routes} routes from {api_count} app{'s' if api_count != 1 else ''}"))
        if ws_routes:
            lines.append(_line("WebSocket", f"{ws_routes} routes"))
        if asgi_mounts:
            lines.append(_line("ASGI", f"{asgi_mounts} mounts"))
        if framework_routes:
            lines.append(_line("Framework", f"+{framework_routes} (admin, docs, static)"))
        lines.append("")

        # Features section (only if there are features)
        if features:
            lines.append(self.style.MIGRATE_HEADING("  Features"))
            for label, value in features:
                lines.append(_line(label, value))
            lines.append("")

        self.stdout.write("\n".join(lines))

    def autodiscover_apis(self):
        """Discover BoltAPI instances from installed apps.

        Deduplicates by object identity to ensure each handler uses the FIRST
        API instance created (with correct config), not duplicates from re-imports.
        """
        apis = []

        # Check explicit settings first
        if hasattr(settings, "BOLT_API"):
            for api_path in settings.BOLT_API:
                api = self.import_api(api_path)
                if api:
                    apis.append((api_path, api))
            return self._deduplicate_apis(apis)

        # Try project-level API first (common pattern)
        project_name = settings.ROOT_URLCONF.split(".")[0]  # Extract project name from ROOT_URLCONF

        # Autodiscovery takes the first BoltAPI instance found.
        # Sub-APIs intended for mounting (e.g. files_api mounted via
        # api.mount("/files", files_api)) must not be discovered as
        # standalone instances.  Place the primary BoltAPI assignment
        # before any sub-API assignments in your api.py file.
        # To register multiple BoltAPI instances explicitly, use the
        # BOLT_API setting instead.
        project_found = False
        for module_suffix in ("api", "bolt_api"):
            if project_found:
                break
            module_name = f"{project_name}.{module_suffix}"
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

    def import_api(self, dotted_path):
        """Import a BoltAPI instance from dotted path like 'myapp.api:api'"""
        if ":" not in dotted_path:
            return None

        module_path, attr_name = dotted_path.split(":", 1)

        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as e:
            # Check if the error is for the target module itself (optional)
            # or for a dependency within the module (fatal error)
            if e.name == module_path or e.name == module_path.split(".")[0]:
                # Target module doesn't exist - this is fine, api.py is optional
                return None
            else:
                # Module exists but has a missing dependency - let the original error bubble up
                # This preserves the full traceback for debugging
                raise

        # If attribute doesn't exist, return None (not an error, just doesn't have that attr)
        if not hasattr(module, attr_name):
            return None

        api = getattr(module, attr_name)

        # Verify it's a BoltAPI instance
        if isinstance(api, BoltAPI):
            return api

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

        # Update next handler ID
        merged._next_handler_id = next_handler_id

        return merged

    def validate_asgi_mount_conflicts(self, routes, asgi_mounts):
        """Validate exact-path conflicts for ASGI mounts."""
        _validate_asgi_mount_conflicts(routes, asgi_mounts, error_cls=CommandError)
