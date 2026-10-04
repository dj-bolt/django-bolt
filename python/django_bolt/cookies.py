"""Cookie handling utilities for django-bolt responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from typing import Literal

SameSitePolicy = Literal["Strict", "Lax", "None", False]


@dataclass(slots=True)
class Cookie:
    """HTTP cookie with all standard attributes.

    Attributes:
        name: Cookie name
        value: Cookie value (default: "")
        max_age: Maximum age in seconds (default: None, session cookie)
        expires: Expiration datetime or string (default: None)
        path: Cookie path (default: "/")
        domain: Cookie domain (default: None, current domain)
        secure: Require HTTPS (default: False)
        httponly: Prevent JavaScript access (default: False)
        samesite: SameSite policy - "Strict", "Lax", "None", or False to omit (default: "Lax")

    Note:
        Invalid cookie names/values are validated and rejected by Rust at serialization time.

    Example:
        >>> cookie = Cookie("session", "abc123", httponly=True, secure=True)
        >>> cookie.to_header_value()
        'session=abc123; httponly; Path=/; SameSite=Lax; Secure'
    """

    name: str
    value: str = ""
    max_age: int | None = None
    expires: datetime | str | None = None
    path: str = "/"
    domain: str | None = None
    secure: bool = False
    httponly: bool = False
    samesite: SameSitePolicy = "Lax"

    def to_header_value(self) -> str:
        """Serialize to Set-Cookie header value using http.cookies for proper escaping."""
        morsel = SimpleCookie()
        morsel[self.name] = self.value
        cookie = morsel[self.name]

        cookie["path"] = self.path

        if self.max_age is not None:
            cookie["max-age"] = str(self.max_age)

        if self.expires is not None:
            if isinstance(self.expires, datetime):
                cookie["expires"] = self.expires.strftime("%a, %d %b %Y %H:%M:%S GMT")
            else:
                cookie["expires"] = self.expires

        if self.domain is not None:
            cookie["domain"] = self.domain

        if self.secure:
            cookie["secure"] = True

        if self.httponly:
            cookie["httponly"] = True

        # samesite can be "Strict", "Lax", "None" (string), or False (omit)
        if self.samesite:
            cookie["samesite"] = self.samesite

        return cookie.output(header="").strip()

    def to_raw_tuple(
        self,
    ) -> tuple[str, str, str, int | None, str | None, str | None, bool, bool, str | None]:
        """Return raw cookie data for Rust serialization.

        Returns a 9-tuple that Rust can extract directly without
        Python object creation overhead. This avoids SimpleCookie
        overhead in the hot path.

        Returns:
            Tuple of (name, value, path, max_age, expires, domain, secure, httponly, samesite)
        """
        expires_str: str | None = None
        if self.expires is not None:
            if isinstance(self.expires, datetime):
                expires_str = self.expires.strftime("%a, %d %b %Y %H:%M:%S GMT")
            else:
                expires_str = self.expires

        # samesite: False means omit, otherwise use the string value
        samesite_str: str | None = self.samesite if self.samesite else None

        return (
            self.name,
            self.value,
            self.path,
            self.max_age,
            expires_str,
            self.domain,
            self.secure,
            self.httponly,
            samesite_str,
        )


def make_delete_cookie(name: str, path: str = "/", domain: str | None = None) -> Cookie:
    """Create a cookie that expires immediately (for deletion).

    Args:
        name: Cookie name to delete
        path: Cookie path (default: "/")
        domain: Cookie domain (default: None)

    Returns:
        Cookie configured to expire immediately
    """
    # A browser ignores a `__Secure-` or `__Host-` cookie with no Secure flag.
    # Django sets the flag on such a delete, so Bolt sets it too.
    return Cookie(
        name=name,
        value="",
        max_age=0,
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        path=path,
        domain=domain,
        secure=name.startswith(("__Secure-", "__Host-")),
    )
