"""Tests for the base Serializer functionality."""

from __future__ import annotations

import pytest

from django_bolt.exceptions import RequestValidationError
from django_bolt.serializers import Email, Serializer, field_validator, model_validator


class TestSerializerBasics:
    """Test basic Serializer functionality."""

    def test_serializer_creation(self):
        """Test creating a simple serializer."""

        class UserSerializer(Serializer):
            username: str
            email: str

        user = UserSerializer(username="alice", email="alice@example.com")
        assert user.username == "alice"
        assert user.email == "alice@example.com"

    def test_serializer_with_defaults(self):
        """Test serializer with default values."""

        class UserSerializer(Serializer):
            username: str
            email: str = "no-email@example.com"

        user = UserSerializer(username="bob")
        assert user.username == "bob"
        assert user.email == "no-email@example.com"

    def test_serializer_to_dict(self):
        """Test converting serializer to dict."""

        class UserSerializer(Serializer):
            username: str
            email: str

        user = UserSerializer(username="charlie", email="charlie@example.com")
        data = user.to_dict()

        assert data == {"username": "charlie", "email": "charlie@example.com"}


class TestFieldValidators:
    """Test field-level validation."""

    def test_field_validator_basic(self):
        """Test basic field validator."""

        class UserSerializer(Serializer):
            email: str

            @field_validator("email")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

        # Valid email should work
        user = UserSerializer(email="valid@example.com")
        assert user.email == "valid@example.com"

        # Invalid email should raise
        with pytest.raises(RequestValidationError) as exc_info:
            UserSerializer(email="invalid")
        assert "Invalid email" in str(exc_info.value)

    def test_field_validator_transformation(self):
        """Test that field validators can transform values."""

        class UserSerializer(Serializer):
            email: str

            @field_validator("email")
            def normalize_email(cls, value):
                return value.lower().strip()

        user = UserSerializer(email="  ALICE@EXAMPLE.COM  ")
        assert user.email == "alice@example.com"

    def test_multiple_field_validators(self):
        """Test multiple validators on the same field."""

        class UserSerializer(Serializer):
            password: str

            @field_validator("password")
            def check_length(cls, value):
                if len(value) < 8:
                    raise ValueError("Password too short")
                return value

            @field_validator("password")
            def check_complexity(cls, value):
                if not any(c.isupper() for c in value):
                    raise ValueError("Password must have uppercase")
                return value

        # Valid password
        user = UserSerializer(password="SecurePass123")
        assert user.password == "SecurePass123"

        # Too short
        with pytest.raises(RequestValidationError):
            UserSerializer(password="Short1")

        # No uppercase
        with pytest.raises(RequestValidationError):
            UserSerializer(password="longpassword1")

    def test_field_validator_without_return(self):
        """Test that validators without return statements preserve the original value."""

        class UserSerializer(Serializer):
            email: str

            @field_validator("email")
            def validate_email(cls, value):
                # Validator that only validates, doesn't transform
                if "@" not in value:
                    raise ValueError("Invalid email")
                # No return statement - should preserve original value

        # Valid email should preserve the original value
        user = UserSerializer(email="valid@example.com")
        assert user.email == "valid@example.com"

        # Invalid email should still raise
        with pytest.raises(RequestValidationError) as exc_info:
            UserSerializer(email="invalid")
        assert "Invalid email" in str(exc_info.value)

    def test_field_validator_with_mixed_returns(self):
        """Test validators with some returning None and some returning values."""

        class UserSerializer(Serializer):
            email: str

            @field_validator("email")
            def validate_format(cls, value):
                # Validates but doesn't return
                if "@" not in value:
                    raise ValueError("Invalid email format")
                # No return

            @field_validator("email")
            def normalize_email(cls, value):
                # Transforms and returns
                return value.lower().strip()

        # Should apply the transformation from the second validator
        user = UserSerializer(email="  TEST@EXAMPLE.COM  ")
        assert user.email == "test@example.com"

        # Should still validate with first validator
        with pytest.raises(RequestValidationError):
            UserSerializer(email="invalid-email")


