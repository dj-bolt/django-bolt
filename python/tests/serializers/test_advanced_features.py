"""Tests for advanced Serializer features: field(), computed_field, dynamic field selection."""

from __future__ import annotations

import msgspec
import pytest

from django_bolt.serializers import (
    Serializer,
    SerializerView,
    computed_field,
    field,
)


class TestFieldFunction:
    """Test the field() function for field configuration."""

    def test_field_read_only(self):
        """Test read_only fields are excluded from dump output based on Meta."""

        class UserSerializer(Serializer):
            id: int
            name: str
            internal_id: str

            class Config:
                # Using Config.write_only for fields that should only be in input
                write_only = {"internal_id"}

        user = UserSerializer(id=1, name="John", internal_id="secret123")

        # write_only fields should not appear in dump
        result = user.dump()
        assert "id" in result
        assert "name" in result
        assert "internal_id" not in result

    def test_write_only_field_is_dropped_and_the_fields_after_it_stay(self):
        """A write-only field does not hide the fields that follow it, also in a subset."""

        class Account(Serializer):
            id: int
            password: str = field(write_only=True)
            token: str
            email: str

            class Config:
                write_only = {"token"}

        account = Account(id=1, password="p", token="t", email="a@example.com")
        public = {"id": 1, "email": "a@example.com"}

        assert account.dump() == public
        assert Account.dump_many([account]) == [public]
        assert Account.subset("id", "token", "email")(id=1, token="t", email="a@example.com").dump() == public

    def test_field_write_only_meta(self):
        """Test write_only fields via Meta class."""

        class UserCreateSerializer(Serializer):
            email: str
            password: str

            class Config:
                write_only = {"password"}

        user = UserCreateSerializer(email="test@example.com", password="secret123")
        result = user.dump()

        assert result["email"] == "test@example.com"
        assert "password" not in result

    def test_field_alias(self):
        """Test field alias for JSON output."""

        # Create a serializer with field config
        class UserSerializer(Serializer):
            first_name: str
            last_name: str

        user = UserSerializer(first_name="John", last_name="Doe")
        result = user.dump()

        assert result["first_name"] == "John"
        assert result["last_name"] == "Doe"


class TestComputedField:
    """Test the @computed_field decorator."""

    def test_basic_computed_field(self):
        """Test basic computed field."""

        class UserSerializer(Serializer):
            first_name: str
            last_name: str

            @computed_field
            def full_name(self) -> str:
                return f"{self.first_name} {self.last_name}"

        user = UserSerializer(first_name="John", last_name="Doe")
        result = user.dump()

        assert result["first_name"] == "John"
        assert result["last_name"] == "Doe"
        assert result["full_name"] == "John Doe"

    def test_computed_field_with_alias(self):
        """Test computed field with alias."""

        class UserSerializer(Serializer):
            first_name: str
            last_name: str

            @computed_field(alias="displayName")
            def display_name(self) -> str:
                return f"{self.first_name} {self.last_name}".upper()

        user = UserSerializer(first_name="John", last_name="Doe")
        result = user.dump()

        # The computed field should use the method name as key (alias affects OpenAPI only)
        assert "displayName" in result
        assert result["displayName"] == "JOHN DOE"

    def test_multiple_computed_fields(self):
        """Test multiple computed fields."""

        class ProductSerializer(Serializer):
            price: float
            quantity: int

            @computed_field
            def total(self) -> float:
                return self.price * self.quantity

            @computed_field
            def formatted_total(self) -> str:
                # Note: computed fields are methods, so call total() not self.total
                return f"${self.total():.2f}"

        product = ProductSerializer(price=19.99, quantity=3)
        result = product.dump()

        assert result["price"] == 19.99
        assert result["quantity"] == 3
        assert result["total"] == pytest.approx(59.97)
        assert result["formatted_total"] == "$59.97"

    def test_computed_field_exclude_none(self):
        """Test computed field with exclude_none."""

        class UserSerializer(Serializer):
            name: str
            email: str | None = None

            @computed_field
            def greeting(self) -> str | None:
                if self.email:
                    return f"Hello {self.name}!"
                return None

        user = UserSerializer(name="John", email=None)
        result = user.dump(exclude_none=True)

        assert "name" in result
        assert "email" not in result
        assert "greeting" not in result

        user2 = UserSerializer(name="Jane", email="jane@example.com")
        result2 = user2.dump(exclude_none=True)
        assert result2["greeting"] == "Hello Jane!"


