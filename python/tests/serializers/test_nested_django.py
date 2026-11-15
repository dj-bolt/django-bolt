"""
Comprehensive tests for nested serializers with Django models.

Tests cover:
- Single nested relationships (ForeignKey with and without select_related)
- Many-to-many nested relationships (with and without prefetch_related)
- Mixed nested structures (nested within nested)
- Query optimization (different query strategies)
- Error handling and validation
"""

from __future__ import annotations

from typing import Annotated

import msgspec
import pytest
from msgspec import Meta

from django_bolt.exceptions import RequestValidationError, SerializationError
from django_bolt.serializers import Serializer, field, field_validator
from tests.test_models import Author, BlogPost, Comment, Tag


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
    """Serializer for BlogPost with nested author and tags."""

    id: int
    title: str
    content: str
    # Author relationship - full object required
    author: AuthorSerializer
    # Tags relationship - full objects required
    tags: list[TagSerializer]
    published: bool = False


class BlogPostDetailedSerializer(Serializer):
    """Serializer for BlogPost with nested comments."""

    id: int
    title: str
    content: str
    author: AuthorSerializer
    tags: list[TagSerializer]
    # Comments nested with their own nested authors
    comments: list[CommentSerializer]
    published: bool = False


class TestSingleNestedForeignKey:
    """Test nested ForeignKey relationships."""

    @pytest.mark.django_db
    def test_from_model_without_select_related(self):
        """Test that unloaded nested FK raises a clear serialization error."""
        author = Author.objects.create(name="Alice Smith", email="alice@example.com", bio="Author bio")
        post = BlogPost.objects.create(title="Test Post", content="Content", author=author)

        # Fetch without select_related
        post = BlogPost.objects.get(id=post.id)

        with pytest.raises(SerializationError) as exc_info:
            BlogPostSerializer.from_model(post)

        error_message = str(exc_info.value)
        assert "author" in error_message
        assert "select_related('author')" in error_message

    @pytest.mark.django_db
    def test_from_model_with_select_related(self):
        """Test that selected FK returns full author object."""
        author = Author.objects.create(name="Bob Jones", email="bob@example.com", bio="Bio")
        post = BlogPost.objects.create(title="Test Post", content="Content", author=author)

        # Fetch with all required relations loaded for BlogPostSerializer
        post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post.id)

        # Convert to serializer
        serializer = BlogPostSerializer.from_model(post)

        assert isinstance(serializer.author, AuthorSerializer)
        assert serializer.author.name == "Bob Jones"
        assert serializer.author.email == "bob@example.com"

    @pytest.mark.django_db
    def test_create_with_nested_author_dict(self):
        """Test creating post with nested author dict."""
        author_data = {"id": 1, "name": "Charlie", "email": "charlie@example.com"}

        serializer = BlogPostSerializer(
            id=1,
            title="New Post",
            content="Content",
            author=author_data,
            tags=[],
            published=False,
        )

        # Check if author was validated
        if isinstance(serializer.author, AuthorSerializer):
            assert serializer.author.name == "Charlie"
        else:
            # Might stay as dict
            assert serializer.author["name"] == "Charlie"

    @pytest.mark.django_db
    def test_create_with_nested_author_id(self):
        """Test that passing just an ID raises a validation error."""

        # Passing just an ID should now raise an error
        with pytest.raises(RequestValidationError) as exc_info:
            BlogPostSerializer(
                id=1,
                title="New Post",
                content="Content",
                author=123,  # Just ID - not allowed
                tags=[],
            )

        # Error message should be helpful
        assert "author" in str(exc_info.value).lower()
        assert "int" in str(exc_info.value).lower() or "id" in str(exc_info.value).lower()

    @pytest.mark.django_db
    def test_create_with_serializer_author(self):
        """Test creating post with AuthorSerializer instance."""
        author = AuthorSerializer(id=1, name="David", email="david@example.com")

        serializer = BlogPostSerializer(
            id=1,
            title="New Post",
            content="Content",
            author=author,
            tags=[],
        )

        assert isinstance(serializer.author, AuthorSerializer)
        assert serializer.author.name == "David"


