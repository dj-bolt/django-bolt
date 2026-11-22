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
    from django.contrib.staticfiles.finders import find, get_finders
except ImportError:
    find = None
    get_finders = None


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


def finder_locations() -> tuple[list[str], list[str]]:
    """
    Give the absolute directory of each location of the staticfiles finders.

    Rust reads these directories once at startup. A file that a finder finds
    must stay inside the directory of its location, also through a symlink.

    Returns:
        The directories, and the class names of the finders that have no
        filesystem storage. Bolt cannot check the files of these finders.
    """
    locations: list[str] = []
    unknown: list[str] = []
    if get_finders is None:
        return locations, unknown
    for finder in get_finders():
        # FileSystemFinder and AppDirectoriesFinder keep `storages`.
        # A BaseStorageFinder keeps one `storage`.
        storages = list(getattr(finder, "storages", {}).values())
        storage = getattr(finder, "storage", None)
        if storage is not None:
            storages.append(storage)
        if not storages and not hasattr(finder, "storages"):
            unknown.append(type(finder).__qualname__)
            continue
        for item in storages:
            location = getattr(item, "location", None)
            if isinstance(location, str):
                locations.append(location)
            else:
                unknown.append(type(finder).__qualname__)
    return locations, unknown
