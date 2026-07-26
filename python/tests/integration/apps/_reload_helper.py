"""Support module (not an app) imported by ``reload_import_dep`` (version 1).

The reload test installs this at the temp project root as ``reload_helper.py``
(next to the copied ``api.py``), then swaps in ``_reload_helper_v2`` at runtime
and asserts the running ``--dev`` server reloads — proving the watcher covers
modules the api module imports, not just ``api.py`` itself.

The leading underscore marks it as a support module rather than an app fixture,
so ``test_apps_smoke`` does not require it to define ``api = BoltAPI()``.
"""

from __future__ import annotations

HELPER_VERSION = "helper-v1"