class TestNestedManyToMany:
    """Test nested many-to-many relationships."""

    @pytest.mark.django_db
    def test_from_model_without_prefetch_related(self):
        """Test that unloaded nested M2M raises a clear serialization error."""
        author = Author.objects.create(name="Eve", email="eve@example.com")
        tag1 = Tag.objects.create(name="python", description="Python tag")
        tag2 = Tag.objects.create(name="django", description="Django tag")

        post = BlogPost.objects.create(title="Post", content="Content", author=author)
        post.tags.add(tag1, tag2)

        # Fetch without prefetch_related
        post = BlogPost.objects.select_related("author").get(id=post.id)

        with pytest.raises(SerializationError) as exc_info:
            BlogPostSerializer.from_model(post)

        error_message = str(exc_info.value)
        assert "tags" in error_message
        assert "prefetch_related('tags')" in error_message

    @pytest.mark.django_db
    def test_from_model_with_prefetch_related(self):
        """Test that prefetched M2M returns full tag objects."""
        author = Author.objects.create(name="Frank", email="frank@example.com")
        tag1 = Tag.objects.create(name="python")
        tag2 = Tag.objects.create(name="django")

        post = BlogPost.objects.create(title="Post", content="Content", author=author)
        post.tags.add(tag1, tag2)

        # Fetch WITH prefetch_related
        post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post.id)

        serializer = BlogPostSerializer.from_model(post)

        # Check tags
        assert isinstance(serializer.tags, list)
        assert len(serializer.tags) == 2

        for tag in serializer.tags:
            assert isinstance(tag, TagSerializer)

    @pytest.mark.django_db
    def test_create_with_tag_dicts(self):
        """Test creating post with nested tag dicts."""
        serializer = BlogPostSerializer(
            id=1,
            title="Post",
            content="Content",
            author={"id": 1, "name": "Test", "email": "test@example.com", "bio": ""},
            tags=[
                {"id": 1, "name": "python"},
                {"id": 2, "name": "django"},
            ],
        )

        assert isinstance(serializer.tags, list)
        assert len(serializer.tags) == 2

    @pytest.mark.django_db
    def test_create_with_tag_ids(self):
        """Test that passing tag IDs raises validation error."""

        # Passing tag IDs should now raise an error
        with pytest.raises(RequestValidationError):
            BlogPostSerializer(
                id=1,
                title="Post",
                content="Content",
                author={"id": 1, "name": "Test", "email": "test@example.com", "bio": ""},
                tags=[1, 2, 3],  # IDs not allowed - need full objects
            )

    @pytest.mark.django_db
    def test_create_with_mixed_tags(self):
        """Test that mixing tag IDs and dicts raises validation error."""

        # Mixed IDs and dicts should now raise an error
        with pytest.raises(RequestValidationError):
            BlogPostSerializer(
                id=1,
                title="Post",
                content="Content",
                author={"id": 1, "name": "Test", "email": "test@example.com", "bio": ""},
                tags=[
                    1,  # ID - not allowed
                    {"id": 2, "name": "django"},  # Dict
                    3,  # ID - not allowed
                ],
            )


class TestNestedWithinNested:
    """Test deeply nested structures (nested within nested)."""

    @pytest.mark.django_db
    def test_from_model_with_full_nesting(self):
        """Test fully nested structure with comments and authors."""
        author = Author.objects.create(name="Grace", email="grace@example.com")
        post = BlogPost.objects.create(title="Post", content="Content", author=author)

        commenter = Author.objects.create(name="Henry", email="henry@example.com")
        Comment.objects.create(post=post, author=commenter, text="Great post!")

        # Fetch with full prefetch
        post = BlogPost.objects.select_related("author").prefetch_related("tags", "comments__author").get(id=post.id)

        serializer = BlogPostDetailedSerializer.from_model(post)

        # Check post author
        if isinstance(serializer.author, AuthorSerializer):
            assert serializer.author.name == "Grace"

        # Check comments
        assert isinstance(serializer.comments, list)
        if serializer.comments:
            first_comment = serializer.comments[0]
            if isinstance(first_comment, CommentSerializer):
                assert first_comment.text == "Great post!"
                # Check nested author in comment
                if isinstance(first_comment.author, AuthorSerializer):
                    assert first_comment.author.name == "Henry"

    @pytest.mark.django_db
    def test_create_deeply_nested(self):
        """Test creating with deeply nested structures."""
        serializer = BlogPostDetailedSerializer(
            id=1,
            title="Post",
            content="Content",
            author={"id": 1, "name": "Grace", "email": "grace@example.com"},
            tags=[
                {"id": 1, "name": "python"},
                {"id": 2, "name": "django"},
            ],
            comments=[
                {
                    "id": 1,
                    "text": "Comment 1",
                    "author": {"id": 2, "name": "Henry", "email": "henry@example.com"},
                },
                {
                    "id": 2,
                    "text": "Comment 2",
                    "author": {"id": 3, "name": "Jane", "email": "jane@example.com"},
                },
            ],
        )

        # Verify structure
        assert isinstance(serializer.title, str)
        assert isinstance(serializer.author, (AuthorSerializer, dict))
        assert isinstance(serializer.tags, list)
        assert isinstance(serializer.comments, list)


