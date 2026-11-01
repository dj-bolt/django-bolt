"""
Comprehensive tests for JSON parsing and msgspec validation in production/debug modes.

Tests cover:
- Invalid JSON body parsing
- msgspec struct validation failures
- Error responses in production vs debug mode
- Request body validation errors
- Type coercion and conversion
"""

import json
from typing import Annotated

import msgspec
import pytest

from django_bolt import BoltAPI
from django_bolt._kwargs import create_body_extractor, get_msgspec_decoder
from django_bolt._kwargs.extractors import _DECODER_CACHE
from django_bolt.error_handlers import handle_exception, msgspec_validation_error_to_dict
from django_bolt.exceptions import RequestValidationError, parse_msgspec_decode_error
from django_bolt.params import Body
from django_bolt.testing import TestClient


class UserCreate(msgspec.Struct):
    """Test user creation struct."""

    name: str
    email: str
    age: int


class UserWithDefaults(msgspec.Struct):
    """Test struct with default values."""

    name: str
    email: str = "user@example.com"
    is_active: bool = True


class NestedAddress(msgspec.Struct):
    """Nested struct for testing."""

    street: str
    city: str
    zipcode: str


class UserWithNested(msgspec.Struct):
    """Struct with nested struct."""

    name: str
    address: NestedAddress


class _Address(msgspec.Struct):
    city: str
    zip: str


class _Person(msgspec.Struct):
    name: str
    address: _Address


class _Company(msgspec.Struct):
    name: str
    owner: _Person


class _Order(msgspec.Struct):
    name: str
    items: list[_Address]


class TestNestedValidationErrorBodies:
    """The 422 body of a nested error has the same type and a full loc as a top-level error."""

    @staticmethod
    def _post(path: str, body: dict) -> list[dict]:
        api = BoltAPI()

        @api.post("/people")
        async def people(person: _Person):
            return {"name": person.name}

        @api.post("/companies")
        async def companies(company: _Company):
            return {"name": company.name}

        @api.post("/orders")
        async def orders(order: _Order):
            return {"name": order.name}

        with TestClient(api) as client:
            response = client.post(path, json=body)
        assert response.status_code == 422, response.text
        return response.json()["detail"]

    def test_top_level_missing_field_is_unchanged(self):
        assert self._post("/people", {"address": {"city": "a", "zip": "1"}}) == [
            {"loc": ["body", "name"], "msg": "Object missing required field `name`", "type": "missing_field"}
        ]

    def test_nested_missing_field_has_the_missing_field_type_and_its_full_loc(self):
        assert self._post("/people", {"name": "x", "address": {"city": "a"}}) == [
            {"loc": ["body", "address", "zip"], "msg": "Object missing required field `zip`", "type": "missing_field"}
        ]

    def test_doubly_nested_missing_field(self):
        body = {"name": "c", "owner": {"name": "o", "address": {"zip": "1"}}}
        assert self._post("/companies", body) == [
            {
                "loc": ["body", "owner", "address", "city"],
                "msg": "Object missing required field `city`",
                "type": "missing_field",
            }
        ]

    def test_missing_field_in_a_list_item_names_the_index(self):
        body = {"name": "x", "items": [{"city": "a", "zip": "1"}, {"city": "b"}]}
        assert self._post("/orders", body) == [
            {
                "loc": ["body", "items", "1", "zip"],
                "msg": "Object missing required field `zip`",
                "type": "missing_field",
            }
        ]

    def test_nested_type_mismatch_is_unchanged(self):
        assert self._post("/people", {"name": "x", "address": {"city": 5, "zip": "1"}}) == [
            {"loc": ["body", "address", "city"], "msg": "Expected `str`, got `int`", "type": "validation_error"}
        ]

    def test_type_mismatch_in_a_list_item_names_the_index(self):
        body = {"name": "x", "items": [{"city": "a", "zip": 1}]}
        assert self._post("/orders", body) == [
            {"loc": ["body", "items", "0", "zip"], "msg": "Expected `str`, got `int`", "type": "validation_error"}
        ]