class TestDynamicFieldSelection:
    """Test dynamic field selection with only(), exclude(), use()."""

    def test_only_basic(self):
        """Test basic only() field selection."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str
            created_at: str

        user = UserSerializer(id=1, name="John", email="john@example.com", created_at="2024-01-01")

        # Only include specific fields
        result = UserSerializer.only("id", "name").dump(user)

        assert result == {"id": 1, "name": "John"}
        assert "email" not in result
        assert "created_at" not in result

    def test_exclude_basic(self):
        """Test basic exclude() field selection."""

        class UserSerializer(Serializer):
            id: int
            name: str
            password: str
            secret_key: str

        user = UserSerializer(id=1, name="John", password="secret123", secret_key="key456")

        # Exclude sensitive fields
        result = UserSerializer.exclude("password", "secret_key").dump(user)

        assert result == {"id": 1, "name": "John"}
        assert "password" not in result
        assert "secret_key" not in result

    def test_use_field_set(self):
        """Test use() with predefined field sets."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str
            password: str
            created_at: str
            updated_at: str

            class Config:
                field_sets = {
                    "list": ["id", "name", "email"],
                    "detail": ["id", "name", "email", "created_at", "updated_at"],
                    "minimal": ["id", "name"],
                }

        user = UserSerializer(
            id=1,
            name="John",
            email="john@example.com",
            password="secret",
            created_at="2024-01-01",
            updated_at="2024-01-15",
        )

        # Use predefined field sets
        list_result = UserSerializer.use("list").dump(user)
        assert list_result == {"id": 1, "name": "John", "email": "john@example.com"}

        detail_result = UserSerializer.use("detail").dump(user)
        assert "created_at" in detail_result
        assert "updated_at" in detail_result
        assert "password" not in detail_result

        minimal_result = UserSerializer.use("minimal").dump(user)
        assert minimal_result == {"id": 1, "name": "John"}

    def test_use_invalid_field_set(self):
        """Test use() with invalid field set name."""

        class UserSerializer(Serializer):
            id: int
            name: str

            class Config:
                field_sets = {"list": ["id", "name"]}

        with pytest.raises(ValueError) as exc_info:
            UserSerializer.use("nonexistent")

        assert "nonexistent" in str(exc_info.value)
        assert "not found" in str(exc_info.value)

    def test_chained_field_selection(self):
        """Test chaining only() and exclude()."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str
            phone: str

        user = UserSerializer(id=1, name="John", email="john@example.com", phone="123-456-7890")

        # Chain only().exclude()
        view = UserSerializer.only("id", "name", "email").exclude("email")
        result = view.dump(user)

        assert result == {"id": 1, "name": "John"}

    def test_dump_many_with_field_selection(self):
        """Test dump_many with field selection."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str

        users = [
            UserSerializer(id=1, name="John", email="john@example.com"),
            UserSerializer(id=2, name="Jane", email="jane@example.com"),
        ]

        result = UserSerializer.only("id", "name").dump_many(users)

        assert len(result) == 2
        assert result[0] == {"id": 1, "name": "John"}
        assert result[1] == {"id": 2, "name": "Jane"}

    def test_field_selection_with_computed_fields(self):
        """Test field selection includes/excludes computed fields."""

        class UserSerializer(Serializer):
            first_name: str
            last_name: str

            @computed_field
            def full_name(self) -> str:
                return f"{self.first_name} {self.last_name}"

        user = UserSerializer(first_name="John", last_name="Doe")

        # Include computed field
        result = UserSerializer.only("first_name", "full_name").dump(user)
        assert result == {"first_name": "John", "full_name": "John Doe"}

        # Exclude computed field
        result2 = UserSerializer.exclude("full_name").dump(user)
        assert "full_name" not in result2
        assert result2 == {"first_name": "John", "last_name": "Doe"}


