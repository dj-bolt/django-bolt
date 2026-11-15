"""Tests for Serializer rename parameter support (camelCase, pascal, kebab, etc.)."""

from datetime import datetime
from uuid import UUID

import msgspec
import pytest

from django_bolt.exceptions import RequestValidationError
from django_bolt.serializers import Serializer, field, field_validator


class TestRenameCamel:
    """Test rename='camel' for camelCase field mapping."""

    def test_rename_camel_model_validate(self):
        """model_validate() accepts camelCase keys when rename='camel'."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            created_at: str

        user = CreateUser.model_validate({"userName": "alice", "createdAt": "2024-01-01"})
        assert user.user_name == "alice"
        assert user.created_at == "2024-01-01"

    def test_rename_camel_model_validate_json(self):
        """model_validate_json() accepts camelCase keys when rename='camel'."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            created_at: str

        user = CreateUser.model_validate_json(b'{"userName": "alice", "createdAt": "2024-01-01"}')
        assert user.user_name == "alice"
        assert user.created_at == "2024-01-01"

    def test_rename_camel_dump(self):
        """dump() outputs camelCase keys when rename='camel'."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            created_at: str

        user = CreateUser(user_name="alice", created_at="2024-01-01")
        data = user.dump()
        assert data == {"userName": "alice", "createdAt": "2024-01-01"}

    def test_rename_camel_dump_json(self):
        """dump_json() outputs camelCase keys when rename='camel'."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            created_at: str

        user = CreateUser(user_name="alice", created_at="2024-01-01")
        json_bytes = user.dump_json()
        decoded = msgspec.json.decode(json_bytes)
        assert decoded == {"userName": "alice", "createdAt": "2024-01-01"}

    def test_rename_camel_dump_many(self):
        """dump_many() outputs camelCase keys when rename='camel'."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            created_at: str

        users = [
            CreateUser(user_name="alice", created_at="2024-01-01"),
            CreateUser(user_name="bob", created_at="2024-02-02"),
        ]
        data = CreateUser.dump_many(users)
        assert data == [
            {"userName": "alice", "createdAt": "2024-01-01"},
            {"userName": "bob", "createdAt": "2024-02-02"},
        ]


class TestRenamePascal:
    """Test rename='pascal' for PascalCase field mapping."""

    def test_rename_pascal_dump(self):
        """dump() outputs PascalCase keys when rename='pascal'."""

        class CreateUser(Serializer, rename="pascal"):
            user_name: str
            created_at: str

        user = CreateUser(user_name="alice", created_at="2024-01-01")
        data = user.dump()
        assert data == {"UserName": "alice", "CreatedAt": "2024-01-01"}

    def test_rename_pascal_model_validate(self):
        """model_validate() accepts PascalCase keys when rename='pascal'."""

        class CreateUser(Serializer, rename="pascal"):
            user_name: str
            created_at: str

        user = CreateUser.model_validate({"UserName": "alice", "CreatedAt": "2024-01-01"})
        assert user.user_name == "alice"
        assert user.created_at == "2024-01-01"


class TestRenameKebab:
    """Test rename='kebab' for kebab-case field mapping."""

    def test_rename_kebab_dump(self):
        """dump() outputs kebab-case keys when rename='kebab'."""

        class CreateUser(Serializer, rename="kebab"):
            user_name: str
            created_at: str

        user = CreateUser(user_name="alice", created_at="2024-01-01")
        data = user.dump()
        assert data == {"user-name": "alice", "created-at": "2024-01-01"}


class TestRenameWithDefaults:
    """Test rename with default field values."""

    def test_rename_with_defaults(self):
        """Default values work correctly with rename='camel'."""

        class UserSettings(Serializer, rename="camel"):
            user_name: str
            is_active: bool = True
            max_retries: int = 3

        settings = UserSettings(user_name="alice")
        assert settings.is_active is True
        assert settings.max_retries == 3

        data = settings.dump()
        assert data == {"userName": "alice", "isActive": True, "maxRetries": 3}

    def test_rename_with_exclude_defaults(self):
        """dump(exclude_defaults=True) works with rename='camel'."""

        class UserSettings(Serializer, rename="camel"):
            user_name: str
            is_active: bool = True
            max_retries: int = 3

        settings = UserSettings(user_name="alice")
        data = settings.dump(exclude_defaults=True)
        # user_name has no default, so it should be included
        # is_active and max_retries are at their default values, so excluded
        assert data == {"userName": "alice"}

    def test_rename_with_exclude_none(self):
        """dump(exclude_none=True) works with rename='camel'."""

        class UserProfile(Serializer, rename="camel"):
            user_name: str
            bio_text: str | None = None

        profile = UserProfile(user_name="alice")
        data = profile.dump(exclude_none=True)
        assert data == {"userName": "alice"}

    def test_rename_and_tag_dump_gives_the_values_of_the_other_dump_paths(self):
        """dump() changes only the keys. The values are the same on each path of dump()."""

        class Plain(Serializer):
            created_at: datetime
            ident: UUID
            labels: set[str]

        class Camel(Serializer, rename="camel"):
            created_at: datetime
            ident: UUID
            labels: set[str]

        class Tagged(Serializer, tag="event"):
            created_at: datetime
            ident: UUID
            labels: set[str]

        values = {"created_at": datetime(2024, 1, 2, 3, 4, 5), "ident": UUID(int=1), "labels": {"a"}}
        camel_values = {"createdAt": values["created_at"], "ident": values["ident"], "labels": values["labels"]}

        assert Plain(**values).dump() == values
        assert Camel(**values).dump() == camel_values
        assert Camel(**values).dump(exclude_none=True) == camel_values
        assert Camel.dump_many([Camel(**values)]) == [camel_values]
        assert Tagged(**values).dump() == {"type": "event", **values}
        assert Tagged.dump_many([Tagged(**values)]) == [{"type": "event", **values}]
        assert list(Tagged(**values).dump()) == ["type", "created_at", "ident", "labels"]


class TestRenameWithFieldAlias:
    """Test rename with per-field alias override."""

    def test_field_alias_overrides_rename(self):
        """Per-field alias takes precedence over class-level rename when by_alias=True."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            created_at: str = field(alias="creation_date")

        user = CreateUser(user_name="alice", created_at="2024-01-01")

        # Without by_alias, rename mapping is used
        data = user.dump()
        assert data == {"userName": "alice", "createdAt": "2024-01-01"}

        # With by_alias, explicit alias overrides rename for that field
        data_alias = user.dump(by_alias=True)
        assert data_alias == {"userName": "alice", "creation_date": "2024-01-01"}


