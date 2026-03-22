"""
Static file serving utilities for Django-Bolt.

Provides static file serving for Django admin and other static assets.
"""

import mimetypes
import os

from django.conf import settings

try:
    from django.contrib.staticfiles.finders import find
except ImportError:
    find = None

from ..exceptions import HTTPException
from ..responses import FileResponse


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


def guess_content_type(file_path: str) -> str:
    """
    Guess content type from file extension.

    Args:
        file_path: Path to file

    Returns:
        MIME type string
    """
    content_type, _ = mimetypes.guess_type(file_path)
    if content_type:
        return content_type

    # Fallback for common static file types
    ext = os.path.splitext(file_path)[1].lower()
    type_map = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".eot": "application/vnd.ms-fontobject",
    }

    return type_map.get(ext, "application/octet-stream")


async def serve_static_file(path: str) -> tuple[int, list[tuple[str, str]], bytes]:
    """
    Serve a static file using Django's static file system.

    Args:
        path: Relative path to static file

    Returns:
        Response tuple: (status_code, headers, body)
    """
    # Find the static file
    file_path = find_static_file(path)

    if not file_path:
        raise HTTPException(404, f"Static file not found: {path}")

    # Return FileResponse (Rust will handle streaming)
    content_type = guess_content_type(file_path)

    # Use FileResponse which returns the special file response format
    return FileResponse(file_path, headers={"content-type": content_type})


def register_static_routes(api, static_url: str | None = None):
    """
    Register static file serving routes on a BoltAPI instance.

    Args:
        api: BoltAPI instance
        static_url: Static URL prefix (default: from settings.STATIC_URL)
    """
    if static_url is None:
        if not hasattr(settings, "STATIC_URL") or not settings.STATIC_URL:
            # Static files not configured
            return
        static_url = settings.STATIC_URL.strip("/")

    if not static_url:
        static_url = "static"

    # Register catch-all route for static files
    route_pattern = f"/{static_url}/{{path:path}}"

    @api.get(route_pattern)
    async def serve_static(path: str):
        """Serve static files for Django admin and other apps."""
        return await serve_static_file(path)

    return serve_static
