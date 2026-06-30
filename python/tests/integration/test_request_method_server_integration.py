"""Real-server coverage for HTTP method handling.

Complements the in-process unit tests in tests/test_request_method.py. The
TestClient coerces unsupported methods to GET when building the Actix request,
so it cannot prove that a non-standard verb is rejected by routing. The real
server parses the verb off the wire, so here we assert that:

1. A standard verb round-trips through request.method, and
2. A non-standard verb is rejected (404/405) before reaching a handler — which
   is what makes HttpMethod::Unknown unreachable in production.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.server_integration


API_BODY = """
@api.get("/echo")
async def echo(request):
    return {"method": request.method}
"""


def test_standard_method_round_trips_on_real_server(make_server_project):
    project = make_server_project(project_api_body=API_BODY)
    with project.start() as server:
        response = server.get("/echo")
    assert response.status_code == 200
    assert response.json()["method"] == "GET"


def test_non_standard_method_rejected_by_real_server(make_server_project):
    project = make_server_project(project_api_body=API_BODY)
    with project.start() as server:
        # A custom verb is parsed off the wire by Actix; Router::find returns no
        # match for it, so it must 404 (or 405) before a PyRequest is built — a
        # handler can never observe request.method == "UNKNOWN".
        response = server.request("PROPFIND", "/echo")
    assert response.status_code in (404, 405), (
        f"non-standard verb must be rejected by routing, got {response.status_code}"
    )
