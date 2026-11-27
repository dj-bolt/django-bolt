from __future__ import annotations

import asyncio
import gc
import importlib
import os
import sys
from types import SimpleNamespace

import pytest
from bolt_mcp import MCP, mount_mcp
from django.core.management import call_command, execute_from_command_line
from django.core.management.base import CommandError

from django_bolt import BoltAPI, WebSocket
from django_bolt.auth import APIKeyAuthentication, IsAuthenticated
from django_bolt.management.commands import runbolt as runbolt_module
from django_bolt.middleware import Middleware
from django_bolt.testing import TestClient, WebSocketTestClient


def _clear_modules(monkeypatch, *module_names: str) -> None:
    for module_name in module_names:
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    importlib.invalidate_caches()


def test_build_dev_worker_command_removes_dev_and_forces_single_process():
    command = runbolt_module._build_dev_worker_command(
        [
            "manage.py",
            "--settings=testproject.settings",
            "runbolt",
            "--dev",
            "--host",
            "127.0.0.1",
            "--processes=4",
        ],
        executable="/usr/bin/python3",
    )

    assert command == [
        "/usr/bin/python3",
        "manage.py",
        "--settings=testproject.settings",
        "runbolt",
        "--host",
        "127.0.0.1",
        "--processes=1",
    ]


def test_build_dev_worker_command_preserves_module_invocation():
    """``python -m src.manage`` must respawn as ``-m src.manage``, not as a script.

    CPython rewrites ``sys.argv[0]`` to manage.py's resolved path for ``-m``
    runs, so rebuilding the command from argv alone re-runs it in script mode.
    Script mode puts manage.py's own directory at the front of ``sys.path``
    instead of the cwd, so a project whose manage.py lives inside the package it
    imports from (``src/manage.py`` with ``src.project.settings``) can no longer
    import its own settings — the worker dies with ModuleNotFoundError.
    """
    command = runbolt_module._build_dev_worker_command(
        ["/abs/backend/src/manage.py", "runbolt", "--dev", "--host", "127.0.0.1"],
        executable="/usr/bin/python3",
        main_module="src.manage",
    )

    assert command == [
        "/usr/bin/python3",
        "-m",
        "src.manage",
        "runbolt",
        "--host",
        "127.0.0.1",
        "--processes",
        "1",
    ]


def test_dev_worker_main_module_reports_dash_m_module():
    spec = SimpleNamespace(name="src.manage", parent="src")
    main_module = SimpleNamespace(__spec__=spec)

    assert runbolt_module._dev_worker_main_module(main_module) == "src.manage"


def test_dev_worker_main_module_unwraps_package_dunder_main():
    """``python -m src`` runs ``src/__main__.py``; respawn the package, not the file."""
    spec = SimpleNamespace(name="src.__main__", parent="src")
    main_module = SimpleNamespace(__spec__=spec)

    assert runbolt_module._dev_worker_main_module(main_module) == "src"


def test_dev_worker_main_module_is_none_for_script_invocation():
    # `python manage.py` leaves __spec__ as None; some environments (Conda)
    # omit the attribute entirely.
    assert runbolt_module._dev_worker_main_module(SimpleNamespace(__spec__=None)) is None
    assert runbolt_module._dev_worker_main_module(SimpleNamespace()) is None
    assert runbolt_module._dev_worker_main_module(None) is None


def test_display_host_uses_loopback_for_dev_wildcard_hosts():
    assert runbolt_module._display_host("0.0.0.0", dev_mode=True) == "127.0.0.1"
    assert runbolt_module._display_host("::", dev_mode=True) == "127.0.0.1"


