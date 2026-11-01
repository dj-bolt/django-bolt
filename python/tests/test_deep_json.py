"""Deeply nested JSON on the worker threads of ``TestClient``.

msgspec uses C stack for each level of nesting. On Python 3.12 and 3.13, the
C recursion limit allows more levels than a 2 MiB thread stack holds. Rust
starts its threads with that stack, so a deep body stopped the process with
SIGSEGV. The script runs in a subprocess, so such a stop cannot stop pytest.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

PYTHON_ROOT = Path(__file__).resolve().parents[1]

_DEEP_JSON_ON_TEST_WORKERS = """
import json

import django
from django.conf import settings

settings.configure(DEBUG=False)
django.setup()

from django_bolt.testing import TestClient
from tests.integration.apps.deep_json import api, lane_api

# This depth fits the stack of the main thread. It overflowed a 2 MiB stack.
DEEP = 7_000
# No thread stack holds this depth. The decoder raises RecursionError.
TOO_DEEP = 100_000


def post(client, path, depth):
    body = b'{"value":' + b"[" * depth + b"]" * depth + b"}"
    return client.post(path, content=body, headers={"content-type": "application/json"}).status_code


statuses = {}
with TestClient(api, raise_server_exceptions=False) as client:
    for path in ("/body", "/dict-body", "/decode"):
        statuses[path] = [post(client, path, DEEP), post(client, path, TOO_DEEP)]
    statuses["/health"] = client.get("/health").status_code
with TestClient(lane_api, raise_server_exceptions=False) as client:
    statuses["/lane/body"] = [post(client, "/lane/body", DEEP), post(client, "/lane/body", TOO_DEEP)]
print(json.dumps(statuses))
"""


# Rust ignores a RUST_MIN_STACK that is not an int. Bolt then sets its own default.
@pytest.mark.parametrize("min_stack", [None, "not-a-number"], ids=["unset", "not-an-int"])
def test_deep_json_on_test_worker_threads_does_not_stop_the_process(min_stack):
    env = {key: value for key, value in os.environ.items() if key != "RUST_MIN_STACK"}
    env["PYTHONPATH"] = str(PYTHON_ROOT)
    if min_stack is not None:
        env["RUST_MIN_STACK"] = min_stack
    result = subprocess.run(
        [sys.executable, "-c", _DEEP_JSON_ON_TEST_WORKERS],
        cwd=PYTHON_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"exit code {result.returncode}\n{result.stdout}\n{result.stderr}"
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        # Bolt decodes the body: a body that is too deep gets 422.
        "/body": [200, 422],
        "/dict-body": [200, 422],
        "/lane/body": [200, 422],
        # The handler decodes the body: its RecursionError is a server error.
        "/decode": [200, 500],
        "/health": 200,
    }


def _recurse(depth: int) -> int:
    return _recurse(depth + 1)


class LoopingStruct(msgspec.Struct):
    value: int

    def __post_init__(self) -> None:
        _recurse(0)


@dataclasses.dataclass
class LoopingDataclass:
    value: int

    def __post_init__(self) -> None:
        _recurse(0)


@pytest.mark.parametrize("path", ["/struct", "/dataclass"])
def test_a_recursion_error_of_application_code_in_a_body_type_is_a_server_error(path):
    """Only the depth error of the decoder is a 422. A RecursionError of a __post_init__ is a fault of the app."""
    api = BoltAPI()

    @api.post("/struct")
    def struct_route(data: LoopingStruct):
        return {"ok": True}

    @api.post("/dataclass")
    def dataclass_route(data: LoopingDataclass):
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.post(path, json={"value": 1})
        assert response.status_code == 500, response.text
