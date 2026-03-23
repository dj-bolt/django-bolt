"""Tests for Django choices field support with Literal types."""

from __future__ import annotations

from typing import Literal, get_args, get_origin

import msgspec
import pytest
from django.db import connection, models

from django_bolt.exceptions import RequestValidationError
from django_bolt.serializers import Serializer, create_serializer
from django_bolt.serializers.fields import get_msgspec_type_for_django_field


class StatusEnum(models.TextChoices):
    """TextChoices enum used to test in-memory Django choice serialization."""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class PriorityEnum(models.IntegerChoices):
    """IntegerChoices enum used to test in-memory Django choice serialization."""

    LOW = 1
    HIGH = 2


class EnumChoiceArticle(models.Model):
    """Temporary model for testing enum-backed Django choices."""

    title = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=StatusEnum, default=StatusEnum.DRAFT)
    priority = models.IntegerField(choices=PriorityEnum, default=PriorityEnum.LOW)

    class Meta:
        app_label = "django_bolt"
        db_table = "django_bolt_enum_choice_article"


class EnumChoiceArticleSerializer(Serializer):
    """Serializer for enum-backed Django choice model tests."""

    id: int
    title: str
    status: str
    priority: int


@pytest.fixture
def enum_choice_article_table():
    """Create the temporary enum choice model table for this test."""
    table_name = EnumChoiceArticle._meta.db_table

    with connection.schema_editor() as schema_editor:
        if table_name not in connection.introspection.table_names():
            schema_editor.create_model(EnumChoiceArticle)

    try:
        yield EnumChoiceArticle
    finally:
        with connection.schema_editor() as schema_editor:
            if table_name in connection.introspection.table_names():
                schema_editor.delete_model(EnumChoiceArticle)


class TestChoicesFieldDetection:
    """Test detection and handling of Django choices fields."""

    def test_charfield_with_choices_generates_literal(self):
        """Test that CharField with choices generates Literal type."""

        # Create a mock CharField with choices
        field = models.CharField(
            max_length=20,
            choices=[
                ("draft", "Draft"),
                ("published", "Published"),
                ("archived", "Archived"),
            ],
        )

        # Get the msgspec type
        field_type = get_msgspec_type_for_django_field(field)

        # Should be a Literal type
        assert get_origin(field_type) is Literal

        # Should contain all choice values
        choice_values = get_args(field_type)
        assert "draft" in choice_values
        assert "published" in choice_values
        assert "archived" in choice_values
        assert len(choice_values) == 3

    def test_charfield_without_choices_remains_str(self):
        """Test that CharField without choices remains str type."""

        field = models.CharField(max_length=100)
        field_type = get_msgspec_type_for_django_field(field)

        # Should be Annotated[str, Meta(max_length=100)]
        # The origin will be str after unwrapping Annotated

        # If it's Annotated, get the first arg (the actual type)
        actual_type = get_args(field_type)[0] if hasattr(field_type, "__origin__") else field_type

        assert actual_type is str

    def test_integerfield_with_choices_generates_literal(self):
        """Test that IntegerField with choices generates Literal type."""

        field = models.IntegerField(
            choices=[
                (1, "Low"),
                (2, "Medium"),
                (3, "High"),
            ],
        )

        field_type = get_msgspec_type_for_django_field(field)

        # Should be a Literal type
        assert get_origin(field_type) is Literal

        # Should contain all choice values
        choice_values = get_args(field_type)
        assert 1 in choice_values
        assert 2 in choice_values
        assert 3 in choice_values
        assert len(choice_values) == 3

    def test_integerfield_without_choices_remains_int(self):
        """Test that IntegerField without choices remains int type."""

        field = models.IntegerField()
        field_type = get_msgspec_type_for_django_field(field)

        # IntegerField may have range validators, so check the unwrapped type
        actual_type = get_args(field_type)[0] if hasattr(field_type, "__origin__") else field_type

        assert actual_type is int


class TestChoicesSerializerCreation:
    """Test creating serializers with choices fields."""

    def test_serializer_with_literal_choices(self):
        """Test creating a serializer with Literal type for choices."""

        class ArticleSerializer(Serializer):
            title: str
            status: Literal["draft", "published", "archived"]

        # Valid status value
        article = ArticleSerializer(title="Test", status="draft")
        assert article.status == "draft"

        # Another valid value
        article2 = ArticleSerializer(title="Test 2", status="published")
        assert article2.status == "published"

    def test_serializer_rejects_invalid_choice(self):
        """Test that serializer rejects invalid choice values."""

        class ArticleSerializer(Serializer):
            title: str
            status: Literal["draft", "published"]

        # Invalid status should raise ValidationError
        with pytest.raises(RequestValidationError):
            ArticleSerializer(title="Test", status="invalid")

    def test_serializer_with_integer_choices(self):
        """Test serializer with integer Literal choices."""

        class PrioritySerializer(Serializer):
            name: str
            level: Literal[1, 2, 3]

        # Valid integer choice
        priority = PrioritySerializer(name="High", level=3)
        assert priority.level == 3

        # Invalid choice should fail
        with pytest.raises(RequestValidationError):
            PrioritySerializer(name="Invalid", level=99)


