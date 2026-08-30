"""
Test that JWT authentication uses Django SECRET_KEY when not specified
"""

from django_bolt import BoltAPI
from django_bolt.auth import IsAuthenticated, JWTAuthentication


def test_jwt_auth_uses_django_secret_key():
    """Test that JWTAuthentication uses Django SECRET_KEY when secret not provided"""
    # Django is already configured by pytest-django with SECRET_KEY='test-secret-key-global'
    from django.conf import settings  # noqa: PLC0415

    # Create JWT auth without explicit secret
    auth = JWTAuthentication()  # No secret specified

    # Should use Django's SECRET_KEY
    assert auth.secret == settings.SECRET_KEY
    print("✓ JWTAuthentication uses Django SECRET_KEY when not specified")


def test_jwt_auth_explicit_secret_overrides():
    """Test that explicit secret overrides Django SECRET_KEY"""
    from django.conf import settings  # noqa: PLC0415

    # Create with explicit secret
    auth = JWTAuthentication(secret="custom-secret")

    # Should use the explicit secret, not Django's
    assert auth.secret == "custom-secret"
    assert auth.secret != settings.SECRET_KEY
    print("✓ Explicit secret overrides Django SECRET_KEY")


def test_route_with_django_secret():
    """Test that route-level auth uses Django SECRET_KEY"""
    from django.conf import settings  # noqa: PLC0415

    api = BoltAPI()

    @api.get(
        "/protected",
        auth=[JWTAuthentication()],  # No secret - should use Django's
        guards=[IsAuthenticated()],
    )
    async def protected_endpoint():
        return {"message": "Protected"}

    # Check that metadata has Django SECRET_KEY
    handler_id = 0
    if handler_id in api._handler_middleware:
        metadata = api._handler_middleware[handler_id]
        auth_backends = metadata.get("auth_backends", [])
        assert len(auth_backends) > 0
        assert auth_backends[0]["secret"] == settings.SECRET_KEY
        print("✓ Route-level JWT auth uses Django SECRET_KEY")


def test_global_auth_with_django_secret():
    """Test global auth configuration with Django SECRET_KEY"""
    from django.conf import settings  # noqa: PLC0415

    # Set auth classes (settings already configured by pytest-django)
    settings.BOLT_AUTHENTICATION_CLASSES = [
        JWTAuthentication()  # No secret - should use Django's
    ]

    try:
        from django_bolt.auth import get_default_authentication_classes  # noqa: PLC0415

        auth_classes = get_default_authentication_classes()
        assert len(auth_classes) > 0
        assert auth_classes[0].secret == settings.SECRET_KEY
        print("✓ Global auth configuration uses Django SECRET_KEY")
    finally:
        # Clean up to prevent leaking to other tests
        del settings.BOLT_AUTHENTICATION_CLASSES


def test_secret_key_is_read_lazily_not_at_construction():
    """A backend built before SECRET_KEY is final must verify with the final key.

    Split settings files often set SECRET_KEY after BOLT_AUTHENTICATION_CLASSES.
    """
    from django.test import override_settings  # noqa: PLC0415

    from django_bolt.auth import create_jwt_for_user  # noqa: PLC0415
    from django_bolt.testing import TestClient  # noqa: PLC0415

    auth = JWTAuthentication()  # Constructed while SECRET_KEY is still the placeholder

    class User:
        id = 1
        username = "late"
        is_staff = False
        is_superuser = False
        is_active = True

    with override_settings(SECRET_KEY="late-final-secret"):
        assert auth.secret == "late-final-secret"
        assert auth.to_metadata()["secret"] == "late-final-secret"

        api = BoltAPI()

        @api.get("/protected", auth=[auth], guards=[IsAuthenticated()])
        async def protected_endpoint():
            return {"ok": True}

        token = create_jwt_for_user(User())
        with TestClient(api) as client:
            assert client.get("/protected", headers={"Authorization": f"Bearer {token}"}).status_code == 200


if __name__ == "__main__":
    test_jwt_auth_uses_django_secret_key()
    test_jwt_auth_explicit_secret_overrides()
    test_route_with_django_secret()
    test_global_auth_with_django_secret()

    print("\n✅ All Django SECRET_KEY integration tests passed!")
