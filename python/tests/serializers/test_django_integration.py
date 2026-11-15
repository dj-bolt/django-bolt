"""Tests for Django model integration with Serializers.

This file tests all advanced serializer features with real Django models:
- from_model / to_model / update_instance
- field() with read_only/write_only
- @computed_field with Django model data
- @field_validator with Django models
- @model_validator for cross-field validation
- Dynamic field selection (only/exclude/use)
- Type-safe subsets (subset/fields)
- Reusable validated types (Email, URL, etc.)
- Nested serializers with Django relationships
- Dump options (exclude_none, exclude_defaults)

NOTE: msgspec Meta constraints (pattern, ge, le, min_length, etc.) and
@field_validator/@model_validator decorators only validate during:
- model_validate_json() - JSON string/bytes parsing
- model_validate() - dict parsing via msgspec.convert

Direct instantiation (MySerializer(field=value)) and from_model() DO run
custom @field_validator and @model_validator, but they bypass msgspec Meta
constraints. This is msgspec's design - Meta constraints are for parsing.
"""

from datetime import datetime
from typing import Annotated

import pytest

from django_bolt.exceptions import RequestValidationError
from django_bolt.serializers import (
    URL,
    Email,
    Meta,
    NonEmptyStr,
    PositiveInt,
    Serializer,
    computed_field,
    create_serializer,
    create_serializer_set,
    field,
    field_validator,
    model_validator,
)
from django_bolt.serializers.types import (
    Latitude,
    Longitude,
    Percentage,
    Username,
)

# Import test models
from tests.test_models import Article, Author, BlogPost, Comment, Tag, User, UserProfile  # noqa: PLC0415

# =============================================================================
# Serializers for testing
# =============================================================================


class AuthorSerializer(Serializer):
    """Serializer for Author model with computed field."""

    id: int
    name: NonEmptyStr
    email: Email
    bio: str = ""

    @computed_field
    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"


class TagSerializer(Serializer):
    """Simple serializer for Tag model."""

    id: int
    name: str
    description: str = ""


class CommentSerializer(Serializer):
    """Serializer for Comment model with nested author."""

    id: int
    text: str
    author: AuthorSerializer
    created_at: datetime


class BlogPostSerializer(Serializer):
    """Full serializer for BlogPost with all relationships."""

    id: int
    title: NonEmptyStr
    content: str
    author: AuthorSerializer
    tags: list[TagSerializer]
    comments: list[CommentSerializer] = []
    published: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        field_sets = {
            "list": ["id", "title", "published", "created_at"],
            "detail": ["id", "title", "content", "author", "tags", "published", "created_at", "updated_at"],
            "admin": ["id", "title", "content", "author", "tags", "comments", "published", "created_at", "updated_at"],
        }

    @computed_field
    def tag_names(self) -> list[str]:
        return [t.name for t in self.tags]

    @computed_field
    def comment_count(self) -> int:
        return len(self.comments)


class UserSerializer(Serializer):
    """User serializer with validators and computed fields."""

    id: int
    username: Username
    email: Email
    password_hash: str = ""
    is_active: bool = True
    is_staff: bool = False
    created_at: datetime | None = None

    class Config:
        write_only = {"password_hash"}
        field_sets = {
            "list": ["id", "username", "is_active"],
            "detail": ["id", "username", "email", "is_active", "is_staff", "created_at"],
            "admin": ["id", "username", "email", "is_active", "is_staff", "created_at"],
        }

    @field_validator("username")
    def validate_username(cls, value: str) -> str:
        """Username must be lowercase."""
        return value.lower()

    @field_validator("email")
    def validate_email(cls, value: str) -> str:
        """Email must be lowercase."""
        return value.lower()

    @computed_field
    def display_name(self) -> str:
        return f"@{self.username}"


class UserProfileSerializer(Serializer):
    """User profile serializer with nested user."""

    id: int
    user: UserSerializer
    bio: str = ""
    avatar_url: URL | None = None
    phone: str = ""
    location: str = ""

    @computed_field
    def has_avatar(self) -> bool:
        return bool(self.avatar_url)


class UserCreateSerializer(Serializer):
    """Input serializer for creating users with validation."""

    username: Username
    email: Email
    password: NonEmptyStr
    password_confirm: NonEmptyStr

    @field_validator("username")
    def validate_username(cls, value: str) -> str:
        return value.lower().strip()

    @field_validator("email")
    def validate_email(cls, value: str) -> str:
        return value.lower().strip()

    @model_validator
    def validate_passwords(self) -> "UserCreateSerializer":
        if self.password != self.password_confirm:
            raise ValueError("Passwords do not match")
        return self


class LocationSerializer(Serializer):
    """Serializer with geographic validated types."""

    name: NonEmptyStr
    latitude: Latitude
    longitude: Longitude
    accuracy: Percentage = 100.0


# =============================================================================
# Test Classes
# =============================================================================


class TestFromModelWithDjango:
    """Test converting Django models to Serializers."""

    @pytest.mark.django_db
    def test_from_model_simple(self):
        """Test basic from_model with Author."""
        author = Author.objects.create(
            name="John Doe",
            email="john@example.com",
            bio="A test author",
        )

        serializer = AuthorSerializer.from_model(author)

        assert serializer.id == author.id
        assert serializer.name == "John Doe"
        assert serializer.email == "john@example.com"
        assert serializer.bio == "A test author"

    @pytest.mark.django_db
    def test_from_model_with_computed_field(self):
        """Test from_model includes computed fields in dump."""
        author = Author.objects.create(
            name="Jane Smith",
            email="jane@example.com",
        )

        serializer = AuthorSerializer.from_model(author)
        result = serializer.dump()

        assert result["display_name"] == "Jane Smith <jane@example.com>"

    @pytest.mark.django_db
    def test_from_model_with_nested_fk(self):
        """Test from_model with ForeignKey relationship."""
        author = Author.objects.create(name="Alice", email="alice@example.com")
        post = BlogPost.objects.create(
            title="Test Post",
            content="Test content",
            author=author,
        )

        # Fetch with select_related for efficient query
        post = BlogPost.objects.select_related("author").get(id=post.id)

        # Create serializer manually with nested data
        author_serializer = AuthorSerializer.from_model(post.author)
        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=[],
            published=post.published,
            created_at=post.created_at,
            updated_at=post.updated_at,
        )

        result = serializer.dump()

        assert result["title"] == "Test Post"
        assert result["author"]["name"] == "Alice"
        assert result["author"]["email"] == "alice@example.com"

    @pytest.mark.django_db
    def test_from_model_with_m2m(self):
        """Test from_model with ManyToMany relationship."""
        author = Author.objects.create(name="Bob", email="bob@example.com")
        tag1 = Tag.objects.create(name="python", description="Python programming")
        tag2 = Tag.objects.create(name="django", description="Django framework")

        post = BlogPost.objects.create(
            title="Python Django Tutorial",
            content="Learn Django with Python",
            author=author,
        )
        post.tags.add(tag1, tag2)

        # Fetch with prefetch_related
        post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post.id)

        # Build serializer with nested data
        author_serializer = AuthorSerializer.from_model(post.author)
        tag_serializers = [TagSerializer.from_model(t) for t in post.tags.all()]

        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=tag_serializers,
            published=post.published,
            created_at=post.created_at,
            updated_at=post.updated_at,
        )

        result = serializer.dump()

        assert len(result["tags"]) == 2
        assert result["tag_names"] == ["python", "django"]

    @pytest.mark.django_db
    def test_from_model_with_validators(self):
        """Test from_model runs field validators."""
        user = User.objects.create(
            username="TestUser",  # Will be lowercased by validator
            email="TEST@EXAMPLE.COM",  # Will be lowercased by validator
            password_hash="hashed",
        )

        serializer = UserSerializer.from_model(user)

        # Validators should have run
        assert serializer.username == "testuser"
        assert serializer.email == "test@example.com"


class TestToModelWithDjango:
    """Test converting Serializers to Django model instances."""

    def test_to_model_creates_unsaved_instance(self):
        """Test creating an unsaved model instance."""
        # Note: to_model transfers ALL fields from the serializer, including id
        # So if you pass id=0, the model will have id=0, not pk=None
        # For truly new instances, use a serializer without id field or omit it

        class AuthorCreateSerializer(Serializer):
            """Serializer without id field for creating new authors."""

            name: NonEmptyStr
            email: Email
            bio: str = ""

        serializer = AuthorCreateSerializer(
            name="New Author",
            email="new@example.com",
            bio="A new author bio",
        )

        author = serializer.to_model(Author)

        assert author.name == "New Author"
        assert author.email == "new@example.com"
        assert author.bio == "A new author bio"
        assert author.pk is None  # Not saved - no id was set

    @pytest.mark.django_db
    def test_to_model_and_save(self):
        """Test creating and saving a model from serializer."""
        serializer = AuthorSerializer(
            id=0,
            name="Saved Author",
            email="saved@example.com",
            bio="Will be saved",
        )

        author = serializer.to_model(Author)
        author.save()

        # Verify saved
        saved_author = Author.objects.get(email="saved@example.com")
        assert saved_author.name == "Saved Author"

    @pytest.mark.django_db
    def test_to_model_excludes_computed_fields(self):
        """Test that computed fields are not set on the model."""
        serializer = AuthorSerializer(
            id=0,
            name="Author With Computed",
            email="computed@example.com",
        )

        author = serializer.to_model(Author)

        # Should not have display_name attribute (it's computed)
        assert not hasattr(author, "display_name") or author.display_name != serializer.display_name()


