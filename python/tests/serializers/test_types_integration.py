"""Integration tests for serializer types with TestClient.

These tests verify that custom validated types (Email, URL, PositiveInt, etc.)
work correctly through the full HTTP request/response cycle:
- Request body validation (POST/PUT)
- Response serialization (GET)
- Error handling for invalid values
"""

from __future__ import annotations

import pytest

from django_bolt.api import BoltAPI
from django_bolt.serializers import (
    URL,
    Email,
    HttpsURL,
    NonEmptyStr,
    NonNegativeInt,
    Percentage,
    Phone,
    PositiveInt,
    Serializer,
    Slug,
    Username,
    computed_field,
)
from django_bolt.serializers.types import (
    UUID,
    HexColor,
    IPv4,
    Latitude,
    Longitude,
    Port,
)
from django_bolt.testing import TestClient

# =============================================================================
# SERIALIZERS - Using validated types
# =============================================================================


class UserInputSerializer(Serializer):
    """Input serializer with validated types."""

    email: Email
    username: Username
    website: URL | None = None


class UserOutputSerializer(Serializer):
    """Output serializer with validated types."""

    id: PositiveInt
    email: Email
    username: Username
    website: URL | None = None


class ContactSerializer(Serializer):
    """Contact serializer with phone and email validation."""

    name: NonEmptyStr
    email: Email
    phone: Phone


class LocationSerializer(Serializer):
    """Location serializer with geo coordinate validation."""

    name: str
    lat: Latitude
    lng: Longitude


class ServerConfigSerializer(Serializer):
    """Server config with port and IP validation."""

    name: str
    host: IPv4
    port: Port


class ThemeSerializer(Serializer):
    """Theme config with hex color validation."""

    name: str
    primary_color: HexColor
    secondary_color: HexColor


class ProgressSerializer(Serializer):
    """Progress tracker with percentage validation."""

    task: str
    completion: Percentage


class EntitySerializer(Serializer):
    """Entity with UUID validation."""

    id: UUID
    name: str


class BlogPostSerializer(Serializer):
    """Blog post with slug and URL validation."""

    title: str
    slug: Slug
    external_url: HttpsURL | None = None


class PaginationSerializer(Serializer):
    """Pagination params with non-negative integers."""

    page: PositiveInt
    per_page: PositiveInt
    total: NonNegativeInt


# =============================================================================
# API ENDPOINTS
# =============================================================================


api = BoltAPI()


# --- User endpoints ---


@api.post("/users")
async def create_user(data: UserInputSerializer):
    """Create user with validated email and username."""
    return UserOutputSerializer(
        id=1,
        email=data.email,
        username=data.username,
        website=data.website,
    )


@api.get("/users/{user_id}")
async def get_user(user_id: int):
    """Get user - returns validated types."""
    return UserOutputSerializer(
        id=user_id,
        email="test@example.com",
        username="john_doe",
        website="https://example.com",
    )


# --- Contact endpoints ---


@api.post("/contacts")
async def create_contact(data: ContactSerializer):
    """Create contact with phone validation."""
    return {"success": True, "contact": data.dump()}


# --- Location endpoints ---


@api.post("/locations")
async def create_location(data: LocationSerializer):
    """Create location with lat/lng validation."""
    return {"success": True, "location": data.dump()}


# --- Server config endpoints ---


@api.post("/servers")
async def create_server(data: ServerConfigSerializer):
    """Create server config with IP/port validation."""
    return {"success": True, "server": data.dump()}


# --- Theme endpoints ---


@api.post("/themes")
async def create_theme(data: ThemeSerializer):
    """Create theme with hex color validation."""
    return {"success": True, "theme": data.dump()}


# --- Progress endpoints ---


@api.post("/progress")
async def update_progress(data: ProgressSerializer):
    """Update progress with percentage validation."""
    return {"success": True, "progress": data.dump()}


# --- Entity endpoints ---


