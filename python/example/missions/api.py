from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Literal

from django.urls import reverse
from msgspec import Meta

from django_bolt import BoltAPI, Request
from django_bolt.exceptions import HTTPException, NotFound
from django_bolt.param_functions import Cookie, File, Form, Header, Query
from django_bolt.responses import HTML, PlainText, Redirect
from django_bolt.serializers import Serializer, field, field_validator
from django_bolt.shortcuts import render
from missions.models import Astronaut, Mission

api = BoltAPI()


# Schemas
class CreateMission(Serializer):
    name: Annotated[str, Meta(min_length=1, max_length=100)]
    description: Annotated[str, Meta(max_length=500)] = ""
    launch_date: datetime | None = None

    @field_validator("name")
    def validate_name(cls, value):
        if value.lower().startswith("test"):
            raise ValueError("Mission name cannot start with 'test'")
        return value


class UpdateMission(Serializer):
    name: Annotated[str, Meta(min_length=1, max_length=100)] | None = None
    status: Literal["planned", "active", "completed", "aborted"] | None = None
    description: Annotated[str, Meta(max_length=500)] | None = None


class MissionResponse(Serializer):
    id: int
    name: str
    status: str
    launch_date: datetime | None = None
    description: str = ""


class MissionListResponse(Serializer):
    missions: list[MissionResponse]
    count: int


class MissionCreatedResponse(Serializer):
    id: int
    name: str
    status: str
    message: str


class AstronautResponse(Serializer):
    id: int
    name: str
    role: str
    mission_id: int


class AstronautCreatedResponse(Serializer):
    id: int
    name: str
    role: str
    mission: str = field(source="mission.name")


class AstronautListResponse(Serializer):
    mission: str
    astronauts: list[AstronautResponse]


class UploadedDocumentResponse(Serializer):
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None


class MissionDocumentsResponse(Serializer):
    mission: str
    title: str
    documents: list[UploadedDocumentResponse]
    count: int


class SecureMissionResponse(Serializer):
    api_key: str
    request_id: str | None = None
    message: str


class PreferencesResponse(Serializer):
    theme: str
    language: str


class MissionReportResponse(Serializer):
    mission: str
    title: str
    summary: str
    attachments: int


# Query parameter model for filtering missions
class MissionFilters(Serializer):
    status: Literal["planned", "active", "completed", "aborted"] | None = None
    limit: Annotated[int, Meta(ge=1, le=100)] = 10


# Form model for creating astronauts
class CreateAstronaut(Serializer):
    name: Annotated[str, Meta(min_length=1, max_length=100)]
    role: Annotated[str, Meta(min_length=1, max_length=50)]

    @field_validator("role")
    def validate_role(cls, value):
        valid_roles = ["Commander", "Pilot", "Mission Specialist", "Flight Engineer", "Payload Specialist"]
        if value not in valid_roles:
            raise ValueError(f"Role must be one of: {', '.join(valid_roles)}")
        return value


@api.get("/mission-control")
async def mission_control_status():
    """Simple status endpoint for the missions example app."""
    return {"status": "operational", "message": "Mission Control Online"}


@api.get("/reverse-demo")
async def reverse_demo():
    """Resolve Bolt route names back to URLs with Django's ``reverse()``.

    These routes live only in Rust's matchit router, yet ``django.urls.reverse``
    resolves them by name because ``testproject/urls.py`` includes
    ``django_bolt.urls`` (a reverse-only urlconf built from the named routes).
    Path params come straight from Django's converters -- nothing here knows
    about Bolt.
    """
    return {
        "missions_list": reverse("missions-list"),
        "mission_detail": reverse("mission-detail", kwargs={"mission_id": 42}),
        "mission_log": reverse("mission-log", kwargs={"mission_id": 42}),
    }


# Endpoints
@api.get("/missions", name="missions-list")
async def list_missions(filters: Annotated[MissionFilters, Query()]) -> MissionListResponse:
    """List all missions with optional filtering."""
    queryset = Mission.objects.all()
    if filters.status:
        queryset = queryset.filter(status=filters.status)
    missions: list[MissionResponse] = []
    async for mission in queryset[: filters.limit]:
        missions.append(await MissionResponse.afrom_model(mission))
    return MissionListResponse(missions=missions, count=len(missions))


@api.get("/missions/{mission_id}", name="mission-detail")
async def get_mission(mission_id: int) -> MissionResponse:
    """Get a specific mission by ID."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
        return await MissionResponse.afrom_model(mission)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc


@api.post("/missions")
async def create_mission(mission: CreateMission) -> MissionCreatedResponse:
    """Create a new mission."""
    new_mission = await Mission.objects.acreate(
        name=mission.name,
        description=mission.description,
        launch_date=mission.launch_date,
        status="planned",
    )
    return MissionCreatedResponse(
        id=new_mission.id,
        name=new_mission.name,
        status=new_mission.status,
        message="Mission created successfully",
    )


@api.put("/missions/{mission_id}")
async def update_mission(mission_id: int, data: UpdateMission) -> MissionResponse:
    """Update a mission."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    if data.name is not None:
        mission.name = data.name
    if data.status is not None:
        mission.status = data.status
    if data.description is not None:
        mission.description = data.description

    await mission.asave()
    return await MissionResponse.afrom_model(mission)


