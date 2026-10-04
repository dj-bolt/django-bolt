#!/usr/bin/env python3
"""Check the Python examples in docs/src and in the package readmes.

Each ```python or ~~~python block must be valid Python. Each name that it
imports from django_bolt or bolt_mcp must exist. Thus a renamed or removed API
fails this check, not the code of a reader.

The check reads the source of the packages and imports nothing. Thus it needs
only the standard library, and the docs CI job can run it without a build of
the Rust extension. The package source uses Python 3.12 syntax, so the check
needs Python 3.12 or later.

A block that is not complete code, for example a signature, has the line
``<!-- fragment -->`` before it. Such a block is not checked.

Usage::

    python scripts/check_doc_snippets.py
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "src"
# The README of each package is its PyPI page.
READMES = (ROOT / "README.md", ROOT / "python" / "bolt-mcp" / "README.md")
# The directory that holds each package of this repository.
SOURCE_ROOTS = {
    "django_bolt": ROOT / "python",
    "bolt_mcp": ROOT / "python" / "bolt-mcp" / "src",
}
FRAGMENT_MARKER = "<!-- fragment -->"
# A fence is 3 or more backticks or tildes. The closing fence has the same
# character, and at least the length of the opening fence.
FENCE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>(?P<char>[`~])(?P=char){2,})(?P<lang>python|py)\b[^\n]*\n"
    r"(?P<body>.*?)^(?P=indent)(?P=fence)(?P=char)*[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
# A module that defines __getattr__ can give any name.
ANY_NAME = frozenset({"*"})


def pages() -> list[Path]:
    return [*sorted(DOCS.rglob("*.md")), *READMES]


def python_blocks(text: str):
    """Give (first line, code) for each Python block that is not a fragment."""
    for match in FENCE.finditer(text):
        before = text[: match.start()].rstrip("\n").rsplit("\n", 1)[-1]
        if before.strip() == FRAGMENT_MARKER:
            continue
        indent = len(match["indent"])
        code = "\n".join(line[indent:] for line in match["body"].splitlines())
        yield text.count("\n", 0, match.start()) + 2, code


def _is_ours(module: str) -> bool:
    return module.split(".")[0] in SOURCE_ROOTS


def _module_file(module: str) -> Path | None:
    root = SOURCE_ROOTS.get(module.split(".")[0])
    if root is None:
        return None
    path = root.joinpath(*module.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py", path.with_suffix(".pyi")):
        if candidate.is_file():
            return candidate
    return None


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _top_level_statements(body: list[ast.stmt]):
    """Give each statement that runs at import, also in if, try and with blocks.

    The body of `if TYPE_CHECKING:` does not run, so its statements are not given.
    """
    for node in body:
        yield node
        if isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                yield from _top_level_statements(node.body)
            yield from _top_level_statements(node.orelse)
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody, *(h.body for h in node.handlers)):
                yield from _top_level_statements(block)
        elif isinstance(node, ast.With):
            yield from _top_level_statements(node.body)


def _target_names(target: ast.expr):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _target_names(element)


@functools.cache
def module_names(module: str) -> frozenset[str] | None:
    """Give the names that a module of this repository defines, or None if it does not exist."""
    path = _module_file(module)
    if path is None:
        return None
    names: set[str] = set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in _top_level_statements(ast.parse(path.read_text(encoding="utf-8"), str(path)).body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "__getattr__":
                return ANY_NAME
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_target_names(target))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names.update(_target_names(node.target))
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
                    continue
                source = node.module or ""
                if node.level:
                    base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                    source = f"{base}.{source}" if source else base
                star = module_names(source) if _is_ours(source) else ANY_NAME
                if star is None or star is ANY_NAME:
                    return ANY_NAME
                names.update(star)
    return frozenset(names)


def _has_name(module: str, name: str) -> bool:
    names = module_names(module)
    if names is None:
        return False
    return names is ANY_NAME or name in names or _module_file(f"{module}.{name}") is not None


def missing_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Give (line, name) for each import from this repository that does not exist."""
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and _is_ours(node.module or ""):
            if module_names(node.module) is None:
                missing.append((node.lineno, node.module))
                continue
            missing.extend(
                (node.lineno, f"{node.module}.{alias.name}")
                for alias in node.names
                if alias.name != "*" and not _has_name(node.module, alias.name)
            )
        elif isinstance(node, ast.Import):
            missing.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if _is_ours(alias.name) and module_names(alias.name) is None
            )
    return missing


def check_page(page: Path) -> list[str]:
    """Give one message for each problem in the Python blocks of a page."""
    problems = []
    for first_line, code in python_blocks(page.read_text(encoding="utf-8")):
        try:
            tree = ast.parse(code)
        except SyntaxError as error:
            problems.append(f"{page.relative_to(ROOT)}:{first_line + (error.lineno or 1) - 1}: {error.msg}")
            continue
        problems.extend(
            f"{page.relative_to(ROOT)}:{first_line + line - 1}: {name} does not exist"
            for line, name in missing_imports(tree)
        )
    return problems


def main() -> int:
    problems = [problem for page in pages() for problem in check_page(page)]
    for problem in problems:
        print(problem)
    blocks = sum(len(list(python_blocks(page.read_text(encoding="utf-8")))) for page in pages())
    print(f"{len(problems)} problem(s) in {blocks} Python blocks on {len(pages())} pages.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
