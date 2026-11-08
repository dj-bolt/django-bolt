"""
Test JWT Token class for Django-Bolt.

Tests the Token dataclass for encoding/decoding JWTs with performance focus.
"""

import time
from datetime import UTC, datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from django_bolt import BoltAPI
from django_bolt.auth import JWTAuthentication, Requires, Token
from django_bolt.testing import TestClient


class TestTokenCreation:
    """Test Token creation and validation"""

    def test_create_basic_token(self):
        """Test creating a basic token"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="user123",
            exp=exp,
        )

        assert token.sub == "user123"
        assert token.exp == exp.replace(microsecond=0)  # Microseconds stripped
        assert token.iat <= datetime.now(UTC)

    def test_create_token_with_staff_flags(self):
        """Test creating token with staff/admin flags"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="admin123",
            exp=exp,
            is_staff=True,
            is_admin=True,
        )

        assert token.is_staff is True
        assert token.is_admin is True

    def test_create_token_with_permissions(self):
        """Test creating token with permissions list"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="user123",
            exp=exp,
            permissions=["users.view", "users.create", "posts.edit"],
        )

        assert len(token.permissions) == 3
        assert "users.view" in token.permissions

    def test_create_token_with_audience_issuer(self):
        """Test creating token with aud and iss claims"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="user123",
            exp=exp,
            aud="my-api",
            iss="auth-service",
        )

        assert token.aud == "my-api"
        assert token.iss == "auth-service"

    def test_create_token_with_extras(self):
        """Test creating token with custom extra claims"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="user123",
            exp=exp,
            extras={
                "tenant_id": "acme-corp",
                "role": "manager",
                "department": "engineering",
            },
        )

        assert token.extras["tenant_id"] == "acme-corp"
        assert token.extras["role"] == "manager"

    def test_create_factory_method(self):
        """Test Token.create() factory method"""
        token = Token.create(
            sub="user123",
            expires_delta=timedelta(minutes=30),
            issuer="my-service",
            is_staff=True,
            permissions=["read", "write"],
            tenant="acme",
        )

        assert token.sub == "user123"
        assert token.iss == "my-service"
        assert token.is_staff is True
        assert token.permissions == ["read", "write"]
        assert token.extras["tenant"] == "acme"

        # Check expiration is ~30 minutes from now
        now = datetime.now(UTC)
        exp_delta = (token.exp - now).total_seconds()
        assert 1790 < exp_delta < 1810  # ~30 minutes with some tolerance


class TestTokenValidation:
    """Test Token validation logic"""

    def test_empty_subject_raises_error(self):
        """Test that empty subject raises ValueError"""
        exp = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(ValueError, match="sub.*must be.*non-empty"):
            Token(sub="", exp=exp)

    def test_past_expiration_raises_error(self):
        """Test that past expiration raises ValueError"""
        exp = datetime.now(UTC) - timedelta(hours=1)

        with pytest.raises(ValueError, match="exp.*must be in the future"):
            Token(sub="user123", exp=exp)

    def test_future_iat_raises_error(self):
        """Test that future iat raises ValueError"""
        exp = datetime.now(UTC) + timedelta(hours=2)
        iat = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(ValueError, match="iat.*must be current or past"):
            Token(sub="user123", exp=exp, iat=iat)

    def test_datetime_normalization(self):
        """Test that datetimes are normalized to UTC without microseconds"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        exp_with_micros = exp.replace(microsecond=123456)

        token = Token(sub="user123", exp=exp_with_micros)

        # Should have microseconds stripped
        assert token.exp.microsecond == 0
        assert token.iat.microsecond == 0