class TestInvalidJSONParsing:
    """Test invalid JSON body parsing and error handling."""

    def test_invalid_json_syntax_returns_422(self):
        """Test that malformed JSON returns 422 with proper error."""
        extractor = create_body_extractor("user", UserCreate)

        # Invalid JSON syntax
        invalid_json = b'{name: "test", email: "test@example.com"}'  # Missing quotes

        # Should convert DecodeError to RequestValidationError (422)
        with pytest.raises(RequestValidationError) as exc_info:
            extractor(invalid_json)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["type"] == "json_invalid"
        # loc is a tuple: ("body", line_num, col_num) when byte position is available
        assert errors[0]["loc"][0] == "body"
        assert "malformed" in errors[0]["msg"].lower() or "keys must be strings" in errors[0]["msg"].lower()

    def test_empty_json_body_returns_422(self):
        """Test that empty JSON body returns 422."""
        extractor = create_body_extractor("user", UserCreate)

        # Should convert DecodeError to RequestValidationError (422)
        with pytest.raises(RequestValidationError) as exc_info:
            extractor(b"")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["type"] == "json_invalid"
        # loc is a tuple: ("body",) when no byte position is available
        assert errors[0]["loc"] == ("body",)
        assert "truncated" in errors[0]["msg"].lower()

    def test_non_json_content_returns_422(self):
        """Test that non-JSON content returns 422."""
        extractor = create_body_extractor("user", UserCreate)

        # Plain text instead of JSON
        # Should convert DecodeError to RequestValidationError (422)
        with pytest.raises(RequestValidationError) as exc_info:
            extractor(b"this is not json")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["type"] == "json_invalid"
        # loc is a tuple: ("body", line_num, col_num) when byte position is available
        assert errors[0]["loc"][0] == "body"
        assert "malformed" in errors[0]["msg"].lower() or "invalid" in errors[0]["msg"].lower()

    def test_invalid_json_object_type(self):
        """Test that JSON with wrong root type fails validation."""
        extractor = create_body_extractor("user", UserCreate)

        # Array instead of object
        with pytest.raises(msgspec.ValidationError):
            extractor(b'["name", "email"]')

        # String instead of object
        with pytest.raises(msgspec.ValidationError):
            extractor(b'"just a string"')

        # Number instead of object
        with pytest.raises(msgspec.ValidationError):
            extractor(b"42")


class TestBodyOfNonStructType:
    """A body with a type that is not a Struct is decoded as the type, with the same errors."""

    def test_valid_json_is_decoded_as_the_type(self):
        extractor = create_body_extractor("ids", list[int])

        assert extractor(b"[1, 2]") == [1, 2]

    def test_invalid_json_gives_a_422_error_with_the_body(self):
        extractor = create_body_extractor("ids", list[int])

        with pytest.raises(RequestValidationError) as exc_info:
            extractor(b"[1,")

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert errors[0]["type"] == "json_invalid"
        assert errors[0]["loc"] == ("body",)
        assert "truncated" in errors[0]["msg"].lower()
        assert exc_info.value.body == b"[1,"

    def test_malformed_json_gives_the_line_and_column(self):
        extractor = create_body_extractor("ids", list[int])

        with pytest.raises(RequestValidationError) as exc_info:
            extractor(b"[1,\n 2, x]")

        error = exc_info.value.errors()[0]
        assert error["loc"] == ("body", 2, 5)
        assert error["ctx"]["line"] == 2
        assert error["ctx"]["column"] == 5

    def test_a_value_of_the_wrong_type_raises_the_msgspec_error(self):
        extractor = create_body_extractor("ids", list[int])

        with pytest.raises(msgspec.ValidationError):
            extractor(b'["a"]')

    def test_an_annotation_that_cannot_be_hashed_still_checks_the_type(self):
        """Metadata such as a dict makes the annotation unhashable. The extractor still validates the body."""
        extractor = create_body_extractor("ids", Annotated[list[int], {"note": "unhashable"}])

        assert extractor(b"[1, 2]") == [1, 2]
        with pytest.raises(msgspec.ValidationError):
            extractor(b'["a"]')

    def test_invalid_json_for_a_struct_keeps_the_body_in_the_error(self):
        extractor = create_body_extractor("user", UserCreate)

        with pytest.raises(RequestValidationError) as exc_info:
            extractor(b'{"name": ')

        assert exc_info.value.body == b'{"name": '

    def test_a_route_binds_a_list_body_and_answers_a_bad_body_with_a_422(self):
        api = BoltAPI()

        @api.post("/ids")
        async def ids(values: Annotated[list[int], Body()]):
            return {"values": values}

        with TestClient(api) as client:
            ok = client.post("/ids", content=b"[1, 2]", headers={"content-type": "application/json"})
            assert ok.status_code == 200, ok.text
            assert ok.json() == {"values": [1, 2]}

            truncated = client.post("/ids", content=b"[1,", headers={"content-type": "application/json"})
            assert truncated.status_code == 422
            assert truncated.json()["detail"][0]["type"] == "json_invalid"
            assert truncated.json()["detail"][0]["loc"] == ["body"]

            wrong = client.post("/ids", content=b'["a"]', headers={"content-type": "application/json"})
            assert wrong.status_code == 422
            assert wrong.json()["detail"][0]["loc"] == ["body", "0"]


