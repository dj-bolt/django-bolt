---
icon: lucide/rocket
---

# Quick Start

In this tutorial, you'll build a **Space Mission Tracker API**—a NASA-style mission control system that tracks space missions and astronauts. Along the way, you'll learn all the core features of Django-Bolt.

## What we're building

By the end of this tutorial, you'll have an API that can:

- List and filter space missions
- Track astronauts and their roles
- Handle file uploads for mission patches
- Render a mission dashboard
- Validate requests and handle errors gracefully

Let's get started.

## Project setup

First, create a Django app for our missions:

```bash
python manage.py startapp missions
```

Add it to your `INSTALLED_APPS` in `settings.py`:

```python
INSTALLED_APPS = [
    ...
    "django_bolt",
    "missions",
]
```

Now define the models in `missions/models.py`:

```python
from django.db import models


class Mission(models.Model):
    STATUS_CHOICES = [
        ("planned", "Planned"),
        ("active", "Active"),
        ("completed", "Completed"),
        ("aborted", "Aborted"),
    ]

    name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="planned")
    launch_date = models.DateTimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    patch_image = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.name


class Astronaut(models.Model):
    name = models.CharField(max_length=100)
    role = models.CharField(max_length=50)  # Commander, Pilot, Mission Specialist
    mission = models.ForeignKey(
        Mission, on_delete=models.CASCADE, related_name="astronauts"
    )

    def __str__(self):
        return f"{self.name} ({self.role})"
```

Run the migrations:

```bash
python manage.py makemigrations missions
python manage.py migrate
```

## Your first endpoint

Create `missions/api.py` and add your first endpoint:

```python
from django_bolt import BoltAPI

api = BoltAPI()


@api.get("/")
async def mission_control_status():
    return {"status": "operational", "message": "Mission Control Online"}
```

Start the server:

```bash
python manage.py runbolt --dev
```

Visit `http://localhost:8000/` in your browser:

```json
{"status": "operational", "message": "Mission Control Online"}
```

If you're exploring the bundled multi-app example project in this repo instead of starting from a fresh tutorial app, the equivalent route lives at `/mission-control` because `/` is already used by the top-level example API.

## Path parameters

Let's add an endpoint to get a specific mission by ID. Path parameters are defined using curly braces:

```python
from missions.models import Mission
from django_bolt.exceptions import NotFound


@api.get("/missions/{mission_id}")
async def get_mission(mission_id: int):
    try:
        mission = await Mission.objects.aget(id=mission_id)
        return {
            "id": mission.id,
            "name": mission.name,
            "status": mission.status,
            "launch_date": str(mission.launch_date) if mission.launch_date else None,
            "description": mission.description,
        }
    except Mission.DoesNotExist:
        raise NotFound(detail=f"Mission {mission_id} not found")
```

The `mission_id` parameter is automatically converted to an integer. If you pass an invalid value like `/missions/abc`, Django-Bolt returns a 422 validation error.

Test it:

- `http://localhost:8000/missions/1` — Returns mission details (if it exists)
- `http://localhost:8000/missions/999` — Returns 404 Not Found
- `http://localhost:8000/missions/abc` — Returns 422 Unprocessable Entity

## Query parameters

Function parameters that don't appear in the path become query parameters. You can group related query parameters into a `Serializer` for validation and reusability:

```python
from typing import Annotated, Literal

from msgspec import Meta

from django_bolt.param_functions import Query
from django_bolt.serializers import Serializer


class MissionFilters(Serializer):
    status: Literal["planned", "active", "completed", "aborted"] | None = None
    limit: Annotated[int, Meta(ge=1, le=100)] = 10


@api.get("/missions")
async def list_missions(filters: Annotated[MissionFilters, Query()]):
    queryset = Mission.objects.all()

    if filters.status:
        queryset = queryset.filter(status=filters.status)

    missions = []
    async for mission in queryset[:filters.limit]:
        missions.append({
            "id": mission.id,
            "name": mission.name,
            "status": mission.status,
        })

    return {"missions": missions, "count": len(missions)}
```

The `MissionFilters` serializer provides:
- **Type validation** — `status` must be one of the allowed values
- **Range constraints** — `limit` must be between 1 and 100
- **Default values** — Both fields are optional with sensible defaults

Try these URLs:

- `http://localhost:8000/missions` — All missions (up to 10)
- `http://localhost:8000/missions?status=active` — Only active missions
- `http://localhost:8000/missions?status=completed&limit=5` — 5 completed missions
- `http://localhost:8000/missions?status=invalid` — Returns 422 (invalid status)
- `http://localhost:8000/missions?limit=200` — Returns 422 (limit exceeds 100)