class TestChoicesIntegrationWithArticleModel:
    """Test choices field support with the actual Article model."""

    @pytest.mark.django_db
    def test_article_status_field_as_literal(self):
        """Test that Article.status field is detected as Literal type."""
        from tests.test_models import Article  # noqa: PLC0415

        # Get the status field from Article model
        status_field = Article._meta.get_field("status")

        # Get the msgspec type
        field_type = get_msgspec_type_for_django_field(status_field)

        # Should be a Literal type
        assert get_origin(field_type) is Literal

        # Should contain "draft" and "published"
        choice_values = get_args(field_type)
        assert "draft" in choice_values
        assert "published" in choice_values

    @pytest.mark.django_db
    def test_article_serializer_with_status_choices(self):
        """Test creating Article serializer with status as Literal."""

        class ArticleSerializer(Serializer):
            title: str
            content: str
            status: Literal["draft", "published"]
            author: str

        # Valid article with draft status
        article_data = ArticleSerializer(
            title="Test Article",
            content="Content here",
            status="draft",
            author="John Doe",
        )
        assert article_data.status == "draft"

        # Valid article with published status
        article_data2 = ArticleSerializer(
            title="Published Article",
            content="Published content",
            status="published",
            author="Jane Doe",
        )
        assert article_data2.status == "published"

    @pytest.mark.django_db
    def test_create_serializer_detects_choices(self):
        """Test that create_serializer helper detects choices fields."""
        from tests.test_models import Article  # noqa: PLC0415

        # Create serializer using helper
        ArticleSerializer = create_serializer(
            Article,
            fields=["id", "title", "status", "author"],
        )

        # Check the status field annotation
        annotations = ArticleSerializer.__annotations__
        assert "status" in annotations

        # The status field should be a Literal type
        status_type = annotations["status"]
        assert get_origin(status_type) is Literal

        # Should have "draft" and "published" choices
        choice_values = get_args(status_type)
        assert "draft" in choice_values
        assert "published" in choice_values


class TestChoicesValidation:
    """Test that choices are properly validated."""

    def test_valid_choice_accepted(self):
        """Test that valid choice values are accepted."""

        class StatusSerializer(Serializer):
            status: Literal["active", "inactive", "pending"]

        # All valid values should work
        s1 = StatusSerializer(status="active")
        assert s1.status == "active"

        s2 = StatusSerializer(status="inactive")
        assert s2.status == "inactive"

        s3 = StatusSerializer(status="pending")
        assert s3.status == "pending"

    def test_invalid_choice_rejected(self):
        """Test that invalid choice values are rejected."""

        class StatusSerializer(Serializer):
            status: Literal["active", "inactive"]

        # Invalid value should raise ValidationError
        with pytest.raises(RequestValidationError) as exc_info:
            StatusSerializer(status="deleted")

        # Error message should mention the invalid value
        assert "deleted" in str(exc_info.value) or "status" in str(exc_info.value)

    def test_empty_string_choice(self):
        """Test that empty string can be a valid choice."""

        class FieldWithEmptyChoice(Serializer):
            value: Literal["", "value1", "value2"]

        # Empty string should be valid
        obj = FieldWithEmptyChoice(value="")
        assert obj.value == ""

    def test_numeric_string_choices(self):
        """Test choices with numeric strings."""

        class NumericStringChoice(Serializer):
            code: Literal["0", "1", "2", "3"]

        obj = NumericStringChoice(code="2")
        assert obj.code == "2"

        # Actual number should not work (type mismatch)
        with pytest.raises(RequestValidationError):
            NumericStringChoice(code=2)  # int instead of str


class TestChoicesFromModelSerialization:
    """Test from_model()/afrom_model() with Django enum choice values."""

    @pytest.mark.django_db(transaction=True)
    def test_from_model_coerces_choice_members_from_created_instance(self, enum_choice_article_table):
        """create() should not require refresh_from_db() before serialization."""
        article = enum_choice_article_table.objects.create(
            title="Hello",
            status=StatusEnum.PUBLISHED,
            priority=PriorityEnum.HIGH,
        )

        assert isinstance(article.status, StatusEnum)
        assert isinstance(article.priority, PriorityEnum)

        serializer = EnumChoiceArticleSerializer.from_model(article)
        dumped = serializer.dump()

        assert serializer.status == StatusEnum.PUBLISHED.value
        assert serializer.priority == PriorityEnum.HIGH.value
        assert dumped == {
            "id": article.id,
            "title": "Hello",
            "status": StatusEnum.PUBLISHED.value,
            "priority": PriorityEnum.HIGH.value,
        }
        assert msgspec.json.decode(serializer.dump_json()) == dumped

    @pytest.mark.asyncio
    @pytest.mark.django_db(transaction=True)
    async def test_afrom_model_coerces_choice_members_from_acreated_instance(self, enum_choice_article_table):
        """acreate() should keep working without a refresh round-trip too."""
        article = await enum_choice_article_table.objects.acreate(
            title="Async Hello",
            status=StatusEnum.PUBLISHED,
            priority=PriorityEnum.HIGH,
        )

        assert isinstance(article.status, StatusEnum)
        assert isinstance(article.priority, PriorityEnum)

        serializer = await EnumChoiceArticleSerializer.afrom_model(article)
        dumped = serializer.dump()

        assert serializer.status == StatusEnum.PUBLISHED.value
        assert serializer.priority == PriorityEnum.HIGH.value
        assert dumped == {
            "id": article.id,
            "title": "Async Hello",
            "status": StatusEnum.PUBLISHED.value,
            "priority": PriorityEnum.HIGH.value,
        }
        assert msgspec.json.decode(serializer.dump_json()) == dumped
