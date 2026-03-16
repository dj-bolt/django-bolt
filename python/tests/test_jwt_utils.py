import pytest
import jwt
from django_bolt.auth.jwt_utils import extract_token_from_header, decode_jwt


def test_extract_valid_bearer_token():
    token = extract_token_from_header("Bearer mytoken123")
    assert token == "mytoken123"


def test_extract_missing_header():
    assert extract_token_from_header(None) is None


def test_extract_malformed_header():
    assert extract_token_from_header("InvalidHeader") is None
    assert extract_token_from_header("Bearer") is None
    assert extract_token_from_header("Token abc123") is None


def test_decode_jwt_invalid_token():
    result = decode_jwt("invalid.token.here", secret="testsecret")
    assert result == {}


def test_decode_jwt_valid_token():
    token = jwt.encode({"sub": "123"}, "testsecret", algorithm="HS256")
    result = decode_jwt(token, secret="testsecret")
    assert result["sub"] == "123"