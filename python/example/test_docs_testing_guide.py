"""Executable proof for docs/src/topics/testing.md.

Every pattern the testing guide teaches, run against this real example project:
TestClient basics, validation, streaming, authenticated routes, database
access, and the live-server subprocess recipe. If the guide is right, this
file passes; if it drifts from reality, this file fails.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import httpx
import pytest
from django.contrib.auth import get_user_model

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication, create_jwt_for_user
from django_bolt.responses import EventSourceResponse, StreamingResponse
from django_bolt.testing import TestClient

# The real app modules, imported the way the guide's "real project pattern" shows.
from testproject.api import api
from users.api import api as users_api
from users.models import User


# ── TestClient basics ─────────────────────────────────────────────────────────
def test_root_route_through_the_real_pipeline():
    with TestClient(api) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "Hello World"}


# ── Validation ──────────────────────────────────────────────────────────────--
def test_bad_body_is_rejected_before_the_handler_runs():
    with TestClient(api) as client:
        # /auth/token needs a username and password; an empty body never
        # reaches the handler.
        assert client.post("/auth/token", json={}).status_code == 422
        # An unknown path is a plain 404.
        assert client.get("/nowhere").status_code == 404


# ── Streaming ─────────────────────────────────────────────────────────────────
# The guide's SSE snippet uses a finite generator; build that small API here
# (the "isolated API fixture pattern") so the assertions the guide makes are the
# assertions that run.
def _sse_api() -> BoltAPI:
    sse_api = BoltAPI()

    @sse_api.get("/sse", response_class=EventSourceResponse)
    async def sse():
        yield {"message": "hello"}
        yield {"message": "world"}

    @sse_api.get("/stream")
    async def stream():
        async def generate():
            for i in range(5):
                yield f"data: {i}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    return sse_api


def test_sse_buffered():
    with TestClient(_sse_api()) as client:
        response = client.get("/sse")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.content.decode()
        assert 'data: {"message":"hello"}' in body
        assert 'data: {"message":"world"}' in body


def test_stream_iterates_lines():
    with TestClient(_sse_api()) as client:
        response = client.get("/stream", stream=True)
        lines = list(response.iter_lines())
        data_lines = [line for line in lines if line.startswith("data:")]
        assert len(data_lines) == 5


# ── Authenticated endpoints ─────────────────────────────────────────────────--
@pytest.mark.django_db(transaction=True)
def test_guarded_route_needs_a_token():
    UserModel = get_user_model()
    user = UserModel.objects.create_user(username="alice", password="s3cret")
    token = create_jwt_for_user(user)

    with TestClient(api) as client:
        # No token: the guard rejects the request before the handler runs.
        assert client.get("/auth/me").status_code == 401

        response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["username"] == "alice"


# ── Database access ───────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def clean_users(db):
    User.objects.all().delete()
    yield
    User.objects.all().delete()


@pytest.mark.django_db(transaction=True)
def test_db_backed_route_sees_committed_rows():
    User.objects.create(username="bob", email="bob@example.com")
    User.objects.create(username="carol", email="carol@example.com")

    with TestClient(users_api) as client:
        response = client.get("/users/mini10")
        assert response.status_code == 200
        usernames = {row["username"] for row in response.json()}
        assert {"bob", "carol"} <= usernames


# ── Testing against a live server ─────────────────────────────────────────────
# The guide's subprocess recipe, run against this project's own runbolt. The
# example's /sse endpoint pushes a timestamp every second forever, so it's a
# genuine test that events arrive over a real socket while the handler streams.
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def bolt_server():
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "manage.py", "runbolt", "--host", "127.0.0.1", "--port", str(port)]
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}")
    try:
        deadline = time.time() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError("runbolt exited before it became ready")
            try:
                client.get("/")  # any route works: a response means it's up
                break
            except httpx.TransportError:
                if time.time() > deadline:
                    raise TimeoutError("server did not become ready in 15s")
                time.sleep(0.2)
        yield client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.server_integration
def test_events_arrive_over_a_real_socket(bolt_server):
    with bolt_server.stream("GET", "/sse") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        first = next(line for line in response.iter_lines() if line.startswith("data:"))
        # The example streams `data: <unix timestamp>`; the point is it arrived.
        float(first.removeprefix("data: "))