class TestParseMsgspecDecodeError:
    """Test the line and column of a JSON decode error."""

    @staticmethod
    def _parse(body: bytes) -> dict:
        with pytest.raises(msgspec.DecodeError) as exc_info:
            msgspec.json.decode(body)
        return parse_msgspec_decode_error(exc_info.value, body)

    def test_ascii_body_column(self):
        """Test a body with ASCII text only."""
        body = b'{"name": "hello", "x": }'
        detail = self._parse(body)

        assert detail["loc"] == ("body", 1, body.index(b"}") + 1)
        assert detail["ctx"]["line"] == 1
        assert detail["ctx"]["column"] == body.index(b"}") + 1

    def test_column_counts_characters_not_bytes(self):
        """Test multi-byte UTF-8 text before the error on the same line."""
        text = '{"name": "héllo", "x": }'
        body = text.encode()
        detail = self._parse(body)

        assert len(body) > len(text)
        assert detail["loc"] == ("body", 1, text.index("}") + 1)
        assert detail["ctx"]["column"] == text.index("}") + 1
        assert detail["ctx"]["byte_position"] == body.index(b"}")
        assert detail["msg"].endswith(f"\n  {text}\n  {' ' * text.index('}')}^")

    def test_column_on_later_line_counts_characters(self):
        """Test the line number and the column after a newline."""
        line_before_error = '  "é€": '
        body = f'{{"a": "é€",\n{line_before_error}}}'.encode()
        detail = self._parse(body)

        assert detail["loc"] == ("body", 2, len(line_before_error) + 1)
        assert detail["ctx"]["line"] == 2

    def test_multi_byte_text_on_earlier_line_does_not_shift_column(self):
        """Test that multi-byte text on an earlier line changes only the line number."""
        body = '{"a": "é",\n"b": }'.encode()
        detail = self._parse(body)

        assert detail["loc"] == ("body", 2, len('"b": ') + 1)


class TestMsgspecStructValidation:
    """Test msgspec struct validation failures."""

    def test_missing_required_field(self):
        """Test that missing required field raises ValidationError."""
        extractor = create_body_extractor("user", UserCreate)

        # Missing 'age' field
        with pytest.raises(msgspec.ValidationError) as exc_info:
            extractor(b'{"name": "John", "email": "john@example.com"}')

        # Verify error mentions the missing field
        assert "age" in str(exc_info.value).lower() or "required" in str(exc_info.value).lower()

    def test_wrong_field_type(self):
        """Test that wrong field type raises ValidationError."""
        extractor = create_body_extractor("user", UserCreate)

        # age should be int, not string
        with pytest.raises(msgspec.ValidationError):
            extractor(b'{"name": "John", "email": "john@example.com", "age": "twenty"}')

        # name should be string, not number
        with pytest.raises(msgspec.ValidationError):
            extractor(b'{"name": 123, "email": "john@example.com", "age": 20}')

    def test_null_for_required_field(self):
        """Test that null for required field raises ValidationError."""
        extractor = create_body_extractor("user", UserCreate)

        with pytest.raises(msgspec.ValidationError):
            extractor(b'{"name": null, "email": "john@example.com", "age": 20}')

    def test_extra_fields_allowed_by_default(self):
        """Test that extra fields are allowed by default in msgspec."""
        extractor = create_body_extractor("user", UserCreate)

        # Should succeed even with extra field
        result = extractor(b'{"name": "John", "email": "john@example.com", "age": 20, "extra": "field"}')
        assert result.name == "John"
        assert result.email == "john@example.com"
        assert result.age == 20

    def test_nested_struct_validation(self):
        """Test validation of nested structs."""
        extractor = create_body_extractor("user", UserWithNested)

        # Valid nested structure
        valid_json = b"""{
            "name": "John",
            "address": {
                "street": "123 Main St",
                "city": "New York",
                "zipcode": "10001"
            }
        }"""
        result = extractor(valid_json)
        assert result.name == "John"
        assert result.address.city == "New York"

        # Invalid nested structure (missing city)
        invalid_json = b"""{
            "name": "John",
            "address": {
                "street": "123 Main St",
                "zipcode": "10001"
            }
        }"""
        with pytest.raises(msgspec.ValidationError):
            extractor(invalid_json)

    def test_array_field_validation(self):
        """Test validation of array fields."""

        class UserWithTags(msgspec.Struct):
            name: str
            tags: list[str]

        extractor = create_body_extractor("user", UserWithTags)

        # Valid array
        result = extractor(b'{"name": "John", "tags": ["admin", "user"]}')
        assert result.tags == ["admin", "user"]

        # Invalid array element type
        with pytest.raises(msgspec.ValidationError):
            extractor(b'{"name": "John", "tags": ["admin", 123]}')

        # Array instead of string element
        with pytest.raises(msgspec.ValidationError):
            extractor(b'{"name": "John", "tags": [["nested"]]}')