@api.delete("/missions/{mission_id}", status_code=204)
async def delete_mission(mission_id: int):
    """Delete a mission."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    await mission.adelete()


@api.get("/missions/{mission_id}/classified")
async def get_classified_info(
    mission_id: int,
    clearance: Annotated[str, Header(alias="X-Clearance-Level")],
):
    """Return protected mission metadata when the caller has clearance."""
    if clearance not in ["top-secret", "confidential"]:
        raise HTTPException(status_code=403, detail="Insufficient clearance level")

    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    return {
        "mission": mission.name,
        "classified_data": "Launch codes: APOLLO-7749-OMEGA",
        "clearance_verified": clearance,
    }


@api.get("/missions/{mission_id}/log", name="mission-log")
async def get_mission_log(mission_id: int):
    """Return a plain-text mission log."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    log = f"""
=== MISSION LOG: {mission.name} ===
Status: {mission.status.upper()}
Launch Date: {mission.launch_date or "TBD"}
Description: {mission.description or "No description"}
================================
    """.strip()

    return PlainText(log)


@api.post("/missions/{mission_id}/patch")
async def upload_mission_patch(
    mission_id: int,
    patch: Annotated[list[dict], File(alias="patch")],
):
    """Upload and persist a mission patch image."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    if not patch:
        raise HTTPException(status_code=400, detail="No file uploaded")

    file_info = patch[0]
    filename = file_info.get("filename", "patch.png")
    content = file_info.get("content", b"")
    size = file_info.get("size", 0)

    save_path = f"media/patches/{mission_id}_{filename}"
    os.makedirs("media/patches", exist_ok=True)
    with open(save_path, "wb") as patch_file:
        patch_file.write(content)

    mission.patch_image = save_path
    await mission.asave()

    return {
        "message": "Mission patch uploaded successfully",
        "filename": filename,
        "size": size,
        "mission": mission.name,
    }


# Astronaut endpoints
@api.post("/missions/{mission_id}/astronauts")
async def add_astronaut(
    mission_id: int,
    data: Annotated[CreateAstronaut, Form()],
) -> AstronautCreatedResponse:
    """Add an astronaut to a mission."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    astronaut = await Astronaut.objects.acreate(
        name=data.name,
        role=data.role,
        mission=mission,
    )
    return await AstronautCreatedResponse.afrom_model(astronaut)


@api.get("/missions/{mission_id}/astronauts")
async def list_astronauts(mission_id: int) -> AstronautListResponse:
    """List all astronauts for a mission."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    astronauts: list[AstronautResponse] = []
    async for astronaut in Astronaut.objects.filter(mission=mission):
        astronauts.append(await AstronautResponse.afrom_model(astronaut))
    return AstronautListResponse(mission=mission.name, astronauts=astronauts)


# Header model for API metadata
class APIHeaders(Serializer):
    x_api_key: str
    x_request_id: str | None = None


# Cookie model for user preferences
class UserPreferences(Serializer):
    theme: str = "light"
    language: str = "en"


# File upload endpoint
@api.post("/missions/{mission_id}/documents")
async def upload_document(
    mission_id: int,
    title: Annotated[str, Form()],
    files: Annotated[list[dict], File(alias="file")],
) -> MissionDocumentsResponse:
    """Upload documents for a mission."""
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    uploaded: list[UploadedDocumentResponse] = []
    for f in files:
        uploaded.append(
            UploadedDocumentResponse(
                filename=f.get("filename"),
                content_type=f.get("content_type"),
                size=f.get("size"),
            )
        )

    return MissionDocumentsResponse(
        mission=mission.name,
        title=title,
        documents=uploaded,
        count=len(uploaded),
    )


# Header struct endpoint
@api.get("/missions/secure")
async def secure_endpoint(headers: Annotated[APIHeaders, Header()]) -> SecureMissionResponse:
    """Endpoint requiring API key header."""
    return SecureMissionResponse(
        api_key=headers.x_api_key,
        request_id=headers.x_request_id,
        message="Access granted",
    )


# Cookie struct endpoint
@api.get("/missions/preferences")
async def get_preferences(cookies: Annotated[UserPreferences, Cookie()]) -> PreferencesResponse:
    """Get user preferences from cookies."""
    return PreferencesResponse(theme=cookies.theme, language=cookies.language)


# Mixed form and file upload
@api.post("/missions/{mission_id}/report")
async def submit_report(
    mission_id: int,
    title: Annotated[str, Form()],
    summary: Annotated[str, Form()] = "",
    attachments: Annotated[list[dict], File(alias="file")] = None,
) -> MissionReportResponse:
    """Submit a mission report with optional attachments."""
    if attachments is None:
        attachments = []
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    return MissionReportResponse(
        mission=mission.name,
        title=title,
        summary=summary,
        attachments=len(attachments),
    )


@api.get("/status-page")
async def status_page():
    """Return a simple HTML status page."""
    return HTML("<h1>Mission Control: All Systems Operational</h1>")


@api.get("/go")
async def go_to_dashboard():
    """Redirect to the missions dashboard."""
    return Redirect("/dashboard")


@api.get("/dashboard")
async def mission_dashboard(request: Request):
    """Render a small dashboard using a Django template."""
    missions = []
    async for mission in Mission.objects.all()[:20]:
        missions.append(
            {
                "id": mission.id,
                "name": mission.name,
                "status": mission.status,
                "description": mission.description,
            }
        )

    return render(request, "missions/dashboard.html", {"missions": missions})