class TestDumpMethods:
    """Test dump methods with various options."""

    def test_dump_exclude_none(self):
        """Test dump with exclude_none option."""

        class UserSerializer(Serializer):
            name: str
            email: str | None = None
            phone: str | None = None

        user = UserSerializer(name="John", email="john@example.com", phone=None)

        # Without exclude_none
        result = user.dump()
        assert result == {"name": "John", "email": "john@example.com", "phone": None}

        # With exclude_none
        result_filtered = user.dump(exclude_none=True)
        assert result_filtered == {"name": "John", "email": "john@example.com"}

    def test_dump_exclude_defaults(self):
        """Test dump with exclude_defaults option."""

        class UserSerializer(Serializer):
            name: str
            role: str = "user"
            active: bool = True

        user = UserSerializer(name="John", role="user", active=True)

        # Without exclude_defaults
        result = user.dump()
        assert result == {"name": "John", "role": "user", "active": True}

        # With exclude_defaults - default values are excluded
        result_filtered = user.dump(exclude_defaults=True)
        assert result_filtered == {"name": "John"}

    @pytest.mark.parametrize(
        ("option", "expected_tag", "expected_post"),
        [
            (
                {"exclude_none": True},
                {"name": "a", "weight": 1},
                {"title": "untitled", "size": 1},
            ),
            (
                {"exclude_defaults": True},
                {"name": "a"},
                {"parent": None, "subtitle": None, "size": 1},
            ),
            (
                {"by_alias": True},
                {"name": "a", "note": None, "w": 1},
                {"note": None, "t": "untitled", "parent": None, "subtitle": None, "size": 1},
            ),
        ],
    )
    def test_dump_option_applies_to_nested_serializers_dump_many_and_views(self, option, expected_tag, expected_post):
        """Each path of dump() gives the same output for an option, at each level."""

        class Tag(Serializer):
            name: str
            note: str | None = None
            weight: int = field(alias="w", default=1)

        class Post(Serializer):
            note: str | None = None
            title: str = field(alias="t", default="untitled")
            parent: int | None
            tag: Tag
            tags: list[Tag]

            @computed_field
            def subtitle(self) -> str | None:
                return None

            @computed_field
            def size(self) -> int:
                return len(self.tags)

        tag = Tag(name="a")
        post = Post(parent=None, tag={"name": "a"}, tags=[{"name": "a"}])
        expected = {"tag": expected_tag, "tags": [expected_tag], **expected_post}

        assert tag.dump(**option) == expected_tag
        assert Tag.dump_many([tag], **option) == [expected_tag]
        assert post.dump(**option) == expected
        assert Post.dump_many((item for item in [post]), **option) == [expected]
        view = Post.exclude("unknown")
        assert view.dump(post, **option) == expected
        assert view.dump_many([post], **option) == [expected]

    def test_dump_json(self):
        """Test dump_json method."""

        class UserSerializer(Serializer):
            id: int
            name: str

        user = UserSerializer(id=1, name="John")
        json_bytes = user.dump_json()

        assert isinstance(json_bytes, bytes)
        assert b'"id":1' in json_bytes or b'"id": 1' in json_bytes
        assert b'"name":"John"' in json_bytes or b'"name": "John"' in json_bytes

    def test_dump_many_json(self):
        """Test dump_many_json class method."""

        class UserSerializer(Serializer):
            id: int
            name: str

        users = [
            UserSerializer(id=1, name="John"),
            UserSerializer(id=2, name="Jane"),
        ]

        json_bytes = UserSerializer.dump_many_json(users)

        assert isinstance(json_bytes, bytes)
        assert b"John" in json_bytes
        assert b"Jane" in json_bytes