class TestProductionVsDebugMode:
    """Test error responses in production vs debug mode."""

    def test_validation_error_in_production_mode(self):
        """Test that validation errors return 422 in production without stack traces."""
        # Simulate validation error
        exc = msgspec.ValidationError("Expected int, got str")

        # Handle in production mode (debug=False)
        status, headers, body = handle_exception(exc, debug=False)

        assert status == 422, "Validation error must return 422"

        # Parse response
        data = json.loads(body)

        # Should have validation errors
        assert "detail" in data
        assert isinstance(data["detail"], list), "Validation errors should be a list"

        # Should NOT have stack traces in production
        if "extra" in data:
            assert "traceback" not in data["extra"], "Production mode should not expose traceback"

    def test_validation_error_in_debug_mode(self):
        """Test that validation errors in debug mode may include more details."""
        # Simulate validation error
        exc = msgspec.ValidationError("Expected int, got str")

        # Handle in debug mode (debug=True)
        status, headers, body = handle_exception(exc, debug=True)

        assert status == 422, "Validation error must return 422 even in debug"

        # Response should still be JSON (not HTML for validation errors)
        headers_dict = dict(headers)
        assert headers_dict.get("content-type") == "application/json"

    def test_generic_exception_differs_by_mode(self):
        """Test that generic exceptions are handled differently in prod vs debug."""
        exc = ValueError("Something went wrong")

        # Production mode - should hide details
        prod_status, prod_headers, prod_body = handle_exception(exc, debug=False)
        assert prod_status == 500

        prod_data = json.loads(prod_body)
        assert prod_data["detail"] == "Internal Server Error", "Production should hide error details"
        assert "extra" not in prod_data, "Production should not expose exception details"

        # Debug mode - should show details (HTML or JSON with traceback)
        debug_status, debug_headers, debug_body = handle_exception(exc, debug=True)
        assert debug_status == 500

        # Debug mode returns either HTML or JSON with traceback
        debug_headers_dict = dict(debug_headers)
        if debug_headers_dict.get("content-type") == "text/html; charset=utf-8":
            # HTML error page
            html = debug_body.decode()
            assert "ValueError" in html
        else:
            # JSON with traceback
            debug_data = json.loads(debug_body)
            assert "extra" in debug_data
            assert "traceback" in debug_data["extra"]


