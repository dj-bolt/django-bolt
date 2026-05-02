from __future__ import annotations

import time

import httpx
import pytest

pytestmark = pytest.mark.server_integration


def _make_sse_project(make_server_project):
    return make_server_project(
        project_api_body="""
        from django_bolt.middleware import cors


        @api.get("/sse-cors-async")
        @cors(origins=["https://example.com", "https://trusted.com"])
        async def sse_cors_async():
            async def gen():
                for i in range(3):
                    yield f"data: message-{i}\\n\\n"
                    await asyncio.sleep(0.01)
            return StreamingResponse(gen(), media_type="text/event-stream")


        @api.get("/sse-cors-sync")
        @cors(origins=["https://sync-app.com"])
        async def sse_cors_sync():
            def gen():
                for i in range(3):
                    yield f"data: sync-{i}\\n\\n"
                    time.sleep(0.01)
            return StreamingResponse(gen(), media_type="text/event-stream")


        @api.get("/sse-cors-credentials")
        @cors(origins=["https://secure.com"], credentials=True)
        async def sse_cors_credentials():
            async def gen():
                yield "data: secure\\n\\n"
            return StreamingResponse(gen(), media_type="text/event-stream")


        @api.get("/sse-cors-wildcard")
        @cors(origins=["*"])
        async def sse_cors_wildcard():
            async def gen():
                yield "data: public\\n\\n"
            return StreamingResponse(gen(), media_type="text/event-stream")


        @api.get("/sse-no-cors")
        async def sse_no_cors():
            async def gen():
                yield "data: no-cors\\n\\n"
            return StreamingResponse(gen(), media_type="text/event-stream")


        @api.get("/sse-timing")
        async def sse_timing():
            async def gen():
                for i in range(3):
                    yield f"data: timing-{i}\\n\\n"
                    await asyncio.sleep(0.2)
            return StreamingResponse(gen(), media_type="text/event-stream")
        """
    )


def test_async_sse_has_cors_headers(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-cors-async", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert response.headers.get("access-control-allow-origin") == "https://example.com"
    vary_headers = ", ".join(response.headers.get_list("vary")).lower()
    assert "origin" in vary_headers
    assert "data: message-0" in response.text
    assert "data: message-1" in response.text
    assert "data: message-2" in response.text


def test_sync_sse_has_cors_headers(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-cors-sync", headers={"Origin": "https://sync-app.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://sync-app.com"


def test_sse_cors_credentials(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-cors-credentials", headers={"Origin": "https://secure.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://secure.com"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_sse_without_cors_decorator_has_no_headers(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server:
        response = server.get("/sse-no-cors", headers={"Origin": "https://example.com"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") is None


def test_sse_streams_in_real_time(make_server_project):
    project = _make_sse_project(make_server_project)

    with project.start() as server, httpx.Client(timeout=10, headers={"Accept-Encoding": "identity"}) as client:
        timestamps: list[float] = []
        with client.stream("GET", server.url("/sse-timing")) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            buffer = bytearray()
            for chunk in response.iter_raw(chunk_size=1):
                buffer.extend(chunk)
                if not buffer.endswith(b"\n\n"):
                    continue
                event = buffer.decode("utf-8")
                if event.startswith("data: timing-"):
                    timestamps.append(time.monotonic())
                buffer.clear()
                if len(timestamps) == 3:
                    break

    assert len(timestamps) == 3
    deltas = [timestamps[index] - timestamps[index - 1] for index in range(1, len(timestamps))]
    assert all(delta >= 0.12 for delta in deltas), deltas