## Request body validation

Use Django-Bolt's `Serializer` class to define and validate request bodies with built-in constraints and custom validators:

```python
from datetime import datetime
from typing import Annotated

from msgspec import Meta

from django_bolt.serializers import Serializer, field, field_validator


class CreateMission(Serializer):
    name: Annotated[str, Meta(min_length=1, max_length=100)]
    description: Annotated[str, Meta(max_length=500)] = ""
    launch_date: datetime | None = None

    @field_validator("name")
    def validate_name(cls, value):
        if value.lower().startswith("test"):
            raise ValueError("Mission name cannot start with 'test'")
        return value


@api.post("/missions")
async def create_mission(mission: CreateMission):
    new_mission = await Mission.objects.acreate(
        name=mission.name,
        description=mission.description,
        launch_date=mission.launch_date,
        status="planned",
    )
    return {
        "id": new_mission.id,
        "name": new_mission.name,
        "status": new_mission.status,
        "message": "Mission created successfully",
    }
```

The `Serializer` class provides:
- **Type constraints** via `Annotated[type, Meta(...)]` — min/max length, numeric ranges, patterns
- **Custom validators** via `@field_validator` — run after type validation, can transform values

Test with curl:

```bash
curl -X POST http://localhost:8000/missions \
  -H "Content-Type: application/json" \
  -d '{"name": "Artemis II", "description": "First crewed Artemis mission"}'
```

Returns:

```json
{"id": 1, "name": "Artemis II", "status": "planned", "message": "Mission created successfully"}
```

If you send invalid data, Django-Bolt collects **all validation errors** and returns them together:

```bash
curl -X POST http://localhost:8000/missions \
  -H "Content-Type: application/json" \
  -d '{"name": "", "description": "x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]x]"}'
```

Returns 422 with all errors:

```json
{
    "detail": [
        {
            "loc": ["body", "name"],
            "msg": "Expected `str` of length >= 1",
            "type": "validation_error"
        },
        {
            "loc": ["body", "description"],
            "msg": "Expected `str` of length <= 500",
            "type": "validation_error"
        }
    ]
}
```

This multi-error collection lets users fix all issues at once instead of discovering them one at a time.

## HTTP methods

Let's add update and delete operations for full CRUD:

```python
from typing import Literal


class UpdateMission(Serializer):
    name: Annotated[str, Meta(min_length=1, max_length=100)] | None = None
    status: Literal["planned", "active", "completed", "aborted"] | None = None
    description: Annotated[str, Meta(max_length=500)] | None = None


@api.put("/missions/{mission_id}")
async def update_mission(mission_id: int, data: UpdateMission):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist:
        raise NotFound(detail=f"Mission {mission_id} not found")

    if data.name is not None:
        mission.name = data.name
    if data.status is not None:
        mission.status = data.status
    if data.description is not None:
        mission.description = data.description

    await mission.asave()
    return {"id": mission.id, "name": mission.name, "status": mission.status}


@api.delete("/missions/{mission_id}", status_code=204)
async def delete_mission(mission_id: int):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist:
        raise NotFound(detail=f"Mission {mission_id} not found")

    await mission.adelete()
```

Django-Bolt supports all HTTP methods: `@api.get`, `@api.post`, `@api.put`, `@api.patch`, `@api.delete`, `@api.head`, `@api.options`.

## Headers

Extract header values using `Annotated` and `Header`. Let's add a classified endpoint that requires clearance:

```python
from typing import Annotated
from django_bolt.param_functions import Header
from django_bolt.exceptions import HTTPException


@api.get("/missions/{mission_id}/classified")
async def get_classified_info(
    mission_id: int,
    clearance: Annotated[str, Header(alias="X-Clearance-Level")],
):
    if clearance not in ["top-secret", "confidential"]:
        raise HTTPException(
            status_code=403,
            detail="Insufficient clearance level"
        )

    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist:
        raise NotFound(detail=f"Mission {mission_id} not found")

    return {
        "mission": mission.name,
        "classified_data": "Launch codes: APOLLO-7749-OMEGA",
        "clearance_verified": clearance,
    }
```

Test it:

```bash
# Without header - returns 422
curl http://localhost:8000/missions/1/classified

# With insufficient clearance - returns 403
curl http://localhost:8000/missions/1/classified \
  -H "X-Clearance-Level: public"

# With proper clearance - returns classified data
curl http://localhost:8000/missions/1/classified \
  -H "X-Clearance-Level: top-secret"
```

## Form data