class TestSerializerView:
    """Test SerializerView class."""

    def test_serializer_view_creation(self):
        """Test creating a SerializerView."""

        class UserSerializer(Serializer):
            id: int
            name: str

        view = UserSerializer.only("id")
        assert isinstance(view, SerializerView)

    def test_serializer_view_from_model(self):
        """Test SerializerView.from_model method."""

        class MockModel:
            id = 1
            name = "John"
            email = "john@example.com"

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str

        view = UserSerializer.only("id", "name")
        user = view.from_model(MockModel())

        assert user.id == 1
        assert user.name == "John"

        # Dump respects field selection
        result = view.dump(user)
        assert result == {"id": 1, "name": "John"}


class _ViewItem(Serializer):
    id: int
    name: str | None = None
    note: str = "n"
    nick: str = field(alias="nickName", default="x")


class TestSerializerViewApi:
    """SerializerView chaining and JSON output, and Serializer.validate."""

    def test_only_on_a_view_narrows_it_and_keeps_exclusions(self):
        item = _ViewItem(id=1, name="a", note="b", nick="c")
        narrowed = _ViewItem.only("id", "name", "note").only("name", "note", "nick")
        assert narrowed.dump(item) == {"name": "a", "note": "b"}
        assert _ViewItem.exclude("note").only("id", "note", "name").dump(item) == {"id": 1, "name": "a"}

    def test_view_dump_json_forwards_each_option(self):
        view = _ViewItem.exclude("note")
        item = _ViewItem(id=1, nick="y")
        assert view.dump_json(item) == b'{"id":1,"name":null,"nick":"y"}'
        assert view.dump_json(item, exclude_none=True) == b'{"id":1,"nick":"y"}'
        assert view.dump_json(item, exclude_defaults=True) == b'{"id":1,"nick":"y"}'
        assert view.dump_json(item, by_alias=True) == b'{"id":1,"name":null,"nickName":"y"}'
        assert view.dump_many_json([item]) == b'[{"id":1,"name":null,"nick":"y"}]'
        assert view.dump_many_json([item], exclude_none=True) == b'[{"id":1,"nick":"y"}]'
        assert view.dump_many_json([item], exclude_defaults=True) == b'[{"id":1,"nick":"y"}]'
        assert view.dump_many_json([item], by_alias=True) == b'[{"id":1,"name":null,"nickName":"y"}]'

    def test_validate_returns_a_new_instance_or_raises(self):
        item = _ViewItem(id=1, name="a")
        checked = item.validate()
        assert checked == item
        assert checked is not item
        with pytest.raises(msgspec.ValidationError):
            _ViewItem(id="not-int").validate()


