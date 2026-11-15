"""`request.build_absolute_uri()` and `request.scheme` for a Bolt request, built by Django.

Django joins the location and gives the scheme. When Django builds the URL
from the request host, it checks that host. Thus `ALLOWED_HOSTS`,
`USE_X_FORWARDED_HOST` and `SECURE_PROXY_SSL_HEADER` apply as in a Django view.
An absolute location comes back as it is, with no host check, as in Django.
Bolt gives the path and the query as the client sent them.
"""

from __future__ import annotations

from django.http import HttpRequest


class _BoltHttpRequest(HttpRequest):
    """A Django request with the META, path and scheme of a Bolt request."""

    def __init__(self, meta: dict, path: str, full_path: str, scheme: str) -> None:
        super().__init__()
        self.META = meta
        self.path = path
        self.path_info = path
        self._bolt_full_path = full_path
        self._bolt_scheme = scheme

    def _get_scheme(self) -> str:
        return self._bolt_scheme

    def get_full_path(self, force_append_slash: bool = False) -> str:
        # Bolt keeps the path and the query encoded, as the client sent them.
        return self._bolt_full_path


def build_absolute_uri(meta: dict, path: str, full_path: str, scheme: str, location: str | None) -> str:
    """Return Django's `build_absolute_uri(location)` for a Bolt request."""
    return _BoltHttpRequest(meta, path, full_path, scheme).build_absolute_uri(location)


def scheme(meta: dict, scheme: str) -> str:
    """Return Django's `request.scheme` for a Bolt request."""
    # Django's `scheme` property reads only META and `_get_scheme()`. Thus the
    # request needs no `__init__`, which makes objects that nothing reads here.
    request = _BoltHttpRequest.__new__(_BoltHttpRequest)
    request.META = meta
    request._bolt_scheme = scheme
    return request.scheme
