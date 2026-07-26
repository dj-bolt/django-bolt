"""Support module (not an app): replacement for ``_reload_helper`` (version 2).

Written over the temp project's ``reload_helper.py`` while the ``--dev``
server is running; the reload test asserts the new version is served. See
``_reload_helper`` for why the name is underscore-prefixed.
"""

from __future__ import annotations

HELPER_VERSION = "helper-v2"
