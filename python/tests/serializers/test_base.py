"""Tests for the base Serializer functionality."""

from __future__ import annotations

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt.exceptions import RequestValidationError
from django_bolt.serializers import Email, Serializer, field_validator, model_validator
from django_bolt.testing import TestClient


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

    def test_exclude_unset_leaves_out_the_fields_that_hold_unset(self):
        """A field with the default msgspec.UNSET is unset until the caller or the input gives it a value."""

        class UserPatch(Serializer):
            username: str | msgspec.UnsetType = msgspec.UNSET
            email: str | None | msgspec.UnsetType = msgspec.UNSET

        patch = msgspec.json.decode(b'{"email": null}', type=UserPatch)

        assert patch.dump(exclude_unset=True) == {"email": None}
        assert patch.to_dict(exclude_unset=True) == {"email": None}
        assert patch.dump_json(exclude_unset=True) == b'{"email":null}'
        assert UserPatch.dump_many([patch], exclude_unset=True) == [{"email": None}]


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


_MODE_CALLS: list[tuple[str, object]] = []


class ModeOrder(Serializer):
    """The validators are not in the order of their modes."""

    v: int

    @field_validator("v", mode="after")
    @classmethod
    def after_1(cls, value):
        _MODE_CALLS.append(("after_1", value))
        return value

    @field_validator("v", mode="before")
    @classmethod
    def before_1(cls, value):
        _MODE_CALLS.append(("before_1", value))
        return value

    @field_validator("v")
    @classmethod
    def after_2(cls, value):
        _MODE_CALLS.append(("after_2", value))
        return value

    @field_validator("v", mode="before")
    @classmethod
    def before_2(cls, value):
        _MODE_CALLS.append(("before_2", value))
        return int(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def model_after_1(self):
        _MODE_CALLS.append(("model_after_1", self.v))

    @model_validator(mode="before")
    @classmethod
    def model_before_1(cls, data):
        _MODE_CALLS.append(("model_before_1", dict(data)))
        return data

    @model_validator
    def model_after_2(self):
        _MODE_CALLS.append(("model_after_2", self.v))

    @model_validator(mode="before")
    @classmethod
    def model_before_2(cls, data):
        _MODE_CALLS.append(("model_before_2", dict(data)))
        return data


class ModeOrderChild(ModeOrder):
    @field_validator("v", mode="before")
    @classmethod
    def child_before(cls, value):
        _MODE_CALLS.append(("child_before", value))
        return value

    @field_validator("v")
    @classmethod
    def child_after(cls, value):
        _MODE_CALLS.append(("child_after", value))
        return value


class BeforeLine(Serializer):
    qty: int
    note: str | None = None

    @field_validator("qty", mode="before")
    @classmethod
    def parse_qty(cls, value):
        if isinstance(value, str):
            if not value.strip().isdigit():
                raise ValueError("qty is not a number")
            return int(value)
        return value

    @field_validator("note", mode="before")
    @classmethod
    def empty_note_is_none(cls, value):
        return None if value == "" else value


class BeforeOrder(Serializer, rename="camel"):
    order_id: int
    tags: list[str] = []
    lines: list[BeforeLine] = []
    first: BeforeLine | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_old_key(cls, data):
        if isinstance(data, dict) and "id" in data:
            data = {**data, "orderId": data["id"]}
            del data["id"]
        return data

    @field_validator("tags", mode="before")
    @classmethod
    def split_tags(cls, value):
        return value.split(",") if isinstance(value, str) else value


class BeforeOuter(Serializer):
    """Has no before validator, but its nested serializer has."""

    order: BeforeOrder


class TestValidatorModes:
    """mode="before" gets the raw input value, mode="after" gets the converted value."""

    # The order that pydantic 2 gives for the same class.
    EXPECTED_ORDER = [
        ("model_before_2", {"v": "5"}),
        ("model_before_1", {"v": "5"}),
        ("before_2", "5"),
        ("before_1", 5),
        ("after_1", 5),
        ("after_2", 5),
        ("model_after_1", 5),
        ("model_after_2", 5),
    ]

    @pytest.mark.parametrize(
        "build",
        [
            lambda: ModeOrder.model_validate({"v": "5"}),
            lambda: ModeOrder.model_validate_json(b'{"v": "5"}'),
            lambda: ModeOrder(v="5"),
        ],
        ids=["model_validate", "model_validate_json", "construction"],
    )
    def test_before_validators_run_first_whatever_the_definition_order(self, build):
        _MODE_CALLS.clear()
        assert build().v == 5
        assert _MODE_CALLS == self.EXPECTED_ORDER

    def test_validators_of_a_subclass(self):
        """Before: subclass, then parent. After: parent, then subclass."""
        _MODE_CALLS.clear()
        ModeOrderChild.model_validate({"v": "5"})
        assert [name for name, _ in _MODE_CALLS] == [
            "model_before_2",
            "model_before_1",
            "child_before",
            "before_2",
            "before_1",
            "after_1",
            "after_2",
            "child_after",
            "model_after_1",
            "model_after_2",
        ]

    def test_before_validators_get_the_raw_input_at_each_depth(self):
        data = {
            "order": {
                "id": 7,
                "tags": "a,b",
                "lines": [{"qty": "3", "note": ""}, {"qty": 4, "note": "n"}],
                "first": {"qty": " 9 "},
            }
        }
        for outer in (BeforeOuter.model_validate(data), BeforeOuter.model_validate_json(msgspec.json.encode(data))):
            order = outer.order
            assert order.order_id == 7
            assert order.tags == ["a", "b"]
            assert [(line.qty, line.note) for line in order.lines] == [(3, None), (4, "n")]
            assert order.first.qty == 9
        # The input of the caller is not changed.
        assert data["order"]["tags"] == "a,b"

    def test_value_of_a_before_validator_is_validated_by_the_field_type(self):
        """The type of the field rejects a wrong value from a before validator."""

        class Wrong(Serializer):
            v: int

            @field_validator("v", mode="before")
            @classmethod
            def to_text(cls, value):
                return "text"

        with pytest.raises(RequestValidationError, match="Expected `int`, got `str`"):
            Wrong.model_validate({"v": 1})
        with pytest.raises(RequestValidationError, match="Expected `int`, got `str`"):
            Wrong.model_validate_json(b'{"v": 1}')

    def test_before_validator_does_not_run_for_an_absent_field(self):
        assert BeforeLine.model_validate({"qty": 1}).note is None
        with pytest.raises(RequestValidationError) as exc_info:
            BeforeOuter.model_validate({"order": {"orderId": 1, "lines": [{"qty": 1}, {"qty": "x"}]}})
        assert exc_info.value.errors() == [
            {"loc": ["body", "order", "lines", 1, "qty"], "msg": "qty is not a number", "type": "value_error"}
        ]

    def test_before_validators_of_a_json_request_body(self):
        api = BoltAPI()

        @api.post("/orders")
        async def create_order(body: BeforeOuter) -> dict:
            order = body.order
            return {"id": order.order_id, "tags": order.tags, "qty": [line.qty for line in order.lines]}

        with TestClient(api) as client:
            response = client.post("/orders", json={"order": {"id": 7, "tags": "a,b", "lines": [{"qty": "3"}]}})
            assert response.status_code == 200
            assert response.json() == {"id": 7, "tags": ["a", "b"], "qty": [3]}

            response = client.post("/orders", json={"order": {"id": 7, "lines": [{"qty": "x"}]}})
            assert response.status_code == 422
            assert response.json()["detail"] == [
                {"loc": ["body", "order", "lines", 0, "qty"], "msg": "qty is not a number", "type": "value_error"}
            ]
            # The type of the field still checks the value.
            response = client.post("/orders", json={"order": {"id": "seven"}})
            assert response.status_code == 422
            assert (
                client.post("/orders", content=b"{not json", headers={"content-type": "application/json"}).status_code
                == 422
            )

    @pytest.mark.parametrize("mode", ["wrap", "plain", "middle"])
    def test_mode_that_is_not_supported_raises_at_class_creation(self, mode):
        with pytest.raises(ValueError, match=f"mode={mode!r} is not supported"):

            class WithFieldValidator(Serializer):
                v: int

                @field_validator("v", mode=mode)
                @classmethod
                def check(cls, value):
                    return value

        with pytest.raises(ValueError, match=f"mode={mode!r} is not supported"):

            class WithModelValidator(Serializer):
                v: int

                @model_validator(mode=mode)
                def check(self):
                    return self


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

    def test_config_validators_is_not_an_option(self):
        """Nothing ran Config.validators. A class that sets it is an error, not a class with no validation."""

        def reject(cls, value):
            raise ValueError("never called")

        with pytest.raises(TypeError, match=r"WithValidators\.Config\.validators is not an option.*@field_validator"):

            class WithValidators(Serializer):
                username: str

                class Config:
                    validators = {"username": [reject]}

        with pytest.raises(TypeError, match=r"Child\.Config\.validators is not an option"):

            class Parent(Serializer):
                username: str

                class Config:
                    write_only = {"username"}

            class Child(Parent):
                class Config(Parent.Config):
                    validators = {}

        assert Parent(username="a").dump() == {}
        assert not hasattr(Serializer.Config, "validators")


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


class _Address(Serializer):
    city: str
    zip: str


class _Person(Serializer):
    name: str
    address: _Address


class _Order(Serializer):
    items: list[_Address]


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

    def test_error_list_has_one_entry_for_each_bad_field_and_none_for_an_absent_optional_field(self):
        class Signup(Serializer):
            nickname: str = "anon"
            age: int
            billing: _Address
            address: _Address
            name: str
            city: str = "x"

        with pytest.raises(RequestValidationError) as exc_info:
            Signup.model_validate({"age": [], "billing": {"city": 1, "zip": "z"}, "address": {"city": "c"}})

        assert exc_info.value.errors() == [
            {"loc": ("body", "age"), "msg": "Expected `int`, got `array`", "type": "validation_error"},
            {
                "loc": ("body", "billing", "city"),
                "msg": "Expected `str`, got `int` - at `$.city`",
                "type": "validation_error",
            },
            {"loc": ("body", "address", "zip"), "msg": "Object missing required field `zip`", "type": "missing"},
            {"loc": ("body", "name"), "msg": "Object missing required field `name`", "type": "missing"},
        ]

    def test_validator_errors_of_a_dict_are_the_errors_of_the_same_json(self):
        class Signup(Serializer):
            email: str
            age: int

            @field_validator("email")
            def check_email(cls, value):
                if "@" not in value:
                    raise ValueError("Invalid email")
                return value

            @model_validator
            def check_age(self):
                if self.age < 0:
                    raise ValueError("Age is negative")

        with pytest.raises(RequestValidationError) as from_json:
            Signup.model_validate_json(b'{"email": "x", "age": -1}')
        with pytest.raises(RequestValidationError) as from_dict:
            Signup.model_validate({"email": "x", "age": -1})

        assert from_dict.value.errors() == from_json.value.errors()
        assert from_dict.value.errors() == [
            {"loc": ["body", "email"], "msg": "Invalid email", "type": "value_error"},
            {"loc": ["body"], "msg": "Age is negative", "type": "value_error"},
        ]

    def test_nested_missing_field_has_the_type_of_a_top_level_one_and_its_full_loc(self):
        with pytest.raises(RequestValidationError) as exc_info:
            _Person.model_validate({"name": "x", "address": {"city": "a"}})

        assert exc_info.value.errors() == [
            {"loc": ("body", "address", "zip"), "msg": "Object missing required field `zip`", "type": "missing"}
        ]

    def test_missing_field_in_a_list_item_has_the_index_in_its_loc(self):
        with pytest.raises(RequestValidationError) as exc_info:
            _Order.model_validate_json(b'{"items": [{"city": "a", "zip": "1"}, {"city": "b"}]}')

        assert exc_info.value.errors() == [
            {"loc": ("body", "items", "1", "zip"), "msg": "Object missing required field `zip`", "type": "missing"}
        ]

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