class TestModelValidators:
    """Test model-level validation."""

    def test_model_validator_basic(self):
        """Test basic model validator."""

        class PasswordSerializer(Serializer):
            password: str
            password_confirm: str

            @model_validator
            def check_passwords_match(self):
                if self.password != self.password_confirm:
                    raise ValueError("Passwords don't match")

        # Matching passwords
        data = PasswordSerializer(password="Secret123", password_confirm="Secret123")
        assert data.password == "Secret123"

        # Non-matching passwords
        with pytest.raises(RequestValidationError) as exc_info:
            PasswordSerializer(password="Secret123", password_confirm="Different456")
        assert "don't match" in str(exc_info.value)

    def test_model_validator_cross_field(self):
        """Test model validator with cross-field logic."""

        class RangeSerializer(Serializer):
            min_value: int
            max_value: int

            @model_validator
            def validate_range(self):
                if self.min_value >= self.max_value:
                    raise ValueError("min_value must be less than max_value")

        # Valid range
        data = RangeSerializer(min_value=1, max_value=10)
        assert data.min_value == 1
        assert data.max_value == 10

        # Invalid range
        with pytest.raises(RequestValidationError):
            RangeSerializer(min_value=10, max_value=1)


class TestValidatorExecution:
    """Test validator execution order and behavior."""

    def test_field_validators_run_before_model_validators(self):
        """Test that field validators run before model validators."""
        execution_order = []

        class TestSerializer(Serializer):
            value: str

            @field_validator("value")
            def field_val(cls, v):
                execution_order.append("field")
                return v

            @model_validator
            def model_val(self):
                execution_order.append("model")

        TestSerializer(value="test")

        # Field validator should run before model validator
        assert execution_order[0] == "field"
        assert execution_order[1] == "model"


class TestOptionalFields:
    """Test optional fields with None values."""

    def test_optional_field(self):
        """Test optional field handling."""

        class UserSerializer(Serializer):
            username: str
            email: str | None = None

        user = UserSerializer(username="alice")
        assert user.username == "alice"
        assert user.email is None

        user2 = UserSerializer(username="bob", email="bob@example.com")
        assert user2.email == "bob@example.com"


class TestSerializerInheritance:
    """Test Serializer inheritance and composition."""

    def test_serializer_inheritance(self):
        """Test inheriting from a base serializer."""

        class BaseUserSerializer(Serializer):
            username: str
            email: str

            @field_validator("email")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

        class AdminSerializer(BaseUserSerializer):
            is_admin: bool = False

        # Child serializer should inherit validators
        with pytest.raises(RequestValidationError):
            AdminSerializer(username="alice", email="invalid", is_admin=True)

        # Valid data should work
        admin = AdminSerializer(username="alice", email="alice@example.com", is_admin=True)
        assert admin.is_admin is True


class TestConfigClass:
    """Test Config class configuration."""

    def test_config_model_reference(self):
        """Test Config.model reference."""

        class DummyModel:
            pass

        class UserSerializer(Serializer):
            username: str

            class Config:
                model = DummyModel

        assert UserSerializer.Config.model == DummyModel


class TestMultiErrorCollection:
    """Test that validation collects all errors instead of stopping at the first."""

    def test_multiple_field_errors_collected(self):
        """Test that multiple field validation errors are collected."""

        class UserSerializer(Serializer):
            email: str
            password: str

            @field_validator("email")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

            @field_validator("password")
            def validate_password(cls, value):
                if len(value) < 8:
                    raise ValueError("Password too short")
                return value

        # Both email and password are invalid
        with pytest.raises(RequestValidationError) as exc_info:
            UserSerializer(email="invalid", password="short")

        # Should contain both errors
        errors = exc_info.value.errors()
        assert len(errors) == 2

        error_msgs = [e["msg"] for e in errors]
        assert "Invalid email" in error_msgs
        assert "Password too short" in error_msgs

    def test_field_and_model_errors_collected(self):
        """Test that field and model validation errors are all collected."""

        class PasswordSerializer(Serializer):
            email: str
            password: str
            password_confirm: str

            @field_validator("email")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

            @model_validator
            def check_passwords_match(self):
                if self.password != self.password_confirm:
                    raise ValueError("Passwords do not match")
                return self

        # Email is invalid AND passwords don't match
        with pytest.raises(RequestValidationError) as exc_info:
            PasswordSerializer(email="invalid", password="secret123", password_confirm="different")

        # Should contain both field error and model error
        errors = exc_info.value.errors()
        assert len(errors) == 2

        error_msgs = [e["msg"] for e in errors]
        assert "Invalid email" in error_msgs
        assert "Passwords do not match" in error_msgs

    def test_error_str_contains_all_messages(self):
        """Test that error string representation includes all errors."""

        class UserSerializer(Serializer):
            email: str
            age: int

            @field_validator("email")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email format")
                return value

            @field_validator("age")
            def validate_age(cls, value):
                if value < 0:
                    raise ValueError("Age must be positive")
                return value

        with pytest.raises(RequestValidationError) as exc_info:
            UserSerializer(email="bad", age=-5)

        error_str = str(exc_info.value)
        assert "Invalid email format" in error_str
        assert "Age must be positive" in error_str

    def test_single_error_still_works(self):
        """Test that single validation error still works correctly."""

        class UserSerializer(Serializer):
            email: str
            name: str

            @field_validator("email")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

        # Only email is invalid
        with pytest.raises(RequestValidationError) as exc_info:
            UserSerializer(email="invalid", name="John")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["msg"] == "Invalid email"

    def test_valid_data_with_validators(self):
        """Test that valid data passes all validators without errors."""

        class UserSerializer(Serializer):
            email: str
            password: str

            @field_validator("email")
            def validate_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

            @field_validator("password")
            def validate_password(cls, value):
                if len(value) < 8:
                    raise ValueError("Password too short")
                return value

        # Both fields are valid
        user = UserSerializer(email="valid@example.com", password="longpassword123")
        assert user.email == "valid@example.com"
        assert user.password == "longpassword123"


