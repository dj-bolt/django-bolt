from __future__ import annotations

from django.apps import AppConfig


class BoltMcpOAuthConfig(AppConfig):
    """Django app holding the bolt-mcp OAuth Authorization Server models.

    Add ``"bolt_mcp.oauth"`` to ``INSTALLED_APPS`` and run ``manage.py migrate`` to
    create the client/code/refresh-token tables. The explicit ``label`` avoids the
    default ``oauth`` label colliding with other apps.
    """

    name = "bolt_mcp.oauth"
    label = "bolt_mcp_oauth"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "bolt-mcp OAuth"