class TestQueryOptimization:
    """Test that the serializer works with different query patterns."""

    @pytest.mark.django_db
    def test_list_without_prefetch(self):
        """Test listing posts without required preloads raises serialization error."""
        author = Author.objects.create(name="Iris", email="iris@example.com")
        tag = Tag.objects.create(name="test")

        post_ids = []
        for i in range(3):
            post = BlogPost.objects.create(title=f"Post {i}", content="Content", author=author)
            post.tags.add(tag)
            post_ids.append(post.id)

        # Fetch only the posts we created
        all_posts = BlogPost.objects.filter(id__in=post_ids)
        with pytest.raises(SerializationError):
            [BlogPostSerializer.from_model(p) for p in all_posts]

    @pytest.mark.django_db
    def test_list_with_select_related(self):
        """Test that select_related alone is insufficient for required nested many relations."""
        author = Author.objects.create(name="Jack", email="jack@example.com")

        post_ids = []
        for i in range(2):
            post = BlogPost.objects.create(title=f"Post {i}", content="Content", author=author)
            post_ids.append(post.id)

        # Fetch only the posts we created with select_related
        all_posts = BlogPost.objects.filter(id__in=post_ids).select_related("author")
        with pytest.raises(SerializationError):
            [BlogPostSerializer.from_model(p) for p in all_posts]

    @pytest.mark.django_db
    def test_list_with_prefetch_related(self):
        """Test listing posts with prefetch_related."""
        author = Author.objects.create(name="Kate", email="kate@example.com")
        tag1 = Tag.objects.create(name="tag1")
        tag2 = Tag.objects.create(name="tag2")

        post_ids = []
        for i in range(2):
            post = BlogPost.objects.create(title=f"Post {i}", content="Content", author=author)
            post.tags.add(tag1, tag2)
            post_ids.append(post.id)

        # Fetch only the posts we created with prefetch_related
        all_posts = BlogPost.objects.filter(id__in=post_ids).select_related("author").prefetch_related("tags")
        serializers = [BlogPostSerializer.from_model(p) for p in all_posts]

        assert len(serializers) == 2
        for serializer in serializers:
            assert isinstance(serializer.author, AuthorSerializer)
            assert serializer.author.name == "Kate"
            assert len(serializer.tags) == 2
            assert all(isinstance(tag, TagSerializer) for tag in serializer.tags)

    @pytest.mark.django_db
    def test_list_with_full_prefetch(self):
        """Test listing with all relationships prefetched."""
        author = Author.objects.create(name="Liam", email="liam@example.com")
        tag = Tag.objects.create(name="featured")

        post = BlogPost.objects.create(title="Featured Post", content="Content", author=author)
        post.tags.add(tag)

        commenter = Author.objects.create(name="Mia", email="mia@example.com")
        Comment.objects.create(post=post, author=commenter, text="Nice!")

        # Fetch only the post we created with complete prefetch
        all_posts = (
            BlogPost.objects.filter(id=post.id).select_related("author").prefetch_related("tags", "comments__author")
        )
        serializers = [BlogPostDetailedSerializer.from_model(p) for p in all_posts]

        assert len(serializers) == 1
        serializer = serializers[0]

        assert isinstance(serializer.author, AuthorSerializer)
        assert serializer.author.name == "Liam"
        assert len(serializer.tags) == 1
        assert len(serializer.comments) == 1


