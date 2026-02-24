"""Django admin route registration via ASGI mount."""

from typing import TYPE_CHECKING

from django_bolt.admin.admin_detection import detect_admin_url_prefix, should_enable_admin

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

        admin_prefix = detect_admin_url_prefix()
        if not admin_prefix:
            return

        mount_path = f"/{admin_prefix.strip('/')}"
        # clear_root_path=True because Django admin's URL patterns already
        # include the mount prefix (/admin/...), so path_info must equal the
        # full path rather than the subpath relative to the mount prefix.
        self.api.mount_django(mount_path, clear_root_path=True)
        self.api._admin_routes_registered = True
