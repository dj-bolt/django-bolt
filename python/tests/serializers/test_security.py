"""
Security tests for Django-Bolt serializers.

Tests cover:
- Circular reference detection in from_model()
- Performance optimizations
"""

from __future__ import annotations

from django_bolt.serializers import Serializer


class TestTypeHintResolutionEdgeCases:
    """Test edge cases in type hint resolution."""

    def test_module_level_serializer(self):
        """Test that module-level serializers resolve type hints correctly."""

        class SimpleSerializer(Serializer):
            id: int
            name: str

        # Should have cached type hints
        assert "id" in SimpleSerializer.__cached_type_hints__
        assert "name" in SimpleSerializer.__cached_type_hints__

    def test_function_scoped_serializer_warning(self):
        """Test that function-scoped serializers still work (with limitations)."""

        def create_serializer():
            class LocalSerializer(Serializer):
                id: int
                name: str

            return LocalSerializer

        LocalSerializer = create_serializer()

        # Should still work, even if type hints have limitations
        instance = LocalSerializer(id=1, name="Test")
        assert instance.id == 1
        assert instance.name == "Test"