@api.post("/entities")
async def create_entity(data: EntitySerializer):
    """Create entity with UUID validation."""
    return {"success": True, "entity": data.dump()}


# --- Blog post endpoints ---


@api.post("/posts")
async def create_post(data: BlogPostSerializer):
    """Create blog post with slug and URL validation."""
    return {"success": True, "post": data.dump()}


# --- Pagination endpoints ---


@api.post("/paginate")
async def paginate(data: PaginationSerializer):
    """Paginate with validated integers."""
    return {"success": True, "pagination": data.dump()}


# =============================================================================
# SUBSET/FIELDS TYPE-SAFE ENDPOINTS
# =============================================================================


class FullUserSerializer(Serializer):
    """Full user serializer with all fields."""

    id: PositiveInt
    email: Email
    username: Username
    phone: Phone | None = None
    website: URL | None = None
    bio: str = ""

    class Config:
        field_sets = {
            "list": ["id", "username"],
            "detail": ["id", "email", "username", "website"],
        }

    @computed_field
    def display_name(self) -> str:
        return f"@{self.username}"


# Create type-safe serializer subclasses
UserListSerializer = FullUserSerializer.fields("list")
UserDetailSerializer = FullUserSerializer.fields("detail")
UserPublicSerializer = FullUserSerializer.subset("id", "username", "display_name")


@api.get("/v2/users")
async def list_users_v2():
    """List users with type-safe subset serializer."""
    users = [
        UserListSerializer(id=1, username="john_doe"),
        UserListSerializer(id=2, username="jane_doe"),
    ]
    return [u.dump() for u in users]


@api.get("/v2/users/{user_id}")
async def get_user_detail_v2(user_id: int):
    """Get user detail with type-safe subset serializer."""
    user = UserDetailSerializer(
        id=user_id,
        email="john@example.com",
        username="john_doe",
        website="https://johndoe.com",
    )
    return user.dump()


@api.get("/v2/users/{user_id}/public")
async def get_user_public_v2(user_id: int):
    """Get public user with computed field via subset."""
    user = UserPublicSerializer(id=user_id, username="john_doe")
    return user.dump()


# =============================================================================
# TEST FIXTURES
# =============================================================================


@pytest.fixture
def client():
    """TestClient for type validation API."""
    return TestClient(api)


# =============================================================================
# TESTS - Valid Input
# =============================================================================