class TestRenameErrorCollection:
    """Test error collection with rename."""

    def test_rename_error_collection_references_renamed_keys(self):
        """Validation errors reference renamed field names (camelCase)."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            age_years: int

        with pytest.raises(RequestValidationError) as exc_info:
            CreateUser.model_validate({"userName": "alice", "ageYears": "not-a-number"})

        errors = exc_info.value.errors()
        assert len(errors) >= 1
        # Error location should use the camelCase key (body, ageYears)
        error_locs = [tuple(e["loc"]) for e in errors]
        assert ("body", "ageYears") in error_locs

    def test_rename_error_collection_json(self):
        """Validation errors from JSON input reference renamed field names."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            age_years: int

        with pytest.raises(RequestValidationError) as exc_info:
            CreateUser.model_validate_json(b'{"userName": "alice", "ageYears": "not-a-number"}')

        errors = exc_info.value.errors()
        assert len(errors) >= 1
        # Error location should use the camelCase key (body, ageYears)
        error_locs = [tuple(e["loc"]) for e in errors]
        assert ("body", "ageYears") in error_locs


# Module-level classes for nested tests (function-scoped classes can't resolve
# type hints for nested references due to Python's type resolution limitations)
class _NestedAddress(Serializer, rename="camel"):
    street_name: str
    zip_code: str


class _NestedUser(Serializer, rename="camel"):
    user_name: str
    home_address: _NestedAddress


class _PascalAddress(Serializer, rename="pascal"):
    street_name: str


class _MixedUser(Serializer, rename="camel"):
    user_name: str
    home_address: _PascalAddress


