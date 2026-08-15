"""Generate src/llms-full.txt by concatenating llms.txt and every docs page.

Run before `zensical build`; the output is copied verbatim into the site root.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).parent / "src"
BASE_URL = "https://bolt.farhana.li/"

ORDER = [
    "index.md",
    "faq.md",
    "architecture.md",
    "benchmarks.md",
    "comparison.md",
    "getting-started/quickstart.md",
    "getting-started/deployment.md",
]

FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def page_url(rel: str) -> str:
    if rel == "index.md":
        return BASE_URL
    return BASE_URL + rel[:-3] + "/"


def main() -> None:
    pages = list(ORDER)
    for group in ("topics", "ref"):
        pages += sorted(p.relative_to(SRC).as_posix() for p in (SRC / group).glob("*.md"))

    parts = [(SRC / "llms.txt").read_text(), "\n\n---\n\n# Full documentation\n"]
    for rel in pages:
        text = FRONTMATTER.sub("", (SRC / rel).read_text())
        parts.append(f"\n\n<!-- source: {page_url(rel)} -->\n\n{text.strip()}\n")
    (SRC / "llms-full.txt").write_text("".join(parts))
    print(f"wrote {SRC / 'llms-full.txt'} ({len(pages)} pages)")


if __name__ == "__main__":
    main()