class TestValidationErrors:
    """Test error handling in nested relationships."""

    def test_invalid_nested_id_type(self):
        """Test that string ID is rejected (strict type validation)."""

        # String IDs should be rejected - only int or AuthorSerializer allowed
        with pytest.raises(RequestValidationError) as exc_info:
            BlogPostSerializer(
                id=1,
                title="Post",
                content="Content",
                author="123",  # String - should be rejected
                tags=[],
            )

        # Verify the error is about the author field
        error_msg = str(exc_info.value)
        assert "author" in error_msg.lower()

    def test_invalid_nested_many_type(self):
        """Test that non-list in many field raises validation error."""

        # Passing a string for a list field should raise ValidationError
        with pytest.raises(RequestValidationError) as exc_info:
            BlogPostSerializer(
                id=1,
                title="Post",
                content="Content",
                author={"id": 1, "name": "Test", "email": "test@example.com"},
                tags="not_a_list",  # Should be list - will fail validation
            )

        # Verify the error is about the tags field
        error_msg = str(exc_info.value)
        assert "tags" in error_msg.lower() or "list" in error_msg.lower()

    def test_nested_within_nested_validation(self):
        """Test that nested validation works in deeply nested structures."""

        # Create a serializer with invalid nested comment (missing required email field)
        with pytest.raises(RequestValidationError) as exc_info:
            BlogPostDetailedSerializer(
                id=1,
                title="Post",
                content="Content",
                author={"id": 1, "name": "Test", "email": "test@example.com"},
                tags=[],
                comments=[
                    {
                        "id": 1,
                        "text": "Comment",
                        "author": {"id": 1, "name": "Author"},
                        # Missing email field but it's required
                    }
                ],
            )

        # Verify the error mentions the missing email field
        error_msg = str(exc_info.value)
        assert "email" in error_msg.lower() or "required" in error_msg.lower()