def test_build_display_url_preserves_bind_host_outside_dev_mode():
    assert runbolt_module._build_display_url("0.0.0.0", 8000, dev_mode=False) == "http://0.0.0.0:8000"
    assert runbolt_module._build_display_url("0.0.0.0", 8000, dev_mode=True, path="/admin/") == (
        "http://127.0.0.1:8000/admin/"
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--port", "65536"], "--port must be at most 65535, got 65536"),
        (["--port", "-1"], "--port must be at least 0, got -1"),
        (["--processes", "0"], "--processes must be at least 1, got 0"),
        (["--workers", "0"], "--workers must be at least 1, got 0"),
        (["--workers", "513"], "--workers must be at most 512, got 513"),
        (["--backlog", "-1"], "--backlog must be at least 0, got -1"),
        (["--keep-alive", "-1"], "--keep-alive must be at least 0, got -1"),
        (["--max-rss", "-1"], "--max-rss must be at least 0, got -1"),
        (["--workers-lifetime", "-1"], "--workers-lifetime must be at least 0, got -1"),
        (["--workers-kill-timeout", "-1"], "--workers-kill-timeout must be at least 0, got -1"),
    ],
)
def test_runbolt_stops_for_an_option_value_out_of_range(argv, message):
    with pytest.raises(CommandError) as error:
        call_command("runbolt", *argv)

    assert str(error.value) == message


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--port", "65535", "--backlog", "-1"], "--backlog must be at least 0, got -1"),
        (["--workers", "512", "--backlog", "-1"], "--backlog must be at least 0, got -1"),
    ],
)
def test_runbolt_accepts_the_highest_value_of_an_option(argv, message):
    """The highest value of a range is a permitted value.

    The second option is out of range, so the command stops before it starts
    a server. Its check comes after the check of the first option. Thus its
    message shows that the first option passed.
    """
    with pytest.raises(CommandError) as error:
        call_command("runbolt", *argv)

    assert str(error.value) == message


def test_collapse_watch_paths_deduplicates_nested_directories(tmp_path):
    root = tmp_path / "project"
    nested = root / "app"
    sibling = tmp_path / "templates"
    nested.mkdir(parents=True)
    sibling.mkdir()

    collapsed = runbolt_module._collapse_watch_paths({root, nested, sibling})

    assert collapsed == [root, sibling]


def test_collect_dev_watch_paths_collapses_root_and_keeps_external_paths(settings, tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    templates_root = tmp_path / "templates"
    static_root = tmp_path / "static"
    external_app = tmp_path / "shared_app"
    settings_module = project_root / "settings.py"
    urls_module = project_root / "urls.py"

    for path in (project_root, templates_root, static_root, external_app):
        path.mkdir(parents=True, exist_ok=True)

    settings_module.write_text("DEBUG = True\n")
    urls_module.write_text("urlpatterns = []\n")

    monkeypatch.chdir(project_root)
    monkeypatch.setattr(
        runbolt_module,
        "_collect_loaded_python_paths",
        lambda: {settings_module, urls_module},
    )
    monkeypatch.setattr(
        runbolt_module,
        "_collect_autodiscovery_python_paths",
        lambda: {external_app / "api.py"},
    )
    monkeypatch.setattr(
        runbolt_module,
        "get_template_directories",
        lambda: {templates_root},
    )

    settings.BASE_DIR = project_root

    watch_paths = set(runbolt_module._collect_dev_watch_paths())

    # Python files inside the project root collapse into the recursive root
    # watch; paths outside the root stay individually watched.
    assert str(project_root) in watch_paths
    assert str(settings_module) not in watch_paths
    assert str(urls_module) not in watch_paths
    assert str(templates_root) in watch_paths
    assert str(external_app / "api.py") in watch_paths
    assert str(static_root) not in watch_paths


def test_execute_from_command_line_dev_calls_reloader_with_debounce(monkeypatch):
    recorded = {}
    argv = [
        "manage.py",
        "runbolt",
        "--dev",
        "--host",
        "127.0.0.1",
        "--processes",
        "4",
        "--reload-dir",
        "/tmp/plugins",
    ]

    def fake_run_dev_reloader(command, watch_paths, ignore_dir_names, ignore_paths, debounce_ms, force_polling):
        recorded["command"] = command
        recorded["watch_paths"] = watch_paths
        recorded["ignore_dir_names"] = ignore_dir_names
        recorded["ignore_paths"] = ignore_paths
        recorded["debounce_ms"] = debounce_ms
        recorded["force_polling"] = force_polling
        return 0

    monkeypatch.setattr(sys, "argv", argv)
    # Script mode, as `python manage.py`: no `-m` spec on __main__. Under
    # `python -m pytest`, the real __main__ has the spec of pytest.
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__spec__=None))

    def fake_collect_dev_watch_paths(reload_dirs):
        recorded["reload_dirs"] = reload_dirs
        return ["/tmp/project"]

    monkeypatch.setattr(runbolt_module, "_collect_dev_watch_paths", fake_collect_dev_watch_paths)
    monkeypatch.setattr(runbolt_module, "_collect_dev_ignore_paths", lambda: ["/tmp/venv"])
    monkeypatch.setattr(
        runbolt_module,
        "_core",
        SimpleNamespace(run_dev_reloader=fake_run_dev_reloader),
    )

    execute_from_command_line(argv)

    assert recorded == {
        "command": [
            sys.executable,
            "manage.py",
            "runbolt",
            "--host",
            "127.0.0.1",
            "--processes",
            "1",
            "--reload-dir",
            "/tmp/plugins",
        ],
        "reload_dirs": ["/tmp/plugins"],
        "watch_paths": ["/tmp/project"],
        "ignore_dir_names": list(runbolt_module.DEV_RELOAD_IGNORE_DIRS),
        "ignore_paths": ["/tmp/venv"],
        "debounce_ms": runbolt_module.DEV_RELOAD_DEBOUNCE_MS,
        "force_polling": False,
    }


