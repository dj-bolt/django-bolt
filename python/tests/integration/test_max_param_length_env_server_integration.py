from __future__ import annotations

import pytest

from .helpers import ServerProject

pytestmark = pytest.mark.server_integration


def _make_project(make_server_project) -> ServerProject:
    """Project exposing every parameter surface the limit is enforced on:
    HTTP path, HTTP query, urlencoded form and multipart form."""
    return make_server_project(
        project_api_body="""
        from typing import Annotated

        from django_bolt.param_functions import Form, Query


        @api.get("/echo/{value}")
        async def echo_path(value: str):
            return {"length": len(value)}

        @api.get("/echo-query")
        async def echo_query(value: str = Query()):
            return {"length": len(value)}

        @api.post("/echo-form")
        async def echo_form(value: Annotated[str, Form()]):
            return {"length": len(value)}
        """
    )


# --- Path parameters ---


def test_runbolt_uses_default_max_param_length_without_env(make_server_project):
    project = _make_project(make_server_project)

    too_long = "a" * 8193
    with project.start() as server:
        response = server.get(f"/echo/{too_long}")

    assert response.status_code == 422
    assert "max 8192 bytes" in response.text


def test_runbolt_honors_django_bolt_max_param_length_env(make_server_project):
    project = _make_project(make_server_project)

    accepted = "a" * 10000
    rejected = "a" * 16385
    with project.start(env={"DJANGO_BOLT_MAX_PARAM_LENGTH": "16384"}) as server:
        accepted_response = server.get(f"/echo/{accepted}")
        rejected_response = server.get(f"/echo/{rejected}")

    assert accepted_response.status_code == 200
    assert accepted_response.json() == {"length": len(accepted)}
    assert rejected_response.status_code == 422
    assert "max 16384 bytes" in rejected_response.text


# --- Query params + form fields (default limit) ---


def test_default_limit_rejects_oversized_query_and_form(make_server_project):
    """The default 8192-byte limit is enforced on query params and both form
    encodings, not just path params."""
    project = _make_project(make_server_project)

    too_long = "a" * 8193
    with project.start() as server:
        query = server.get(f"/echo-query?value={too_long}")
        urlencoded = server.request("POST", "/echo-form", data={"value": too_long})
        multipart = server.request("POST", "/echo-form", files={"value": (None, too_long)})

    assert query.status_code == 422, query.text
    assert "max 8192 bytes" in query.text
    assert urlencoded.status_code == 422, urlencoded.text
    assert "max 8192 bytes" in urlencoded.text
    assert multipart.status_code == 422, multipart.text
    assert "max 8192 bytes" in multipart.text


# --- Query params + form fields (env override) ---


def test_env_override_applies_to_query_and_form(make_server_project):
    """The configured limit flows through to query params and both form encodings:
    values under the raised limit are accepted, values above it are rejected."""
    project = _make_project(make_server_project)

    accepted = "a" * 10000  # over default 8192, under configured 16384
    rejected = "a" * 16385  # over configured 16384
    with project.start(env={"DJANGO_BOLT_MAX_PARAM_LENGTH": "16384"}) as server:
        query_ok = server.get(f"/echo-query?value={accepted}")
        query_bad = server.get(f"/echo-query?value={rejected}")

        urlencoded_ok = server.request("POST", "/echo-form", data={"value": accepted})
        urlencoded_bad = server.request("POST", "/echo-form", data={"value": rejected})

        multipart_ok = server.request("POST", "/echo-form", files={"value": (None, accepted)})
        multipart_bad = server.request("POST", "/echo-form", files={"value": (None, rejected)})

    assert query_ok.status_code == 200, query_ok.text
    assert query_ok.json() == {"length": len(accepted)}
    assert query_bad.status_code == 422
    assert "max 16384 bytes" in query_bad.text

    assert urlencoded_ok.status_code == 200, urlencoded_ok.text
    assert urlencoded_ok.json() == {"length": len(accepted)}
    assert urlencoded_bad.status_code == 422
    assert "max 16384 bytes" in urlencoded_bad.text

    assert multipart_ok.status_code == 200, multipart_ok.text
    assert multipart_ok.json() == {"length": len(accepted)}
    assert multipart_bad.status_code == 422
    assert "max 16384 bytes" in multipart_bad.text