class TestNestedFieldValidation:
    """Test field validation in nested serializers (email, name, etc)."""

    def test_nested_author_with_valid_email(self):
        """Test creating post with valid author email."""
        # Create directly with AuthorSerializer to test field validation
        author = AuthorSerializer(id=1, name="Alice", email="alice@example.com")
        serializer = BlogPostSerializer(
            id=1,
            title="Post",
            content="Content",
            author=author,
            tags=[],
        )

        # Should create successfully
        assert isinstance(serializer.author, AuthorSerializer)
        assert serializer.author.email == "alice@example.com"

    def test_nested_author_with_invalid_email_no_at_sign(self):
        """Test that email without @ sign raises validation error (via msgspec.convert)."""
        # Meta constraints are enforced during deserialization - msgspec.ValidationError
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "Bob", "email": "bobexample.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "email" in error_msg.lower()

    def test_nested_author_with_invalid_email_no_domain(self):
        """Test that email without domain extension raises validation error (via msgspec.convert)."""
        # Meta constraints are enforced during deserialization - msgspec.ValidationError
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "Charlie", "email": "charlie@example"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "email" in error_msg.lower()

    def test_nested_author_with_invalid_name_too_short(self):
        """Test that name that's too short raises validation error (via msgspec.convert)."""

        # Meta constraints are enforced during deserialization - msgspec.ValidationError
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "A", "email": "a@example.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "name" in error_msg.lower()

    def test_nested_author_with_empty_name(self):
        """Test that empty name raises validation error (via msgspec.convert)."""

        # Meta constraints are enforced during deserialization - msgspec.ValidationError
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "", "email": "test@example.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "name" in error_msg.lower()

    def test_nested_author_with_whitespace_only_name(self):
        """Test that whitespace-only name is trimmed."""
        author = AuthorSerializer(
            id=1,
            name="  David  ",  # Whitespace around
            email="david@example.com",
        )

        assert author.name == "David"

    def test_nested_author_email_case_normalization(self):
        """Test that email is converted to lowercase."""
        author = AuthorSerializer(
            id=1,
            name="Eve",
            email="Eve@EXAMPLE.COM",  # Mixed case
        )

        assert author.email == "eve@example.com"

    def test_multiple_valid_authors(self):
        """Test multiple valid authors in nested structure."""
        author1 = AuthorSerializer(id=1, name="Frank", email="frank@example.com")
        author2 = AuthorSerializer(id=2, name="Grace", email="grace@example.com")

        # Both should validate successfully
        assert author1.name == "Frank"
        assert author2.name == "Grace"

    def test_nested_author_missing_email_field(self):
        """Test that missing required email field raises error."""

        with pytest.raises(TypeError):  # Missing required parameter
            AuthorSerializer(
                id=1,
                name="Henry",
                # Missing email field
            )

    def test_deeply_nested_validation_error_propagation(self):
        """Test that validation errors in deeply nested structures propagate (via msgspec.convert)."""

        # Meta constraints are enforced during deserialization - msgspec.ValidationError
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 2, "name": "J", "email": "j@example.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        # Should mention the field that failed validation
        assert "name" in error_msg.lower()

    def test_validation_with_valid_author_and_tags(self):
        """Test successful validation with valid author and tags."""
        author = AuthorSerializer(id=1, name="Jack", email="jack@example.com", bio="Author bio")

        # All validation should pass
        assert author.name == "Jack"
        assert author.email == "jack@example.com"
        assert author.bio == "Author bio"

    @pytest.mark.django_db
    def test_from_model_with_valid_author(self):
        """Test that from_model extracts author data correctly."""
        # Create author with valid data
        author = Author.objects.create(name="Karen", email="karen@example.com", bio="Test author")
        post = BlogPost.objects.create(title="Post", content="Content", author=author)

        # Fetch and convert
        post = BlogPost.objects.select_related("author").prefetch_related("tags").get(id=post.id)
        serializer = BlogPostSerializer.from_model(post)

        assert isinstance(serializer.author, AuthorSerializer)
        assert serializer.author.name == "Karen"

    def test_valid_author_in_nested_comment(self):
        """Test validation of valid author in nested comment."""
        author = AuthorSerializer(id=1, name="Mike", email="mike@example.com")

        comment = CommentSerializer(id=1, text="Comment", author=author)

        assert comment.author.name == "Mike"

    def test_validation_error_for_short_name(self):
        """Test that validation errors are properly raised for short names (via msgspec.convert)."""

        # Meta constraints are enforced during deserialization - msgspec.ValidationError
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "X", "email": "x@example.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        # Error message should indicate which field failed
        assert "name" in error_msg.lower()


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    @pytest.mark.django_db
    def test_empty_related_lists(self):
        """Test post with no tags or comments."""
        author = Author.objects.create(name="Noah", email="noah@example.com")
        post = BlogPost.objects.create(title="Post", content="Content", author=author)

        post = BlogPost.objects.select_related("author").prefetch_related("tags", "comments").get(id=post.id)

        serializer = BlogPostDetailedSerializer.from_model(post)

        assert serializer.tags == []
        assert serializer.comments == []

    def test_annotated_nested_serializer_is_a_nested_field(self):
        """Annotated[...] around a serializer type keeps the field a nested serializer."""

        class Owner(Serializer):
            name: str
            password: str = field(write_only=True, default="")

        class Car(Serializer):
            owner: Annotated[Owner, Meta(description="The owner")]
            drivers: list[Annotated[Owner, Meta(description="A driver")]] = []

        car = Car(owner={"name": "a", "password": "p"}, drivers=[{"name": "b", "password": "p"}])

        assert isinstance(car.owner, Owner)
        assert isinstance(car.drivers[0], Owner)
        assert car.dump() == {"owner": {"name": "a"}, "drivers": [{"name": "b"}]}

    def test_optional_nested_field(self):
        """Test optional nested field with None."""

        class OptionalAuthorSerializer(Serializer):
            title: str
            author: AuthorSerializer | None = None

        # Should accept None
        serializer = OptionalAuthorSerializer(title="Post", author=None)
        assert serializer.author is None

    @pytest.mark.django_db
    def test_nested_model_instance_vs_dict(self):
        """Test that both model instances and dicts work, but plain IDs don't."""

        # Test with dict - should work
        serializer1 = CommentSerializer(
            id=1,
            text="Comment",
            author={"id": 1, "name": "Author", "email": "author@example.com"},
        )
        assert isinstance(serializer1.author, (AuthorSerializer, dict))

        # Test with AuthorSerializer instance - should work
        author = AuthorSerializer(id=1, name="Author", email="author@example.com")
        serializer2 = CommentSerializer(
            id=1,
            text="Comment",
            author=author,
        )
        assert isinstance(serializer2.author, AuthorSerializer)

        # Test with plain ID - should fail
        with pytest.raises(RequestValidationError):
            CommentSerializer(
                id=1,
                text="Comment",
                author=1,  # Plain ID not allowed
            )