class TestUpdateInstanceWithDjango:
    """Test updating Django model instances with Serializers."""

    @pytest.mark.django_db
    def test_update_instance_basic(self):
        """Test updating model instance fields."""
        author = Author.objects.create(
            name="Original Name",
            email="original@example.com",
        )

        update_data = AuthorSerializer(
            id=author.id,
            name="Updated Name",
            email="updated@example.com",
            bio="New bio",
        )

        updated_author = update_data.update_instance(author)

        assert updated_author.name == "Updated Name"
        assert updated_author.email == "updated@example.com"
        assert updated_author.bio == "New bio"
        # Not saved yet
        original = Author.objects.get(id=author.id)
        assert original.name == "Original Name"

    @pytest.mark.django_db
    def test_update_instance_and_save(self):
        """Test updating and saving model instance."""
        author = Author.objects.create(
            name="Original",
            email="original@example.com",
        )

        update_data = AuthorSerializer(
            id=author.id,
            name="Updated",
            email="updated@example.com",
        )

        updated_author = update_data.update_instance(author)
        updated_author.save()

        # Verify saved
        refreshed = Author.objects.get(id=author.id)
        assert refreshed.name == "Updated"


class TestFieldValidatorsWithDjango:
    """Test @field_validator with Django models."""

    @pytest.mark.django_db
    def test_field_validator_transforms_value(self):
        """Test field validators transform values during from_model."""
        user = User.objects.create(
            username="MixedCase",
            email="UPPER@CASE.COM",
            password_hash="hash",
        )

        serializer = UserSerializer.from_model(user)

        # Validators should lowercase
        assert serializer.username == "mixedcase"
        assert serializer.email == "upper@case.com"

    def test_field_validator_on_parse(self):
        """Test field validators run during JSON parsing."""
        json_data = (
            b'{"username": "TestUser", "email": "TEST@EXAMPLE.COM", "password": "secret", "password_confirm": "secret"}'
        )

        user_create = UserCreateSerializer.model_validate_json(json_data)

        assert user_create.username == "testuser"
        assert user_create.email == "test@example.com"

    def test_field_validator_transforms_and_strips(self):
        """Test field validators transform values during direct instantiation."""
        # Note: When using Username type with pattern validation, whitespace fails
        # msgspec Meta validation. So we test with direct instantiation where
        # pattern validation is bypassed but field validators still run.
        user_create = UserCreateSerializer(
            username="TESTUSER",  # Uppercase, will be lowercased
            email="  EMAIL@EXAMPLE.COM  ",  # Will be lowercased and stripped
            password="secret",
            password_confirm="secret",
        )

        # Field validators run on direct instantiation
        assert user_create.username == "testuser"
        assert user_create.email == "email@example.com"


class TestModelValidatorsWithDjango:
    """Test @model_validator with Django model scenarios."""

    def test_model_validator_passes(self):
        """Test model validator passes when valid."""
        user_create = UserCreateSerializer(
            username="validuser",
            email="valid@example.com",
            password="secret123",
            password_confirm="secret123",
        )

        # Should not raise
        assert user_create.password == user_create.password_confirm

    def test_model_validator_fails(self):
        """Test model validator raises on invalid data."""
        # Model validators raise msgspec.ValidationError (wrapping the ValueError)
        with pytest.raises(RequestValidationError, match="Passwords do not match"):
            UserCreateSerializer(
                username="user",
                email="user@example.com",
                password="secret123",
                password_confirm="different",
            )

    def test_model_validator_via_json_parse(self):
        """Test model validator runs during JSON parsing."""
        json_data = b'{"username": "user", "email": "user@example.com", "password": "abc", "password_confirm": "xyz"}'

        # Model validators raise RequestValidationError
        with pytest.raises(RequestValidationError, match="Passwords do not match"):
            UserCreateSerializer.model_validate_json(json_data)


class TestComputedFieldsWithDjango:
    """Test @computed_field with Django models."""

    @pytest.mark.django_db
    def test_computed_field_in_dump(self):
        """Test computed fields appear in dump output."""
        author = Author.objects.create(name="Author", email="author@example.com")

        serializer = AuthorSerializer.from_model(author)
        result = serializer.dump()

        assert "display_name" in result
        assert result["display_name"] == "Author <author@example.com>"

    @pytest.mark.django_db
    def test_computed_field_with_relationships(self):
        """Test computed field accessing nested data."""
        author = Author.objects.create(name="Bob", email="bob@example.com")
        tag1 = Tag.objects.create(name="python")
        tag2 = Tag.objects.create(name="django")

        post = BlogPost.objects.create(title="Test", content="Content", author=author)
        post.tags.add(tag1, tag2)

        # Fetch with relationships
        post = BlogPost.objects.select_related("author").prefetch_related("tags", "comments").get(id=post.id)

        # Build serializer
        author_serializer = AuthorSerializer.from_model(post.author)
        tag_serializers = [TagSerializer.from_model(t) for t in post.tags.all()]
        comment_serializers = [
            CommentSerializer(
                id=c.id,
                text=c.text,
                author=AuthorSerializer.from_model(c.author),
                created_at=c.created_at,
            )
            for c in post.comments.all()
        ]

        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=tag_serializers,
            comments=comment_serializers,
            published=post.published,
            created_at=post.created_at,
            updated_at=post.updated_at,
        )

        result = serializer.dump()

        assert result["tag_names"] == ["python", "django"]
        assert result["comment_count"] == 0

    @pytest.mark.django_db
    def test_computed_field_exclude_none(self):
        """Test computed field respects exclude_none."""

        class OptionalComputedSerializer(Serializer):
            name: str
            value: int | None = None

            @computed_field
            def doubled(self) -> int | None:
                if self.value is not None:
                    return self.value * 2
                return None

        s1 = OptionalComputedSerializer(name="test", value=None)
        result = s1.dump(exclude_none=True)

        assert "value" not in result
        assert "doubled" not in result

        s2 = OptionalComputedSerializer(name="test", value=5)
        result2 = s2.dump(exclude_none=True)

        assert result2["doubled"] == 10


class TestDynamicFieldSelectionWithDjango:
    """Test only/exclude/use with Django models."""

    @pytest.mark.django_db
    def test_only_with_django_model(self):
        """Test only() with Django model data."""
        author = Author.objects.create(name="Alice", email="alice@example.com", bio="Bio text")

        serializer = AuthorSerializer.from_model(author)

        # Use only() to select specific fields
        result = AuthorSerializer.only("id", "name").dump(serializer)

        assert result == {"id": author.id, "name": "Alice"}
        assert "email" not in result
        assert "bio" not in result

    @pytest.mark.django_db
    def test_exclude_with_django_model(self):
        """Test exclude() with Django model data."""
        user = User.objects.create(
            username="testuser",
            email="test@example.com",
            password_hash="secret_hash",
        )

        serializer = UserSerializer.from_model(user)

        # Exclude sensitive fields (password_hash already excluded via Meta.write_only)
        result = UserSerializer.exclude("is_staff", "created_at").dump(serializer)

        assert "is_staff" not in result
        assert "created_at" not in result
        assert "username" in result

    @pytest.mark.django_db
    def test_use_field_set_with_django(self):
        """Test use() with predefined field sets."""
        author = Author.objects.create(name="Bob", email="bob@example.com")
        post = BlogPost.objects.create(
            title="Test Post",
            content="Content here",
            author=author,
            published=True,
        )

        author_serializer = AuthorSerializer.from_model(author)
        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=[],
            published=post.published,
            created_at=post.created_at,
            updated_at=post.updated_at,
        )

        # List view - minimal fields
        list_result = BlogPostSerializer.use("list").dump(serializer)
        assert set(list_result.keys()) == {"id", "title", "published", "created_at"}

        # Detail view - more fields
        detail_result = BlogPostSerializer.use("detail").dump(serializer)
        assert "author" in detail_result
        assert "content" in detail_result
        assert "comments" not in detail_result

    @pytest.mark.django_db
    def test_dump_many_with_field_selection(self):
        """Test dump_many with only() on multiple instances."""
        a1 = Author.objects.create(name="Author1", email="a1@example.com")
        a2 = Author.objects.create(name="Author2", email="a2@example.com")
        a3 = Author.objects.create(name="Author3", email="a3@example.com")

        authors = Author.objects.filter(id__in=[a1.id, a2.id, a3.id])
        serializers = [AuthorSerializer.from_model(a) for a in authors]

        result = AuthorSerializer.only("id", "name").dump_many(serializers)

        assert len(result) == 3
        for item in result:
            assert set(item.keys()) == {"id", "name"}