class TestValidTypeInput:
    """Test valid input with validated types."""

    def test_create_user_valid(self, client):
        """Test creating user with valid email and username."""
        payload = {
            "email": "test@example.com",
            "username": "john_doe",
            "website": "https://example.com",
        }
        response = client.post("/users", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 1
        assert data["email"] == "test@example.com"
        assert data["username"] == "john_doe"

    def test_create_contact_valid(self, client):
        """Test creating contact with valid phone."""
        payload = {
            "name": "John Doe",
            "email": "john@example.com",
            "phone": "+14155551234",
        }
        response = client.post("/contacts", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["contact"]["phone"] == "+14155551234"

    def test_create_location_valid(self, client):
        """Test creating location with valid lat/lng."""
        payload = {
            "name": "San Francisco",
            "lat": 37.7749,
            "lng": -122.4194,
        }
        response = client.post("/locations", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["location"]["lat"] == pytest.approx(37.7749)
        assert data["location"]["lng"] == pytest.approx(-122.4194)

    def test_create_server_valid(self, client):
        """Test creating server config with valid IP and port."""
        payload = {
            "name": "Web Server",
            "host": "192.168.1.1",
            "port": 8080,
        }
        response = client.post("/servers", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["server"]["host"] == "192.168.1.1"
        assert data["server"]["port"] == 8080

    def test_create_theme_valid(self, client):
        """Test creating theme with valid hex colors."""
        payload = {
            "name": "Dark Theme",
            "primary_color": "#FF5733",
            "secondary_color": "#000000",
        }
        response = client.post("/themes", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["theme"]["primary_color"] == "#FF5733"

    def test_update_progress_valid(self, client):
        """Test updating progress with valid percentage."""
        payload = {
            "task": "Build feature",
            "completion": 75.5,
        }
        response = client.post("/progress", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["progress"]["completion"] == pytest.approx(75.5)

    def test_create_entity_valid(self, client):
        """Test creating entity with valid UUID."""
        payload = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "name": "Test Entity",
        }
        response = client.post("/entities", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["entity"]["id"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_create_post_valid(self, client):
        """Test creating blog post with valid slug."""
        payload = {
            "title": "My Blog Post",
            "slug": "my-blog-post",
            "external_url": "https://example.com/post",
        }
        response = client.post("/posts", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["post"]["slug"] == "my-blog-post"

    def test_paginate_valid(self, client):
        """Test pagination with valid positive integers."""
        payload = {
            "page": 1,
            "per_page": 20,
            "total": 100,
        }
        response = client.post("/paginate", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["pagination"]["page"] == 1


# =============================================================================
# TESTS - Invalid Input (Validation Errors)
# =============================================================================


class TestInvalidTypeInput:
    """Test invalid input triggers validation errors."""

    def test_create_user_invalid_email(self, client):
        """Test that invalid email is rejected."""
        payload = {
            "email": "not-an-email",
            "username": "john_doe",
        }
        response = client.post("/users", json=payload)

        assert response.status_code in [400, 422]

    def test_create_user_invalid_username_with_space(self, client):
        """Test that username with space is rejected."""
        payload = {
            "email": "test@example.com",
            "username": "user name",  # Space not allowed
        }
        response = client.post("/users", json=payload)

        assert response.status_code in [400, 422]

    @pytest.mark.parametrize(
        ("path", "payload", "field_name"),
        [
            ("/users", {"email": "test@example.com", "username": "john_doe"}, "email"),
            ("/users", {"email": "test@example.com", "username": "john_doe"}, "username"),
            ("/users", {"email": "a@b.co", "username": "john", "website": "https://example.com"}, "website"),
            ("/contacts", {"name": "John", "email": "john@example.com", "phone": "+14155551234"}, "phone"),
            ("/servers", {"name": "Web", "host": "192.168.1.1", "port": 8080}, "host"),
            ("/themes", {"name": "Dark", "primary_color": "#FF5733", "secondary_color": "#000000"}, "primary_color"),
            ("/entities", {"id": "550e8400-e29b-41d4-a716-446655440000", "name": "Entity"}, "id"),
            ("/posts", {"title": "Post", "slug": "my-blog-post"}, "slug"),
            ("/posts", {"title": "Post", "slug": "post", "external_url": "https://example.com/post"}, "external_url"),
        ],
    )
    def test_value_with_a_line_break_at_its_end_is_rejected(self, client, path, payload, field_name):
        """A pattern type accepts the value, but not the value with a line break after it."""
        assert client.post(path, json=payload).status_code == 200

        response = client.post(path, json={**payload, field_name: payload[field_name] + "\n"})

        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["loc"] == ["body", field_name]

    def test_create_contact_invalid_phone(self, client):
        """Test that invalid phone is rejected."""
        payload = {
            "name": "John",
            "email": "john@example.com",
            "phone": "555-1234",  # Not E.164 format
        }
        response = client.post("/contacts", json=payload)

        assert response.status_code in [400, 422]

    def test_create_location_invalid_latitude(self, client):
        """Test that latitude > 90 is rejected."""
        payload = {
            "name": "Invalid",
            "lat": 91.0,  # Max 90
            "lng": 0.0,
        }
        response = client.post("/locations", json=payload)

        assert response.status_code in [400, 422]

    def test_create_location_invalid_longitude(self, client):
        """Test that longitude > 180 is rejected."""
        payload = {
            "name": "Invalid",
            "lat": 0.0,
            "lng": 181.0,  # Max 180
        }
        response = client.post("/locations", json=payload)

        assert response.status_code in [400, 422]

    def test_create_server_invalid_port_zero(self, client):
        """Test that port 0 is rejected."""
        payload = {
            "name": "Server",
            "host": "192.168.1.1",
            "port": 0,  # Min 1
        }
        response = client.post("/servers", json=payload)

        assert response.status_code in [400, 422]

    def test_create_server_invalid_port_too_high(self, client):
        """Test that port > 65535 is rejected."""
        payload = {
            "name": "Server",
            "host": "192.168.1.1",
            "port": 65536,  # Max 65535
        }
        response = client.post("/servers", json=payload)

        assert response.status_code in [400, 422]

    def test_create_server_invalid_ip(self, client):
        """Test that invalid IP address is rejected."""
        payload = {
            "name": "Server",
            "host": "999.999.999.999",  # Invalid IP
            "port": 8080,
        }
        response = client.post("/servers", json=payload)

        assert response.status_code in [400, 422]

    def test_create_theme_invalid_hex_color(self, client):
        """Test that invalid hex color is rejected."""
        payload = {
            "name": "Theme",
            "primary_color": "FF5733",  # Missing #
            "secondary_color": "#000000",
        }
        response = client.post("/themes", json=payload)

        assert response.status_code in [400, 422]

    def test_create_theme_invalid_hex_color_short(self, client):
        """Test that short hex color is rejected."""
        payload = {
            "name": "Theme",
            "primary_color": "#FFF",  # Too short
            "secondary_color": "#000000",
        }
        response = client.post("/themes", json=payload)

        assert response.status_code in [400, 422]

    def test_update_progress_invalid_percentage_negative(self, client):
        """Test that negative percentage is rejected."""
        payload = {
            "task": "Task",
            "completion": -1.0,  # Min 0
        }
        response = client.post("/progress", json=payload)

        assert response.status_code in [400, 422]

    def test_update_progress_invalid_percentage_over_100(self, client):
        """Test that percentage > 100 is rejected."""
        payload = {
            "task": "Task",
            "completion": 100.1,  # Max 100
        }
        response = client.post("/progress", json=payload)

        assert response.status_code in [400, 422]

    def test_create_entity_invalid_uuid(self, client):
        """Test that invalid UUID is rejected."""
        payload = {
            "id": "not-a-uuid",
            "name": "Entity",
        }
        response = client.post("/entities", json=payload)

        assert response.status_code in [400, 422]

    def test_create_post_invalid_slug(self, client):
        """Test that invalid slug is rejected."""
        payload = {
            "title": "Post",
            "slug": "My Blog Post",  # Spaces not allowed
        }
        response = client.post("/posts", json=payload)

        assert response.status_code in [400, 422]

    def test_create_post_invalid_https_url(self, client):
        """Test that HTTP URL is rejected when HTTPS is required."""
        payload = {
            "title": "Post",
            "slug": "my-post",
            "external_url": "http://example.com",  # Must be HTTPS
        }
        response = client.post("/posts", json=payload)

        assert response.status_code in [400, 422]

    def test_paginate_invalid_page_zero(self, client):
        """Test that page 0 is rejected."""
        payload = {
            "page": 0,  # Must be > 0
            "per_page": 20,
            "total": 100,
        }
        response = client.post("/paginate", json=payload)

        assert response.status_code in [400, 422]

    def test_paginate_invalid_page_negative(self, client):
        """Test that negative page is rejected."""
        payload = {
            "page": -1,
            "per_page": 20,
            "total": 100,
        }
        response = client.post("/paginate", json=payload)

        assert response.status_code in [400, 422]

    def test_paginate_invalid_total_negative(self, client):
        """Test that negative total is rejected."""
        payload = {
            "page": 1,
            "per_page": 20,
            "total": -5,  # Must be >= 0
        }
        response = client.post("/paginate", json=payload)

        assert response.status_code in [400, 422]


# =============================================================================
# TESTS - Type-Safe Subset Serializers
# =============================================================================


class TestSubsetIntegration:
    """Test subset() and fields() with API endpoints."""

    def test_list_users_returns_subset_fields(self, client):
        """Test that list endpoint returns only subset fields."""
        response = client.get("/v2/users")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

        # Should only have id and username
        for user in data:
            assert "id" in user
            assert "username" in user
            # Should NOT have email, phone, etc.
            assert "email" not in user
            assert "phone" not in user
            assert "display_name" not in user

    def test_get_user_detail_returns_detail_fields(self, client):
        """Test that detail endpoint returns detail fields."""
        response = client.get("/v2/users/1")

        assert response.status_code == 200
        data = response.json()

        # Should have detail fields
        assert data["id"] == 1
        assert data["email"] == "john@example.com"
        assert data["username"] == "john_doe"
        assert data["website"] == "https://johndoe.com"

        # Should NOT have phone, bio
        assert "phone" not in data
        assert "bio" not in data

    def test_get_user_public_includes_computed_field(self, client):
        """Test that public endpoint includes computed display_name."""
        response = client.get("/v2/users/1/public")

        assert response.status_code == 200
        data = response.json()

        # Should have id, username, and computed display_name
        assert data["id"] == 1
        assert data["username"] == "john_doe"
        assert data["display_name"] == "@john_doe"

        # Should NOT have email, website, etc.
        assert "email" not in data
        assert "website" not in data


# =============================================================================
# TESTS - Response Serialization
# =============================================================================


class TestResponseSerialization:
    """Test that responses are properly serialized with validated types."""

    def test_get_user_response_format(self, client):
        """Test that GET response uses validated types correctly."""
        response = client.get("/users/42")

        assert response.status_code == 200
        data = response.json()

        # All values should be properly serialized
        assert data["id"] == 42
        assert data["email"] == "test@example.com"
        assert data["username"] == "john_doe"
        assert data["website"] == "https://example.com"

    def test_edge_case_latitude_bounds(self, client):
        """Test latitude at exact bounds."""
        # Test -90 (minimum)
        payload = {"name": "South Pole", "lat": -90.0, "lng": 0.0}
        response = client.post("/locations", json=payload)
        assert response.status_code == 200

        # Test 90 (maximum)
        payload = {"name": "North Pole", "lat": 90.0, "lng": 0.0}
        response = client.post("/locations", json=payload)
        assert response.status_code == 200

    def test_edge_case_longitude_bounds(self, client):
        """Test longitude at exact bounds."""
        # Test -180 (minimum)
        payload = {"name": "West", "lat": 0.0, "lng": -180.0}
        response = client.post("/locations", json=payload)
        assert response.status_code == 200

        # Test 180 (maximum)
        payload = {"name": "East", "lat": 0.0, "lng": 180.0}
        response = client.post("/locations", json=payload)
        assert response.status_code == 200

    def test_edge_case_port_bounds(self, client):
        """Test port at exact bounds."""
        # Test 1 (minimum)
        payload = {"name": "Server", "host": "127.0.0.1", "port": 1}
        response = client.post("/servers", json=payload)
        assert response.status_code == 200

        # Test 65535 (maximum)
        payload = {"name": "Server", "host": "127.0.0.1", "port": 65535}
        response = client.post("/servers", json=payload)
        assert response.status_code == 200

    def test_edge_case_percentage_bounds(self, client):
        """Test percentage at exact bounds."""
        # Test 0 (minimum)
        payload = {"task": "Start", "completion": 0.0}
        response = client.post("/progress", json=payload)
        assert response.status_code == 200

        # Test 100 (maximum)
        payload = {"task": "Done", "completion": 100.0}
        response = client.post("/progress", json=payload)
        assert response.status_code == 200