class TestTokenEncoding:
    """Test Token encoding to JWT strings"""

    def test_encode_basic_token(self):
        """Test encoding a basic token"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp)

        secret = "test-secret"
        encoded = token.encode(secret=secret)

        # Should be a valid JWT string (3 parts separated by dots)
        assert isinstance(encoded, str)
        parts = encoded.split(".")
        assert len(parts) == 3

        # Decode to verify contents
        decoded = pyjwt.decode(encoded, secret, algorithms=["HS256"])
        assert decoded["sub"] == "user123"
        assert "exp" in decoded
        assert "iat" in decoded

    def test_encode_with_claims(self):
        """Test encoding token with all claims"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="user123",
            exp=exp,
            is_staff=True,
            is_admin=False,
            permissions=["read", "write"],
            aud="my-api",
            iss="auth-service",
        )

        secret = "test-secret"
        encoded = token.encode(secret=secret)

        decoded = pyjwt.decode(encoded, secret, algorithms=["HS256"], audience="my-api", issuer="auth-service")

        assert decoded["sub"] == "user123"
        assert decoded["is_staff"] is True
        assert decoded["is_admin"] is False
        assert decoded["permissions"] == ["read", "write"]
        assert decoded["aud"] == "my-api"
        assert decoded["iss"] == "auth-service"

    def test_encode_with_different_algorithms(self):
        """Test encoding with different algorithms"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp)

        for algorithm in ["HS256", "HS384", "HS512"]:
            secret = "test-secret-for-" + algorithm
            encoded = token.encode(secret=secret, algorithm=algorithm)

            decoded = pyjwt.decode(encoded, secret, algorithms=[algorithm])
            assert decoded["sub"] == "user123"

    def test_encode_with_custom_headers(self):
        """Test encoding with custom JWT headers"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp)

        secret = "test-secret"
        headers = {"kid": "key-id-123", "typ": "JWT"}
        encoded = token.encode(secret=secret, headers=headers)

        # Decode without verification to check headers
        unverified = pyjwt.get_unverified_header(encoded)
        assert unverified["kid"] == "key-id-123"


class TestTokenDecoding:
    """Test Token decoding from JWT strings"""

    def test_decode_basic_token(self):
        """Test decoding a basic token"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        original = Token(sub="user123", exp=exp)

        secret = "test-secret"
        encoded = original.encode(secret=secret)

        # Decode back
        decoded = Token.decode(encoded, secret=secret)

        assert decoded.sub == "user123"
        assert decoded.exp.timestamp() == pytest.approx(original.exp.timestamp(), abs=1)
        assert decoded.iat.timestamp() == pytest.approx(original.iat.timestamp(), abs=1)

    def test_decode_with_all_claims(self):
        """Test decoding token with all claims"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        original = Token(
            sub="admin123",
            exp=exp,
            is_staff=True,
            is_admin=True,
            permissions=["full-access"],
            aud="my-api",
            iss="auth-service",
            jti="unique-id-123",
        )

        secret = "test-secret"
        encoded = original.encode(secret=secret)

        decoded = Token.decode(encoded, secret=secret, audience="my-api", issuer="auth-service")

        assert decoded.sub == "admin123"
        assert decoded.is_staff is True
        assert decoded.is_admin is True
        assert decoded.permissions == ["full-access"]
        assert decoded.aud == "my-api"
        assert decoded.iss == "auth-service"
        assert decoded.jti == "unique-id-123"

    def test_decode_with_extras(self):
        """Test decoding token with extra claims"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        original = Token(sub="user123", exp=exp, extras={"tenant": "acme", "role": "admin", "level": 5})

        secret = "test-secret"
        encoded = original.encode(secret=secret)

        decoded = Token.decode(encoded, secret=secret)

        assert decoded.extras["tenant"] == "acme"
        assert decoded.extras["role"] == "admin"
        assert decoded.extras["level"] == 5

    def test_decode_expired_token_raises_error(self):
        """Test that decoding expired token raises ValueError"""
        # Create token that will be expired
        payload = {
            "sub": "user123",
            "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
            "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
        }

        secret = "test-secret"
        encoded = pyjwt.encode(payload, secret, algorithm="HS256")

        with pytest.raises(ValueError, match="Failed to decode token"):
            Token.decode(encoded, secret=secret)

    def test_decode_with_wrong_secret_raises_error(self):
        """Test that decoding with wrong secret raises ValueError"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp)

        encoded = token.encode(secret="secret1")

        with pytest.raises(ValueError, match="Failed to decode token"):
            Token.decode(encoded, secret="wrong-secret")

    def test_decode_with_audience_mismatch_raises_error(self):
        """Test that audience mismatch raises ValueError"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp, aud="api1")

        secret = "test-secret"
        encoded = token.encode(secret=secret)

        with pytest.raises(ValueError, match="Failed to decode token"):
            Token.decode(encoded, secret=secret, audience="wrong-audience")

    def test_decode_skip_expiry_verification(self):
        """Test decoding with expiry verification disabled"""
        # Create expired token
        payload = {
            "sub": "user123",
            "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
            "iat": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
        }

        secret = "test-secret"
        encoded = pyjwt.encode(payload, secret, algorithm="HS256")

        # Should succeed with verify_exp=False
        decoded = Token.decode(encoded, secret=secret, verify_exp=False)
        assert decoded.sub == "user123"


class TestTokenToDictConversion:
    """Test Token.to_dict() conversion"""

    def test_to_dict_basic(self):
        """Test converting token to dict"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp)

        d = token.to_dict()

        assert d["sub"] == "user123"
        assert isinstance(d["exp"], int)  # Unix timestamp
        assert isinstance(d["iat"], int)
        assert d["exp"] > d["iat"]

    def test_to_dict_with_optional_fields(self):
        """Test to_dict with optional fields"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="user123",
            exp=exp,
            aud="my-api",
            iss="auth",
            jti="unique",
            is_staff=True,
        )

        d = token.to_dict()

        assert d["aud"] == "my-api"
        assert d["iss"] == "auth"
        assert d["jti"] == "unique"
        assert d["is_staff"] is True

    def test_to_dict_excludes_none_values(self):
        """Test that None values are excluded from dict"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(
            sub="user123",
            exp=exp,
            aud=None,
            iss=None,
        )

        d = token.to_dict()

        assert "aud" not in d
        assert "iss" not in d

    def test_to_dict_includes_extras(self):
        """Test that extras are included in dict"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp, extras={"custom1": "value1", "custom2": 42})

        d = token.to_dict()

        assert d["custom1"] == "value1"
        assert d["custom2"] == 42


class TestTokenRoundTrip:
    """Test encoding and decoding round-trip"""

    def test_round_trip_preserves_data(self):
        """Test that encode->decode preserves all data"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        original = Token(
            sub="user123",
            exp=exp,
            is_staff=True,
            is_admin=False,
            permissions=["read", "write", "delete"],
            aud="my-api",
            iss="auth-service",
            extras={"tenant": "acme", "role": "manager"},
        )

        secret = "test-secret"
        encoded = original.encode(secret=secret)
        decoded = Token.decode(encoded, secret=secret, audience="my-api", issuer="auth-service")

        # Compare all fields (timestamps may differ slightly due to rounding)
        assert decoded.sub == original.sub
        assert decoded.exp.timestamp() == pytest.approx(original.exp.timestamp(), abs=1)
        assert decoded.is_staff == original.is_staff
        assert decoded.is_admin == original.is_admin
        assert decoded.permissions == original.permissions
        assert decoded.aud == original.aud
        assert decoded.iss == original.iss
        assert decoded.extras == original.extras

    def test_multiple_round_trips(self):
        """Test multiple encode/decode cycles"""
        exp = datetime.now(UTC) + timedelta(hours=1)
        token = Token(sub="user123", exp=exp, is_staff=True)

        secret = "test-secret"

        # Multiple round trips
        for _ in range(3):
            encoded = token.encode(secret=secret)
            token = Token.decode(encoded, secret=secret)

            assert token.sub == "user123"
            assert token.is_staff is True


