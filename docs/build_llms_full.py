"""Write src/llms-full.txt: llms.txt, then each page in the order of the navigation.

The navigation in zensical.toml sets the order, so a new page is in the file
when it is in the navigation. Run this script before `zensical build`. The
build copies the file to the root of the site.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "src"
BASE_URL = "https://bolt.farhana.li/"
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def nav_pages(items: list) -> list[str]:
    """Give the path of each page in the navigation, in order."""
    pages = []
    for item in items:
        for value in item.values() if isinstance(item, dict) else [item]:
            if isinstance(value, list):
                pages += nav_pages(value)
            else:
                pages.append(value)
    return pages


def page_url(rel: str) -> str:
    path = rel.removesuffix(".md").removesuffix("index")
    return BASE_URL + (path + "/" if path and not path.endswith("/") else path)


def main() -> None:
    config = tomllib.loads((ROOT / "zensical.toml").read_text())
    # The landing page is a template. Its Markdown has no content to read.
    pages = [page for page in nav_pages(config["project"]["nav"]) if page != "index.md"]
    parts = [(SRC / "llms.txt").read_text(), "\n\n---\n\n# Full documentation\n"]
    for rel in pages:
        text = FRONTMATTER.sub("", (SRC / rel).read_text())
        parts.append(f"\n\n<!-- source: {page_url(rel)} -->\n\n{text.strip()}\n")
    (SRC / "llms-full.txt").write_text("".join(parts))
    print(f"wrote {SRC / 'llms-full.txt'} ({len(pages)} pages)")


if __name__ == "__main__":
    main()
