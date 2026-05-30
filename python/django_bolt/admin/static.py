"""
Static file lookup for Django-Bolt.

`find_static_file` resolves a relative static path via Django's staticfiles
finders. It is the DEBUG-only fallback used by the native Rust static handler
(`src/static_files.rs`) for app/admin assets that haven't been collected into
STATIC_ROOT. Production serving (and all media serving) goes through the Rust
handler directly; there is no Python static request route.
"""

from __future__ import annotations

try:
    from django.contrib.staticfiles.finders import find
except ImportError:
    find = None


def find_static_file(path: str) -> str | None:
    """
    Find a static file using Django's static file finders.

    Args:
        path: Relative path to static file (e.g., 'admin/css/base.css')

    Returns:
        Absolute path to file if found, None otherwise
    """
    if find is not None:
        found_path = find(path)
        if found_path:
            return found_path

    return None