class TestSubsetWithDjango:
    """Test subset() and fields() with Django models."""

    @pytest.mark.django_db
    def test_subset_from_model(self):
        """Test subset class works with from_model."""
        user = User.objects.create(
            username="testuser",
            email="test@example.com",
            password_hash="hash",
            is_staff=True,
        )

        # Create a mini serializer
        UserMini = UserSerializer.subset("id", "username", "email")

        mini_user = UserMini.from_model(user)

        assert mini_user.id == user.id
        assert mini_user.username == "testuser"
        assert mini_user.email == "test@example.com"

        result = mini_user.dump()
        assert set(result.keys()) == {"id", "username", "email"}

    @pytest.mark.django_db
    def test_fields_from_field_set_with_django(self):
        """Test fields() creates proper subset from field_sets."""
        user = User.objects.create(
            username="listuser",
            email="list@example.com",
            password_hash="hash",
        )

        # Create list serializer from field set
        UserListSerializer = UserSerializer.fields("list")

        list_user = UserListSerializer.from_model(user)

        result = list_user.dump()
        assert set(result.keys()) == {"id", "username", "is_active"}

    @pytest.mark.django_db
    def test_subset_preserves_validators(self):
        """Test that subset serializers preserve field validators."""
        UserMini = UserSerializer.subset("id", "username", "email")

        # Validators should still run
        mini = UserMini(
            id=1,
            username="UPPERCASE",
            email="UPPER@EXAMPLE.COM",
        )

        assert mini.username == "uppercase"
        assert mini.email == "upper@example.com"

    @pytest.mark.django_db
    def test_subset_with_computed_field(self):
        """Test subset includes computed fields."""
        user = User.objects.create(
            username="compute",
            email="compute@example.com",
            password_hash="hash",
        )

        # Include computed field in subset
        UserWithDisplay = UserSerializer.subset("id", "username", "display_name")

        user_serializer = UserWithDisplay.from_model(user)
        result = user_serializer.dump()

        assert result["display_name"] == "@compute"

    @pytest.mark.django_db
    def test_subset_from_parent(self):
        """Test creating subset instance from parent instance."""
        user = User.objects.create(
            username="parent",
            email="parent@example.com",
            password_hash="hash",
            is_staff=True,
        )

        full_user = UserSerializer.from_model(user)
        UserMini = UserSerializer.subset("id", "username")

        mini_user = UserMini.from_parent(full_user)

        assert mini_user.id == full_user.id
        assert mini_user.username == full_user.username
        assert set(mini_user.dump().keys()) == {"id", "username"}


class TestValidatedTypesWithDjango:
    """Test reusable validated types with Django models."""

    @pytest.mark.django_db
    def test_email_type_validation(self):
        """Test Email type validates correctly."""

        class StrictAuthorSerializer(Serializer):
            name: NonEmptyStr
            email: Email

        # Valid email
        author = StrictAuthorSerializer.model_validate_json(b'{"name": "Test", "email": "valid@example.com"}')
        assert author.email == "valid@example.com"

        # Invalid email - Meta pattern validation in msgspec
        with pytest.raises(RequestValidationError):
            StrictAuthorSerializer.model_validate_json(b'{"name": "Test", "email": "invalid-email"}')

    def test_positive_int_validation(self):
        """Test PositiveInt type validates correctly."""

        class ItemSerializer(Serializer):
            name: str
            quantity: PositiveInt

        # Valid
        item = ItemSerializer.model_validate_json(b'{"name": "Widget", "quantity": 10}')
        assert item.quantity == 10

        # Invalid (zero) - Meta constraint validation in msgspec
        with pytest.raises(RequestValidationError):
            ItemSerializer.model_validate_json(b'{"name": "Widget", "quantity": 0}')

        # Invalid (negative) - Meta constraint validation in msgspec
        with pytest.raises(RequestValidationError):
            ItemSerializer.model_validate_json(b'{"name": "Widget", "quantity": -5}')

    def test_geographic_types_validation(self):
        """Test Latitude and Longitude types."""
        # Valid location
        loc = LocationSerializer.model_validate_json(b'{"name": "NYC", "latitude": 40.7128, "longitude": -74.0060}')
        assert loc.latitude == 40.7128
        assert loc.longitude == -74.0060

        # Invalid latitude (> 90) - Meta constraint in msgspec
        with pytest.raises(RequestValidationError):
            LocationSerializer.model_validate_json(b'{"name": "Invalid", "latitude": 91.0, "longitude": 0}')

        # Invalid longitude (> 180) - Meta constraint in msgspec
        with pytest.raises(RequestValidationError):
            LocationSerializer.model_validate_json(b'{"name": "Invalid", "latitude": 0, "longitude": 181.0}')

    def test_validated_types_in_subset(self):
        """Test validated types are preserved in subset."""
        UserMini = UserSerializer.subset("id", "username", "email")

        # Validation should still work (via JSON parsing) - Meta constraint in msgspec
        with pytest.raises(RequestValidationError):
            UserMini.model_validate_json(b'{"id": 1, "username": "ab", "email": "invalid"}')


class TestDumpOptionsWithDjango:
    """Test dump options with Django model data."""

    @pytest.mark.django_db
    def test_dump_exclude_none(self):
        """Test dump(exclude_none=True) with Django model."""
        author = Author.objects.create(name="Author", email="author@example.com")

        # Create serializer with optional None field
        class AuthorWithOptional(Serializer):
            id: int
            name: str
            email: str
            bio: str | None = None

        _ = AuthorWithOptional.from_model(author)  # verify from_model works
        # bio is empty string from Django, not None
        # Let's manually set it to None
        serializer_with_none = AuthorWithOptional(
            id=author.id,
            name=author.name,
            email=author.email,
            bio=None,
        )

        result = serializer_with_none.dump(exclude_none=True)

        assert "bio" not in result
        assert "name" in result

    @pytest.mark.django_db
    def test_dump_exclude_defaults(self):
        """Test dump(exclude_defaults=True) with Django model."""
        user = User.objects.create(
            username="defaultuser",
            email="default@example.com",
            password_hash="hash",
            is_active=True,  # Default value
            is_staff=False,  # Default value
        )

        serializer = UserSerializer.from_model(user)

        # exclude_defaults removes fields with default values
        result = serializer.dump(exclude_defaults=True)

        # is_active=True and is_staff=False are defaults, should be excluded
        assert "is_active" not in result
        assert "is_staff" not in result

    @pytest.mark.django_db
    def test_dump_exclude_none_with_computed(self):
        """Test exclude_none works with computed fields."""

        class ComputedNullable(Serializer):
            name: str
            status: str | None = None

            @computed_field
            def message(self) -> str | None:
                if self.status:
                    return f"Status: {self.status}"
                return None

        s = ComputedNullable(name="test", status=None)
        result = s.dump(exclude_none=True)

        assert "status" not in result
        assert "message" not in result


class TestNestedSerializersWithDjango:
    """Test nested serializers with Django relationships."""

    @pytest.mark.django_db
    def test_nested_foreignkey(self):
        """Test nested serializer with ForeignKey."""
        author = Author.objects.create(name="Author", email="author@example.com")
        post = BlogPost.objects.create(
            title="Nested Test",
            content="Testing nested FK",
            author=author,
        )

        post = BlogPost.objects.select_related("author").get(id=post.id)

        author_serializer = AuthorSerializer.from_model(post.author)
        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=[],
            published=post.published,
        )

        result = serializer.dump()

        assert result["author"]["name"] == "Author"
        assert result["author"]["email"] == "author@example.com"
        assert result["author"]["display_name"] == "Author <author@example.com>"

    @pytest.mark.django_db
    def test_nested_many_to_many(self):
        """Test nested serializer with ManyToMany."""
        author = Author.objects.create(name="Author", email="author@example.com")
        tag1 = Tag.objects.create(name="tag1")
        tag2 = Tag.objects.create(name="tag2")

        post = BlogPost.objects.create(title="M2M Test", content="Content", author=author)
        post.tags.add(tag1, tag2)

        post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post.id)

        author_serializer = AuthorSerializer.from_model(post.author)
        tag_serializers = [TagSerializer.from_model(t) for t in post.tags.all()]

        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=tag_serializers,
            published=post.published,
        )

        result = serializer.dump()

        assert len(result["tags"]) == 2
        assert result["tags"][0]["name"] == "tag1"
        assert result["tags"][1]["name"] == "tag2"

    @pytest.mark.django_db
    def test_deeply_nested_relationships(self):
        """Test deeply nested serializers (post -> comments -> author)."""
        author1 = Author.objects.create(name="PostAuthor", email="post@example.com")
        author2 = Author.objects.create(name="Commenter", email="comment@example.com")

        post = BlogPost.objects.create(title="Deep Nesting", content="Content", author=author1)
        Comment.objects.create(post=post, author=author2, text="A comment")

        post = BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").get(id=post.id)

        author_serializer = AuthorSerializer.from_model(post.author)
        comment_serializers = [
            CommentSerializer(
                id=c.id,
                text=c.text,
                author=AuthorSerializer.from_model(c.author),
                created_at=c.created_at,
            )
            for c in post.comments.all()
        ]

        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=[],
            comments=comment_serializers,
            published=post.published,
        )

        result = serializer.dump()

        assert len(result["comments"]) == 1
        assert result["comments"][0]["text"] == "A comment"
        assert result["comments"][0]["author"]["name"] == "Commenter"
        assert result["comment_count"] == 1

    @pytest.mark.django_db
    def test_onetoone_relationship(self):
        """Test nested serializer with OneToOne relationship."""
        user = User.objects.create(
            username="profileuser",
            email="profile@example.com",
            password_hash="hash",
        )
        profile = UserProfile.objects.create(
            user=user,
            bio="Test bio",
            location="NYC",
        )

        profile = UserProfile.objects.select_related("user").get(id=profile.id)

        user_serializer = UserSerializer.from_model(profile.user)
        profile_serializer = UserProfileSerializer(
            id=profile.id,
            user=user_serializer,
            bio=profile.bio,
            avatar_url=None,
            phone=profile.phone,
            location=profile.location,
        )

        result = profile_serializer.dump()

        assert result["user"]["username"] == "profileuser"
        assert result["bio"] == "Test bio"
        assert result["has_avatar"] is False


