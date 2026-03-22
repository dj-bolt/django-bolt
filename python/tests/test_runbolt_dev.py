from __future__ import annotations

from types import SimpleNamespace

from django_bolt.management.commands import runbolt as runbolt_module


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


def test_collapse_watch_paths_deduplicates_nested_directories(tmp_path):
    root = tmp_path / "project"
    nested = root / "app"
    sibling = tmp_path / "templates"
    nested.mkdir(parents=True)
    sibling.mkdir()

    collapsed = runbolt_module._collapse_watch_paths({root, nested, sibling})

    assert collapsed == [root, sibling]


def test_collect_dev_watch_paths_prefers_project_paths(settings, tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    templates_root = tmp_path / "templates"
    static_root = tmp_path / "static"
    external_app = tmp_path / "shared_app"
    venv_root = tmp_path / "venv"
    venv_app = venv_root / "lib" / "site-packages" / "third_party_app"

    for path in (project_root, templates_root, static_root, external_app, venv_app):
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(project_root)
    monkeypatch.setattr(runbolt_module.sys, "prefix", str(venv_root))
    monkeypatch.setattr(runbolt_module.sys, "base_prefix", str(venv_root))
    monkeypatch.setattr(runbolt_module.sys, "exec_prefix", str(venv_root))
    monkeypatch.setattr(
        runbolt_module,
        "apps",
        SimpleNamespace(
            ready=True,
            get_app_configs=lambda: [
                SimpleNamespace(path=str(external_app)),
                SimpleNamespace(path=str(venv_app)),
            ],
        ),
    )

    settings.BASE_DIR = project_root
    settings.TEMPLATES = [{"DIRS": [templates_root]}]
    settings.STATICFILES_DIRS = [static_root]

    watch_paths = set(runbolt_module._collect_dev_watch_paths())

    assert str(project_root) in watch_paths
    assert str(templates_root) in watch_paths
    assert str(static_root) in watch_paths
    assert str(external_app) in watch_paths
    assert str(venv_app) not in watch_paths