Handle form submissions using `Form`. You can group form fields into a `Serializer` with validation:

```python
from django_bolt.param_functions import Form


class CreateAstronaut(Serializer):
    name: Annotated[str, Meta(min_length=1, max_length=100)]
    role: Annotated[str, Meta(min_length=1, max_length=50)]

    @field_validator("role")
    def validate_role(cls, value):
        valid_roles = ["Commander", "Pilot", "Mission Specialist", "Flight Engineer", "Payload Specialist"]
        if value not in valid_roles:
            raise ValueError(f"Role must be one of: {', '.join(valid_roles)}")
        return value


@api.post("/missions/{mission_id}/astronauts")
async def add_astronaut(
    mission_id: int,
    data: Annotated[CreateAstronaut, Form()],
):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist:
        raise NotFound(detail=f"Mission {mission_id} not found")

    astronaut = await Astronaut.objects.acreate(
        name=data.name,
        role=data.role,
        mission=mission,
    )

    return {
        "id": astronaut.id,
        "name": astronaut.name,
        "role": astronaut.role,
        "mission": mission.name,
    }
```

The `CreateAstronaut` form model validates:
- **Field constraints** — Name and role have length limits
- **Custom validation** — Role must be one of the predefined options

Test with a form submission:

```bash
curl -X POST http://localhost:8000/missions/1/astronauts \
  -d "name=Neil Armstrong" \
  -d "role=Commander"
```

## File uploads

Handle file uploads using `File`. Let's add mission patch upload:

```python
from django_bolt.param_functions import File
import os


@api.post("/missions/{mission_id}/patch")
async def upload_mission_patch(
    mission_id: int,
    patch: Annotated[list[dict], File(alias="patch")],
):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist:
        raise NotFound(detail=f"Mission {mission_id} not found")

    if not patch:
        raise HTTPException(status_code=400, detail="No file uploaded")

    file_info = patch[0]
    filename = file_info.get("filename", "patch.png")
    content = file_info.get("content", b"")
    size = file_info.get("size", 0)

    # Save to media directory (simplified example)
    save_path = f"media/patches/{mission_id}_{filename}"
    os.makedirs("media/patches", exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(content)

    mission.patch_image = save_path
    await mission.asave()

    return {
        "message": "Mission patch uploaded successfully",
        "filename": filename,
        "size": size,
        "mission": mission.name,
    }
```

Test with a file:

```bash
curl -X POST http://localhost:8000/missions/1/patch \
  -F "patch=@mission_patch.png"
```

## Response serializers

So far we've been building response dicts by hand. That's a great way to learn the basics and keeps the early examples easy to follow.

Once the same response shapes start showing up in multiple endpoints, move them into `Serializer` classes too. That gives you reusable, typed response contracts and keeps the model-to-JSON mapping in one place.

```python
import asyncio

from django_bolt.serializers import Serializer, field


class MissionResponse(Serializer):
    id: int
    name: str
    status: str
    launch_date: datetime | None = None
    description: str = ""


class MissionListResponse(Serializer):
    missions: list[MissionResponse]
    count: int


class AstronautResponse(Serializer):
    id: int
    name: str
    role: str
    mission_id: int


class AstronautListResponse(Serializer):
    mission: str
    astronauts: list[AstronautResponse]


class AstronautCreatedResponse(Serializer):
    id: int
    name: str
    role: str
    mission: str = field(source="mission.name")


@api.get("/missions")
async def list_missions(filters: Annotated[MissionFilters, Query()]) -> MissionListResponse:
    queryset = Mission.objects.all()
    if filters.status:
        queryset = queryset.filter(status=filters.status)

    mission_tasks = [
        MissionResponse.afrom_model(mission)
        async for mission in queryset[:filters.limit]
    ]
    missions = list(await asyncio.gather(*mission_tasks))

    return MissionListResponse(missions=missions, count=len(missions))


@api.get("/missions/{mission_id}")
async def get_mission(mission_id: int) -> MissionResponse:
    try:
        mission = await Mission.objects.aget(id=mission_id)
        return await MissionResponse.afrom_model(mission)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc


@api.post("/missions/{mission_id}/astronauts")
async def add_astronaut(
    mission_id: int,
    data: Annotated[CreateAstronaut, Form()],
) -> AstronautCreatedResponse:
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
```

A few things to notice:

- In async handlers, `await afrom_model()` is the safest default for response serializers. It keeps working if you later add related fields.
- `from_model()` is still great when you're mapping already-loaded model data in sync code, or when you know your async response stays flat and fully loaded.
- Plain `list[AstronautResponse]` is enough for nested response fields. The type hint is the only nesting API.