class TestWriteOnlyFieldsWithDjango:
    """Test write_only fields with Django models."""

    @pytest.mark.django_db
    def test_write_only_excluded_from_dump(self):
        """Test write_only fields are excluded from dump."""
        user = User.objects.create(
            username="secureuser",
            email="secure@example.com",
            password_hash="super_secret_hash",
        )

        serializer = UserSerializer.from_model(user)
        result = serializer.dump()

        # password_hash is write_only, should not appear
        assert "password_hash" not in result
        assert "username" in result

    @pytest.mark.django_db
    def test_write_only_in_various_views(self):
        """Test write_only fields excluded across field sets."""
        user = User.objects.create(
            username="viewuser",
            email="view@example.com",
            password_hash="hash",
        )

        serializer = UserSerializer.from_model(user)

        # All views should exclude password_hash
        for field_set in ["list", "detail", "admin"]:
            result = UserSerializer.use(field_set).dump(serializer)
            assert "password_hash" not in result


class TestCreateSerializerHelpersWithDjango:
    """Test create_serializer and create_serializer_set helpers."""

    def test_create_serializer_from_django_model(self):
        """Test creating serializer from Django model."""
        from django.contrib.auth.models import User as DjangoUser  # noqa: PLC0415

        UserSerializer = create_serializer(
            DjangoUser,
            fields=["id", "username", "email", "is_active"],
        )

        assert "id" in UserSerializer.__annotations__
        assert "username" in UserSerializer.__annotations__
        assert "email" in UserSerializer.__annotations__
        assert "is_active" in UserSerializer.__annotations__

    @pytest.mark.django_db
    def test_create_serializer_config_applies_end_to_end(self):
        """Test create_serializer uses Config metadata for model mapping and dump behavior."""
        UserGeneratedSerializer = create_serializer(
            User,
            fields=["id", "username", "email", "password_hash", "created_at"],
            write_only={"password_hash"},
            read_only={"id", "created_at"},
            serializer_name="UserGeneratedSerializer",
        )

        user = User.objects.create(
            username="helperuser",
            email="helper@example.com",
            password_hash="super-secret",
        )

        serializer = UserGeneratedSerializer.from_model(user)
        dumped = serializer.dump()

        assert UserGeneratedSerializer.Config.model is User
        assert UserGeneratedSerializer.__write_only_fields__ == frozenset({"password_hash"})
        assert UserGeneratedSerializer.__read_only_fields__ == frozenset({"id", "created_at"})
        assert dumped["id"] == user.id
        assert dumped["username"] == "helperuser"
        assert dumped["email"] == "helper@example.com"
        assert "created_at" in dumped
        assert "password_hash" not in dumped

    @pytest.mark.django_db
    def test_create_serializer_set_with_django(self):
        """Test create_serializer_set with Django model."""
        from django.contrib.auth.models import User as DjangoUser  # noqa: PLC0415

        UserCreate, UserUpdate, UserPublic = create_serializer_set(
            DjangoUser,
            create_fields=["username", "email", "password"],
            update_fields=["email", "first_name", "last_name"],
            public_fields=["id", "username", "email"],
        )

        # Verify each has correct fields
        assert "username" in UserCreate.__annotations__
        assert "password" in UserCreate.__annotations__

        assert "email" in UserUpdate.__annotations__
        assert "username" not in UserUpdate.__annotations__

        assert "id" in UserPublic.__annotations__
        assert "password" not in UserPublic.__annotations__


class TestBulkOperationsWithDjango:
    """Test bulk operations with Django models."""

    @pytest.mark.django_db
    def test_dump_many_with_django_queryset(self):
        """Test dump_many with a queryset of models."""
        a1 = Author.objects.create(name="Author1", email="a1@example.com")
        a2 = Author.objects.create(name="Author2", email="a2@example.com")
        a3 = Author.objects.create(name="Author3", email="a3@example.com")

        authors = Author.objects.filter(id__in=[a1.id, a2.id, a3.id])
        serializers = [AuthorSerializer.from_model(a) for a in authors]

        result = AuthorSerializer.dump_many(serializers)

        assert len(result) == 3
        assert all("display_name" in item for item in result)

    @pytest.mark.django_db
    def test_dump_many_json_with_django(self):
        """Test dump_many_json with Django models."""
        Author.objects.create(name="JSON1", email="j1@example.com")
        Author.objects.create(name="JSON2", email="j2@example.com")

        authors = Author.objects.all()
        serializers = [AuthorSerializer.from_model(a) for a in authors]

        json_bytes = AuthorSerializer.dump_many_json(serializers)

        assert isinstance(json_bytes, bytes)
        assert b"JSON1" in json_bytes
        assert b"JSON2" in json_bytes

    @pytest.mark.django_db
    def test_dump_many_with_field_selection_django(self):
        """Test dump_many with only() on Django data."""
        User.objects.create(username="bulk1", email="bulk1@example.com", password_hash="h1")
        User.objects.create(username="bulk2", email="bulk2@example.com", password_hash="h2")

        users = User.objects.all()
        serializers = [UserSerializer.from_model(u) for u in users]

        result = UserSerializer.only("id", "username").dump_many(serializers)

        assert len(result) == 2
        for item in result:
            assert set(item.keys()) == {"id", "username"}


class TestComplexDjangoScenarios:
    """Test complex real-world Django scenarios."""

    @pytest.mark.django_db
    def test_api_list_view_pattern(self):
        """Test typical API list view pattern."""
        # Create test data
        author = Author.objects.create(name="Blogger", email="blogger@example.com")
        created_posts = []
        for i in range(5):
            post = BlogPost.objects.create(
                title=f"Post {i + 1}",
                content=f"Content for post {i + 1}",
                author=author,
                published=i % 2 == 0,
            )
            created_posts.append(post.id)

        # Fetch only the posts we created
        posts = BlogPost.objects.filter(id__in=created_posts).select_related("author").order_by("-created_at")

        # Use list field set for minimal data
        BlogPostList = BlogPostSerializer.fields("list")

        results = []
        for post in posts:
            # For list view, we don't need all nested data
            s = BlogPostList(
                id=post.id,
                title=post.title,
                published=post.published,
                created_at=post.created_at,
            )
            results.append(s.dump())

        assert len(results) == 5
        for item in results:
            assert set(item.keys()) == {"id", "title", "published", "created_at"}

    @pytest.mark.django_db
    def test_api_detail_view_pattern(self):
        """Test typical API detail view pattern with full relationships."""
        author = Author.objects.create(name="DetailAuthor", email="detail@example.com")
        tag1 = Tag.objects.create(name="detail-tag-1")
        tag2 = Tag.objects.create(name="detail-tag-2")

        post = BlogPost.objects.create(
            title="Detailed Post",
            content="Full content here",
            author=author,
            published=True,
        )
        post.tags.add(tag1, tag2)

        Comment.objects.create(post=post, author=author, text="Self comment")

        # Detail view needs full data
        post = BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").get(id=post.id)

        # Build full serializer
        author_serializer = AuthorSerializer.from_model(post.author)
        tag_serializers = [TagSerializer.from_model(t) for t in post.tags.all()]
        comment_serializers = [
            CommentSerializer(
                id=c.id,
                text=c.text,
                author=AuthorSerializer.from_model(c.author),
                created_at=c.created_at,
            )
            for c in post.comments.all()
        ]

        serializer = BlogPostSerializer(
            id=post.id,
            title=post.title,
            content=post.content,
            author=author_serializer,
            tags=tag_serializers,
            comments=comment_serializers,
            published=post.published,
            created_at=post.created_at,
            updated_at=post.updated_at,
        )

        # Full dump includes computed fields
        result = serializer.dump()

        assert result["title"] == "Detailed Post"
        assert result["author"]["name"] == "DetailAuthor"
        assert len(result["tags"]) == 2
        assert len(result["comments"]) == 1
        # Computed fields are included in full dump
        assert result["tag_names"] == ["detail-tag-1", "detail-tag-2"]
        assert result["comment_count"] == 1

        # use("admin") filters to admin field_set only - computed fields must be
        # explicitly included in the field_set to appear in output
        admin_result = BlogPostSerializer.use("admin").dump(serializer)
        assert "title" in admin_result
        assert "comments" in admin_result

    @pytest.mark.django_db
    def test_one_serializer_multiple_views_pattern(self):
        """Test the DRF-replacement pattern: one serializer, multiple views."""
        # Create user
        user = User.objects.create(
            username="multiview",
            email="multi@example.com",
            password_hash="secret_hash",
            is_staff=True,
        )

        full_serializer = UserSerializer.from_model(user)

        # List view - minimal
        list_result = UserSerializer.use("list").dump(full_serializer)
        assert set(list_result.keys()) == {"id", "username", "is_active"}

        # Detail view - more info
        detail_result = UserSerializer.use("detail").dump(full_serializer)
        assert "email" in detail_result
        assert "password_hash" not in detail_result  # write_only

        # Admin view - all allowed fields
        admin_result = UserSerializer.use("admin").dump(full_serializer)
        assert "is_staff" in admin_result
        assert "password_hash" not in admin_result

        # Custom subset for public API
        UserPublic = UserSerializer.subset("id", "username", "display_name")
        public_user = UserPublic.from_parent(full_serializer)
        public_result = public_user.dump()
        assert public_result == {
            "id": user.id,
            "username": "multiview",
            "display_name": "@multiview",
        }


