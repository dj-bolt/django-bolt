"""
Integration tests for nested serializers with async/sync views and TestClient.

This test suite demonstrates 5 different API endpoints that use nested serializers:
1. Simple nested ForeignKey (Author in BlogPost) - ASYNC endpoints with async ORM
2. Many-to-many nested relationships (Tags in BlogPost) - SYNC endpoints
3. Deeply nested structures (Comments with Authors in BlogPost) - SYNC endpoints
4. Mixed nested relationships with validation - SYNC endpoints
5. User authentication & profile management - ASYNC endpoints with async ORM

These tests use TestClient to make real HTTP requests and verify the full
request/response cycle with nested serializers. Tests demonstrate that both
async and sync endpoints work seamlessly with sync tests through TestClient.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated

import msgspec
import pytest
from asgiref.sync import sync_to_async
from msgspec import Meta

from django_bolt.api import BoltAPI
from django_bolt.exceptions import BadRequest
from django_bolt.serializers import (
    Email,
    NonEmptyStr,
    Serializer,
    computed_field,
    field,
    field_validator,
    model_validator,
)
from django_bolt.testing import TestClient
from tests.test_models import Author, BlogPost, Comment, Tag, User

# ============================================================================
# SERIALIZERS - Define all serializers for nested relationships
# ============================================================================


class AuthorSerializer(Serializer):
    """Serializer for Author model with email validation."""

    id: int
    # Use Meta(min_length=2) for declarative validation
    name: Annotated[str, Meta(min_length=2)]
    # Use Meta(pattern=...) for email validation
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+\.[^@]+$")]
    bio: str = ""

    @field_validator("name")
    def strip_name(cls, value: str) -> str:
        """Strip whitespace from name."""
        return value.strip()

    @field_validator("email")
    def lowercase_email(cls, value: str) -> str:
        """Convert email to lowercase."""
        return value.lower()


class TagSerializer(Serializer):
    """Serializer for Tag model."""

    id: int
    name: str
    description: str = ""


class CommentSerializer(Serializer):
    """Serializer for Comment model with nested author."""

    id: int
    text: str
    # Author as full object
    author: AuthorSerializer


class BlogPostSerializer(Serializer):
    """Serializer for BlogPost with nested author and tags - OUTPUT ONLY."""

    id: int
    title: str
    content: str
    # Author relationship - full object required
    author: AuthorSerializer
    # Tags relationship - full objects required
    tags: list[TagSerializer]
    published: bool = False


class BlogPostInputSerializer(Serializer):
    """Serializer for BlogPost input (CREATE/UPDATE) - accepts nested objects only."""

    title: str
    content: str
    # For input, we accept full nested objects (msgspec can decode these)
    author: AuthorSerializer
    tags: list[TagSerializer]
    published: bool = False


class BlogPostDetailedSerializer(Serializer):
    """Serializer for BlogPost with nested comments - OUTPUT ONLY."""

    id: int
    title: str
    content: str
    author: AuthorSerializer
    tags: list[TagSerializer]
    # Comments nested with their own nested authors
    comments: list[CommentSerializer]
    published: bool = False


class BlogPostDetailedInputSerializer(Serializer):
    """Serializer for BlogPost with nested comments - INPUT for CREATE."""

    title: str
    content: str
    author: AuthorSerializer
    tags: list[TagSerializer]
    # Comments nested with their own nested authors
    comments: list[CommentSerializer]
    published: bool = False


# ============================================================================
# API 1: Simple Nested ForeignKey (ASYNC with async ORM queries)
# ============================================================================


api_1_nested_fk = BoltAPI()


@api_1_nested_fk.get("/posts/{post_id}")
async def get_post_simple(post_id: int):
    """
    API 1: Get a single post with its nested author (ASYNC).

    Query with select_related to include the author as a nested object.
    Uses async ORM (.aget) with prefetch_related to demonstrate async query support.
    """
    post = await BlogPost.objects.select_related("author").prefetch_related("tags").aget(id=post_id)
    return BlogPostSerializer.from_model(post)


@api_1_nested_fk.post("/posts")
async def create_post_simple(data: BlogPostInputSerializer):
    """
    API 1: Create a new post with author reference (ASYNC).

    Accepts nested author (must be full object for input).
    Uses async ORM (.acreate) to demonstrate async query support.
    After creation, refetches with select_related/prefetch_related for serialization.
    """
    author_id = data.author.id
    created_post = await BlogPost.objects.acreate(
        title=data.title,
        content=data.content,
        author_id=author_id,
        published=data.published,
    )

    # Refetch with relationships for serialization
    post = await BlogPost.objects.select_related("author").prefetch_related("tags").aget(id=created_post.id)
    return BlogPostSerializer.from_model(post)


# ============================================================================
# API 2: Many-to-Many Nested Relationships
# ============================================================================


api_2_nested_m2m = BoltAPI()


@api_2_nested_m2m.get("/posts/{post_id}/detailed")
def get_post_with_tags(post_id: int):
    """
    API 2: Get a post with author and tags as nested objects.

    Uses prefetch_related to load tags efficiently.
    """
    post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post_id)
    return BlogPostSerializer.from_model(post)


@api_2_nested_m2m.post("/posts/with-tags")
def create_post_with_tags(data: BlogPostInputSerializer):
    """
    API 2: Create a post with author and tags.

    Accepts both author and tags as full nested objects.
    """
    author_id = data.author.id

    post = BlogPost.objects.create(
        title=data.title,
        content=data.content,
        author_id=author_id,
        published=data.published,
    )

    # Add tags
    tag_ids = [tag.id for tag in data.tags]
    if tag_ids:
        post.tags.set(tag_ids)

    post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post.id)
    return BlogPostSerializer.from_model(post)


# ============================================================================
# API 3: Deeply Nested Structures (Nested within Nested)
# ============================================================================


api_3_deeply_nested = BoltAPI()


@api_3_deeply_nested.get("/posts/{post_id}/full")
def get_post_full(post_id: int):
    """
    API 3: Get a post with all relationships fully populated.

    Includes author, tags, and comments with their authors.
    This demonstrates deeply nested structures (comments -> authors).
    """
    post = BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").get(id=post_id)
    return BlogPostDetailedSerializer.from_model(post)


@api_3_deeply_nested.post("/posts/full")
def create_post_full(data: BlogPostDetailedInputSerializer):
    """
    API 3: Create a post with full nested structure.

    Accepts author, tags, and comments (each with their own nested authors).
    """
    # Extract author ID
    author_id = data.author.id

    # Create post
    post = BlogPost.objects.create(
        title=data.title,
        content=data.content,
        author_id=author_id,
        published=data.published,
    )

    # Add tags
    tag_ids = [tag.id for tag in data.tags]
    if tag_ids:
        post.tags.set(tag_ids)

    # Add comments
    for comment_data in data.comments:
        comment_author_id = (
            comment_data.author.id if isinstance(comment_data.author, AuthorSerializer) else comment_data.author
        )

        Comment.objects.create(
            post=post,
            author_id=comment_author_id,
            text=comment_data.text,
        )

    post = BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").get(id=post.id)
    return BlogPostDetailedSerializer.from_model(post)


# ============================================================================
# API 4: Mixed Nested with Validation
# ============================================================================


class BlogPostCreateSerializer(Serializer):
    """Stricter serializer for creating posts - author and tags must be full objects."""

    # Use Meta(min_length=...) for declarative validation
    title: Annotated[str, Meta(min_length=3)]
    content: Annotated[str, Meta(min_length=10)]
    # Author must be a full object, not just ID
    author: AuthorSerializer
    # Tags must be full objects for input
    tags: list[TagSerializer]
    published: bool = False

    @field_validator("title")
    def strip_title(cls, value: str) -> str:
        """Strip whitespace from title."""
        return value.strip()

    @field_validator("content")
    def strip_content(cls, value: str) -> str:
        """Strip whitespace from content."""
        return value.strip()


api_4_mixed_validation = BoltAPI()


@api_4_mixed_validation.post("/posts/strict")
def create_post_strict(data: BlogPostCreateSerializer):
    """
    API 4: Create a post with strict validation.

    - Author must be a full AuthorSerializer (not just ID)
    - Tags must be full TagSerializers (not just IDs)
    - Title and content are validated for minimum length
    - Author validation (email, name) is enforced
    """
    # At this point, data.author is guaranteed to be an AuthorSerializer
    # and data.tags is guaranteed to be list[TagSerializer]
    # because allow_id_fallback=False

    # Create post
    post = BlogPost.objects.create(
        title=data.title,
        content=data.content,
        author_id=data.author.id,
        published=data.published,
    )

    # Add tags
    tag_ids = [tag.id for tag in data.tags]
    if tag_ids:
        post.tags.set(tag_ids)

    post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post.id)
    return BlogPostSerializer.from_model(post)


@api_4_mixed_validation.get("/posts/{post_id}/validate")
def get_post_and_validate(post_id: int):
    """
    API 4: Get a post and validate the response structure.

    Demonstrates that the response is properly serialized and can be
    re-validated through the same serializer.
    """
    post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post_id)

    # Get the response data
    response_data = BlogPostSerializer.from_model(post)

    # Validate that it can be re-serialized
    # This ensures type safety and structure consistency
    validated = BlogPostSerializer(
        id=response_data.id,
        title=response_data.title,
        content=response_data.content,
        author=response_data.author,
        tags=response_data.tags,
        published=response_data.published,
    )

    return validated


# ============================================================================
# PYTEST FIXTURES - TestClient instances for each API
# ============================================================================


@pytest.fixture
def client_api_1():
    """TestClient for API 1: Simple nested ForeignKey."""
    return TestClient(api_1_nested_fk)


@pytest.fixture
def client_api_2():
    """TestClient for API 2: Many-to-many nested relationships."""
    return TestClient(api_2_nested_m2m)


@pytest.fixture
def client_api_3():
    """TestClient for API 3: Deeply nested structures."""
    return TestClient(api_3_deeply_nested)


@pytest.fixture
def client_api_4():
    """TestClient for API 4: Mixed nested with validation."""
    return TestClient(api_4_mixed_validation)


# ============================================================================
# TESTS - Using TestClient for HTTP request/response testing
# ============================================================================


class TestAPI1SimpleNestedFK:
    """Test API 1: Simple nested ForeignKey relationships."""

    @pytest.mark.django_db(transaction=True)
    def test_get_post_with_select_related(self, client_api_1):
        """Test getting a post with author loaded via select_related."""
        # Create test data
        author = Author.objects.create(name="Alice", email="alice@example.com")
        post = BlogPost.objects.create(title="Test Post", content="This is test content", author=author)

        # Make HTTP request
        response = client_api_1.get(f"/posts/{post.id}")

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Post"
        assert data["id"] == post.id
        # Author should be either ID or nested object
        if isinstance(data["author"], dict):
            assert data["author"]["name"] == "Alice"
        else:
            assert data["author"] == author.id

    @pytest.mark.django_db(transaction=True)
    def test_create_post_with_author_dict(self, client_api_1):
        """Test creating a post with author as nested dict via POST."""
        # Create author first
        author = Author.objects.create(name="Bob", email="bob@example.com")

        # Send POST request with nested author dict
        payload = {
            "title": "New Post",
            "content": "New post content here",
            "author": {"id": author.id, "name": "Bob", "email": "bob@example.com"},
            "tags": [],
            "published": False,
        }
        response = client_api_1.post("/posts", json=payload)

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "New Post"
        assert BlogPost.objects.filter(title="New Post").exists()

    @pytest.mark.django_db(transaction=True)
    def test_create_post_with_author_id(self, client_api_1):
        """Test creating a post with author as nested object."""
        # Create author first
        author = Author.objects.create(name="Charlie", email="charlie@example.com")

        # Send POST request with full author object (required by input serializer)
        payload = {
            "title": "Simple Post",
            "content": "Simple post content",
            "author": {"id": author.id, "name": "Charlie", "email": "charlie@example.com"},
            "tags": [],
            "published": False,
        }
        response = client_api_1.post("/posts", json=payload)

        # Verify response and database
        assert response.status_code == 200
        created_post = BlogPost.objects.get(title="Simple Post")
        assert created_post.author_id == author.id


class TestAPI2NestedM2M:
    """Test API 2: Many-to-many nested relationships."""

    @pytest.mark.django_db(transaction=True)
    def test_get_post_with_tags_prefetch(self, client_api_2):
        """Test getting a post with tags loaded via prefetch_related."""
        # Create test data
        author = Author.objects.create(name="Dave", email="dave@example.com")
        post = BlogPost.objects.create(title="Tagged Post", content="Post content", author=author)

        # Create and add tags
        tag1 = Tag.objects.create(name="python")
        tag2 = Tag.objects.create(name="django")
        post.tags.add(tag1, tag2)

        # Make HTTP request
        response = client_api_2.get(f"/posts/{post.id}/detailed")

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Tagged Post"
        assert len(data["tags"]) == 2
        # Tags should be either IDs or nested objects
        for tag in data["tags"]:
            assert isinstance(tag, (int, dict))

    @pytest.mark.django_db(transaction=True)
    def test_create_post_with_tags_dicts(self, client_api_2):
        """Test creating a post with tags as nested dicts."""
        # Create author
        author = Author.objects.create(name="Eve", email="eve@example.com")

        # Create tags first
        tag1 = Tag.objects.create(name="testing")
        tag2 = Tag.objects.create(name="integration")

        # Send POST request with nested tag dicts and full author
        payload = {
            "title": "Post with Tags",
            "content": "Content with tags",
            "author": {"id": author.id, "name": "Eve", "email": "eve@example.com"},
            "tags": [
                {"id": tag1.id, "name": "testing"},
                {"id": tag2.id, "name": "integration"},
            ],
            "published": True,
        }
        response = client_api_2.post("/posts/with-tags", json=payload)

        # Verify response and database
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Post with Tags"
        created_post = BlogPost.objects.get(title="Post with Tags")
        assert created_post.tags.count() == 2
        assert created_post.published is True

    @pytest.mark.django_db(transaction=True)
    def test_create_post_with_tags_ids(self, client_api_2):
        """Test creating a post with tags as nested objects."""
        # Create author
        author = Author.objects.create(name="Frank", email="frank@example.com")

        # Create tags
        tag1 = Tag.objects.create(name="tag1")
        tag2 = Tag.objects.create(name="tag2")

        # Send POST request with full tag objects (required by input serializer)
        payload = {
            "title": "Post with Tag IDs",
            "content": "Content with tag IDs",
            "author": {"id": author.id, "name": "Frank", "email": "frank@example.com"},
            "tags": [
                {"id": tag1.id, "name": "tag1"},
                {"id": tag2.id, "name": "tag2"},
            ],
            "published": False,
        }
        response = client_api_2.post("/posts/with-tags", json=payload)

        # Verify response and database
        assert response.status_code == 200
        created_post = BlogPost.objects.get(title="Post with Tag IDs")
        assert created_post.tags.count() == 2


class TestAPI3DeeplyNested:
    """Test API 3: Deeply nested structures (nested within nested)."""

    @pytest.mark.django_db(transaction=True)
    def test_get_post_full_with_comments_and_authors(self, client_api_3):
        """Test getting a post with all relationships including nested comments."""
        # Create authors
        post_author = Author.objects.create(name="Grace", email="grace@example.com")
        commenter = Author.objects.create(name="Henry", email="henry@example.com")

        # Create post with tags
        post = BlogPost.objects.create(title="Complex Post", content="Complex post content", author=post_author)

        tag = Tag.objects.create(name="complex")
        post.tags.add(tag)

        # Create comments
        Comment.objects.create(post=post, author=commenter, text="Great post!")

        # Make HTTP request
        response = client_api_3.get(f"/posts/{post.id}/full")

        # Verify response structure
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Complex Post"

        # Author should be nested
        if isinstance(data["author"], dict):
            assert data["author"]["name"] == "Grace"

        # Tags should be present
        assert len(data["tags"]) > 0

        # Comments should be nested with authors
        assert len(data["comments"]) > 0
        first_comment = data["comments"][0]
        if isinstance(first_comment, dict):
            assert first_comment["text"] == "Great post!"
            if isinstance(first_comment["author"], dict):
                assert first_comment["author"]["name"] == "Henry"

    @pytest.mark.django_db(transaction=True)
    def test_create_post_full_with_nested_comments(self, client_api_3):
        """Test creating a post with full nested structure including comments."""
        # Create authors in DB first
        post_author = Author.objects.create(name="Iris", email="iris@example.com")
        commenter = Author.objects.create(name="Jack", email="jack@example.com")

        # Create tag
        tag = Tag.objects.create(name="nested")

        # Send POST request with full nesting
        payload = {
            "title": "Full Post",
            "content": "Full post with nested comments",
            "author": {"id": post_author.id, "name": "Iris", "email": "iris@example.com"},
            "tags": [{"id": tag.id, "name": "nested"}],
            "comments": [
                {
                    "id": 0,
                    "text": "Nested comment 1",
                    "author": {"id": commenter.id, "name": "Jack", "email": "jack@example.com"},
                },
                {
                    "id": 0,
                    "text": "Nested comment 2",
                    "author": {"id": commenter.id, "name": "Jack", "email": "jack@example.com"},
                },
            ],
            "published": True,
        }
        response = client_api_3.post("/posts/full", json=payload)

        # Verify response and database
        assert response.status_code == 200
        created_post = BlogPost.objects.get(title="Full Post")
        assert created_post.author_id == post_author.id
        assert created_post.tags.count() == 1
        assert created_post.comments.count() == 2


class TestAPI4MixedValidation:
    """Test API 4: Mixed nested with validation."""

    @pytest.mark.django_db(transaction=True)
    def test_create_post_strict_with_full_author(self, client_api_4):
        """Test creating a post with strict validation and full author object."""
        # Create author
        author = Author.objects.create(name="Kate", email="kate@example.com")

        # Send POST request with full author (required by strict validation)
        payload = {
            "title": "Strict Post",
            "content": "This is a strict post with full author object",
            "author": {"id": author.id, "name": "Kate", "email": "kate@example.com"},
            "tags": [],
            "published": True,
        }
        response = client_api_4.post("/posts/strict", json=payload)

        # Verify response and database
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Strict Post"
        created_post = BlogPost.objects.get(title="Strict Post")
        assert created_post.author_id == author.id
        assert created_post.published is True

    @pytest.mark.django_db(transaction=True)
    def test_create_post_strict_validation_fails_without_author_object(self, client_api_4):
        """Test that strict validation fails if author is just an ID."""
        # Create author
        author = Author.objects.create(name="Liam", email="liam@example.com")

        # Send POST request with just author ID (should fail)
        payload = {
            "title": "Should Fail",
            "content": "This should fail because author is just ID",
            "author": author.id,  # Just ID - not allowed
            "tags": [],
            "published": False,
        }
        response = client_api_4.post("/posts/strict", json=payload)

        # Verify request fails
        assert response.status_code == 400 or response.status_code == 422

    @pytest.mark.django_db(transaction=True)
    def test_create_post_strict_title_validation(self, client_api_4):
        """Test that title validation is enforced."""
        author = Author.objects.create(name="Mia", email="mia@example.com")

        # Send POST request with short title (should fail)
        payload = {
            "title": "Hi",  # Too short
            "content": "This content is long enough but title is not",
            "author": {"id": author.id, "name": "Mia", "email": "mia@example.com"},
            "tags": [],
            "published": False,
        }
        response = client_api_4.post("/posts/strict", json=payload)

        # Verify request fails
        assert response.status_code == 400 or response.status_code == 422

    @pytest.mark.django_db(transaction=True)
    def test_create_post_strict_content_validation(self, client_api_4):
        """Test that content validation is enforced."""
        author = Author.objects.create(name="Noah", email="noah@example.com")

        # Send POST request with short content (should fail)
        payload = {
            "title": "Valid Title",
            "content": "Short",  # Too short
            "author": {"id": author.id, "name": "Noah", "email": "noah@example.com"},
            "tags": [],
            "published": False,
        }
        response = client_api_4.post("/posts/strict", json=payload)

        # Verify request fails
        assert response.status_code == 400 or response.status_code == 422

    @pytest.mark.django_db(transaction=True)
    def test_create_post_strict_author_email_validation(self, client_api_4):
        """Test that author email validation is enforced in nested object."""
        # Send POST request with invalid author email
        payload = {
            "title": "Valid Title",
            "content": "This is valid content for the post",
            "author": {"id": 1, "name": "Invalid", "email": "invalidemail"},
            "tags": [],
            "published": False,
        }
        response = client_api_4.post("/posts/strict", json=payload)

        # Verify request fails
        assert response.status_code == 400 or response.status_code == 422

    @pytest.mark.django_db(transaction=True)
    def test_get_post_and_validate_roundtrip(self, client_api_4):
        """Test that response can be re-validated through the serializer."""
        # Create test data
        author = Author.objects.create(name="Olivia", email="olivia@example.com")
        post = BlogPost.objects.create(title="Roundtrip Post", content="Testing roundtrip validation", author=author)

        tag = Tag.objects.create(name="roundtrip")
        post.tags.add(tag)

        # Make HTTP request
        response = client_api_4.get(f"/posts/{post.id}/validate")

        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Roundtrip Post"
        assert data["id"] == post.id


# ============================================================================
# API 5: User Authentication & Profile Management - Full Cycle Tests
# ============================================================================


# User Authentication Serializers
class UserSerializer(Serializer):
    """Serializer for User model - OUTPUT."""

    id: int
    username: str
    email: str
    is_active: bool = True
    is_staff: bool = False


class UserSignupSerializer(Serializer):
    """Serializer for user registration with password confirmation validation."""

    username: Annotated[str, Meta(min_length=3, max_length=150)]
    email: Annotated[str, Meta(pattern=r"^[^@]+@[^@]+\.[^@]+$")]
    password: Annotated[str, Meta(min_length=8)]
    confirm_password: str

    @field_validator("username")
    def strip_username(cls, value: str) -> str:
        return value.strip()

    @field_validator("email")
    def lowercase_email(cls, value: str) -> str:
        return value.lower()

    @model_validator
    def validate_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class UserLoginSerializer(Serializer):
    """Serializer for user login - accepts username or email."""

    username: str
    password: str

    @field_validator("username")
    def strip_username(cls, value: str) -> str:
        return value.strip()


class UserProfileSerializer(Serializer):
    """Serializer for user profile with nested user data - OUTPUT."""

    id: int
    user: UserSerializer
    bio: Annotated[str, Meta(max_length=500)] = ""
    avatar_url: str = ""
    phone: Annotated[str, Meta(max_length=20)] = ""
    location: Annotated[str, Meta(max_length=100)] = ""


# Helper functions
def hash_password(password: str) -> str:
    """Simple password hashing for testing (use bcrypt/argon2 in production)."""
    return hashlib.sha256(password.encode()).hexdigest()


# API endpoints
api_5_user_auth = BoltAPI()


@api_5_user_auth.post("/auth/signup")
async def signup(data: UserSignupSerializer):
    """User registration endpoint."""
    if await User.objects.filter(username=data.username).aexists():
        raise BadRequest(detail="Username already exists")

    if await User.objects.filter(email=data.email).aexists():
        raise BadRequest(detail="Email already exists")

    user = await User.objects.acreate(
        username=data.username,
        email=data.email,
        password_hash=hash_password(data.password),
        is_active=True,
        is_staff=False,
    )
    return UserSerializer.from_model(user)


@pytest.fixture
def client_api_5():
    """TestClient for API 5: User authentication."""
    return TestClient(api_5_user_auth)


# Simple test to verify the framework works
class TestAPI5UserRegistration:
    """Test user registration with comprehensive validation."""

    @pytest.mark.django_db(transaction=True)
    def test_successful_signup(self, client_api_5):
        """Test successful user registration."""
        payload = {
            "username": "johndoe",
            "email": "john@example.com",
            "password": "SecurePass123!",
            "confirm_password": "SecurePass123!",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "johndoe"
        assert data["email"] == "john@example.com"
        assert "password" not in data

        # Verify user exists in database (using sync ORM in test)
        assert User.objects.filter(username="johndoe").exists()

    @pytest.mark.django_db(transaction=True)
    def test_signup_password_mismatch(self, client_api_5):
        """Test that signup fails when passwords don't match."""
        payload = {
            "username": "janedoe",
            "email": "jane@example.com",
            "password": "SecurePass123!",
            "confirm_password": "DifferentPass456!",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        # Should fail validation
        assert response.status_code in [400, 422]
        data = response.json()
        # detail can be a string or list of validation errors
        detail_str = str(data["detail"]).lower()
        assert "password" in detail_str or "match" in detail_str
        assert not User.objects.filter(username="janedoe").exists()

    @pytest.mark.django_db(transaction=True)
    def test_signup_duplicate_username(self, client_api_5):
        """Test that signup fails when username already exists."""
        # Create existing user
        User.objects.create(
            username="existinguser", email="existing@example.com", password_hash=hash_password("password123")
        )

        # Try to sign up with same username
        payload = {
            "username": "existinguser",
            "email": "newemail@example.com",
            "password": "SecurePass123!",
            "confirm_password": "SecurePass123!",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        # Should fail with BadRequest
        assert response.status_code == 400
        data = response.json()
        assert "username" in data["detail"].lower() and "already exists" in data["detail"].lower()

    @pytest.mark.django_db(transaction=True)
    def test_signup_duplicate_email(self, client_api_5):
        """Test that signup fails when email already exists."""
        # Create existing user
        User.objects.create(
            username="existinguser", email="existing@example.com", password_hash=hash_password("password123")
        )

        # Try to sign up with same email
        payload = {
            "username": "newuser",
            "email": "existing@example.com",
            "password": "SecurePass123!",
            "confirm_password": "SecurePass123!",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        # Should fail with BadRequest
        assert response.status_code == 400
        data = response.json()
        assert "email" in data["detail"].lower() and "already exists" in data["detail"].lower()

    @pytest.mark.django_db(transaction=True)
    def test_signup_invalid_email_format(self, client_api_5):
        """Test that signup fails with invalid email format."""
        payload = {
            "username": "testuser",
            "email": "notanemail",
            "password": "SecurePass123!",
            "confirm_password": "SecurePass123!",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        # Should fail validation with email-related error
        assert response.status_code in [400, 422]
        data = response.json()
        # Check if detail mentions email or regex pattern
        detail_str = str(data["detail"]).lower()
        assert "email" in detail_str or "regex" in detail_str or "pattern" in detail_str
        assert not User.objects.filter(username="testuser").exists()

    @pytest.mark.django_db(transaction=True)
    def test_signup_short_username(self, client_api_5):
        """Test that signup fails with username shorter than 3 characters."""
        payload = {
            "username": "ab",
            "email": "test@example.com",
            "password": "SecurePass123!",
            "confirm_password": "SecurePass123!",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        # Should fail validation with username length error
        assert response.status_code in [400, 422]
        data = response.json()
        detail_str = str(data["detail"]).lower()
        assert "username" in detail_str or "length" in detail_str or ">= 3" in detail_str
        assert not User.objects.filter(username="ab").exists()

    @pytest.mark.django_db(transaction=True)
    def test_signup_short_password(self, client_api_5):
        """Test that signup fails with password shorter than 8 characters."""
        payload = {
            "username": "testuser",
            "email": "test@example.com",
            "password": "short",
            "confirm_password": "short",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        # Should fail validation with password length error
        assert response.status_code in [400, 422]
        data = response.json()
        detail_str = str(data["detail"]).lower()
        assert "password" in detail_str or "length" in detail_str or ">= 8" in detail_str
        assert not User.objects.filter(username="testuser").exists()

    @pytest.mark.django_db(transaction=True)
    def test_signup_email_case_normalization(self, client_api_5):
        """Test that email is normalized to lowercase."""
        payload = {
            "username": "testuser",
            "email": "Test@Example.COM",
            "password": "SecurePass123!",
            "confirm_password": "SecurePass123!",
        }
        response = client_api_5.post("/auth/signup", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "test@example.com"

        # Verify in database
        user = User.objects.get(username="testuser")
        assert user.email == "test@example.com"


# ============================================================================
# API 6: Advanced Serializer Features with Full Object Assertions
# ============================================================================


class AdvancedAuthorSerializer(Serializer):
    """Author serializer with computed fields and validators."""

    id: int
    name: NonEmptyStr
    email: Email
    bio: str = ""

    class Config:
        write_only = {"bio"}
        field_sets = {
            "list": ["id", "name"],
            "detail": ["id", "name", "email", "display_name"],
            "admin": ["id", "name", "email", "bio", "display_name", "email_domain"],
        }

    @field_validator("name")
    def validate_name(cls, value: str) -> str:
        return value.strip().title()

    @field_validator("email")
    def validate_email(cls, value: str) -> str:
        return value.lower().strip()

    @computed_field
    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"

    @computed_field
    def email_domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""


class AdvancedTagSerializer(Serializer):
    """Tag serializer with computed fields."""

    id: int
    name: str
    description: str = ""

    @computed_field
    def slug(self) -> str:
        return self.name.lower().replace(" ", "-")


class AdvancedCommentSerializer(Serializer):
    """Comment serializer with nested author and computed fields."""

    id: int
    text: str
    author: AdvancedAuthorSerializer
    created_at: datetime | None = None

    @computed_field
    def text_preview(self) -> str:
        return self.text[:50] + "..." if len(self.text) > 50 else self.text

    @computed_field
    def word_count(self) -> int:
        return len(self.text.split())


class AdvancedBlogPostSerializer(Serializer):
    """BlogPost serializer with all advanced features."""

    id: int
    title: NonEmptyStr
    content: str
    author: AdvancedAuthorSerializer
    tags: list[AdvancedTagSerializer]
    comments: list[AdvancedCommentSerializer] = []
    published: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        field_sets = {
            "list": ["id", "title", "published", "created_at", "tag_count"],
            "detail": [
                "id",
                "title",
                "content",
                "author",
                "tags",
                "published",
                "created_at",
                "updated_at",
                "tag_names",
                "comment_count",
            ],
            "admin": [
                "id",
                "title",
                "content",
                "author",
                "tags",
                "comments",
                "published",
                "created_at",
                "updated_at",
                "tag_names",
                "comment_count",
                "content_preview",
            ],
        }

    @field_validator("title")
    def validate_title(cls, value: str) -> str:
        return value.strip()

    @computed_field
    def tag_names(self) -> list[str]:
        return [t.name for t in self.tags]

    @computed_field
    def tag_count(self) -> int:
        return len(self.tags)

    @computed_field
    def comment_count(self) -> int:
        return len(self.comments)

    @computed_field
    def content_preview(self) -> str:
        return self.content[:100] + "..." if len(self.content) > 100 else self.content


class AdvancedBlogPostInputSerializer(Serializer):
    """Input serializer for creating/updating blog posts."""

    title: Annotated[str, Meta(min_length=3, max_length=300)]
    content: Annotated[str, Meta(min_length=10)]
    author: AdvancedAuthorSerializer
    tags: list[AdvancedTagSerializer] = []
    published: bool = False

    @field_validator("title")
    def validate_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("content")
    def validate_content(cls, value: str) -> str:
        return value.strip()


# API 6 endpoints
api_6_advanced = BoltAPI()


@api_6_advanced.get("/posts")
async def list_posts():
    """List all posts with minimal fields (list view)."""
    posts = await sync_to_async(list)(
        BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").all()[:20]
    )
    serializers = [AdvancedBlogPostSerializer.from_model(p) for p in posts]
    return AdvancedBlogPostSerializer.use("list").dump_many(serializers)


@api_6_advanced.get("/posts/{post_id}")
async def get_post_detail(post_id: int):
    """Get post with detail view fields."""
    post = await BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").aget(id=post_id)
    serializer = AdvancedBlogPostSerializer.from_model(post)
    return AdvancedBlogPostSerializer.use("detail").dump(serializer)


@api_6_advanced.get("/posts/{post_id}/admin")
async def get_post_admin(post_id: int):
    """Get post with admin view fields (includes comments)."""
    post = await BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").aget(id=post_id)
    serializer = AdvancedBlogPostSerializer.from_model(post)
    return AdvancedBlogPostSerializer.use("admin").dump(serializer)


@api_6_advanced.get("/posts/{post_id}/full")
async def get_post_full_unfiltered(post_id: int):
    """Get post with all fields (no field_set filtering)."""
    post = await BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").aget(id=post_id)
    return AdvancedBlogPostSerializer.from_model(post)


@api_6_advanced.post("/posts")
async def create_post_advanced(data: AdvancedBlogPostInputSerializer):
    """Create a post with validation."""
    created_post = await BlogPost.objects.acreate(
        title=data.title,
        content=data.content,
        author_id=data.author.id,
        published=data.published,
    )

    # Add tags
    if data.tags:
        tag_ids = [tag.id for tag in data.tags]
        await sync_to_async(created_post.tags.set)(tag_ids)

    # Refetch with relationships
    post = await (
        BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").aget(id=created_post.id)
    )
    return AdvancedBlogPostSerializer.from_model(post)


@api_6_advanced.get("/authors/{author_id}")
async def get_author_detail(author_id: int):
    """Get author with detail view."""
    author = await Author.objects.aget(id=author_id)
    serializer = AdvancedAuthorSerializer.from_model(author)
    return AdvancedAuthorSerializer.use("detail").dump(serializer)


@api_6_advanced.get("/authors/{author_id}/admin")
async def get_author_admin(author_id: int):
    """Get author with admin view (includes bio and email_domain)."""
    author = await Author.objects.aget(id=author_id)
    serializer = AdvancedAuthorSerializer.from_model(author)
    return AdvancedAuthorSerializer.use("admin").dump(serializer)


@pytest.fixture
def client_api_6():
    """TestClient for API 6: Advanced serializer features."""
    return TestClient(api_6_advanced)


class TestAPI6AdvancedSerializerFeatures:
    """Test API 6: Advanced serializer features with full object assertions."""

    @pytest.mark.django_db(transaction=True)
    def test_get_post_full_asserts_all_fields(self, client_api_6):
        """Test full post response with all computed fields and nested objects."""
        # Create test data
        author = Author.objects.create(
            name="  jane doe  ",  # Will be transformed to "Jane Doe"
            email="JANE@EXAMPLE.COM",  # Will be lowercased
            bio="A passionate writer about technology and life.",
        )
        tag1 = Tag.objects.create(name="Python", description="Python programming language")
        tag2 = Tag.objects.create(name="Django Framework", description="Web framework")
        post = BlogPost.objects.create(
            title="  My Awesome Post  ",
            content="This is a detailed content that is more than one hundred characters long to test the content preview computed field properly.",
            author=author,
            published=True,
        )
        post.tags.add(tag1, tag2)
        commenter = Author.objects.create(name="Bob Smith", email="bob@example.com")
        Comment.objects.create(
            post=post,
            author=commenter,
            text="This is a great post! I really enjoyed reading it and learned a lot from it.",
        )

        # Make request
        response = client_api_6.get(f"/posts/{post.id}/full")

        # Assert response
        assert response.status_code == 200
        data = response.json()

        # Assert main post fields
        assert data["id"] == post.id
        assert data["title"] == "My Awesome Post"  # Stripped
        assert data["content"].startswith("This is a detailed content")
        assert data["published"] is True

        # Assert computed fields
        assert data["tag_names"] == ["Python", "Django Framework"]
        assert data["tag_count"] == 2
        assert data["comment_count"] == 1
        assert data["content_preview"].endswith("...")
        assert len(data["content_preview"]) == 103  # 100 chars + "..."

        # Assert nested author with computed fields
        assert data["author"]["id"] == author.id
        assert data["author"]["name"] == "Jane Doe"  # Title-cased
        assert data["author"]["email"] == "jane@example.com"  # Lowercased
        assert data["author"]["display_name"] == "Jane Doe <jane@example.com>"
        assert data["author"]["email_domain"] == "example.com"

        # Assert nested tags with computed fields
        assert len(data["tags"]) == 2
        assert data["tags"][0]["name"] == "Python"
        assert data["tags"][0]["slug"] == "python"
        assert data["tags"][1]["name"] == "Django Framework"
        assert data["tags"][1]["slug"] == "django-framework"

        # Assert nested comments with nested authors and computed fields
        assert len(data["comments"]) == 1
        comment = data["comments"][0]
        assert "great post" in comment["text"].lower()
        assert comment["text_preview"].endswith("...")
        assert comment["word_count"] == 16
        assert comment["author"]["name"] == "Bob Smith"
        assert comment["author"]["email"] == "bob@example.com"

    @pytest.mark.django_db(transaction=True)
    def test_get_post_list_view_minimal_fields(self, client_api_6):
        """Test list view returns only minimal fields."""
        author = Author.objects.create(name="Alice", email="alice@example.com")
        tag = Tag.objects.create(name="Testing")
        post = BlogPost.objects.create(
            title="List View Post",
            content="Some content here for the list view test",
            author=author,
            published=True,
        )
        post.tags.add(tag)

        response = client_api_6.get("/posts")

        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1

        # Find our post
        post_data = next((p for p in data if p["id"] == post.id), None)
        assert post_data is not None

        # List view should only have these fields
        assert set(post_data.keys()) == {"id", "title", "published", "created_at", "tag_count"}
        assert post_data["title"] == "List View Post"
        assert post_data["published"] is True
        assert post_data["tag_count"] == 1

        # These should NOT be in list view
        assert "content" not in post_data
        assert "author" not in post_data
        assert "tags" not in post_data
        assert "comments" not in post_data

    @pytest.mark.django_db(transaction=True)
    def test_get_post_detail_view_fields(self, client_api_6):
        """Test detail view returns appropriate fields."""
        author = Author.objects.create(name="Charlie", email="charlie@example.com")
        tag = Tag.objects.create(name="Detail")
        post = BlogPost.objects.create(
            title="Detail View Post",
            content="Detailed content for the detail view test endpoint",
            author=author,
            published=True,
        )
        post.tags.add(tag)
        Comment.objects.create(post=post, author=author, text="A comment")

        response = client_api_6.get(f"/posts/{post.id}")

        assert response.status_code == 200
        data = response.json()

        # Detail view should have these fields
        expected_keys = {
            "id",
            "title",
            "content",
            "author",
            "tags",
            "published",
            "created_at",
            "updated_at",
            "tag_names",
            "comment_count",
        }
        assert set(data.keys()) == expected_keys

        # Should have computed fields
        assert data["tag_names"] == ["Detail"]
        assert data["comment_count"] == 1

        # Should NOT have admin-only fields
        assert "comments" not in data
        assert "content_preview" not in data

        # Author should be nested but with limited fields (from field_set)
        assert "author" in data
        assert data["author"]["name"] == "Charlie"

    @pytest.mark.django_db(transaction=True)
    def test_get_post_admin_view_all_fields(self, client_api_6):
        """Test admin view returns all fields including comments."""
        author = Author.objects.create(name="Admin User", email="admin@example.com", bio="Admin bio")
        tag = Tag.objects.create(name="Admin Tag")
        post = BlogPost.objects.create(
            title="Admin View Post",
            content="Admin content that is longer than one hundred characters to test the content preview computed field in admin view.",
            author=author,
            published=True,
        )
        post.tags.add(tag)
        Comment.objects.create(post=post, author=author, text="Admin comment")

        response = client_api_6.get(f"/posts/{post.id}/admin")

        assert response.status_code == 200
        data = response.json()

        # Admin view should have ALL fields including comments
        expected_keys = {
            "id",
            "title",
            "content",
            "author",
            "tags",
            "comments",
            "published",
            "created_at",
            "updated_at",
            "tag_names",
            "comment_count",
            "content_preview",
        }
        assert set(data.keys()) == expected_keys

        # Should have comments (admin-only)
        assert len(data["comments"]) == 1
        assert data["comments"][0]["text"] == "Admin comment"

        # Should have content_preview (admin-only computed field)
        assert "content_preview" in data
        assert data["content_preview"].endswith("...")

    @pytest.mark.django_db(transaction=True)
    def test_author_write_only_field_excluded(self, client_api_6):
        """Test that write_only fields are excluded from response."""
        author = Author.objects.create(
            name="Secret Author", email="secret@example.com", bio="This bio should be hidden in detail view"
        )

        # Detail view should not include bio (write_only)
        response = client_api_6.get(f"/authors/{author.id}")

        assert response.status_code == 200
        data = response.json()

        # Detail view fields
        assert data["name"] == "Secret Author"
        assert data["email"] == "secret@example.com"
        assert data["display_name"] == "Secret Author <secret@example.com>"
        assert "bio" not in data  # write_only

    @pytest.mark.django_db(transaction=True)
    def test_author_admin_view_includes_all(self, client_api_6):
        """Test admin view includes email_domain but bio is still write_only."""
        author = Author.objects.create(name="Full Author", email="full@company.org", bio="Full bio text here")

        response = client_api_6.get(f"/authors/{author.id}/admin")

        assert response.status_code == 200
        data = response.json()

        # Admin view should have all output fields
        assert data["name"] == "Full Author"
        assert data["email"] == "full@company.org"
        # NOTE: bio is write_only, so it's NEVER included in output even in admin view
        # write_only takes precedence over field_sets
        assert "bio" not in data
        assert data["display_name"] == "Full Author <full@company.org>"
        assert data["email_domain"] == "company.org"

    @pytest.mark.django_db(transaction=True)
    def test_create_post_with_validation(self, client_api_6):
        """Test creating a post with all validations."""
        author = Author.objects.create(name="Creator", email="creator@example.com")
        tag = Tag.objects.create(name="New Tag")

        payload = {
            "title": "  New Post Title  ",  # Will be stripped
            "content": "This is the content of the new post with enough characters.",
            "author": {"id": author.id, "name": "Creator", "email": "creator@example.com"},
            "tags": [{"id": tag.id, "name": "New Tag"}],
            "published": True,
        }

        response = client_api_6.post("/posts", json=payload)

        assert response.status_code == 200
        data = response.json()

        # Verify response
        assert data["title"] == "New Post Title"  # Stripped
        assert data["published"] is True
        assert data["tag_count"] == 1
        assert data["tag_names"] == ["New Tag"]
        assert data["comment_count"] == 0

        # Verify author transformation
        assert data["author"]["name"] == "Creator"
        assert data["author"]["display_name"] == "Creator <creator@example.com>"

        # Verify in database
        post = BlogPost.objects.get(title="New Post Title")
        assert post.author_id == author.id
        assert post.tags.count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_create_post_validation_fails_short_title(self, client_api_6):
        """Test that title validation fails for short titles."""
        author = Author.objects.create(name="Test", email="test@example.com")

        payload = {
            "title": "AB",  # Too short (< 3)
            "content": "This content is long enough to pass validation.",
            "author": {"id": author.id, "name": "Test", "email": "test@example.com"},
            "tags": [],
            "published": False,
        }

        response = client_api_6.post("/posts", json=payload)

        assert response.status_code in [400, 422]
        assert not BlogPost.objects.filter(title="AB").exists()

    @pytest.mark.django_db(transaction=True)
    def test_create_post_validation_fails_short_content(self, client_api_6):
        """Test that content validation fails for short content."""
        author = Author.objects.create(name="Test", email="test@example.com")

        payload = {
            "title": "Valid Title",
            "content": "Short",  # Too short (< 10)
            "author": {"id": author.id, "name": "Test", "email": "test@example.com"},
            "tags": [],
            "published": False,
        }

        response = client_api_6.post("/posts", json=payload)

        assert response.status_code in [400, 422]
        assert not BlogPost.objects.filter(title="Valid Title").exists()

    @pytest.mark.django_db(transaction=True)
    def test_nested_computed_fields_chain(self, client_api_6):
        """Test that computed fields work correctly through nested serializers."""
        author = Author.objects.create(
            name="  nested author  ",  # Will be "Nested Author"
            email="NESTED@EXAMPLE.COM",  # Will be "nested@example.com"
        )
        tag = Tag.objects.create(name="Nested Tag")  # slug: "nested-tag"
        post = BlogPost.objects.create(
            title="Nested Test",
            content="Content for nested test with computed fields",
            author=author,
            published=True,
        )
        post.tags.add(tag)
        Comment.objects.create(
            post=post,
            author=author,
            text="This is a longer comment to test word count",  # 9 words
        )

        response = client_api_6.get(f"/posts/{post.id}/full")

        assert response.status_code == 200
        data = response.json()

        # Check author computed fields
        assert data["author"]["name"] == "Nested Author"
        assert data["author"]["email"] == "nested@example.com"
        assert data["author"]["display_name"] == "Nested Author <nested@example.com>"
        assert data["author"]["email_domain"] == "example.com"

        # Check tag computed fields
        assert data["tags"][0]["slug"] == "nested-tag"

        # Check comment computed fields
        assert data["comments"][0]["word_count"] == 9
        assert data["comments"][0]["author"]["display_name"] == "Nested Author <nested@example.com>"

    @pytest.mark.django_db(transaction=True)
    def test_dump_many_with_field_set(self, client_api_6):
        """Test dump_many with field_set returns consistent structure."""
        author = Author.objects.create(name="Bulk Author", email="bulk@example.com")

        # Create multiple posts
        for i in range(5):
            post = BlogPost.objects.create(
                title=f"Bulk Post {i}",
                content=f"Content for bulk post {i} with enough text",
                author=author,
                published=i % 2 == 0,
            )
            tag = Tag.objects.create(name=f"Tag {i}")
            post.tags.add(tag)

        response = client_api_6.get("/posts")

        assert response.status_code == 200
        data = response.json()

        # Should have at least 5 posts
        assert len(data) >= 5

        # All posts should have consistent structure (list view)
        for post in data:
            assert set(post.keys()) == {"id", "title", "published", "created_at", "tag_count"}
            assert isinstance(post["tag_count"], int)

    @pytest.mark.django_db(transaction=True)
    def test_empty_nested_collections(self, client_api_6):
        """Test that empty nested collections are handled correctly."""
        author = Author.objects.create(name="Solo Author", email="solo@example.com")
        post = BlogPost.objects.create(
            title="Solo Post",
            content="A post with no tags and no comments",
            author=author,
            published=False,
        )

        response = client_api_6.get(f"/posts/{post.id}/full")

        assert response.status_code == 200
        data = response.json()

        # Empty collections
        assert data["tags"] == []
        assert data["comments"] == []

        # Computed fields should handle empty collections
        assert data["tag_names"] == []
        assert data["tag_count"] == 0
        assert data["comment_count"] == 0


# ============================================================================
# RESPONSE NORMALIZATION - Each test builds its own BoltAPI
# ============================================================================


class PublicUser(Serializer):
    """A user. The password goes in, but never goes out."""

    id: int
    username: str
    password: str = field(write_only=True, default="")


class LoginUser(Serializer):
    """A user. The login field reads the username attribute of a model."""

    id: int
    login: str = field(source="username", default="")


class Obj:
    """An object with attributes, as a Django model instance has."""

    def __init__(self, **attributes):
        self.__dict__.update(attributes)


class Dumpable(msgspec.Struct):
    """A Struct with a dump() method. It is not a Bolt Serializer."""

    id: int

    def dump(self):
        return {"dumped": True}


def test_serializer_route_validates_a_returned_dict_and_drops_write_only_fields():
    api = BoltAPI()

    @api.get("/one-sync")
    def one_sync() -> PublicUser:
        return {"id": 1, "username": "a", "password": "p"}

    @api.get("/one-async")
    async def one_async() -> PublicUser:
        return {"id": 1, "username": "a", "password": "p"}

    @api.get("/many-sync")
    def many_sync() -> list[PublicUser]:
        return [{"id": 1, "username": "a", "password": "p"}]

    @api.get("/many-async")
    async def many_async() -> list[PublicUser]:
        return [{"id": 1, "username": "a", "password": "p"}]

    with TestClient(api) as client:
        for path in ("/one-sync", "/one-async"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == {"id": 1, "username": "a"}, path

        for path in ("/many-sync", "/many-async"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == [{"id": 1, "username": "a"}], path


def test_serializer_route_dumps_a_returned_serializer_instance():
    """The route dumps the instance. It does not read the instance again with from_model()."""
    api = BoltAPI()

    @api.get("/login-sync")
    def login_sync() -> LoginUser:
        return LoginUser(id=1, login="x")

    @api.get("/login-async")
    async def login_async() -> LoginUser:
        return LoginUser(id=1, login="x")

    @api.get("/logins-sync")
    def logins_sync() -> list[LoginUser]:
        return [LoginUser(id=1, login="x")]

    @api.get("/logins-async")
    async def logins_async() -> list[LoginUser]:
        return [LoginUser(id=1, login="x")]

    @api.get("/public")
    def public() -> PublicUser:
        return PublicUser(id=1, username="a", password="p")

    @api.get("/public-optional")
    async def public_optional() -> PublicUser | None:
        return PublicUser(id=1, username="a", password="p")

    with TestClient(api) as client:
        for path in ("/login-sync", "/login-async"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == {"id": 1, "login": "x"}, path

        for path in ("/logins-sync", "/logins-async"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == [{"id": 1, "login": "x"}], path

        for path in ("/public", "/public-optional"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == {"id": 1, "username": "a"}, path


def test_serializer_route_reads_returned_objects_and_tuples():
    api = BoltAPI()

    @api.get("/public-objects")
    async def public_objects() -> list[PublicUser]:
        return [Obj(id=1, username="a", password="p")]

    @api.get("/login-objects")
    async def login_objects() -> list[LoginUser]:
        return [Obj(id=1, username="x")]

    @api.get("/public-object")
    def public_object() -> PublicUser:
        return Obj(id=1, username="a", password="p")

    @api.get("/tuple-sync")
    def tuple_sync() -> list[PublicUser]:
        return ({"id": 1, "username": "a"},)

    @api.get("/tuple-async")
    async def tuple_async() -> list[PublicUser]:
        return ({"id": 1, "username": "a"},)

    with TestClient(api) as client:
        expected = {
            "/public-objects": [{"id": 1, "username": "a"}],
            "/login-objects": [{"id": 1, "login": "x"}],
            "/public-object": {"id": 1, "username": "a"},
            "/tuple-sync": [{"id": 1, "username": "a"}],
            "/tuple-async": [{"id": 1, "username": "a"}],
        }
        for path, body in expected.items():
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == body, path


def test_untyped_route_dumps_serializers_but_not_other_objects_with_dump():
    api = BoltAPI()

    @api.get("/serializers")
    def serializers():
        return [PublicUser(id=1, username="a", password="p")]

    @api.get("/dumpable")
    def dumpable():
        return Dumpable(id=1)

    @api.get("/dumpables")
    def dumpables():
        return [Dumpable(id=1)]

    with TestClient(api) as client:
        expected = {
            "/serializers": [{"id": 1, "username": "a"}],
            "/dumpable": {"id": 1},
            "/dumpables": [{"id": 1}],
        }
        for path, body in expected.items():
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.json() == body, path
