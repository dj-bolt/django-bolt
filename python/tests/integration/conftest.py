from __future__ import annotations

import os
import secrets
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from .helpers import create_server_project


@pytest.fixture
def make_server_project(tmp_path_factory):
    project_counter = count()

    def factory(**kwargs):
        project_root = tmp_path_factory.mktemp(f"server_project_{next(project_counter)}")
        return create_server_project(project_root, **kwargs)

    return factory


@pytest.fixture(scope="module")
def artifact_path() -> Path:
    artifact = os.environ.get("DJANGO_BOLT_ARTIFACT_PATH")
    if not artifact:
        pytest.skip("Set DJANGO_BOLT_ARTIFACT_PATH to run artifact smoke tests.")
    path = Path(artifact)
    if not path.exists():
        pytest.skip(f"Artifact path does not exist: {path}")
    return path


POSTGRES_DSN_VARIABLE = "DJANGO_BOLT_TEST_POSTGRES_DSN"


@pytest.fixture
def postgres_database() -> Any:
    """A fresh database on the configured PostgreSQL server, dropped at exit.

    Each test gets its own database so parallel workers and the ``migrate``
    of each server do not meet. The tests skip without the DSN variable.
    """
    dsn = os.environ.get(POSTGRES_DSN_VARIABLE)
    if not dsn:
        pytest.skip(f"Set {POSTGRES_DSN_VARIABLE} to run the PostgreSQL tests.")
    # psycopg needs libpq when it is imported. Only PostgreSQL tests load it.
    import psycopg  # noqa: PLC0415
    from psycopg import sql  # noqa: PLC0415
    from psycopg.conninfo import conninfo_to_dict  # noqa: PLC0415

    name = f"bolt_test_{secrets.token_hex(6)}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = {**conninfo_to_dict(dsn), "dbname": name}
    try:
        yield params
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