@pytest.mark.parametrize(
    ("argv", "warnings"),
    [
        (["--processes", "4"], ["Warning: dev mode forces --processes=1 for auto-reload"]),
        (["--max-rss", "64"], ["Warning: worker recycling options are ignored in dev mode"]),
        (["--workers-lifetime", "60"], ["Warning: worker recycling options are ignored in dev mode"]),
        (["--respawn-failed-workers"], ["Warning: worker recycling options are ignored in dev mode"]),
        (["--processes", "1", "--reload-dir", "/tmp/plugins"], []),
    ],
)
def test_runbolt_dev_warns_about_the_options_that_it_ignores(monkeypatch, capsys, argv, warnings):
    monkeypatch.setattr(runbolt_module, "_collect_dev_watch_paths", lambda _reload_dirs: ["/tmp/project"])
    monkeypatch.setattr(runbolt_module, "_collect_dev_ignore_paths", lambda: ["/tmp/venv"])
    monkeypatch.setattr(runbolt_module, "_core", SimpleNamespace(run_dev_reloader=lambda *_args: 0))

    call_command("runbolt", "--dev", *argv)

    lines = [line.strip() for line in capsys.readouterr().out.splitlines() if "Warning" in line]
    assert lines == warnings


_LIFESPAN_API_SOURCE = """
from contextlib import asynccontextmanager

from django_bolt.api import BoltAPI

EVENTS = []


@asynccontextmanager
async def lifespan(app):
    EVENTS.append("startup")
    yield
    EVENTS.append("shutdown")


api = BoltAPI(lifespan=lifespan)


@api.get("/pong")
async def pong():
    return {}
"""


@pytest.mark.django_db
# runbolt calls setup_django_logging(), which starts a queue listener on the stderr of this test.
@pytest.mark.usefixtures("bolt_logging_state")
@pytest.mark.parametrize(
    ("argv", "backlog", "mounts", "warnings"),
    [
        ([], "1024", ["/admin"], []),
        (["--backlog", "77"], "77", ["/admin"], []),
        (["--no-admin"], "1024", [], []),
        (["--reload-dir", "/tmp/plugins"], "1024", ["/admin"], ["Warning: --reload-dir is ignored without --dev"]),
    ],
)
def test_runbolt_passes_its_options_to_the_server(
    settings, tmp_path, monkeypatch, capsys, argv, backlog, mounts, warnings
):
    (tmp_path / "options_api.py").write_text(
        "from django_bolt.api import BoltAPI\napi = BoltAPI()\n\n\n@api.get('/ping')\nasync def ping():\n    return {}\n"
    )
    (tmp_path / "options_lifespan_api.py").write_text(_LIFESPAN_API_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "options_api", "options_lifespan_api")
    settings.BOLT_API = ["options_api:api", "options_lifespan_api:api"]
    # runbolt writes these variables. monkeypatch puts the old values back after the test.
    for name in ("DJANGO_BOLT_WORKERS", "DJANGO_BOLT_BACKLOG", "DJANGO_BOLT_SHUTDOWN_TIMEOUT"):
        monkeypatch.setenv(name, "0")
    started = {"mounts": []}

    def fake_start_server(dispatch, host, port, compression, error_renderer):
        started["backlog"] = os.environ["DJANGO_BOLT_BACKLOG"]
        started["address"] = (host, port)
        sys.modules["options_lifespan_api"].EVENTS.append("serve")

    monkeypatch.setattr(
        runbolt_module,
        "_core",
        SimpleNamespace(
            register_routes=lambda routes, _native_routes, _no_route: started.update(
                paths=[route[1] for route in routes]
            ),
            register_asgi_mounts=lambda asgi_mounts: started.update(mounts=[prefix for prefix, _ in asgi_mounts]),
            register_middleware_metadata=lambda _metadata: None,
            start_server=fake_start_server,
        ),
    )

    frozen = gc.get_freeze_count()

    call_command("runbolt", "--host", "127.0.0.1", "--port", "8123", *argv)

    assert started["address"] == ("127.0.0.1", 8123)
    # Only a server that starts freezes the objects of its process: see ``django_bolt._gc``.
    # A frozen object stays for the life of the process, and this process runs more tests.
    assert gc.get_freeze_count() == frozen
    # The two APIs of BOLT_API are one server, and the lifespan of the second API runs around it.
    assert {"/ping", "/pong"} <= set(started["paths"])
    assert sys.modules["options_lifespan_api"].EVENTS == ["startup", "serve", "shutdown"]
    assert started["backlog"] == backlog
    assert started["mounts"] == mounts
    lines = [line.strip() for line in capsys.readouterr().out.splitlines() if "Warning" in line]
    assert lines == warnings