@pytest.fixture(params=["JST-9", "PST8"])
def local_timezone(request, monkeypatch):
    """Set the local time zone of the process to one that is not UTC.

    JST-9 is nine hours ahead of UTC. PST8 is eight hours behind.
    A Token must use UTC and must not use the local time zone.
    """
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


def _all_claims_token(**overrides) -> Token:
    """Build a token that sets every standard claim, with a past ``iat`` and a past ``nbf``."""
    now = datetime.now(UTC)
    fields = {
        "sub": "claims-user",
        "exp": now + timedelta(hours=5),
        "iat": now - timedelta(hours=2),
        "nbf": now - timedelta(hours=1),
        "iss": "auth-service",
        "aud": "my-api",
        "jti": "jti-1",
        "is_staff": True,
        "is_superuser": True,
        "is_admin": False,
        "permissions": ["read"],
    }
    fields.update(overrides)
    return Token(**fields)


class TestTokenClaimValidation:
    """Token checks its own claims when it is built."""

    def test_none_subject_raises_value_error(self):
        """A subject of None gives a ValueError, not a TypeError."""
        with pytest.raises(ValueError, match=r"^sub \(subject\) must be a non-empty string$"):
            Token(sub=None, exp=datetime.now(UTC) + timedelta(hours=1))

    def test_one_character_subject_is_valid(self):
        """A subject of one character is not empty."""
        token = Token(sub="a", exp=datetime.now(UTC) + timedelta(hours=1))

        assert token.sub == "a"

    def test_expiry_at_the_current_second_raises_error(self):
        """An expiry in the current second is not in the future."""
        # Start in the first half of a second. Then the stripped microseconds stay in one second.
        while datetime.now(UTC).microsecond > 500_000:
            time.sleep(0.01)

        with pytest.raises(ValueError, match=r"^exp \(expiration\) must be in the future$"):
            Token(sub="user123", exp=datetime.now(UTC))

    def test_expiry_that_is_not_a_datetime_raises_error(self):
        with pytest.raises(ValueError, match=r"^exp must be a datetime object$"):
            Token(sub="user123", exp="tomorrow")

    def test_future_iat_message(self):
        with pytest.raises(ValueError, match=r"^iat \(issued at\) must be current or past time$"):
            Token(
                sub="user123",
                exp=datetime.now(UTC) + timedelta(hours=2),
                iat=datetime.now(UTC) + timedelta(hours=1),
            )

    def test_nbf_is_normalized_to_utc_without_microseconds(self):
        """``nbf`` follows the same rules as ``exp``: UTC, with no microseconds."""
        offset = timezone(timedelta(hours=5))
        nbf = datetime.now(offset).replace(microsecond=123456)

        token = Token(sub="user123", exp=datetime.now(UTC) + timedelta(hours=1), nbf=nbf)

        assert token.nbf == nbf.replace(microsecond=0)
        assert token.nbf.utcoffset() == timedelta(0)
        assert token.nbf.microsecond == 0


