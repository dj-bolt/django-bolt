"""
Path parameter names of a route pattern.
"""

from __future__ import annotations

import re

# Pre-compiled regex pattern for extracting path parameters
# Matches both {param} and {param:type} formats (e.g., {path:path} for catch-all)
PATH_PARAM_REGEX = re.compile(r"\{(\w+)(?::\w+)?\}")


def extract_path_params(path: str) -> set[str]:
    """
    Extract path parameter names from a route pattern.

    Examples:
        "/users/{user_id}" -> {"user_id"}
        "/posts/{post_id}/comments/{comment_id}" -> {"post_id", "comment_id"}
        "/files/{path:path}" -> {"path"}  # catch-all path parameter
    """
    return set(PATH_PARAM_REGEX.findall(path))