class TestComplexScenarios:
    """Test complex real-world scenarios."""

    def test_one_serializer_multiple_views(self):
        """Test using one serializer for multiple API responses (solving DRF multi-serializer problem)."""

        class UserSerializer(Serializer):
            id: int
            username: str
            email: str
            password_hash: str
            created_at: str
            last_login: str | None = None
            is_active: bool = True
            profile_picture: str | None = None

            class Config:
                write_only = {"password_hash"}
                field_sets = {
                    "list": ["id", "username", "is_active"],
                    "detail": ["id", "username", "email", "created_at", "last_login", "profile_picture"],
                    "admin": ["id", "username", "email", "created_at", "last_login", "is_active"],
                }

            @computed_field
            def display_name(self) -> str:
                return f"@{self.username}"

        # Create a user
        user = UserSerializer(
            id=1,
            username="johndoe",
            email="john@example.com",
            password_hash="hashed_password_123",
            created_at="2024-01-01T00:00:00Z",
            last_login="2024-01-15T10:30:00Z",
            is_active=True,
            profile_picture="https://example.com/avatar.jpg",
        )

        # List view - minimal fields
        list_result = UserSerializer.use("list").dump(user)
        assert list_result == {"id": 1, "username": "johndoe", "is_active": True}
        assert "password_hash" not in list_result
        assert "email" not in list_result

        # Detail view - more fields
        detail_result = UserSerializer.use("detail").dump(user)
        assert "email" in detail_result
        assert "profile_picture" in detail_result
        assert "password_hash" not in detail_result

        # Admin view
        admin_result = UserSerializer.use("admin").dump(user)
        assert "is_active" in admin_result

        # Custom view with computed field
        custom_result = UserSerializer.only("id", "display_name").dump(user)
        assert custom_result == {"id": 1, "display_name": "@johndoe"}

    def test_nested_serializer_dump(self):
        """Test dumping nested serializers."""

        class AddressSerializer(Serializer):
            street: str
            city: str
            zip_code: str

        class UserSerializer(Serializer):
            id: int
            name: str
            address: AddressSerializer

        address = AddressSerializer(street="123 Main St", city="NYC", zip_code="10001")
        user = UserSerializer(id=1, name="John", address=address)

        result = user.dump()

        assert result["id"] == 1
        assert result["name"] == "John"
        assert result["address"] == {
            "street": "123 Main St",
            "city": "NYC",
            "zip_code": "10001",
        }

    def test_list_of_nested_serializers_dump(self):
        """Test dumping list of nested serializers."""

        class TagSerializer(Serializer):
            id: int
            name: str

        class PostSerializer(Serializer):
            id: int
            title: str
            tags: list[TagSerializer]

        tags = [
            TagSerializer(id=1, name="python"),
            TagSerializer(id=2, name="django"),
        ]
        post = PostSerializer(id=1, title="Hello World", tags=tags)

        result = post.dump()

        assert result["id"] == 1
        assert result["title"] == "Hello World"
        assert len(result["tags"]) == 2
        assert result["tags"][0] == {"id": 1, "name": "python"}
        assert result["tags"][1] == {"id": 2, "name": "django"}


class TestSubsetMethod:
    """Test the subset() method for creating type-safe serializer subclasses."""

    def test_subset_basic(self):
        """Test basic subset creation with explicit fields."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str
            password: str
            created_at: str

        # Create a subset with only id and name
        UserMiniSerializer = UserSerializer.subset("id", "name")

        # Verify it's a proper type (subclass of Serializer)
        assert issubclass(UserMiniSerializer, Serializer)

        # Verify it has only the specified fields
        assert set(UserMiniSerializer.__struct_fields__) == {"id", "name"}

        # Create an instance
        mini_user = UserMiniSerializer(id=1, name="John")
        assert mini_user.id == 1
        assert mini_user.name == "John"

        # Dump should only have the subset fields
        result = mini_user.dump()
        assert result == {"id": 1, "name": "John"}

    def test_subset_different_fields_different_class(self):
        """Test that different field combinations create different classes."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str

        UserMini = UserSerializer.subset("id", "name")
        UserEmail = UserSerializer.subset("id", "email")

        # Should be different classes
        assert UserMini is not UserEmail
        assert set(UserMini.__struct_fields__) == {"id", "name"}
        assert set(UserEmail.__struct_fields__) == {"id", "email"}

    def test_subset_with_defaults(self):
        """Test subset preserves default values."""

        class UserSerializer(Serializer):
            id: int
            name: str
            role: str = "user"
            active: bool = True

        UserSubset = UserSerializer.subset("id", "name", "role")

        # Should be able to create instance using default
        user = UserSubset(id=1, name="John")
        assert user.id == 1
        assert user.name == "John"
        assert user.role == "user"

    def test_subset_keeps_the_struct_options_of_the_parent(self):
        """A subset has the rename, omit_defaults, forbid_unknown_fields and frozen options of its parent."""

        class Account(Serializer, rename="camel", omit_defaults=True, forbid_unknown_fields=True, frozen=True):
            user_name: str
            display_name: str = ""
            email_address: str = ""

        AccountMini = Account.subset("user_name", "display_name")
        mini = AccountMini.model_validate({"userName": "ada", "displayName": "Ada"})

        assert mini.dump() == {"userName": "ada", "displayName": "Ada"}
        assert msgspec.json.encode(AccountMini(user_name="ada")) == b'{"userName":"ada"}'
        with pytest.raises(msgspec.ValidationError, match="unknown field `emailAddress`"):
            msgspec.convert({"userName": "ada", "emailAddress": "a@example.com"}, AccountMini)
        with pytest.raises(AttributeError):
            mini.user_name = "bob"

    def test_subset_with_computed_field(self):
        """Test subset includes computed fields."""

        class UserSerializer(Serializer):
            first_name: str
            last_name: str

            @computed_field
            def full_name(self) -> str:
                return f"{self.first_name} {self.last_name}"

        # Include computed field in subset - must include all fields the computed field depends on
        UserWithFullName = UserSerializer.subset("first_name", "last_name", "full_name")

        user = UserWithFullName(first_name="John", last_name="Doe")
        result = user.dump()

        assert result["first_name"] == "John"
        assert result["last_name"] == "Doe"
        assert result["full_name"] == "John Doe"

    def test_subset_from_parent(self):
        """Test creating subset instance from parent instance."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str
            password: str

        UserMini = UserSerializer.subset("id", "name")

        # Create full instance
        full_user = UserSerializer(id=1, name="John", email="john@example.com", password="secret")

        # Convert to mini using from_parent
        mini_user = UserMini.from_parent(full_user)

        assert mini_user.id == 1
        assert mini_user.name == "John"
        assert mini_user.dump() == {"id": 1, "name": "John"}

    def test_subset_with_custom_name(self):
        """Test subset with custom class name."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str

        UserListSerializer = UserSerializer.subset("id", "name", name="UserListSerializer")

        # Verify custom name
        assert UserListSerializer.__name__ == "UserListSerializer"

    def test_subset_type_safety(self):
        """Test that subset maintains type annotations."""

        class UserSerializer(Serializer):
            id: int
            name: str
            age: int | None = None

        UserMini = UserSerializer.subset("id", "name", "age")

        # Get type hints from the subset class
        from typing import get_type_hints  # noqa: PLC0415

        hints = get_type_hints(UserMini)

        assert hints["id"] is int
        assert hints["name"] is str


