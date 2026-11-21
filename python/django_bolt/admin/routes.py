"""Django admin route registration via ASGI mount."""

import sys
from typing import TYPE_CHECKING

from django.core.asgi import get_asgi_application

from django_bolt.admin.admin_detection import (
    admin_url_needs_path_parameter,
    detect_admin_url_prefixes,
    should_enable_admin,
)

if TYPE_CHECKING:
    from django_bolt.api import BoltAPI


class AdminRouteRegistrar:
    """Registers Django admin as an ASGI mount on a BoltAPI instance."""

    def __init__(self, api: "BoltAPI"):
        self.api = api

    def register_routes(self, host: str = "localhost", port: int = 8000) -> None:
        """Mount Django admin. host/port accepted for backward compat only."""
        _ = (host, port)
        if self.api._admin_routes_registered:
            return

        if not should_enable_admin():
            return

        admin_prefixes = detect_admin_url_prefixes()
        if admin_prefixes is None:
            return

        # A mount of the user at the root path stays. It gets each path that has no Bolt route.
        if admin_prefixes == [""] and "/" in self.api._asgi_mount_prefixes:
            self.api._admin_routes_registered = True
            return

        if admin_url_needs_path_parameter():
            print(
                "[django-bolt] Warning: the URL pattern of the admin site has a path parameter. "
                "Bolt mounts Django at the root path for it. "
                "Django answers each path that has no Bolt route.",
                file=sys.stderr,
            )
        elif "" in admin_prefixes:
            print(
                "[django-bolt] Warning: the admin site is at the root path. "
                "Django answers each path that has no Bolt route.",
                file=sys.stderr,
            )

        # An admin site in i18n_patterns() has one prefix for each language.
        # One Django app serves all of them.
        asgi_app = get_asgi_application()
        for admin_prefix in admin_prefixes:
            # clear_root_path=True because Django admin's URL patterns already
            # include the mount prefix (/admin/...), so path_info must equal the
            # full path rather than the subpath relative to the mount prefix.
            self.api.mount_django(f"/{admin_prefix}", asgi_app, clear_root_path=True)
        self.api._admin_routes_registered = True
