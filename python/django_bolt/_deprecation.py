"""Warnings for public names that Bolt removes in the next minor release."""

from __future__ import annotations

import warnings


def warn_deprecated(name: str, instead: str = "") -> None:
    """Warn that the caller of the deprecated ``name`` uses it.

    Call this function directly from the deprecated function, so that the
    warning points to the line that calls the deprecated function.
    """
    message = f"{name} is deprecated. Bolt removes it in the next minor release."
    if instead:
        message = f"{message} {instead}"
    warnings.warn(message, DeprecationWarning, stacklevel=3)