class TestFieldsMethod:
    """Test the fields() method for creating subsets from field_sets."""

    def test_fields_from_field_set(self):
        """Test creating subset from a predefined field set."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str
            password: str
            created_at: str

            class Config:
                field_sets = {
                    "list": ["id", "name"],
                    "detail": ["id", "name", "email", "created_at"],
                }

        # Create serializer from field set
        UserListSerializer = UserSerializer.fields("list")
        UserDetailSerializer = UserSerializer.fields("detail")

        # Verify field sets
        assert set(UserListSerializer.__struct_fields__) == {"id", "name"}
        assert set(UserDetailSerializer.__struct_fields__) == {"id", "name", "email", "created_at"}

        # Create instances
        list_user = UserListSerializer(id=1, name="John")
        assert list_user.dump() == {"id": 1, "name": "John"}

    def test_fields_auto_names(self):
        """Test that fields() auto-generates sensible class names."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str

            class Config:
                field_sets = {
                    "list": ["id", "name"],
                    "detail": ["id", "name", "email"],
                }

        UserList = UserSerializer.fields("list")
        UserDetail = UserSerializer.fields("detail")

        # Names should be capitalized field set name
        assert UserList.__name__ == "UserSerializerList"
        assert UserDetail.__name__ == "UserSerializerDetail"

    def test_fields_custom_name(self):
        """Test fields() with custom name."""

        class UserSerializer(Serializer):
            id: int
            name: str

            class Config:
                field_sets = {"mini": ["id", "name"]}

        UserMini = UserSerializer.fields("mini", name="UserMiniResponse")
        assert UserMini.__name__ == "UserMiniResponse"

    def test_fields_invalid_field_set(self):
        """Test fields() raises error for unknown field set."""

        class UserSerializer(Serializer):
            id: int
            name: str

            class Config:
                field_sets = {"list": ["id", "name"]}

        with pytest.raises(ValueError) as exc_info:
            UserSerializer.fields("nonexistent")

        assert "nonexistent" in str(exc_info.value)
        assert "not found" in str(exc_info.value)


