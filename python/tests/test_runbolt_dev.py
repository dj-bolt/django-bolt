from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

from django.core.management import execute_from_command_line

from django_bolt.management.commands import runbolt as runbolt_module


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
