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


def test_collect_dev_watch_paths_prefers_python_and_template_paths(settings, tmp_path, monkeypatch):
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

    assert str(settings_module) in watch_paths
    assert str(urls_module) in watch_paths
    assert str(templates_root) in watch_paths
    assert str(external_app / "api.py") in watch_paths
    assert str(static_root) not in watch_paths
    assert str(project_root) not in watch_paths


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
    monkeypatch.setattr(runbolt_module, "_collect_dev_watch_paths", lambda: ["/tmp/project"])
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
        ],
        "watch_paths": ["/tmp/project"],
        "ignore_dir_names": list(runbolt_module.DEV_RELOAD_IGNORE_DIRS),
        "ignore_paths": ["/tmp/venv"],
        "debounce_ms": runbolt_module.DEV_RELOAD_DEBOUNCE_MS,
        "force_polling": False,
    }


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