class TestTokenLocalTimezone:
    """Token claims do not change when the local time zone is not UTC."""

    def test_aware_datetime_keeps_the_same_instant(self, local_timezone):
        exp = datetime(2099, 1, 1, 12, 0, 0, 999, tzinfo=timezone(timedelta(hours=5)))

        token = Token(sub="user123", exp=exp)

        assert token.exp == exp.replace(microsecond=0)
        assert token.exp.utcoffset() == timedelta(0)

    def test_naive_datetime_is_read_as_utc(self, local_timezone):
        token = Token(sub="user123", exp=datetime(2099, 1, 1, 12, 0, 0))

        assert token.exp == datetime(2099, 1, 1, 12, 0, 0, tzinfo=UTC)

    def test_new_token_with_default_iat_is_valid(self, local_timezone):
        """The check of ``exp`` and ``iat`` compares UTC times with UTC times."""
        token = Token(sub="user123", exp=datetime.now(UTC) + timedelta(hours=1))

        assert abs(token.iat.timestamp() - time.time()) < 5

    def test_create_uses_utc_now(self, local_timezone):
        token = Token.create(sub="user123", expires_delta=timedelta(minutes=30))

        assert abs(token.iat.timestamp() - time.time()) < 5
        assert abs(token.exp.timestamp() - (time.time() + 1800)) < 5

    def test_decode_restores_the_same_instants(self, local_timezone):
        original = _all_claims_token()
        encoded = original.encode(secret="test-secret")

        decoded = Token.decode(encoded, secret="test-secret", audience="my-api", issuer="auth-service")

        assert decoded.exp.timestamp() == int(original.exp.timestamp())
        assert decoded.iat.timestamp() == int(original.iat.timestamp())
        assert decoded.nbf.timestamp() == int(original.nbf.timestamp())


class TestTokenCreateDefaults:
    """Token.create() sets safe defaults."""

    def test_defaults_give_no_staff_and_no_admin(self):
        token = Token.create(sub="user123")

        assert token.is_staff is False
        assert token.is_admin is False

    def test_default_lifetime_is_one_hour(self):
        token = Token.create(sub="user123")

        assert token.exp - token.iat == timedelta(hours=1)
        assert abs(token.iat.timestamp() - time.time()) < 5

    def test_audience_is_set(self):
        token = Token.create(sub="user123", audience="my-api")

        assert token.aud == "my-api"

    def test_admin_implies_staff(self):
        """An admin is also staff. A staff user is not an admin."""
        admin = Token.create(sub="user123", is_admin=True)
        assert (admin.is_staff, admin.is_admin) == (True, True)
        staff = Token.create(sub="user123", is_staff=True)
        assert (staff.is_staff, staff.is_admin) == (True, False)
        plain = Token.create(sub="user123")
        assert (plain.is_staff, plain.is_admin) == (False, False)

    def test_a_staff_token_does_not_pass_requires_is_admin(self):
        """Requires("is_admin") refuses a staff token. Requires("is_staff") allows a staff token and an admin token."""
        secret = "admin-implies-staff-secret"
        api = BoltAPI()

        @api.get("/admin-only", auth=[JWTAuthentication(secret=secret)], guards=[Requires("is_admin")])
        async def admin_only():
            return {"ok": True}

        @api.get("/staff-only", auth=[JWTAuthentication(secret=secret)], guards=[Requires("is_staff")])
        async def staff_only():
            return {"ok": True}

        staff = Token.create(sub="1", is_staff=True).encode(secret)
        admin = Token.create(sub="1", is_admin=True).encode(secret)
        with TestClient(api) as client:
            assert client.get("/admin-only", headers={"Authorization": f"Bearer {staff}"}).status_code == 403
            assert client.get("/admin-only", headers={"Authorization": f"Bearer {admin}"}).status_code == 200
            # An admin is also staff.
            assert client.get("/staff-only", headers={"Authorization": f"Bearer {admin}"}).status_code == 200
            assert client.get("/staff-only", headers={"Authorization": f"Bearer {staff}"}).status_code == 200