def test_collect_dev_watch_paths_includes_api_transitive_imports(settings, tmp_path, monkeypatch):
    """The dev supervisor must watch modules imported BY api.py, not just api.py.

    The supervisor never runs autodiscovery, so without priming imports the
    watch list only covers django.setup()'s import graph plus the api module
    file itself — editing a helper module api.py imports would never reload.
    """
    (tmp_path / "urls.py").write_text("urlpatterns = []\n")
    (tmp_path / "api.py").write_text(
        "from django_bolt.api import BoltAPI\nimport dev_watch_helper_a\napi = BoltAPI()\n"
    )
    helper = tmp_path / "dev_watch_helper_a.py"
    helper.write_text("VALUE = 1\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "urls", "api", "dev_watch_helper_a")
    monkeypatch.setattr(runbolt_module, "apps", SimpleNamespace(ready=True, get_app_configs=lambda: []))
    settings.ROOT_URLCONF = "urls"

    watch_paths = set(runbolt_module._collect_dev_watch_paths())

    assert str(helper.resolve()) in watch_paths


def test_collect_dev_watch_paths_includes_bolt_api_setting_imports(settings, tmp_path, monkeypatch):
    (tmp_path / "explicit_api.py").write_text(
        "from django_bolt.api import BoltAPI\nimport dev_watch_helper_b\napi = BoltAPI()\n"
    )
    helper = tmp_path / "dev_watch_helper_b.py"
    helper.write_text("VALUE = 1\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "explicit_api", "dev_watch_helper_b")
    monkeypatch.setattr(runbolt_module, "apps", SimpleNamespace(ready=True, get_app_configs=lambda: []))
    settings.BOLT_API = ["explicit_api:api"]

    watch_paths = set(runbolt_module._collect_dev_watch_paths())

    assert str(helper.resolve()) in watch_paths
    assert str((tmp_path / "explicit_api.py").resolve()) in watch_paths


def test_collect_dev_watch_paths_watches_project_root_by_default(settings, tmp_path, monkeypatch):
    """The project root (BASE_DIR) is always watched recursively, uvicorn-style,
    so newly created modules and packages reload without a dev-server restart."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside_module = tmp_path / "outside.py"
    outside_module.write_text("VALUE = 1\n")

    monkeypatch.chdir(project_root)
    monkeypatch.setattr(runbolt_module, "_prime_dev_watch_imports", lambda: None)
    monkeypatch.setattr(runbolt_module, "_collect_loaded_python_paths", lambda: {outside_module})
    monkeypatch.setattr(runbolt_module, "_collect_autodiscovery_python_paths", lambda: set())
    monkeypatch.setattr(runbolt_module, "get_template_directories", lambda: set())
    settings.BASE_DIR = project_root

    watch_paths = set(runbolt_module._collect_dev_watch_paths())

    assert str(project_root) in watch_paths
    assert str(outside_module) in watch_paths


def test_collect_dev_watch_paths_includes_reload_dirs(settings, tmp_path, monkeypatch, capsys):
    """--reload-dir entries append extra directories/files to the watch list
    (the escape hatch for code the import graph can't see, e.g. files created
    after startup). Missing entries warn instead of failing startup."""
    (tmp_path / "urls.py").write_text("urlpatterns = []\n")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    missing = tmp_path / "does_not_exist"

    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "urls")
    monkeypatch.setattr(runbolt_module, "apps", SimpleNamespace(ready=True, get_app_configs=lambda: []))
    settings.ROOT_URLCONF = "urls"

    watch_paths = set(runbolt_module._collect_dev_watch_paths([str(plugins), str(missing)]))

    assert str(plugins.resolve()) in watch_paths
    assert str(missing.resolve()) not in watch_paths
    assert "does_not_exist" in capsys.readouterr().err


def test_collect_dev_watch_paths_survives_broken_api_module(settings, tmp_path, monkeypatch, capsys):
    """A broken api.py must not kill the supervisor: the worker reports the
    real error, and the api module file itself stays watched so fixing it
    still triggers a reload."""
    (tmp_path / "urls.py").write_text("urlpatterns = []\n")
    api_module = tmp_path / "api.py"
    api_module.write_text("from django_bolt.api import BoltAPI\nraise RuntimeError('boom')\napi = BoltAPI()\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "urls", "api")
    monkeypatch.setattr(runbolt_module, "apps", SimpleNamespace(ready=True, get_app_configs=lambda: []))
    settings.ROOT_URLCONF = "urls"

    watch_paths = set(runbolt_module._collect_dev_watch_paths())

    assert str(api_module.resolve()) in watch_paths
    assert "boom" in capsys.readouterr().err


def test_autodiscover_apis_uses_top_level_api_for_plain_root_urlconf(settings, tmp_path, monkeypatch):
    (tmp_path / "urls.py").write_text("urlpatterns = []\n")
    (tmp_path / "api.py").write_text("from django_bolt.api import BoltAPI\napi = BoltAPI()\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "urls", "api", "urls.api")
    monkeypatch.setattr(runbolt_module, "apps", SimpleNamespace(get_app_configs=lambda: []))
    settings.ROOT_URLCONF = "urls"

    apis = runbolt_module.Command().autodiscover_apis()

    assert [api_path for api_path, _ in apis] == ["api:api"]


def test_autodiscover_apis_prefers_package_api_for_package_root_urlconf(settings, tmp_path, monkeypatch):
    urls_package = tmp_path / "urls"
    urls_package.mkdir()
    (urls_package / "__init__.py").write_text("urlpatterns = []\n")
    (urls_package / "api.py").write_text("from django_bolt.api import BoltAPI\napi = BoltAPI()\n")

    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "urls", "api", "urls.api")
    monkeypatch.setattr(runbolt_module, "apps", SimpleNamespace(get_app_configs=lambda: []))
    settings.ROOT_URLCONF = "urls"

    apis = runbolt_module.Command().autodiscover_apis()

    assert [api_path for api_path, _ in apis] == ["urls.api:api"]


def test_autodiscover_apis_finds_the_apis_of_installed_apps(settings, tmp_path, monkeypatch):
    def write_app(name: str, files: dict[str, str]) -> None:
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("")
        for file_name, source in files.items():
            (package / file_name).write_text("from django_bolt.api import BoltAPI\n" + source)

    write_app("discover_project", {"urls.py": "urlpatterns = []\n", "api.py": "api = BoltAPI()\n"})
    # The project API covers each app below the project package.
    (tmp_path / "discover_project" / "billing").mkdir()
    (tmp_path / "discover_project" / "billing" / "__init__.py").write_text("")
    (tmp_path / "discover_project" / "billing" / "api.py").write_text(
        "from django_bolt.api import BoltAPI\napi = BoltAPI()\n"
    )
    write_app(
        "discover_shop", {"api.py": "first = BoltAPI()\nsecond = BoltAPI()\n", "bolt_api.py": "api = BoltAPI()\n"}
    )
    write_app("discover_blog", {"bolt_api.py": "api = BoltAPI()\n"})
    write_app("discover_hinted", {"api.py": "api = BoltAPI()\n", "custom.py": "custom_api = BoltAPI()\n"})
    write_app("discover_plain", {"models.py": ""})

    monkeypatch.syspath_prepend(str(tmp_path))
    app_configs = [
        SimpleNamespace(name="django_bolt"),
        SimpleNamespace(name="discover_project.billing"),
        SimpleNamespace(name="discover_shop"),
        SimpleNamespace(name="discover_plain"),
        SimpleNamespace(name="discover_hinted", bolt_api="discover_hinted.custom:custom_api"),
        SimpleNamespace(name="discover_blog"),
    ]
    monkeypatch.setattr(runbolt_module, "apps", SimpleNamespace(get_app_configs=lambda: app_configs))
    settings.ROOT_URLCONF = "discover_project.urls"
    try:
        apis = runbolt_module.Command().autodiscover_apis()
    finally:
        _clear_modules(
            monkeypatch,
            *(name for name in list(sys.modules) if name.startswith("discover_")),
        )

    assert [api_path for api_path, _ in apis] == [
        "discover_project.api:api",
        "discover_shop.api:first",
        "discover_hinted.custom:custom_api",
        "discover_blog.bolt_api:api",
    ]


def test_merge_apis_serves_the_routes_of_each_api():
    first = BoltAPI()
    second = BoltAPI()

    @first.get("/first")
    async def first_route():
        return {"api": "first"}

    @second.post("/second", status_code=201)
    async def second_route(count: int):
        return {"api": "second", "count": count}

    @second.get("/private", auth=[APIKeyAuthentication(api_keys={"key1"})], guards=[IsAuthenticated()])
    async def private_route():
        return {"api": "second", "private": True}

    @second.websocket("/ws/echo")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("echo:" + await websocket.receive_text())

    async def asgi_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"mounted"})

    second.mount_asgi("/legacy", asgi_app)

    # One API needs no merge.
    assert runbolt_module.Command().merge_apis([("first.api", first)]) is first

    merged = runbolt_module.Command().merge_apis([("first.api", first), ("second.api", second)])

    with TestClient(merged, use_http_layer=True) as client:
        assert client.get("/first").json() == {"api": "first"}
        created = client.post("/second?count=3")
        assert (created.status_code, created.json()) == (201, {"api": "second", "count": 3})
        assert client.post("/second?count=x").status_code == 422
        assert client.get("/private").status_code == 401
        assert client.get("/private", headers={"X-API-Key": "key1"}).json() == {"api": "second", "private": True}
        assert client.get("/legacy/page").text == "mounted"

    async def talk() -> str:
        async with WebSocketTestClient(merged, "/ws/echo") as websocket:
            await websocket.send_text("hello")
            return await websocket.receive_text()

    assert asyncio.run(talk()) == "echo:hello"


def test_merge_apis_keeps_the_guards_of_a_websocket_route_and_the_handler_of_each_route():
    first = BoltAPI()
    second = BoltAPI()

    @first.get("/first")
    async def first_route():
        return {"api": "first"}

    @first.websocket("/ws/private", auth=[APIKeyAuthentication(api_keys={"key1"})], guards=[IsAuthenticated()])
    async def private_echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("private:" + await websocket.receive_text())

    @first.websocket("/ws/open")
    async def open_echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("open:" + await websocket.receive_text())

    # These routes come after the WebSocket routes of the first API.
    @second.get("/second")
    async def second_route():
        return {"api": "second"}

    @second.get("/third")
    async def third_route():
        return {"api": "third"}

    merged = runbolt_module.Command().merge_apis([("first.api", first), ("second.api", second)])

    async def talk(path: str, headers: dict[str, str] | None = None) -> str:
        async with WebSocketTestClient(merged, path, headers=headers) as websocket:
            await websocket.send_text("hello")
            return await websocket.receive_text()

    with pytest.raises(PermissionError):
        asyncio.run(talk("/ws/private"))
    assert asyncio.run(talk("/ws/private", {"X-API-Key": "key1"})) == "private:hello"
    assert asyncio.run(talk("/ws/open")) == "open:hello"
    with TestClient(merged) as client:
        assert client.get("/first").json() == {"api": "first"}
        assert client.get("/second").json() == {"api": "second"}
        assert client.get("/third").json() == {"api": "third"}


class _FirstApiMiddleware(Middleware):
    """Add the header ``x-api`` with the name of its API."""

    api_name = "first"

    async def process_request(self, request):
        response = await self.get_response(request)
        response.headers["x-api"] = self.api_name
        return response


class _SecondApiMiddleware(_FirstApiMiddleware):
    api_name = "second"


def test_merge_apis_runs_the_middleware_of_the_first_api_for_a_path_with_no_route():
    first = BoltAPI(middleware=[_FirstApiMiddleware])
    second = BoltAPI(middleware=[_SecondApiMiddleware])

    @first.get("/first")
    async def first_route():
        return {"api": "first"}

    @second.get("/second")
    async def second_route():
        return {"api": "second"}

    merged = runbolt_module.Command().merge_apis([("first.api", first), ("second.api", second)])

    with TestClient(merged) as client:
        missing = client.get("/no-route")
        second_response = client.get("/second")

    assert missing.status_code == 404
    assert missing.headers["x-api"] == "first"
    assert second_response.headers["x-api"] == "second"


def test_docs_routes_of_a_merged_api_run_the_middleware_given_at_registration():
    """The docs routes of a merged API run the middleware that ``_register_openapi_routes`` gets.

    This test makes the call itself. It does not cover the two call sites in runbolt.
    """
    first = BoltAPI(middleware=[_FirstApiMiddleware])
    second = BoltAPI(middleware=[_SecondApiMiddleware])

    @first.get("/first")
    async def first_route():
        return {"api": "first"}

    @second.get("/second")
    async def second_route():
        return {"api": "second"}

    merged = runbolt_module.Command().merge_apis([("first.api", first), ("second.api", second)])
    merged._register_openapi_routes(middleware=[_FirstApiMiddleware])

    with TestClient(merged) as client:
        docs = client.get("/docs/openapi.json")

    assert docs.status_code == 200
    assert docs.headers["x-api"] == "first"
    assert set(docs.json()["paths"]) == {"/first", "/second"}


def test_docs_routes_of_a_merged_api_use_the_trailing_slash_of_the_first_api():
    """The docs routes that runbolt adds to a merged API end with "/" when the first API appends one."""
    first = BoltAPI(trailing_slash="append")
    second = BoltAPI()

    @first.get("/first")
    async def first_route():
        return {"api": "first"}

    @second.get("/second")
    async def second_route():
        return {"api": "second"}

    merged = runbolt_module.Command().merge_apis([("first.api", first), ("second.api", second)])
    merged._register_openapi_routes()

    with TestClient(merged) as client:
        with_slash = client.get("/docs/openapi.json/")
        without_slash = client.get("/docs/openapi.json", follow_redirects=False)

    assert with_slash.status_code == 200
    assert set(with_slash.json()["paths"]) == {"/first/", "/second"}
    assert without_slash.status_code == 308
    assert without_slash.headers["location"] == "/docs/openapi.json/"


def _two_apis_and_their_merge() -> tuple[BoltAPI, BoltAPI, BoltAPI]:
    first = BoltAPI()
    second = BoltAPI()

    @first.get("/first")
    async def first_route():
        return {"api": "first"}

    @second.get("/second")
    async def second_route():
        return {"api": "second"}

    merged = runbolt_module.Command().merge_apis([("first.api", first), ("second.api", second)])
    return first, second, merged


def test_an_api_cannot_mount_a_merge_that_has_its_routes():
    first, second, merged = _two_apis_and_their_merge()

    for source in (first, second):
        with pytest.raises(ValueError, match="mount cycle"):
            source.mount("/merged", merged)
    assert [path for _method, path, _id, _handler in first._routes] == ["/first"]
    assert [path for _method, path, _id, _handler in second._routes] == ["/second"]


def test_an_unrelated_api_can_mount_a_merge():
    _first, _second, merged = _two_apis_and_their_merge()
    parent = BoltAPI()
    parent.mount("/merged", merged)

    with TestClient(parent) as client:
        assert client.get("/merged/first").json() == {"api": "first"}
        assert client.get("/merged/second").json() == {"api": "second"}


def _api_with_mcp(server_name: str, path: str) -> BoltAPI:
    api = BoltAPI()
    mount_mcp(api, MCP(server_name, "1.0"), path=path)
    return api


def test_merge_apis_serves_the_mcp_mount_of_each_api():
    merged = runbolt_module.Command().merge_apis(
        [
            ("first.api", _api_with_mcp("first-server", "/mcp")),
            ("second.api", _api_with_mcp("second-server", "/tools/mcp")),
        ]
    )

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    with TestClient(merged) as client:
        for path, server_name in (("/mcp", "first-server"), ("/tools/mcp", "second-server")):
            response = client.post(path, json=initialize, headers={"Accept": "application/json, text/event-stream"})
            assert response.status_code == 200
            assert f'"serverInfo":{{"name":"{server_name}","version":"1.0"}}' in response.text


def _conflict_route(api: BoltAPI) -> None:
    @api.get("/same")
    async def same():
        return {}


def _conflict_websocket(api: BoltAPI) -> None:
    @api.websocket("/ws/same")
    async def same(websocket: WebSocket):
        await websocket.accept()


async def _asgi_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _conflict_mcp_mounts(api: BoltAPI) -> None:
    mount_mcp(api, MCP("other", "1.0"), path="/other/mcp")
    mount_mcp(api, MCP("one", "1.0"))


def _conflict_mcp_route(api: BoltAPI) -> None:
    @api.post("/mcp")
    async def mcp_route():
        return {}


@pytest.mark.parametrize(
    ("add_first", "add_second", "message"),
    [
        (_conflict_route, _conflict_route, "Route conflict: GET /same defined in both first.api and second.api"),
        (
            _conflict_websocket,
            _conflict_websocket,
            "WebSocket route conflict: WS /ws/same defined in both first.api and second.api",
        ),
        (
            lambda api: api.mount_asgi("/legacy", _asgi_app),
            lambda api: api.mount_asgi("/legacy", _asgi_app),
            "ASGI mount conflict: /legacy defined in both first.api and second.api",
        ),
        (
            _conflict_mcp_mounts,
            lambda api: mount_mcp(api, MCP("two", "1.0")),
            "MCP mount conflict: /mcp defined in both first.api and second.api",
        ),
        # An MCP mount of one API on the path of a route of another API.
        (
            _conflict_mcp_route,
            lambda api: mount_mcp(api, MCP("two", "1.0")),
            "MCP mount path '/mcp' conflicts with an existing HTTP route",
        ),
    ],
    ids=["route", "websocket", "asgi-mount", "mcp-mount", "mcp-mount-on-a-route"],
)
def test_merge_apis_stops_for_one_path_in_two_apis(add_first, add_second, message):
    first = BoltAPI()
    second = BoltAPI()
    # The first API also has a mount that the second API does not have.
    first.mount_asgi("/only-first", _asgi_app)
    add_first(first)
    add_second(second)

    with pytest.raises(CommandError) as error:
        runbolt_module.Command().merge_apis([("first.api", first), ("second.api", second)])

    assert str(error.value) == message


@pytest.mark.parametrize(
    ("entry", "error_type", "message"),
    [
        ("broken_entry_api", CommandError, "expected 'module:attr' path"),
        ("broken_entry_missing:api", CommandError, "No module named 'broken_entry_missing'"),
        ("broken_entry_missing.sub:api", CommandError, "No module named 'broken_entry_missing'"),
        ("broken_entry_package.missing:api", CommandError, "No module named 'broken_entry_package.missing'"),
        ("broken_entry_api:missing", CommandError, "module 'broken_entry_api' has no attribute 'missing'"),
        ("broken_entry_api:api:extra", CommandError, "module 'broken_entry_api' has no attribute 'api:extra'"),
        ("broken_entry_api:other", CommandError, "'broken_entry_api'.other is not a BoltAPI instance"),
        # The module exists, and one of its imports does not. The cause stays visible.
        ("broken_entry_dependency:api", ModuleNotFoundError, "No module named 'broken_entry_not_installed'"),
    ],
)
def test_bolt_api_entry_that_cannot_be_imported_stops_startup(
    settings, tmp_path, monkeypatch, entry, error_type, message
):
    (tmp_path / "broken_entry_api.py").write_text(
        "from django_bolt.api import BoltAPI\napi = BoltAPI()\nother = object()\n"
    )
    (tmp_path / "broken_entry_dependency.py").write_text("import broken_entry_not_installed\n")
    (tmp_path / "broken_entry_package").mkdir()
    (tmp_path / "broken_entry_package" / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    _clear_modules(monkeypatch, "broken_entry_api", "broken_entry_dependency", "broken_entry_package")
    settings.BOLT_API = ["broken_entry_api:api", entry]

    with pytest.raises(error_type) as error:
        runbolt_module.Command().autodiscover_apis()

    if error_type is CommandError:
        message = f"BOLT_API entry {entry!r} could not be imported: {message}"
    assert str(error.value) == message