class TestRequestValidationErrorHandling:
    """Test RequestValidationError handling."""

    def test_request_validation_error_format(self):
        """Test that RequestValidationError returns proper format."""
        errors = [
            {"loc": ["body", "email"], "msg": "Invalid email format", "type": "value_error"},
            {"loc": ["body", "age"], "msg": "Must be a positive integer", "type": "value_error"},
        ]

        exc = RequestValidationError(errors)
        status, headers, body = handle_exception(exc, debug=False)

        assert status == 422

        data = json.loads(body)

        # Should return errors in detail field
        assert "detail" in data
        assert isinstance(data["detail"], list)
        assert len(data["detail"]) == 2

        # Each error should have loc, msg, type
        for error in data["detail"]:
            assert "loc" in error
            assert "msg" in error
            assert "type" in error

    def test_request_validation_error_with_body(self):
        """Test RequestValidationError preserves request body for debugging."""
        errors = [{"loc": ["body", "name"], "msg": "Field required", "type": "missing"}]
        body = {"email": "test@example.com"}  # Missing 'name'

        exc = RequestValidationError(errors, body=body)

        # Error should store the body
        assert exc.body == body

    def test_msgspec_error_to_request_validation_error(self):
        """Test that msgspec.ValidationError is converted properly."""

        # Create a validation error
        class TestStruct(msgspec.Struct):
            name: str
            age: int

        try:
            msgspec.json.decode(b'{"name": "John", "age": "invalid"}', type=TestStruct)
        except msgspec.ValidationError as e:
            errors = msgspec_validation_error_to_dict(e)

            assert isinstance(errors, list)
            assert len(errors) > 0

            # Each error should have required fields
            for error in errors:
                assert "loc" in error
                assert "msg" in error
                assert "type" in error


class TestTypeCoercionEdgeCases:
    """Test edge cases in type coercion and conversion.

    Note: Type coercion for basic types (int, float, bool) is now done in Rust.
    The convert_primitive function has been removed - Rust handles all coercion.
    """

    def test_optional_fields_with_none(self):
        """Test that optional fields handle None correctly."""

        class UserOptional(msgspec.Struct):
            name: str
            email: str | None = None

        extractor = create_body_extractor("user", UserOptional)

        # Explicit null
        result = extractor(b'{"name": "John", "email": null}')
        assert result.name == "John"
        assert result.email is None

        # Missing optional field
        result = extractor(b'{"name": "John"}')
        assert result.name == "John"
        assert result.email is None


class TestJSONParsingPerformance:
    """Test JSON parsing performance characteristics."""

    def test_decoder_caching(self):
        """Test that msgspec decoders are cached for performance."""
        # Clear cache
        _DECODER_CACHE.clear()

        # First call should create decoder
        decoder1 = get_msgspec_decoder(UserCreate)
        assert UserCreate in _DECODER_CACHE

        # Second call should return cached decoder
        decoder2 = get_msgspec_decoder(UserCreate)
        assert decoder1 is decoder2, "Decoder should be cached"

    def test_large_json_parsing(self):
        """Test parsing of large JSON payloads."""

        class LargeStruct(msgspec.Struct):
            items: list[dict]

        extractor = create_body_extractor("data", LargeStruct)

        # Create large JSON with 1000 items
        items = [{"id": i, "name": f"item_{i}"} for i in range(1000)]
        large_json = json.dumps({"items": items}).encode()

        # Should parse successfully
        result = extractor(large_json)
        assert len(result.items) == 1000

    def test_deeply_nested_json(self):
        """Test parsing of deeply nested JSON structures."""

        class Level3(msgspec.Struct):
            value: str

        class Level2(msgspec.Struct):
            level3: Level3

        class Level1(msgspec.Struct):
            level2: Level2

        extractor = create_body_extractor("data", Level1)

        nested_json = b"""{
            "level2": {
                "level3": {
                    "value": "deep"
                }
            }
        }"""

        result = extractor(nested_json)
        assert result.level2.level3.value == "deep"


class TestIntegrationWithBoltAPI:
    """Integration tests with BoltAPI."""

    def test_api_handles_invalid_json_body(self):
        """Test that BoltAPI properly handles invalid JSON in request body."""
        api = BoltAPI()

        @api.post("/users")
        async def create_user(user: UserCreate):
            return {"id": 1, "name": user.name}

        # The route should be registered
        assert len(api._routes) == 1

    def test_api_validation_error_response(self):
        """Test that API returns proper validation error response."""
        api = BoltAPI()

        @api.post("/users")
        async def create_user(user: UserCreate):
            return {"id": 1, "name": user.name}

        # Simulate request with missing field
        # This would normally be caught during binding
        # Here we test the error handler behavior
        errors = [{"loc": ["body", "age"], "msg": "Field required", "type": "missing"}]
        exc = RequestValidationError(errors)

        status, headers, body = handle_exception(exc, debug=False)
        assert status == 422

        data = json.loads(body)
        assert len(data["detail"]) == 1
        assert data["detail"][0]["loc"] == ["body", "age"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