class TestSubsetResponseModel:
    """Test using subset serializers as response_model."""

    def test_subset_as_type_annotation(self):
        """Test subset can be used as a type annotation."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str

        UserMini = UserSerializer.subset("id", "name")

        # This simulates using it as a return type annotation
        def get_user() -> UserMini:  # type: ignore
            return UserMini(id=1, name="John")

        result = get_user()
        assert isinstance(result, UserMini)
        assert isinstance(result, Serializer)

    def test_subset_list_type(self):
        """Test subset can be used in list type annotations."""

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str

        UserMini = UserSerializer.subset("id", "name")

        def list_users() -> list[UserMini]:  # type: ignore
            return [
                UserMini(id=1, name="John"),
                UserMini(id=2, name="Jane"),
            ]

        result = list_users()
        assert len(result) == 2
        assert all(isinstance(u, UserMini) for u in result)

    def test_subset_with_from_model(self):
        """Test subset's from_model works correctly."""

        class MockModel:
            id = 1
            name = "John"
            email = "john@example.com"
            password = "secret"

        class UserSerializer(Serializer):
            id: int
            name: str
            email: str
            password: str

        UserMini = UserSerializer.subset("id", "name")

        # from_model should only populate the subset fields
        mini_user = UserMini.from_model(MockModel())
        assert mini_user.id == 1
        assert mini_user.name == "John"
        assert mini_user.dump() == {"id": 1, "name": "John"}


class TestSubsetCompleteWorkflow:
    """Test complete workflow: one serializer, multiple type-safe variants."""

    def test_complete_drf_replacement_workflow(self):
        """Test the complete workflow that replaces multiple DRF serializers."""

        class UserSerializer(Serializer):
            id: int
            username: str
            email: str
            password: str
            created_at: str
            last_login: str | None = None
            is_staff: bool = False

            class Config:
                write_only = {"password"}
                field_sets = {
                    "list": ["id", "username"],
                    "detail": ["id", "username", "email", "created_at", "last_login"],
                    "admin": ["id", "username", "email", "created_at", "last_login", "is_staff"],
                }

            @computed_field
            def display_name(self) -> str:
                return f"@{self.username}"

        # Create type-safe serializers for different views
        UserListSerializer = UserSerializer.fields("list")
        UserDetailSerializer = UserSerializer.fields("detail")
        UserAdminSerializer = UserSerializer.fields("admin")
        UserPublicSerializer = UserSerializer.subset("id", "username", "display_name")

        # Verify each has correct fields
        assert set(UserListSerializer.__struct_fields__) == {"id", "username"}
        assert set(UserDetailSerializer.__struct_fields__) == {"id", "username", "email", "created_at", "last_login"}
        assert set(UserAdminSerializer.__struct_fields__) == {
            "id",
            "username",
            "email",
            "created_at",
            "last_login",
            "is_staff",
        }
        assert set(UserPublicSerializer.__struct_fields__) == {"id", "username"}

        # All are proper Serializer subclasses
        assert issubclass(UserListSerializer, Serializer)
        assert issubclass(UserDetailSerializer, Serializer)
        assert issubclass(UserAdminSerializer, Serializer)
        assert issubclass(UserPublicSerializer, Serializer)

        # Create instances and verify dumps
        list_user = UserListSerializer(id=1, username="johndoe")
        assert list_user.dump() == {"id": 1, "username": "johndoe"}

        detail_user = UserDetailSerializer(
            id=1, username="johndoe", email="john@example.com", created_at="2024-01-01", last_login=None
        )
        result = detail_user.dump()
        assert result["id"] == 1
        assert result["email"] == "john@example.com"
        assert "password" not in result  # write_only excluded

        # Public serializer with computed field
        public_user = UserPublicSerializer(id=1, username="johndoe")
        public_result = public_user.dump()
        assert public_result["id"] == 1
        assert public_result["username"] == "johndoe"
        assert public_result["display_name"] == "@johndoe"
