"""Simple tests for type-driven nested serializer support."""

from __future__ import annotations

from django_bolt.serializers import Serializer


def test_nested_annotation():
    """Test nested serializers inferred from plain serializer types."""

    class AuthorSerializer(Serializer):
        id: int
        username: str

    class BookDetailSerializer(Serializer):
        title: str
        author: AuthorSerializer

    author = AuthorSerializer(id=1, username="alice")
    book = BookDetailSerializer(title="Test Book", author=author)

    assert book.title == "Test Book"
    assert isinstance(book.author, AuthorSerializer)
    assert book.author.username == "alice"


def test_nested_with_dict():
    """Test that nested fields accept dict input."""

    class AuthorSerializer(Serializer):
        id: int
        username: str

    class BookSerializer(Serializer):
        title: str
        author: AuthorSerializer

    book = BookSerializer(title="Test", author={"id": 123, "username": "bob"})

    assert isinstance(book.author, AuthorSerializer)
    assert book.author.id == 123
    assert book.author.username == "bob"


def test_simple_id_reference():
    """Test using plain int for ID-only fields."""

    class BookListSerializer(Serializer):
        title: str
        author_id: int

    book = BookListSerializer(title="Test", author_id=42)

    assert book.author_id == 42


def test_nested_with_serializer_instance():
    """Test passing a Serializer instance to a nested field."""

    class AuthorSerializer(Serializer):
        id: int
        username: str

    class BookSerializer(Serializer):
        title: str
        author: AuthorSerializer

    author = AuthorSerializer(id=1, username="alice")
    book = BookSerializer(title="Test", author=author)

    assert isinstance(book.author, AuthorSerializer)
    assert book.author.username == "alice"


def test_nested_many_with_objects():
    """Test that list[Serializer] fields accept nested objects."""

    class TagSerializer(Serializer):
        id: int
        name: str

    class BookSerializer(Serializer):
        title: str
        tags: list[TagSerializer]

    book = BookSerializer(
        title="Test",
        tags=[
            {"id": 1, "name": "python"},
            {"id": 2, "name": "django"},
        ],
    )

    assert len(book.tags) == 2
    assert all(isinstance(tag, TagSerializer) for tag in book.tags)
    assert book.tags[0].name == "python"


def test_nested_many_accepts_empty_list():
    """Test that nested list fields accept empty lists."""

    class TagSerializer(Serializer):
        id: int
        name: str

    class BookSerializer(Serializer):
        title: str
        tags: list[TagSerializer]

    book = BookSerializer(title="Test", tags=[])

    assert book.tags == []


def test_nested_many_has_no_default_limit():
    """Nested lists have no built-in item cap."""

    class TagSerializer(Serializer):
        id: int
        name: str

    class BookSerializer(Serializer):
        title: str
        tags: list[TagSerializer]

    book = BookSerializer(title="Test", tags=[{"id": i, "name": f"t{i}"} for i in range(1001)])
    assert len(book.tags) == 1001


def test_list_of_ids_without_nested():
    """Test using plain list[int] for ID-only fields."""

    class BookListSerializer(Serializer):
        title: str
        tag_ids: list[int]

    book = BookListSerializer(title="Test", tag_ids=[1, 2, 3])

    assert book.tag_ids == [1, 2, 3]