class TestModelValidateCollectsAllErrors:
    """Regression tests for issue #201 — model_validate/model_validate_json must
    collect every error, including missing required fields, not just those that
    happen to be present in the payload."""

    def test_missing_required_field_collected_alongside_invalid_field(self):
        class Contact(Serializer):
            first_name: str
            last_name: str
            email: Email | None = None

        with pytest.raises(RequestValidationError) as exc_info:
            Contact.model_validate({"first_name": "Foo", "email": 42})

        errors = exc_info.value.errors()
        locs = {tuple(err["loc"]) for err in errors}
        assert ("body", "last_name") in locs, f"expected missing last_name error, got {errors!r}"
        assert ("body", "email") in locs, f"expected email type error, got {errors!r}"

    def test_missing_required_field_collected_from_json(self):
        class Contact(Serializer):
            first_name: str
            last_name: str
            email: Email | None = None

        with pytest.raises(RequestValidationError) as exc_info:
            Contact.model_validate_json(b'{"first_name": "Foo", "email": 42}')

        errors = exc_info.value.errors()
        locs = {tuple(err["loc"]) for err in errors}
        assert ("body", "last_name") in locs
        assert ("body", "email") in locs

    def test_multiple_missing_required_fields_all_reported(self):
        class Signup(Serializer):
            username: str
            password: str
            age: int

        with pytest.raises(RequestValidationError) as exc_info:
            Signup.model_validate({"username": "x"})

        errors = exc_info.value.errors()
        locs = {tuple(err["loc"]) for err in errors}
        assert ("body", "password") in locs
        assert ("body", "age") in locs
        for err in errors:
            if tuple(err["loc"]) in {("body", "password"), ("body", "age")}:
                assert err["type"] == "missing"

    def test_optional_missing_field_does_not_produce_error(self):
        class Profile(Serializer):
            name: str
            nickname: str = "anon"

        profile = Profile.model_validate({"name": "alice"})
        assert profile.name == "alice"
        assert profile.nickname == "anon"

    def test_renamed_missing_required_field_reports_encoded_key(self):
        class Payload(Serializer, rename="camel"):
            first_name: str
            last_name: str

        with pytest.raises(RequestValidationError) as exc_info:
            Payload.model_validate({"firstName": "Foo"})

        errors = exc_info.value.errors()
        locs = {tuple(err["loc"]) for err in errors}
        assert ("body", "lastName") in locs


def test_subclass_used_after_parent_dumps_all_fields():
    """A field-less subclass must build its own dump specs even when the parent was
    used (and lazily collected) first — it used to inherit the parent's 'collected'
    flag with empty specs and dump ``{}``."""

    class Owner(Serializer):
        id: int

    class Parent(Serializer):
        id: int
        owner: Owner  # nested: forces the spec-driven (non-fast-path) dump

    class Child(Parent):
        pass

    assert Parent(id=1, owner=Owner(id=9)).dump() == {"id": 1, "owner": {"id": 9}}
    assert Child(id=2, owner=Owner(id=9)).dump() == {"id": 2, "owner": {"id": 9}}
    assert Child.dump_many([Child(id=3, owner=Owner(id=9))]) == [{"id": 3, "owner": {"id": 9}}]