class TestRenameNested:
    """Test nested serializers with rename."""

    def test_rename_nested_serializers(self):
        """Nested serializers respect their own rename configuration."""
        addr = _NestedAddress(street_name="Main St", zip_code="12345")
        user = _NestedUser(user_name="alice", home_address=addr)
        data = user.dump()

        assert data == {
            "userName": "alice",
            "homeAddress": {
                "streetName": "Main St",
                "zipCode": "12345",
            },
        }

    def test_mixed_rename_nested(self):
        """Parent and child can use different rename strategies."""
        addr = _PascalAddress(street_name="Main St")
        user = _MixedUser(user_name="alice", home_address=addr)
        data = user.dump()

        assert data == {
            "userName": "alice",
            "homeAddress": {
                "StreetName": "Main St",
            },
        }


class TestRenameBackwardCompat:
    """Test backward compatibility - existing serializers without rename still work."""

    def test_no_rename_backward_compat(self):
        """Serializers without rename continue to use snake_case keys."""

        class UserSerializer(Serializer):
            user_name: str
            created_at: str

        user = UserSerializer(user_name="alice", created_at="2024-01-01")
        data = user.dump()
        assert data == {"user_name": "alice", "created_at": "2024-01-01"}

    def test_no_rename_fast_path_still_works(self):
        """Serializers without rename still use the fast path."""

        class SimpleSerializer(Serializer):
            name: str
            age: int

        # Fast path should be enabled (no rename, no computed fields, etc.)
        assert SimpleSerializer.__dump_fast_path__ is True
        assert SimpleSerializer.__rename_map__ == {}
        assert SimpleSerializer.__has_rename__ is False

    def test_rename_keeps_fast_path(self):
        """Serializers with rename still use the fast path."""

        class CamelSerializer(Serializer, rename="camel"):
            user_name: str

        # Fast path is still enabled. dump() pairs the encoded names with the values.
        assert CamelSerializer.__dump_fast_path__ is True
        assert CamelSerializer.__rename_map__ == {"user_name": "userName"}
        assert CamelSerializer.__has_rename__ is True


class TestRenameWithFieldValidators:
    """Test rename with field validators."""

    def test_rename_with_field_validator(self):
        """Field validators work correctly with rename='camel'."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            email_address: str

            @field_validator("email_address")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value.lower()

        user = CreateUser.model_validate({"userName": "Alice", "emailAddress": "ALICE@EXAMPLE.COM"})
        assert user.email_address == "alice@example.com"

        # dump should use camelCase
        data = user.dump()
        assert data == {"userName": "Alice", "emailAddress": "alice@example.com"}

    def test_rename_field_validator_error(self):
        """Field validator errors work with renamed fields."""

        class CreateUser(Serializer, rename="camel"):
            user_name: str
            email_address: str

            @field_validator("email_address")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

        # Field validator errors are raised as RequestValidationError inside __post_init__.
        # msgspec wraps exceptions from __post_init__ in ValidationError, but model_validate
        # extracts and re-raises the inner RequestValidationError.
        with pytest.raises((RequestValidationError, msgspec.ValidationError)) as exc_info:
            CreateUser.model_validate({"userName": "Alice", "emailAddress": "bad-email"})

        exc = exc_info.value
        # Extract errors from either the direct exception or the wrapped cause
        if isinstance(exc, RequestValidationError):
            errors = exc.errors()
        elif hasattr(exc, "__cause__") and isinstance(exc.__cause__, RequestValidationError):
            errors = exc.__cause__.errors()
        else:
            pytest.fail(f"Expected RequestValidationError, got {type(exc)}: {exc}")

        assert len(errors) >= 1
        # Field validator errors use Python field names (they run after struct creation)
        assert errors[0]["loc"] == ["body", "email_address"]


class TestRenameRoundTrip:
    """Test full round-trip: JSON input -> Serializer -> JSON output."""

    def test_full_round_trip(self):
        """Full round trip: JSON with camelCase -> Serializer -> camelCase JSON."""

        class UserProfile(Serializer, rename="camel"):
            first_name: str
            last_name: str
            is_active: bool = True

        # Input as camelCase JSON
        json_input = b'{"firstName": "Alice", "lastName": "Smith", "isActive": false}'
        user = UserProfile.model_validate_json(json_input)

        assert user.first_name == "Alice"
        assert user.last_name == "Smith"
        assert user.is_active is False

        # Output should also be camelCase
        output = user.dump()
        assert output == {"firstName": "Alice", "lastName": "Smith", "isActive": False}

        # JSON output should also use camelCase
        json_output = msgspec.json.decode(user.dump_json())
        assert json_output == {"firstName": "Alice", "lastName": "Smith", "isActive": False}