## Response types

Django-Bolt supports multiple response types. Let's add some variety:

```python
from django_bolt.responses import PlainText, HTML, Redirect


@api.get("/missions/{mission_id}/log")
async def get_mission_log(mission_id: int):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist:
        raise NotFound(detail=f"Mission {mission_id} not found")

    log = f"""
=== MISSION LOG: {mission.name} ===
Status: {mission.status.upper()}
Launch Date: {mission.launch_date or 'TBD'}
Description: {mission.description or 'No description'}
================================
    """.strip()

    return PlainText(log)


@api.get("/status-page")
async def status_page():
    return HTML("""
        <html>
        <head><title>Mission Control</title></head>
        <body style="font-family: monospace; background: #000; color: #0f0; padding: 20px;">
            <h1>MISSION CONTROL STATUS</h1>
            <p>All systems operational</p>
            <p>Visit <a href="/docs" style="color: #0ff;">/docs</a> for API documentation</p>
        </body>
        </html>
    """)


@api.get("/go")
async def go_to_dashboard():
    return Redirect("/status-page")
```

- `/missions/1/log` — Plain text mission log
- `/status-page` — HTML status page
- `/go` — Redirects to status page

## Django templates

Render Django templates using the `render` function:

First, create the template at `missions/templates/missions/dashboard.html`:

```html
<!DOCTYPE html>
<html>
<head>
    <title>Mission Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background: #1a1a2e; color: #eee; }
        h1 { color: #00d4ff; }
        .mission { background: #16213e; padding: 15px; margin: 10px 0; border-radius: 8px; }
        .status { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; }
        .planned { background: #ffc107; color: #000; }
        .active { background: #28a745; }
        .completed { background: #6c757d; }
    </style>
</head>
<body>
    <h1>Mission Dashboard</h1>
    <p>Total missions: {{ missions|length }}</p>
    {% for mission in missions %}
    <div class="mission">
        <strong>{{ mission.name }}</strong>
        <span class="status {{ mission.status }}">{{ mission.status|upper }}</span>
        <p>{{ mission.description|default:"No description" }}</p>
    </div>
    {% empty %}
    <p>No missions found.</p>
    {% endfor %}
</body>
</html>
```

Now add the endpoint:

```python
from django_bolt import Request
from django_bolt.shortcuts import render


@api.get("/dashboard")
async def mission_dashboard(request: Request):
    missions = []
    async for mission in Mission.objects.all()[:20]:
        missions.append({
            "name": mission.name,
            "status": mission.status,
            "description": mission.description,
        })

    return render(request, "missions/dashboard.html", {"missions": missions})
```

Visit `http://localhost:8000/dashboard` to see the rendered dashboard.

## Error handling

You've already seen error handling throughout this tutorial. Here's a summary of available exceptions:

```python
from django_bolt.exceptions import (
    HTTPException,  # Generic exception with custom status code
    BadRequest,     # 400
    Unauthorized,   # 401
    Forbidden,      # 403
    NotFound,       # 404
)

# Generic exception
raise HTTPException(status_code=418, detail="I'm a teapot")

# Convenience exceptions
raise BadRequest(detail="Invalid mission parameters")
raise Unauthorized(detail="Authentication required")
raise Forbidden(detail="Insufficient permissions")
raise NotFound(detail="Mission not found")
```

## API documentation

Django-Bolt automatically generates OpenAPI documentation. Visit `http://localhost:8000/docs` to see the interactive Swagger UI.

Add descriptions and tags to improve your documentation:

```python
@api.get(
    "/missions/{mission_id}",
    summary="Get mission details",
    description="Retrieve detailed information about a specific space mission",
    tags=["missions"],
)
async def get_mission(mission_id: int):
    ...
```

You can also configure API-level settings via `OpenAPIConfig`:

```python
from django_bolt.openapi import OpenAPIConfig

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="Space Mission Tracker",
        description="NASA-style mission control API",
        version="1.0.0",
    )
)
```

## Complete code

Here's the complete `missions/api.py` file:

```python
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Annotated, Literal

from msgspec import Meta

from django_bolt import BoltAPI, Request
from django_bolt.exceptions import HTTPException, NotFound
from django_bolt.openapi import OpenAPIConfig
from django_bolt.param_functions import File, Form, Header, Query
from django_bolt.responses import HTML, PlainText, Redirect
from django_bolt.serializers import Serializer, field, field_validator
from django_bolt.shortcuts import render

from missions.models import Astronaut, Mission

api = BoltAPI(
    openapi_config=OpenAPIConfig(
        title="Space Mission Tracker",
        description="NASA-style mission control API",
        version="1.0.0",
    )
)


# Input and output schemas
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


# Endpoints
@api.get("/", tags=["status"])
async def mission_control_status():
    return {"status": "operational", "message": "Mission Control Online"}


@api.get("/missions", tags=["missions"])
async def list_missions(filters: Annotated[MissionFilters, Query()]) -> MissionListResponse:
    queryset = Mission.objects.all()
    if filters.status:
        queryset = queryset.filter(status=filters.status)

    mission_tasks = [
        MissionResponse.afrom_model(mission)
        async for mission in queryset[:filters.limit]
    ]
    missions = list(await asyncio.gather(*mission_tasks))
    return MissionListResponse(missions=missions, count=len(missions))


@api.get("/missions/{mission_id}", tags=["missions"])
async def get_mission(mission_id: int) -> MissionResponse:
    try:
        mission = await Mission.objects.aget(id=mission_id)
        return await MissionResponse.afrom_model(mission)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc


@api.post("/missions", tags=["missions"])
async def create_mission(mission: CreateMission) -> MissionCreatedResponse:
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


@api.put("/missions/{mission_id}", tags=["missions"])
async def update_mission(mission_id: int, data: UpdateMission) -> MissionResponse:
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


@api.delete("/missions/{mission_id}", status_code=204, tags=["missions"])
async def delete_mission(mission_id: int):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    await mission.adelete()


@api.get("/missions/{mission_id}/classified", tags=["missions"])
async def get_classified_info(
    mission_id: int,
    clearance: Annotated[str, Header(alias="X-Clearance-Level")],
):
    if clearance not in ["top-secret", "confidential"]:
        raise HTTPException(status_code=403, detail="Insufficient clearance level")

    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    return {
        "mission": mission.name,
        "classified_data": "Launch codes: APOLLO-7749-OMEGA",
    }


@api.get("/missions/{mission_id}/log", tags=["missions"])
async def get_mission_log(mission_id: int):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    log = f"=== MISSION LOG: {mission.name} ===\nStatus: {mission.status.upper()}"
    return PlainText(log)


@api.post("/missions/{mission_id}/patch", tags=["missions"])
async def upload_mission_patch(
    mission_id: int,
    patch: Annotated[list[dict], File(alias="patch")],
):
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    if not patch:
        raise HTTPException(status_code=400, detail="No file uploaded")

    file_info = patch[0]
    filename = file_info.get("filename", "patch.png")
    content = file_info.get("content", b"")

    save_path = f"media/patches/{mission_id}_{filename}"
    os.makedirs("media/patches", exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(content)

    mission.patch_image = save_path
    await mission.asave()

    return {"message": "Patch uploaded", "filename": filename}


@api.post("/missions/{mission_id}/astronauts", tags=["astronauts"])
async def add_astronaut(
    mission_id: int,
    data: Annotated[CreateAstronaut, Form()],
) -> AstronautCreatedResponse:
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


@api.get("/missions/{mission_id}/astronauts", tags=["astronauts"])
async def list_astronauts(mission_id: int) -> AstronautListResponse:
    try:
        mission = await Mission.objects.aget(id=mission_id)
    except Mission.DoesNotExist as exc:
        raise NotFound(detail=f"Mission {mission_id} not found") from exc

    astronaut_tasks = [
        AstronautResponse.afrom_model(astronaut)
        async for astronaut in Astronaut.objects.filter(mission=mission)
    ]
    astronauts = list(await asyncio.gather(*astronaut_tasks))
    return AstronautListResponse(mission=mission.name, astronauts=astronauts)


@api.get("/status-page", tags=["status"])
async def status_page():
    return HTML("<h1>Mission Control: All Systems Operational</h1>")


@api.get("/go", tags=["status"])
async def go_to_dashboard():
    return Redirect("/dashboard")


@api.get("/dashboard", tags=["status"])
async def mission_dashboard(request: Request):
    missions = []
    async for mission in Mission.objects.all()[:20]:
        missions.append({
            "name": mission.name,
            "status": mission.status,
            "description": mission.description,
        })
    return render(request, "missions/dashboard.html", {"missions": missions})
```

## Next steps

You've built a complete Space Mission Tracker API. Here's where to go next:

- **[Deployment](deployment.md)** — Deploy with multiple processes for production
- **[Authentication](../topics/authentication.md)** — Add JWT or API key authentication
- **[Class-Based Views](../topics/class-based-views.md)** — Organize routes with ViewSets
- **[Middleware](../topics/middleware.md)** — Add CORS, rate limiting, and custom middleware