class TestTokenToDictClaims:
    """to_dict() writes each claim that is set."""

    def test_nbf_and_is_superuser_are_written(self):
        token = _all_claims_token()

        claims = token.to_dict()

        assert claims["nbf"] == int(token.nbf.timestamp())
        assert claims["is_superuser"] is True
        assert "is_superuser" not in _all_claims_token(is_superuser=None).to_dict()
        assert _all_claims_token(is_superuser=False).to_dict()["is_superuser"] is False


class TestTokenEncodeErrors:
    def test_unknown_algorithm_raises_value_error_with_reason(self):
        token = Token(sub="user123", exp=datetime.now(UTC) + timedelta(hours=1))

        with pytest.raises(ValueError, match=r"^Failed to encode token: .*[Aa]lgorithm"):
            token.encode(secret="test-secret", algorithm="BOGUS")


class TestTokenDecodeChecks:
    """Token.decode() applies the checks that the caller asks for."""

    SECRET = "test-secret"

    def _encode(self, **claims) -> str:
        now = int(time.time())
        payload = {"sub": "user123", "exp": now + 7200, "iat": now - 60}
        payload.update(claims)
        return pyjwt.encode(payload, self.SECRET, algorithm="HS256")

    def test_future_nbf_is_rejected_by_default(self):
        encoded = self._encode(nbf=int(time.time()) + 3600)

        with pytest.raises(ValueError, match="Failed to decode token"):
            Token.decode(encoded, secret=self.SECRET)

    def test_future_nbf_is_accepted_when_nbf_check_is_off(self):
        nbf = int(time.time()) + 3600
        encoded = self._encode(nbf=nbf)

        decoded = Token.decode(encoded, secret=self.SECRET, verify_nbf=False)

        assert decoded.nbf.timestamp() == nbf

    def test_audience_claim_is_ignored_when_no_audience_is_asked(self):
        encoded = self._encode(aud="my-api")

        decoded = Token.decode(encoded, secret=self.SECRET)

        assert decoded.aud == "my-api"

    def test_empty_audience_and_issuer_ask_for_no_check(self):
        """An empty string works as no value. A token without ``aud`` and ``iss`` stays valid."""
        encoded = self._encode()

        decoded = Token.decode(encoded, secret=self.SECRET, audience="", issuer="")

        assert decoded.sub == "user123"

    def test_wrong_issuer_is_rejected(self):
        encoded = self._encode(iss="evil-service")

        with pytest.raises(ValueError, match="Failed to decode token"):
            Token.decode(encoded, secret=self.SECRET, issuer="auth-service")

    def test_matching_issuer_is_accepted(self):
        encoded = self._encode(iss="auth-service")

        assert Token.decode(encoded, secret=self.SECRET, issuer="auth-service").iss == "auth-service"

    def test_missing_iat_defaults_to_one_hour_before_exp(self):
        now = int(time.time())
        encoded = pyjwt.encode({"sub": "user123", "exp": now + 7200}, self.SECRET, algorithm="HS256")

        decoded = Token.decode(encoded, secret=self.SECRET)

        assert decoded.iat.timestamp() == now + 7200 - 3600

    def test_iat_claim_wins_over_the_default(self):
        now = int(time.time())
        encoded = self._encode(iat=now - 30)

        assert Token.decode(encoded, secret=self.SECRET).iat.timestamp() == now - 30

    def test_standard_claims_are_not_extras(self):
        """Each standard claim goes to its own field. Only custom claims go to ``extras``."""
        original = _all_claims_token(extras={"tenant": "acme"})
        encoded = original.encode(secret=self.SECRET)

        decoded = Token.decode(encoded, secret=self.SECRET, audience="my-api", issuer="auth-service")

        assert decoded.extras == {"tenant": "acme"}
        assert decoded.jti == "jti-1"
        assert decoded.is_superuser is True
        assert decoded.is_staff is True
        assert decoded.is_admin is False
        assert decoded.permissions == ["read"]
        assert decoded.nbf == original.nbf
        assert decoded.iat == original.iat
        assert decoded.exp == original.exp


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
