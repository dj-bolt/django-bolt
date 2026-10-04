"""The Python examples in the documentation parse and import names that exist.

scripts/check_doc_snippets.py has the rules. The docs CI job runs it for a
change to the docs. This test runs it for a change to the code, for example
a renamed API that a page still imports.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_doc_snippets.py"
_spec = importlib.util.spec_from_file_location("check_doc_snippets", SCRIPT)
check_doc_snippets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_doc_snippets)

PAGES = check_doc_snippets.pages()


def test_the_docs_have_python_examples():
    """A wrong path would find no page, and the test below would check nothing."""
    assert len(PAGES) > 20
    assert sum(len(list(check_doc_snippets.python_blocks(page.read_text(encoding="utf-8")))) for page in PAGES) > 400


@pytest.mark.parametrize("page", PAGES, ids=[str(page.relative_to(check_doc_snippets.ROOT)) for page in PAGES])
def test_python_examples_parse_and_import_names_that_exist(page):
    problems = check_doc_snippets.check_page(page)
    assert not problems, "\n".join(problems)


def test_a_missing_name_is_found():
    """The check reads the source, so it must also see a name that is not there."""
    tree = check_doc_snippets.ast.parse("from django_bolt import BoltAPI, NoSuchName\nimport django_bolt.no_module")
    assert check_doc_snippets.missing_imports(tree) == [(1, "django_bolt.NoSuchName"), (2, "django_bolt.no_module")]


def test_the_readmes_are_checked():
    """README.md is the PyPI page of each package."""
    assert check_doc_snippets.ROOT / "README.md" in PAGES
    assert check_doc_snippets.ROOT / "python" / "bolt-mcp" / "README.md" in PAGES


def test_a_missing_bolt_mcp_name_is_found():
    """bolt_mcp is in this repository too, and the MCP pages import from it."""
    tree = check_doc_snippets.ast.parse("from bolt_mcp import MCP, NoSuchName\nimport bolt_mcp.no_module")
    assert check_doc_snippets.missing_imports(tree) == [(1, "bolt_mcp.NoSuchName"), (2, "bolt_mcp.no_module")]


def test_tilde_fences_and_longer_fences_are_checked():
    """A closing fence has the character of the opening fence, and at least its length."""
    text = "~~~python\na = 1\n~~~\n\n````py\nb = 2\n```\nc = 3\n````\n"
    assert list(check_doc_snippets.python_blocks(text)) == [(2, "a = 1"), (6, "b = 2\n```\nc = 3")]


def test_a_name_imported_only_for_type_checking_is_missing(tmp_path, monkeypatch):
    """The body of `if TYPE_CHECKING:` does not run, so its names cannot be imported."""
    package = tmp_path / "docs_type_checking_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from decimal import Decimal\n"
        "else:\n"
        "    from fractions import Fraction\n"
        "if typing.TYPE_CHECKING:\n"
        "    from uuid import UUID\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(check_doc_snippets.SOURCE_ROOTS, package.name, tmp_path)
    check_doc_snippets.module_names.cache_clear()
    tree = check_doc_snippets.ast.parse(f"from {package.name} import Decimal, Fraction, UUID")
    assert check_doc_snippets.missing_imports(tree) == [(1, f"{package.name}.Decimal"), (1, f"{package.name}.UUID")]


def test_the_check_reads_utf8_in_any_locale():
    """The docs have characters that are not ASCII. A C locale must not stop the check."""
    env = {**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