class TestAuthorSerializerValidation:
    """Test AuthorSerializer Meta constraints and field validators."""

    def test_meta_email_pattern_valid(self):
        """Test that valid email passes Meta pattern validation via msgspec.convert."""

        # Valid emails should pass
        author = msgspec.convert({"id": 1, "name": "Test User", "email": "test@example.com"}, type=AuthorSerializer)
        assert author.email == "test@example.com"
        assert author.name == "Test User"

    def test_meta_email_pattern_invalid_no_at(self):
        """Test that email without @ fails Meta pattern validation."""

        # Email without @ should fail - msgspec.ValidationError from Meta constraint
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "Test", "email": "testexample.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "email" in error_msg.lower()

    def test_meta_email_pattern_invalid_no_tld(self):
        """Test that email without TLD fails Meta pattern validation."""

        # Email without TLD (.com, .org, etc) should fail - msgspec.ValidationError
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "Test", "email": "test@example"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "email" in error_msg.lower()

    def test_meta_name_min_length_valid(self):
        """Test that name with 2+ characters passes Meta min_length validation."""

        # Exactly 2 characters should pass
        author = msgspec.convert({"id": 1, "name": "AB", "email": "test@example.com"}, type=AuthorSerializer)
        assert author.name == "AB"

        # More than 2 characters should pass
        author2 = msgspec.convert({"id": 2, "name": "Alice", "email": "alice@example.com"}, type=AuthorSerializer)
        assert author2.name == "Alice"

    def test_meta_name_min_length_invalid(self):
        """Test that name with less than 2 characters fails Meta min_length validation."""

        # Single character should fail - msgspec.ValidationError from Meta constraint
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 1, "name": "A", "email": "test@example.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "name" in error_msg.lower()

        # Empty string should fail - msgspec.ValidationError from Meta constraint
        with pytest.raises(msgspec.ValidationError) as exc_info:
            msgspec.convert({"id": 2, "name": "", "email": "test@example.com"}, type=AuthorSerializer)

        error_msg = str(exc_info.value)
        assert "name" in error_msg.lower()

    def test_field_validator_name_strip(self):
        """Test that @field_validator strips whitespace from name."""
        # Direct instantiation - field validators ALWAYS run
        author = AuthorSerializer(id=1, name="  Alice Smith  ", email="alice@example.com")
        assert author.name == "Alice Smith"

        # Via msgspec.convert - field validators run after Meta validation
        author2 = msgspec.convert({"id": 2, "name": "  Bob Jones  ", "email": "bob@example.com"}, type=AuthorSerializer)
        assert author2.name == "Bob Jones"

    def test_field_validator_email_lowercase(self):
        """Test that @field_validator converts email to lowercase."""
        # Direct instantiation - field validators ALWAYS run
        author = AuthorSerializer(id=1, name="Test User", email="TEST@EXAMPLE.COM")
        assert author.email == "test@example.com"

        # Via msgspec.convert - field validators run after Meta validation
        author2 = msgspec.convert({"id": 2, "name": "Another User", "email": "MiXeD@CaSe.COM"}, type=AuthorSerializer)
        assert author2.email == "mixed@case.com"

    def test_combined_meta_and_field_validators(self):
        """Test that both Meta validation and field validators work together."""

        # Valid data with transformations
        author = msgspec.convert({"id": 1, "name": "  Valid Name  ", "email": "VALID@EMAIL.COM"}, type=AuthorSerializer)
        # Field validators should have transformed the data
        assert author.name == "Valid Name"  # Stripped
        assert author.email == "valid@email.com"  # Lowercased

    def test_meta_validation_with_field_validator_interaction(self):
        """Test that Meta validation happens before field validators on deserialization."""

        # Even though field validator would strip whitespace,
        # Meta validation sees the original value first during deserialization
        # Note: This test demonstrates the order of operations
        author = msgspec.convert({"id": 1, "name": "  AB  ", "email": "test@example.com"}, type=AuthorSerializer)
        # Meta validation passed (min_length=2 on "  AB  " which is 6 chars)
        # Then field validator stripped it to "AB"
        assert author.name == "AB"