# =============================================================================
# Comprehensive Serializer showcasing ALL features
# =============================================================================


class ComprehensiveProductSerializer(Serializer):
    """
    A single serializer class demonstrating ALL features of the Serializer system.

    This serializer showcases:
    1. Basic field types with type annotations
    2. Reusable validated types (Email, URL, NonEmptyStr, PositiveInt, etc.)
    3. Annotated constraints with msgspec.Meta (min_length, max_length, ge, le, pattern)
    4. field() configuration (read_only, write_only, source, alias, default, default_factory)
    5. @field_validator decorator for field-level validation/transformation
    6. @model_validator decorator for cross-field validation
    7. @computed_field decorator for calculated output fields
    8. Nested serializers inferred from serializer type hints
    9. Meta class configuration (write_only, read_only, field_sets)
    10. Dynamic field selection (only, exclude, use)
    11. Type-safe subsets (subset, fields)
    12. Dump options (exclude_none, exclude_defaults)
    13. Django model integration (from_model, to_model, update_instance, to_dict)
    14. Bulk operations (dump_many, dump_many_json)
    15. JSON serialization (dump_json, model_validate_json, model_validate)

    """

    # -------------------------------------------------------------------------
    # 1. Basic field with read_only (output-only, auto-generated)
    # -------------------------------------------------------------------------
    id: int = field(read_only=True, description="Auto-generated product ID")

    # -------------------------------------------------------------------------
    # 2. Reusable validated type (NonEmptyStr - min_length=1)
    # -------------------------------------------------------------------------
    name: NonEmptyStr

    # -------------------------------------------------------------------------
    # 3. Annotated constraints with msgspec.Meta
    # -------------------------------------------------------------------------
    sku: Annotated[str, Meta(pattern=r"^[A-Z]{2,4}-[0-9]{4,8}$", description="Stock Keeping Unit")]

    # -------------------------------------------------------------------------
    # 4. Field with alias (JSON uses different name)
    # -------------------------------------------------------------------------
    description: Annotated[str, Meta(max_length=2000)] = field(
        alias="desc",
        default="",
        description="Product description",
    )

    # -------------------------------------------------------------------------
    # 5. Numeric constraint types
    # -------------------------------------------------------------------------
    price: Annotated[float, Meta(ge=0.0, description="Product price in USD")]
    quantity: PositiveInt  # Must be > 0
    discount_percent: Percentage = 0.0  # 0-100 range

    # -------------------------------------------------------------------------
    # 6. Write-only field (input only, never in output)
    # -------------------------------------------------------------------------
    internal_cost: float = field(write_only=True, default=0.0)

    # -------------------------------------------------------------------------
    # 7. Source mapping (API field maps to different model attribute)
    # -------------------------------------------------------------------------
    category_name: str = field(source="category.name", default="Uncategorized")

    # -------------------------------------------------------------------------
    # 8. Field with default_factory (for mutable defaults)
    # -------------------------------------------------------------------------
    tags_list: list[str] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # 9. Optional fields with None
    # -------------------------------------------------------------------------
    website: URL | None = None
    manufacturer_email: Email | None = None

    # -------------------------------------------------------------------------
    # 10. Datetime fields
    # -------------------------------------------------------------------------
    created_at: datetime | None = field(read_only=True, default=None)
    updated_at: datetime | None = field(read_only=True, default=None)

    # -------------------------------------------------------------------------
    # 11. Boolean with default
    # -------------------------------------------------------------------------
    is_active: bool = True
    is_featured: bool = False

    # -------------------------------------------------------------------------
    # 12. Nested serializer (single object - ForeignKey equivalent)
    # -------------------------------------------------------------------------
    supplier: AuthorSerializer | None = None

    # -------------------------------------------------------------------------
    # 13. Nested serializer list (ManyToMany equivalent)
    # -------------------------------------------------------------------------
    related_tags: list[TagSerializer] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # Config class configuration
    # -------------------------------------------------------------------------
    class Config:
        # Fields only in output, never accepted in input
        read_only = {"id", "created_at", "updated_at"}

        # Fields only in input, never in output
        write_only = {"internal_cost"}

        # Predefined field sets for different views
        field_sets = {
            "list": ["id", "name", "sku", "price", "is_active"],
            "detail": [
                "id",
                "name",
                "sku",
                "description",
                "price",
                "quantity",
                "discount_percent",
                "website",
                "is_active",
                "is_featured",
                "created_at",
                "updated_at",
            ],
            "admin": [
                "id",
                "name",
                "sku",
                "description",
                "price",
                "quantity",
                "discount_percent",
                "internal_cost",
                "category_name",
                "tags_list",
                "website",
                "manufacturer_email",
                "is_active",
                "is_featured",
                "supplier",
                "related_tags",
                "created_at",
                "updated_at",
                # Include computed fields explicitly
                "display_price",
                "is_on_sale",
                "tag_count",
            ],
            "export": ["id", "name", "sku", "price", "quantity", "is_active"],
        }

    # -------------------------------------------------------------------------
    # 14. @field_validator - Transform/validate individual fields
    # -------------------------------------------------------------------------
    @field_validator("name")
    def normalize_name(cls, value: str) -> str:
        """Normalize product name: strip whitespace, title case."""
        return value.strip().title()

    @field_validator("sku")
    def uppercase_sku(cls, value: str) -> str:
        """Ensure SKU is uppercase."""
        return value.upper()

    @field_validator("manufacturer_email")
    def lowercase_email(cls, value: str | None) -> str | None:
        """Normalize email to lowercase."""
        if value is not None:
            return value.lower()
        return value

    # -------------------------------------------------------------------------
    # 15. @model_validator - Cross-field validation
    # -------------------------------------------------------------------------
    @model_validator
    def validate_pricing(self) -> "ComprehensiveProductSerializer":
        """Ensure discount doesn't exceed price logic."""
        if self.discount_percent > 0 and self.price <= 0:
            raise ValueError("Cannot apply discount to zero-priced product")
        if self.discount_percent >= 100:
            raise ValueError("Discount cannot be 100% or more")
        return self

    # -------------------------------------------------------------------------
    # 16. @computed_field - Calculated output-only fields
    # -------------------------------------------------------------------------
    @computed_field
    def display_price(self) -> str:
        """Formatted price string."""
        return f"${self.price:.2f}"

    @computed_field
    def is_on_sale(self) -> bool:
        """Whether product has an active discount."""
        return self.discount_percent > 0

    @computed_field
    def discounted_price(self) -> float | None:
        """Price after discount, or None if no discount."""
        if self.discount_percent > 0:
            return round(self.price * (1 - self.discount_percent / 100), 2)
        return None

    @computed_field
    def tag_count(self) -> int:
        """Number of tags."""
        return len(self.tags_list)

    @computed_field
    def full_title(self) -> str:
        """Full product title with SKU."""
        return f"{self.name} ({self.sku})"


