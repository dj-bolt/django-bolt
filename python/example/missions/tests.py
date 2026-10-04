"""Tests for missions API endpoints."""

from __future__ import annotations

import pytest

from django_bolt.testing import TestClient
from missions.api import api
from missions.models import Astronaut, Mission


@pytest.fixture(autouse=True)
def clean_db(db):
    """Clean database before each test."""
    Astronaut.objects.all().delete()
    Mission.objects.all().delete()
    yield
    Astronaut.objects.all().delete()
    Mission.objects.all().delete()


@pytest.fixture(scope="class")
def client():
    """Shared TestClient for all tests in the class."""
    with TestClient(api) as c:
        yield c


@pytest.mark.django_db(transaction=True)
class TestMissionEndpoints:
    """Test mission CRUD endpoints."""

    def test_list_missions_empty(self, client):
        """GET /missions returns empty list."""
        response = client.get("/missions")
        assert response.status_code == 200
        assert response.json() == {"missions": [], "count": 0}

    def test_create_mission(self, client):
        """POST /missions creates a mission."""
        response = client.post(
            "/missions",
            json={"name": "Artemis II", "description": "Crewed mission"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Artemis II"
        assert data["status"] == "planned"
        assert "id" in data
        assert Mission.objects.filter(name="Artemis II").exists()

    def test_create_mission_validation_name_required(self, client):
        """POST /missions requires name."""
        response = client.post("/missions", json={})
        assert response.status_code == 422

    def test_create_mission_validation_name_min_length(self, client):
        """POST /missions rejects empty name."""
        response = client.post("/missions", json={"name": ""})
        assert response.status_code == 422

    def test_create_mission_validation_name_max_length(self, client):
        """POST /missions rejects name over 100 chars."""
        response = client.post("/missions", json={"name": "A" * 101})
        assert response.status_code == 422

    def test_create_mission_validation_custom_validator(self, client):
        """POST /missions rejects names starting with 'test'."""
        response = client.post("/missions", json={"name": "Test Mission"})
        assert response.status_code == 422

    def test_get_mission(self, client):
        """GET /missions/{id} returns mission details (API-based setup)."""
        # Create via API
        create_resp = client.post(
            "/missions",
            json={"name": "Apollo 11", "description": "Moon landing"},
        )
        mission_id = create_resp.json()["id"]

        # Fetch via API
        response = client.get(f"/missions/{mission_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == mission_id
        assert data["name"] == "Apollo 11"

    def test_get_mission_not_found(self, client):
        """GET /missions/{id} returns 404 for unknown id."""
        response = client.get("/missions/99999")
        assert response.status_code == 404

    def test_update_mission(self, client):
        """PUT /missions/{id} updates mission (ORM-based setup)."""
        mission = Mission.objects.create(name="Mars Rover", status="planned")

        response = client.put(
            f"/missions/{mission.id}",
            json={"status": "active"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "active"

        mission.refresh_from_db()
        assert mission.status == "active"

    def test_update_mission_invalid_status(self, client):
        """PUT /missions/{id} rejects invalid status (ORM-based setup)."""
        mission = Mission.objects.create(name="Valid Name", status="planned")

        response = client.put(
            f"/missions/{mission.id}",
            json={"status": "invalid"},
        )
        assert response.status_code == 422

    def test_delete_mission(self, client):
        """DELETE /missions/{id} removes mission (API-based setup)."""
        # Create via API
        create_resp = client.post("/missions", json={"name": "Cancelled"})
        mission_id = create_resp.json()["id"]

        # Delete via API
        response = client.delete(f"/missions/{mission_id}")
        assert response.status_code == 204

        # Verify deleted via API
        get_resp = client.get(f"/missions/{mission_id}")
        assert get_resp.status_code == 404

    def test_list_missions_with_filter(self, client):
        """GET /missions?status=active filters results (ORM-based setup)."""
        Mission.objects.create(name="Active 1", status="active")
        Mission.objects.create(name="Active 2", status="active")
        Mission.objects.create(name="Completed", status="completed")

        response = client.get("/missions?status=active")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2


@pytest.mark.django_db(transaction=True)
class TestAstronautEndpoints:
    """Test astronaut endpoints."""

    def test_add_astronaut(self, client):
        """POST /missions/{id}/astronauts adds astronaut (ORM-based setup)."""
        mission = Mission.objects.create(name="Moon Mission")

        response = client.post(
            f"/missions/{mission.id}/astronauts",
            data={"name": "Neil Armstrong", "role": "Commander"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Neil Armstrong"
        assert data["role"] == "Commander"
        assert Astronaut.objects.filter(name="Neil Armstrong").exists()

    def test_add_astronaut_invalid_role(self, client):
        """POST /missions/{id}/astronauts validates role via Form model."""
        mission = Mission.objects.create(name="Moon Mission")

        response = client.post(
            f"/missions/{mission.id}/astronauts",
            data={"name": "John Doe", "role": "Invalid Role"},
        )
        assert response.status_code == 422

    def test_list_astronauts(self, client):
        """GET /missions/{id}/astronauts lists crew (ORM-based setup)."""
        mission = Mission.objects.create(name="Apollo 11")
        Astronaut.objects.create(name="Neil Armstrong", role="Commander", mission=mission)
        Astronaut.objects.create(name="Buzz Aldrin", role="Pilot", mission=mission)

        response = client.get(f"/missions/{mission.id}/astronauts")
        assert response.status_code == 200
        data = response.json()
        assert len(data["astronauts"]) == 2


@pytest.mark.django_db(transaction=True)
class TestFileUploadEndpoints:
    """Test file upload endpoints."""

    def test_upload_single_file(self, client):
        """POST /missions/{id}/documents uploads a single file."""
        mission = Mission.objects.create(name="Document Test")

        response = client.post(
            f"/missions/{mission.id}/documents",
            data={"title": "Mission Report"},
            files={"file": ("report.pdf", b"PDF content here", "application/pdf")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["mission"] == "Document Test"
        assert data["title"] == "Mission Report"
        assert data["count"] == 1
        assert data["documents"][0]["filename"] == "report.pdf"
        assert data["documents"][0]["content_type"] == "application/pdf"

    def test_upload_multiple_files(self, client):
        """POST /missions/{id}/documents uploads multiple files."""
        mission = Mission.objects.create(name="Multi Doc Test")

        response = client.post(
            f"/missions/{mission.id}/documents",
            data={"title": "Mission Files"},
            files=[
                ("file", ("doc1.txt", b"Content 1", "text/plain")),
                ("file", ("doc2.txt", b"Content 2", "text/plain")),
            ],
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        filenames = [d["filename"] for d in data["documents"]]
        assert "doc1.txt" in filenames
        assert "doc2.txt" in filenames

    def test_upload_mission_not_found(self, client):
        """POST /missions/{id}/documents returns 404 for unknown mission."""
        response = client.post(
            "/missions/99999/documents",
            data={"title": "Test"},
            files={"file": ("test.txt", b"test", "text/plain")},
        )
        assert response.status_code == 404

    def test_mixed_form_and_file(self, client):
        """POST /missions/{id}/report accepts form data with optional files."""
        mission = Mission.objects.create(name="Report Test")

        # With attachments
        response = client.post(
            f"/missions/{mission.id}/report",
            data={"title": "Weekly Report", "summary": "All systems nominal"},
            files={"file": ("attachment.pdf", b"PDF data", "application/pdf")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Weekly Report"
        assert data["summary"] == "All systems nominal"
        assert data["attachments"] == 1

    def test_form_without_optional_file(self, client):
        """POST /missions/{id}/report works without optional files."""
        mission = Mission.objects.create(name="No Attachment Test")

        response = client.post(
            f"/missions/{mission.id}/report",
            data={"title": "Status Update"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Status Update"
        assert data["summary"] == ""
        assert data["attachments"] == 0


@pytest.mark.django_db(transaction=True)
class TestHeaderStructEndpoints:
    """Test header struct endpoints."""

    def test_header_struct_required(self, client):
        """GET /missions/secure requires X-Api-Key header."""
        response = client.get(
            "/missions/secure",
            headers={"X-Api-Key": "secret123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["api_key"] == "secret123"
        assert data["request_id"] is None
        assert data["message"] == "Access granted"

    def test_header_struct_with_optional(self, client):
        """GET /missions/secure accepts optional X-Request-Id header."""
        response = client.get(
            "/missions/secure",
            headers={"X-Api-Key": "mykey", "X-Request-Id": "req-456"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["api_key"] == "mykey"
        assert data["request_id"] == "req-456"

    def test_header_struct_missing_required(self, client):
        """GET /missions/secure returns 422 without required header."""
        response = client.get("/missions/secure")
        assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
class TestCookieStructEndpoints:
    """Test cookie struct endpoints."""

    def test_cookie_struct_with_values(self, client):
        """GET /missions/preferences reads cookies."""
        response = client.get(
            "/missions/preferences",
            headers={"cookie": "theme=dark; language=es"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["theme"] == "dark"
        assert data["language"] == "es"

    def test_cookie_struct_with_defaults(self, client):
        """GET /missions/preferences uses defaults for missing cookies."""
        response = client.get("/missions/preferences")
        assert response.status_code == 200
        data = response.json()
        assert data["theme"] == "light"
        assert data["language"] == "en"

    def test_cookie_struct_partial(self, client):
        """GET /missions/preferences uses default for missing cookie."""
        response = client.get(
            "/missions/preferences",
            headers={"cookie": "theme=dark"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["theme"] == "dark"
        assert data["language"] == "en"


@pytest.mark.django_db(transaction=True)
class TestAdditionalMissionRoutes:
    """Test tutorial-style routes that are also exposed in the example app."""

    def test_mission_control_status(self, client):
        """GET /mission-control returns the mission status payload."""
        response = client.get("/mission-control")
        assert response.status_code == 200
        assert response.json() == {"status": "operational", "message": "Mission Control Online"}

    def test_classified_endpoint_requires_clearance(self, client):
        """GET /missions/{id}/classified enforces the header guard."""
        mission = Mission.objects.create(name="Secret Mission", status="planned")

        denied = client.get(f"/missions/{mission.id}/classified", headers={"X-Clearance-Level": "public"})
        assert denied.status_code == 403

        allowed = client.get(
            f"/missions/{mission.id}/classified",
            headers={"X-Clearance-Level": "top-secret"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["mission"] == "Secret Mission"

    def test_mission_log_returns_plain_text(self, client):
        """GET /missions/{id}/log returns a text log."""
        mission = Mission.objects.create(name="Apollo 11", status="active")

        response = client.get(f"/missions/{mission.id}/log")
        assert response.status_code == 200
        assert "MISSION LOG: Apollo 11" in response.text
        assert response.headers["content-type"].startswith("text/plain")

    def test_upload_patch_persists_filename(self, client):
        """POST /missions/{id}/patch stores the patch path and returns metadata."""
        mission = Mission.objects.create(name="Patch Test", status="planned")

        response = client.post(
            f"/missions/{mission.id}/patch",
            files={"patch": ("patch.txt", b"patch-bytes", "text/plain")},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["filename"] == "patch.txt"
        assert payload["mission"] == "Patch Test"

        mission.refresh_from_db()
        assert mission.patch_image.endswith("patch.txt")

    def test_status_page_and_dashboard_render_html(self, client):
        """HTML routes should render successfully."""
        Mission.objects.create(name="Dashboard Mission", status="active", description="Visible in template")

        status_page = client.get("/status-page")
        assert status_page.status_code == 200
        assert "Mission Control" in status_page.text

        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "Dashboard Mission" in dashboard.text

    def test_go_redirects_to_dashboard(self, client):
        """GET /go redirects to /dashboard."""
        response = client.get("/go", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/dashboard"
