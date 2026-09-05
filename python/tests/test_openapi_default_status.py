"""Check documented status codes for handlers without response annotations."""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI, Response
from django_bolt.testing import TestClient


@pytest.mark.parametrize("status_code", [200, 201, 202, 204])
def test_unannotated_handler_documents_default_status_code(status_code):
    api = BoltAPI()

    @api.get("/result", status_code=status_code)
    async def result():
        if status_code == 204:
            return Response(status_code=204)
        return {"message": "Success"}

    api._register_openapi_routes()
    with TestClient(api) as client:
        response = client.get("/result")
        assert response.status_code == status_code
        schema_response = client.get("/docs/openapi.json")
        assert schema_response.status_code == 200

    responses = schema_response.json()["paths"]["/result"]["get"]["responses"]
    assert set(responses) == {str(status_code)}