class TestComprehensiveSerializer:
    """Tests demonstrating ALL features of ComprehensiveProductSerializer."""

    # -------------------------------------------------------------------------
    # Test 1: Basic instantiation and field validators
    # -------------------------------------------------------------------------
    def test_basic_instantiation_with_field_validators(self):
        """Test that field validators run during direct instantiation."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="  test product  ",  # Will be stripped and title-cased
            sku="ab-1234",  # Will be uppercased
            price=99.99,
            quantity=10,
        )

        # Field validators should have transformed values
        assert product.name == "Test Product"  # Stripped and title-cased
        assert product.sku == "AB-1234"  # Uppercased

    # -------------------------------------------------------------------------
    # Test 2: Model validator (cross-field validation)
    # -------------------------------------------------------------------------
    def test_model_validator_passes(self):
        """Test model validator allows valid combinations."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Valid Product",
            sku="XX-9999",
            price=100.0,
            quantity=5,
            discount_percent=25.0,  # 25% off $100 is valid
        )
        assert product.discount_percent == 25.0

    def test_model_validator_fails_on_invalid_discount(self):
        """Test model validator rejects invalid discount on zero price."""
        with pytest.raises(RequestValidationError, match="Cannot apply discount"):
            ComprehensiveProductSerializer(
                id=1,
                name="Invalid Product",
                sku="XX-0000",
                price=0.0,  # Zero price
                quantity=1,
                discount_percent=10.0,  # But has discount
            )

    def test_model_validator_fails_on_100_percent_discount(self):
        """Test model validator rejects 100% discount."""
        with pytest.raises(RequestValidationError, match="Discount cannot be 100%"):
            ComprehensiveProductSerializer(
                id=1,
                name="Free Product",
                sku="XX-0000",
                price=50.0,
                quantity=1,
                discount_percent=100.0,  # 100% discount not allowed
            )

    # -------------------------------------------------------------------------
    # Test 3: Computed fields
    # -------------------------------------------------------------------------
    def test_computed_fields_in_dump(self):
        """Test computed fields appear in dump output."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Gadget",
            sku="GD-1234",
            price=149.99,
            quantity=100,
            discount_percent=20.0,
            tags_list=["electronics", "sale", "new"],
        )

        result = product.dump()

        # Computed fields should be in output
        assert result["display_price"] == "$149.99"
        assert result["is_on_sale"] is True
        assert result["discounted_price"] == 119.99  # 149.99 * 0.8
        assert result["tag_count"] == 3
        assert result["full_title"] == "Gadget (GD-1234)"

    def test_computed_field_no_discount(self):
        """Test computed discounted_price is None when no discount."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Regular Item",
            sku="RI-5555",
            price=50.0,
            quantity=10,
            discount_percent=0.0,
        )

        result = product.dump()
        assert result["is_on_sale"] is False
        assert result["discounted_price"] is None

    # -------------------------------------------------------------------------
    # Test 4: Write-only fields excluded from dump
    # -------------------------------------------------------------------------
    def test_write_only_excluded_from_dump(self):
        """Test write_only fields are not in dump output."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Secret Cost Product",
            sku="SC-1111",
            price=200.0,
            quantity=5,
            internal_cost=75.0,  # Write-only field
        )

        result = product.dump()
        assert "internal_cost" not in result

    # -------------------------------------------------------------------------
    # Test 5: Read-only fields (via Meta.read_only)
    # -------------------------------------------------------------------------
    def test_read_only_fields_in_output(self):
        """Test read_only fields appear in output."""
        now = datetime.now()
        product = ComprehensiveProductSerializer(
            id=42,
            name="Product",
            sku="PR-4242",
            price=10.0,
            quantity=1,
            created_at=now,
        )

        result = product.dump()
        assert result["id"] == 42
        assert result["created_at"] == now

    # -------------------------------------------------------------------------
    # Test 6: Optional fields with None
    # -------------------------------------------------------------------------
    def test_optional_fields_none(self):
        """Test optional fields can be None."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Basic",
            sku="BA-0001",
            price=5.0,
            quantity=1,
            website=None,
            manufacturer_email=None,
        )

        result = product.dump()
        assert result["website"] is None
        assert result["manufacturer_email"] is None

    def test_optional_fields_with_values(self):
        """Test optional fields with actual values."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Full Details",
            sku="FD-9999",
            price=999.99,
            quantity=1,
            website="https://example.com/product",
            manufacturer_email="CONTACT@MAKER.COM",  # Will be lowercased
        )

        result = product.dump()
        assert result["website"] == "https://example.com/product"
        assert result["manufacturer_email"] == "contact@maker.com"  # Lowercased

    # -------------------------------------------------------------------------
    # Test 7: Field with default_factory
    # -------------------------------------------------------------------------
    def test_default_factory_creates_new_list(self):
        """Test default_factory creates independent list instances."""
        p1 = ComprehensiveProductSerializer(id=1, name="P1", sku="P1-0001", price=1.0, quantity=1)
        p2 = ComprehensiveProductSerializer(id=2, name="P2", sku="P2-0002", price=2.0, quantity=1)

        # Each should have independent list
        p1.tags_list.append("tag1")
        assert p1.tags_list == ["tag1"]
        assert p2.tags_list == []  # Not affected

    # -------------------------------------------------------------------------
    # Test 8: Nested serializers
    # -------------------------------------------------------------------------
    def test_nested_serializer_single(self):
        """Test nested serializer for single object."""
        supplier = AuthorSerializer(
            id=100,
            name="ACME Corp",
            email="sales@acme.com",
            bio="Leading supplier",
        )

        product = ComprehensiveProductSerializer(
            id=1,
            name="ACME Widget",
            sku="AW-1000",
            price=50.0,
            quantity=100,
            supplier=supplier,
        )

        result = product.dump()
        assert result["supplier"]["id"] == 100
        assert result["supplier"]["name"] == "ACME Corp"
        assert result["supplier"]["display_name"] == "ACME Corp <sales@acme.com>"

    def test_nested_serializer_many(self):
        """Test nested serializer with many=True."""
        tags = [
            TagSerializer(id=1, name="electronics"),
            TagSerializer(id=2, name="gadgets"),
            TagSerializer(id=3, name="new-arrivals"),
        ]

        product = ComprehensiveProductSerializer(
            id=1,
            name="Tagged Product",
            sku="TP-1111",
            price=75.0,
            quantity=50,
            related_tags=tags,
        )

        result = product.dump()
        assert len(result["related_tags"]) == 3
        assert result["related_tags"][0]["name"] == "electronics"
        assert result["related_tags"][2]["name"] == "new-arrivals"

    # -------------------------------------------------------------------------
    # Test 9: Dynamic field selection with only()
    # -------------------------------------------------------------------------
    def test_only_specific_fields(self):
        """Test only() returns specific fields."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Selective",
            sku="SL-1111",
            price=100.0,
            quantity=10,
            is_active=True,
            is_featured=True,
        )

        result = ComprehensiveProductSerializer.only("id", "name", "price").dump(product)

        assert result == {"id": 1, "name": "Selective", "price": 100.0}
        assert "sku" not in result
        assert "quantity" not in result

    # -------------------------------------------------------------------------
    # Test 10: Dynamic field selection with exclude()
    # -------------------------------------------------------------------------
    def test_exclude_specific_fields(self):
        """Test exclude() removes specific fields."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Exclusion Test",
            sku="EX-1111",
            price=50.0,
            quantity=5,
        )

        result = ComprehensiveProductSerializer.exclude(
            "created_at",
            "updated_at",
            "supplier",
            "related_tags",
            "tags_list",
            "category_name",
            "website",
            "manufacturer_email",
            "is_featured",
            "discount_percent",
            "internal_cost",
        ).dump(product)

        assert "id" in result
        assert "name" in result
        assert "created_at" not in result
        assert "supplier" not in result

    # -------------------------------------------------------------------------
    # Test 11: Dynamic field selection with use() (predefined field sets)
    # -------------------------------------------------------------------------
    def test_use_list_field_set(self):
        """Test use() with 'list' field set."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="List View",
            sku="LV-1111",
            price=25.0,
            quantity=100,
            is_active=True,
        )

        result = ComprehensiveProductSerializer.use("list").dump(product)

        assert set(result.keys()) == {"id", "name", "sku", "price", "is_active"}

    def test_use_detail_field_set(self):
        """Test use() with 'detail' field set."""
        now = datetime.now()
        product = ComprehensiveProductSerializer(
            id=1,
            name="Detail View",
            sku="DV-2222",
            description="Full description here",
            price=150.0,
            quantity=20,
            discount_percent=10.0,
            website="https://example.com",
            is_active=True,
            is_featured=True,
            created_at=now,
            updated_at=now,
        )

        result = ComprehensiveProductSerializer.use("detail").dump(product)

        expected_keys = {
            "id",
            "name",
            "sku",
            "desc",  # The alias of `description` is its key in the output.
            "price",
            "quantity",
            "discount_percent",
            "website",
            "is_active",
            "is_featured",
            "created_at",
            "updated_at",
        }
        assert set(result.keys()) == expected_keys

    def test_use_admin_field_set_with_computed(self):
        """Test use('admin') includes computed fields when explicitly listed."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Admin View",
            sku="AD-3333",
            price=500.0,
            quantity=10,
            discount_percent=15.0,
            tags_list=["premium", "featured"],
        )

        result = ComprehensiveProductSerializer.use("admin").dump(product)

        # Admin field set explicitly includes computed fields
        assert "display_price" in result
        assert "is_on_sale" in result
        assert "tag_count" in result
        assert result["display_price"] == "$500.00"
        assert result["tag_count"] == 2

    # -------------------------------------------------------------------------
    # Test 12: Type-safe subsets with subset() - uses non-kw_only parent
    # -------------------------------------------------------------------------
    def test_subset_with_simple_serializer(self):
        """Test subset() works with serializers that don't use kw_only."""
        # Use AuthorSerializer which doesn't have kw_only=True
        AuthorMini = AuthorSerializer.subset("id", "name")

        # Should be a proper class
        assert isinstance(AuthorMini, type)

        # Can instantiate
        mini = AuthorMini(id=1, name="Mini Author")
        assert mini.id == 1
        assert mini.name == "Mini Author"

        # Dump only has those fields
        result = mini.dump()
        assert set(result.keys()) == {"id", "name"}

    def test_subset_with_computed_field_simple(self):
        """Test subset includes computed fields from simple serializer."""
        # Use AuthorSerializer which has a display_name computed field
        AuthorWithDisplay = AuthorSerializer.subset("id", "name", "email", "display_name")

        author = AuthorWithDisplay(id=1, name="John", email="john@example.com")
        result = author.dump()

        assert result["display_name"] == "John <john@example.com>"

    # -------------------------------------------------------------------------
    # Test 13: Type-safe subsets with fields() - uses BlogPostSerializer
    # -------------------------------------------------------------------------
    def test_fields_from_field_set_simple(self):
        """Test fields() creates class from Meta.field_sets (simple serializer)."""
        # BlogPostSerializer has field_sets defined
        PostList = BlogPostSerializer.fields("list")

        # Instantiate with required fields
        list_item = PostList(
            id=1,
            title="Post Title",
            published=True,
            created_at=datetime.now(),
        )

        result = list_item.dump()
        assert set(result.keys()) == {"id", "title", "published", "created_at"}

    # -------------------------------------------------------------------------
    # Test 14: from_parent() to create subset from full instance
    # -------------------------------------------------------------------------
    def test_from_parent_with_simple_serializer(self):
        """Test from_parent() creates subset from parent instance."""
        full_author = AuthorSerializer(
            id=42,
            name="Full Author",
            email="full@example.com",
            bio="Full bio text",
        )

        # For computed fields that depend on other fields, include all required fields
        AuthorMini = AuthorSerializer.subset("id", "name", "email", "display_name")
        mini = AuthorMini.from_parent(full_author)

        assert mini.id == 42
        assert mini.name == "Full Author"
        result = mini.dump()
        assert result["display_name"] == "Full Author <full@example.com>"

    # -------------------------------------------------------------------------
    # Test 15: Dump with exclude_none=True
    # -------------------------------------------------------------------------
    def test_dump_exclude_none(self):
        """Test dump(exclude_none=True) omits None values."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="No Nulls",
            sku="NN-0001",
            price=10.0,
            quantity=1,
            website=None,  # Will be excluded
            manufacturer_email=None,  # Will be excluded
            supplier=None,  # Will be excluded
        )

        result = product.dump(exclude_none=True)

        assert "website" not in result
        assert "manufacturer_email" not in result
        assert "supplier" not in result
        assert "name" in result  # Non-None fields still present

    # -------------------------------------------------------------------------
    # Test 16: Dump with exclude_defaults=True
    # -------------------------------------------------------------------------
    def test_dump_exclude_defaults(self):
        """Test dump(exclude_defaults=True) omits default values."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Custom Values",
            sku="CV-0001",
            price=100.0,
            quantity=1,
            is_active=True,  # Default value
            is_featured=False,  # Default value
            discount_percent=0.0,  # Default value
        )

        result = product.dump(exclude_defaults=True)

        # Default values should be excluded
        assert "is_active" not in result  # True is default
        assert "is_featured" not in result  # False is default
        assert "discount_percent" not in result  # 0.0 is default

        # Non-default values should be present
        assert "id" in result
        assert "name" in result
        assert "price" in result

    # -------------------------------------------------------------------------
    # Test 17: dump_many for bulk serialization
    # -------------------------------------------------------------------------
    def test_dump_many(self):
        """Test dump_many for multiple instances."""
        products = [
            ComprehensiveProductSerializer(id=1, name="Product 1", sku="P1-0001", price=10.0, quantity=1),
            ComprehensiveProductSerializer(id=2, name="Product 2", sku="P2-0002", price=20.0, quantity=2),
            ComprehensiveProductSerializer(id=3, name="Product 3", sku="P3-0003", price=30.0, quantity=3),
        ]

        results = ComprehensiveProductSerializer.dump_many(products)

        assert len(results) == 3
        assert results[0]["name"] == "Product 1"
        assert results[1]["price"] == 20.0
        assert results[2]["quantity"] == 3

    def test_dump_many_with_field_selection(self):
        """Test dump_many with only() field selection."""
        products = [
            ComprehensiveProductSerializer(id=1, name="P1", sku="S1-0001", price=10.0, quantity=1),
            ComprehensiveProductSerializer(id=2, name="P2", sku="S2-0002", price=20.0, quantity=2),
        ]

        results = ComprehensiveProductSerializer.only("id", "name").dump_many(products)

        for result in results:
            assert set(result.keys()) == {"id", "name"}

    # -------------------------------------------------------------------------
    # Test 18: dump_many_json for JSON bytes output
    # -------------------------------------------------------------------------
    def test_dump_many_json(self):
        """Test dump_many_json returns JSON bytes."""
        products = [
            ComprehensiveProductSerializer(id=1, name="Product One", sku="P1-0001", price=10.0, quantity=1),
            ComprehensiveProductSerializer(id=2, name="Product Two", sku="P2-0002", price=20.0, quantity=2),
        ]

        json_bytes = ComprehensiveProductSerializer.dump_many_json(products)

        assert isinstance(json_bytes, bytes)
        # Name validator title-cases, so check for title-cased names
        assert b"Product One" in json_bytes
        assert b"Product Two" in json_bytes

    # -------------------------------------------------------------------------
    # Test 19: dump_json for single instance
    # -------------------------------------------------------------------------
    def test_dump_json(self):
        """Test dump_json returns JSON bytes for single instance."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Single Product",  # Will be title-cased by validator
            sku="SP-0001",
            price=50.0,
            quantity=10,
        )

        json_bytes = product.dump_json()

        assert isinstance(json_bytes, bytes)
        assert b"Single Product" in json_bytes
        assert b'"price":50.0' in json_bytes or b'"price": 50.0' in json_bytes

    # -------------------------------------------------------------------------
    # Test 20: model_validate_json for JSON parsing with validation
    # -------------------------------------------------------------------------
    def test_model_validate_json(self):
        """Test model_validate_json parses and validates JSON.

        NOTE: msgspec pattern validation runs BEFORE field validators,
        so the SKU must already match the pattern in the JSON input.
        The field validator then uppercases it (though already uppercase).
        """
        json_data = b"""{
            "id": 1,
            "name": "  json parsed  ",
            "sku": "JP-1234",
            "price": 99.99,
            "quantity": 10
        }"""

        product = ComprehensiveProductSerializer.model_validate_json(json_data)

        # Field validators should have run
        assert product.name == "Json Parsed"  # Stripped and title-cased
        assert product.sku == "JP-1234"  # Already uppercase, kept uppercase

    def test_model_validate_json_fails_on_invalid_sku(self):
        """Test model_validate_json rejects invalid SKU pattern."""
        json_data = b"""{
            "id": 1,
            "name": "Bad SKU",
            "sku": "invalid-format",
            "price": 10.0,
            "quantity": 1
        }"""

        # Meta pattern validation in msgspec
        with pytest.raises(RequestValidationError):
            ComprehensiveProductSerializer.model_validate_json(json_data)

    # -------------------------------------------------------------------------
    # Test 21: model_validate for dict parsing with validation
    # -------------------------------------------------------------------------
    def test_model_validate_dict(self):
        """Test model_validate parses dict with validation.

        NOTE: Like JSON parsing, dict parsing via msgspec validates
        the pattern BEFORE field validators run. So SKU must match pattern.
        """
        data = {
            "id": 1,
            "name": "  dict parsed  ",
            "sku": "DP-5678",  # Must match pattern (uppercase)
            "price": 150.0,
            "quantity": 5,
        }

        product = ComprehensiveProductSerializer.model_validate(data)

        assert product.name == "Dict Parsed"
        assert product.sku == "DP-5678"

    # -------------------------------------------------------------------------
    # Test 22: to_dict method
    # -------------------------------------------------------------------------
    def test_to_dict(self):
        """Test to_dict returns raw dictionary of struct fields only.

        NOTE: to_dict() returns only struct fields, NOT computed fields.
        Use dump() if you need computed fields in output.
        """
        product = ComprehensiveProductSerializer(
            id=1,
            name="To Dict",
            sku="TD-0001",
            price=25.0,
            quantity=5,
        )

        data = product.to_dict()

        assert isinstance(data, dict)
        assert data["name"] == "To Dict"
        assert data["sku"] == "TD-0001"
        assert data["price"] == 25.0
        # to_dict returns struct fields, NOT computed fields
        assert "display_price" not in data  # Computed fields not included
        # All struct fields are included (even write_only)
        assert "internal_cost" in data

    # -------------------------------------------------------------------------
    # Test 23: Chained field selection
    # -------------------------------------------------------------------------
    def test_chained_field_selection(self):
        """Test chaining use() with exclude()."""
        product = ComprehensiveProductSerializer(
            id=1,
            name="Chained",
            sku="CH-0001",
            price=100.0,
            quantity=10,
            is_active=True,
        )

        # Start with list field set, then exclude some
        result = ComprehensiveProductSerializer.use("list").exclude("is_active").dump(product)

        assert set(result.keys()) == {"id", "name", "sku", "price"}
        assert "is_active" not in result

    # -------------------------------------------------------------------------
    # Test 24: Validated types (Email, URL, etc.) via JSON parsing
    # -------------------------------------------------------------------------
    def test_validated_type_email_valid(self):
        """Test Email type accepts valid email via JSON parsing."""
        json_data = b"""{
            "id": 1,
            "name": "Email Test",
            "sku": "ET-0001",
            "price": 10.0,
            "quantity": 1,
            "manufacturer_email": "VALID@EXAMPLE.COM"
        }"""

        product = ComprehensiveProductSerializer.model_validate_json(json_data)
        assert product.manufacturer_email == "valid@example.com"  # Lowercased

    def test_validated_type_email_invalid(self):
        """Test Email type rejects invalid email via JSON parsing."""
        json_data = b"""{
            "id": 1,
            "name": "Bad Email",
            "sku": "BE-0001",
            "price": 10.0,
            "quantity": 1,
            "manufacturer_email": "not-an-email"
        }"""

        # Meta pattern validation in msgspec
        with pytest.raises(RequestValidationError):
            ComprehensiveProductSerializer.model_validate_json(json_data)

    def test_validated_type_url_valid(self):
        """Test URL type accepts valid URL via JSON parsing."""
        json_data = b"""{
            "id": 1,
            "name": "URL Test",
            "sku": "UT-0001",
            "price": 10.0,
            "quantity": 1,
            "website": "https://example.com/product"
        }"""

        product = ComprehensiveProductSerializer.model_validate_json(json_data)
        assert product.website == "https://example.com/product"

    def test_validated_type_positive_int_invalid(self):
        """Test PositiveInt rejects zero/negative via JSON parsing."""
        json_data = b"""{
            "id": 1,
            "name": "Zero Qty",
            "sku": "ZQ-0001",
            "price": 10.0,
            "quantity": 0
        }"""

        # Meta constraint validation in msgspec
        with pytest.raises(RequestValidationError):
            ComprehensiveProductSerializer.model_validate_json(json_data)

    # -------------------------------------------------------------------------
    # Test 25: Integration with Django models
    # -------------------------------------------------------------------------
    @pytest.mark.django_db
    def test_full_integration_with_django_models(self):
        """Test full integration scenario with Django models."""
        # Create related models
        supplier = Author.objects.create(
            name="Django Supplier",
            email="supplier@django.test",
            bio="Test supplier",
        )

        tag1 = Tag.objects.create(name="integration")
        tag2 = Tag.objects.create(name="django")

        # Create supplier serializer
        supplier_serializer = AuthorSerializer.from_model(supplier)
        tag_serializers = [TagSerializer.from_model(t) for t in [tag1, tag2]]

        # Create comprehensive product
        product = ComprehensiveProductSerializer(
            id=1,
            name="Integration Product",
            sku="IP-1234",
            price=299.99,
            quantity=50,
            discount_percent=15.0,
            is_active=True,
            is_featured=True,
            supplier=supplier_serializer,
            related_tags=tag_serializers,
            tags_list=["test", "integration"],
        )

        # Full dump
        full_result = product.dump()
        assert full_result["name"] == "Integration Product"
        assert full_result["supplier"]["name"] == "Django Supplier"
        assert len(full_result["related_tags"]) == 2
        assert full_result["display_price"] == "$299.99"
        assert full_result["is_on_sale"] is True
        assert full_result["discounted_price"] == 254.99  # 299.99 * 0.85

        # List view
        list_result = ComprehensiveProductSerializer.use("list").dump(product)
        assert set(list_result.keys()) == {"id", "name", "sku", "price", "is_active"}

        # Admin view with computed
        admin_result = ComprehensiveProductSerializer.use("admin").dump(product)
        assert "display_price" in admin_result
        assert "supplier" in admin_result
        assert "related_tags" in admin_result
        assert admin_result["tag_count"] == 2

        # Use only() for public view (runtime field selection)
        public_result = ComprehensiveProductSerializer.only("id", "name", "price", "display_price", "is_on_sale").dump(
            product
        )
        assert set(public_result.keys()) == {"id", "name", "price", "display_price", "is_on_sale"}

    # -------------------------------------------------------------------------
    # Test 26: Bulk operations with Django
    # -------------------------------------------------------------------------
    @pytest.mark.django_db
    def test_bulk_operations_with_django(self):
        """Test bulk operations with Django model data."""
        # Create multiple authors as suppliers
        suppliers = [Author.objects.create(name=f"Supplier {i}", email=f"s{i}@test.com") for i in range(3)]

        # Create products with suppliers
        products = [
            ComprehensiveProductSerializer(
                id=i + 1,
                name=f"Bulk Product {i + 1}",
                sku=f"BP-{i + 1:04d}",
                price=float((i + 1) * 100),
                quantity=(i + 1) * 10,
                supplier=AuthorSerializer.from_model(suppliers[i]),
            )
            for i in range(3)
        ]

        # Bulk dump
        results = ComprehensiveProductSerializer.dump_many(products)
        assert len(results) == 3
        assert results[0]["supplier"]["name"] == "Supplier 0"
        assert results[2]["price"] == 300.0

        # Bulk dump with field selection
        list_results = ComprehensiveProductSerializer.use("list").dump_many(products)
        for result in list_results:
            assert set(result.keys()) == {"id", "name", "sku", "price", "is_active"}


# =============================================================================
# Tests for field() source parameter with from_model()
# =============================================================================


class TestFromModelWithSource:
    """Test that from_model() correctly handles field(source=...) to map
    serializer field names to different model attribute names.

    Bug: When using field(source="model_attr"), from_model() was checking
    hasattr(instance, field_name) where field_name is the serializer field,
    not the source attribute. This caused validation errors when the source
    attribute exists but the serializer field name doesn't match.
    """

    @pytest.mark.django_db
    def test_from_model_with_simple_source(self):
        """Test from_model with source mapping to a different attribute name."""

        class AuthorWithRenamedEmail(Serializer):
            """Serializer that renames 'email' to 'email_address' in API."""

            id: int
            name: str
            email_address: str = field(source="email")  # API uses email_address, model uses email

        author = Author.objects.create(
            name="Test Author",
            email="test@example.com",
        )

        # from_model should use 'email' attribute (source) to populate 'email_address' field
        serializer = AuthorWithRenamedEmail.from_model(author)

        assert serializer.id == author.id
        assert serializer.name == "Test Author"
        assert serializer.email_address == "test@example.com"

    @pytest.mark.django_db
    def test_from_model_with_source_and_read_only(self):
        """Test from_model with source combined with read_only."""

        class UserWithSourceAndReadOnly(Serializer):
            """Serializer with source and read_only combined."""

            id: int = field(read_only=True)
            username: str
            mail: str = field(source="email", read_only=True)  # Renamed + read_only

        user = User.objects.create(
            username="testuser",
            email="test@example.com",
            password_hash="hash",
        )

        serializer = UserWithSourceAndReadOnly.from_model(user)

        assert serializer.id == user.id
        assert serializer.username == "testuser"
        assert serializer.mail == "test@example.com"

        # Verify dump excludes write_only but includes read_only
        result = serializer.dump()
        assert result["mail"] == "test@example.com"

    @pytest.mark.django_db
    def test_from_model_with_multiple_source_fields(self):
        """Test from_model with multiple fields using source."""

        class ArticleRenamedFields(Serializer):
            """Serializer with multiple renamed fields."""

            id: int
            headline: str = field(source="title")  # title -> headline
            body: str = field(source="content")  # content -> body
            writer: str = field(source="author")  # author -> writer

        article = Article.objects.create(
            title="Original Title",
            content="Original Content",
            author="John Doe",
        )

        serializer = ArticleRenamedFields.from_model(article)

        assert serializer.id == article.id
        assert serializer.headline == "Original Title"
        assert serializer.body == "Original Content"
        assert serializer.writer == "John Doe"

    @pytest.mark.django_db
    def test_from_model_with_nested_source_path(self):
        """Test from_model with dot-notation source for nested access."""

        class PostWithAuthorName(Serializer):
            """Serializer that flattens author.name to author_name."""

            id: int
            title: str
            author_name: str = field(source="author.name")  # Nested path

        author = Author.objects.create(
            name="Nested Author",
            email="nested@example.com",
        )
        post = BlogPost.objects.create(
            title="Test Post",
            content="Content",
            author=author,
        )
        post = BlogPost.objects.select_related("author").get(id=post.id)

        serializer = PostWithAuthorName.from_model(post)

        assert serializer.id == post.id
        assert serializer.title == "Test Post"
        assert serializer.author_name == "Nested Author"

    @pytest.mark.django_db
    def test_from_model_source_with_optional_field(self):
        """Test from_model with source on optional field."""

        class AuthorWithOptionalRename(Serializer):
            """Serializer with optional field using source."""

            id: int
            name: str
            biography: str | None = field(source="bio", default=None)

        # Author with bio
        author_with_bio = Author.objects.create(
            name="Author With Bio",
            email="bio@example.com",
            bio="A detailed biography",
        )
        serializer = AuthorWithOptionalRename.from_model(author_with_bio)
        assert serializer.biography == "A detailed biography"

        # Author without bio (empty string)
        author_no_bio = Author.objects.create(
            name="Author Without Bio",
            email="nobio@example.com",
            bio="",
        )
        serializer = AuthorWithOptionalRename.from_model(author_no_bio)
        assert serializer.biography == ""

    @pytest.mark.django_db
    def test_from_model_source_field_not_in_output_when_write_only(self):
        """Test that source fields with write_only are excluded from dump."""

        class UserWithWriteOnlySource(Serializer):
            """Serializer with write_only source field."""

            id: int
            username: str
            secret_hash: str = field(source="password_hash", write_only=True)

        user = User.objects.create(
            username="testuser",
            email="test@example.com",
            password_hash="supersecret",
        )

        serializer = UserWithWriteOnlySource.from_model(user)

        # Field should be populated
        assert serializer.secret_hash == "supersecret"

        # But excluded from dump
        result = serializer.dump()
        assert "secret_hash" not in result

    @pytest.mark.django_db
    def test_from_model_source_preserves_field_type(self):
        """Test that source mapping preserves correct field type."""

        class UserWithBooleanSource(Serializer):
            """Serializer that renames boolean field."""

            id: int
            username: str
            activated: bool = field(source="is_active")

        user = User.objects.create(
            username="testuser",
            email="test@example.com",
            password_hash="hash",
            is_active=True,
        )

        serializer = UserWithBooleanSource.from_model(user)

        assert serializer.activated is True
        assert isinstance(serializer.activated, bool)
