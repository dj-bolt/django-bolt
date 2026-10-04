"""The Python examples in the documentation parse and import names that exist.

scripts/check_doc_snippets.py has the rules. The docs CI job runs it for a
change to the docs. This test runs it for a change to the code, for example
a renamed API that a page still imports.
"""

from __future__ import annotations

import importlib.util
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
    assert sum(len(list(check_doc_snippets.python_blocks(page.read_text()))) for page in PAGES) > 400


@pytest.mark.parametrize("page", PAGES, ids=[str(page.relative_to(check_doc_snippets.DOCS)) for page in PAGES])
def test_python_examples_parse_and_import_names_that_exist(page):
    problems = check_doc_snippets.check_page(page)
    assert not problems, "\n".join(problems)


def test_a_missing_name_is_found():
    """The check reads the source, so it must also see a name that is not there."""
    tree = check_doc_snippets.ast.parse("from django_bolt import BoltAPI, NoSuchName\nimport django_bolt.no_module")
    assert check_doc_snippets.missing_imports(tree) == [(1, "django_bolt.NoSuchName"), (2, "django_bolt.no_module")]
