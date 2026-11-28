"""The async ORM reads on the event loop, on a real ``runbolt`` server.

``test_async_orm_on_loop_postgres.py`` has the behavior of each read, with
``TestClient``. A real server runs a handler on an Actix worker thread. The
loop connections of that thread send their queries from its Tokio runtime,
which ``TestClient`` does not have.

Set ``DJANGO_BOLT_TEST_POSTGRES_DSN`` to run these tests. Without the variable, they skip.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from .apps import app_module
from .apps.loop_orm import USERNAMES
from .helpers import postgres_settings

pytestmark = [pytest.mark.server_integration, pytest.mark.postgres]

# The ``django_bolt.async_orm`` logger tells where the reads of a database run. The file is in the project directory.
LOOP_LOG = """
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"file": {"class": "logging.FileHandler", "filename": str(BASE_DIR / "loop.log")}},
    "loggers": {"django_bolt.async_orm": {"handlers": ["file"], "level": "INFO", "propagate": False}},
}
"""


def test_a_server_reads_on_the_event_loop(make_server_project, postgres_database):
    project = make_server_project(
        api_module=app_module("loop_orm"),
        settings_extra=postgres_settings(postgres_database) + "\nBOLT_ASYNC_ORM_ON_LOOP = True\n" + LOOP_LOG,
    )

    def read(index: int) -> tuple[int, object]:
        username = USERNAMES[index % len(USERNAMES)]
        with httpx.Client(timeout=30) as client:
            response = client.get(f"{server.base_url}/user/{username}")
        return response.status_code, response.json()

    # One ORM thread has one connection. Reads on more connections did not run on that thread.
    with project.start(env={"DJANGO_BOLT_ORM_THREADS": "1"}) as server:
        assert server.request("POST", "/seed").json() == {"users": 3}

        assert server.get("/usernames").json() == USERNAMES
        assert server.get("/backends/8").json() == {"connections": 8}
        with ThreadPoolExecutor(16) as senders:
            results = list(senders.map(read, range(200)))

    expected = [(200, {"username": USERNAMES[index % 3], "is_active": True}) for index in range(200)]
    assert results == expected
    # Each worker thread writes the line for its own event loop. A free-threaded server has one for each CPU.
    lines = project.path("loop.log").read_text().splitlines()
    assert lines, "no worker thread made a pool of loop connections"
    assert set(lines) == {"The async ORM reads of the database 'default' run on the event loop."}
