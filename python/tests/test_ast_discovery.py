from __future__ import annotations

import importlib
import sys
import textwrap
import types
from pathlib import Path

import pytest

from django_bolt.management.commands.runbolt import find_bolt_api_names


def _write_module(tmp_path: Path, module_name: str, source: str) -> str:
    """Write *source* to a temporary .py file and register it so find_spec works."""
    file_path = tmp_path / f"{module_name}.py"
    file_path.write_text(textwrap.dedent(source))

    # Create a minimal ModuleSpec so importlib.util.find_spec returns it
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    # Inject into sys.modules so find_spec resolves
    mod = types.ModuleType(module_name)
    mod.__spec__ = spec
    mod.__file__ = str(file_path)
    sys.modules[module_name] = mod
    return module_name


@pytest.fixture(autouse=True)
def _cleanup_modules():
    """Remove test modules from sys.modules after each test."""
    before = set(sys.modules)
    yield
    for key in set(sys.modules) - before:
        del sys.modules[key]


class TestFindBoltApiNames:
    def test_simple_assignment(self, tmp_path):
        name = _write_module(tmp_path, "_test_simple", """
            from django_bolt.api import BoltAPI
            app = BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["app"]

    def test_canonical_api_name(self, tmp_path):
        name = _write_module(tmp_path, "_test_canonical", """
            from django_bolt.api import BoltAPI
            api = BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["api"]

    def test_ignores_non_bolt_api_from_other_lib(self, tmp_path):
        name = _write_module(tmp_path, "_test_ignore_other_lib", """
            from some_other_lib import BoltAPI
            api = BoltAPI()
        """)
        assert find_bolt_api_names(name) == []

    def test_ignores_attribute_call_from_other_lib(self, tmp_path):
        name = _write_module(tmp_path, "_test_ignore_other_attr", """
            import some_other_lib.api
            app = some_other_lib.api.BoltAPI()
        """)
        assert find_bolt_api_names(name) == []

    def test_import_alias(self, tmp_path):
        name = _write_module(tmp_path, "_test_alias", """
            from django_bolt.api import BoltAPI as Bolt
            v1 = Bolt()
        """)
        assert find_bolt_api_names(name) == ["v1"]

    def test_attribute_style_call(self, tmp_path):
        name = _write_module(tmp_path, "_test_attr", """
            import django_bolt.api
            app = django_bolt.api.BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["app"]

    def test_aliased_module_import(self, tmp_path):
        name = _write_module(tmp_path, "_test_alias_module", """
            import django_bolt.api as bolt
            app = bolt.BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["app"]

    def test_annotated_assignment(self, tmp_path):
        name = _write_module(tmp_path, "_test_annassign", """
            from django_bolt.api import BoltAPI
            app: BoltAPI = BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["app"]

    def test_multiple_instances(self, tmp_path):
        name = _write_module(tmp_path, "_test_multi", """
            from django_bolt.api import BoltAPI
            v1 = BoltAPI()
            v2 = BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["v1", "v2"]

    def test_ignores_non_bolt_calls(self, tmp_path):
        name = _write_module(tmp_path, "_test_ignore", """
            from django_bolt.api import BoltAPI
            api = BoltAPI()
            other = SomethingElse()
            x = int("5")
        """)
        assert find_bolt_api_names(name) == ["api"]

    def test_ignores_nested_assignment(self, tmp_path):
        name = _write_module(tmp_path, "_test_nested", """
            from django_bolt.api import BoltAPI
            def create():
                inner = BoltAPI()
                return inner
        """)
        assert find_bolt_api_names(name) == []

    def test_no_bolt_import(self, tmp_path):
        name = _write_module(tmp_path, "_test_noimport", """
            x = 42
            y = "hello"
        """)
        assert find_bolt_api_names(name) == []

    def test_nonexistent_module(self):
        assert find_bolt_api_names("nonexistent_module_xyz_123") == []

    def test_syntax_error(self, tmp_path):
        name = _write_module(tmp_path, "_test_syntax_err", """
            def broken(
        """)
        assert find_bolt_api_names(name) == []

    def test_multi_target_assignment(self, tmp_path):
        name = _write_module(tmp_path, "_test_multitarget", """
            from django_bolt.api import BoltAPI
            a = b = BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["a", "b"]

    def test_with_constructor_args(self, tmp_path):
        name = _write_module(tmp_path, "_test_args", """
            from django_bolt.api import BoltAPI
            app = BoltAPI(enable_logging=True, trailing_slash=False)
        """)
        assert find_bolt_api_names(name) == ["app"]

    def test_star_import(self, tmp_path):
        name = _write_module(tmp_path, "_test_star", """
            from django_bolt.api import *
            app = BoltAPI()
        """)
        assert find_bolt_api_names(name) == ["app"]

    def test_bare_bolt_api_without_import(self, tmp_path):
        name = _write_module(tmp_path, "_test_bare", """
            app = BoltAPI()
        """)
        assert find_bolt_api_names(name) == []
