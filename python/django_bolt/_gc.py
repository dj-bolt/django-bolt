"""Take the objects of the startup out of the garbage collector of a server process.

A server has many objects that live as long as its process: the modules of
Django and of the application, the models and the routes. A full collection of
the garbage collector reads each object that it tracks, and the server answers
no request during that time. After ``gc.freeze()``, a collection reads only the
objects that the requests made.

Measured with the example project, one process and 2,000 requests in one
second: a full collection took 30 to 45 ms, and 1 to 4 ms after the freeze.
"""

from __future__ import annotations

import gc

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SETTING = "BOLT_GC_FREEZE"


def freeze_setting() -> bool:
    """
    Read ``BOLT_GC_FREEZE``. The default is True.

    Raises:
        ImproperlyConfigured: The setting is not a bool.
    """
    value = getattr(settings, SETTING, True)
    if type(value) is not bool:
        raise ImproperlyConfigured(f"{SETTING} must be a bool, got {type(value).__name__} {value!r}.")
    return value


def freeze_startup_objects() -> None:
    """Freeze the objects of this process. The Rust server calls it when it starts: see ``start_server``.

    The startup is complete then: the routes are registered, and each
    lifespan started. A process with no server does not come here, so a test
    process that runs the ``runbolt`` command with no server freezes nothing.

    Reference counts still free a frozen object. The collector does not: a
    frozen object in a reference cycle stays for the life of the process. So
    this function first collects the garbage of the startup.

    Raises:
        ImproperlyConfigured: ``BOLT_GC_FREEZE`` is not a bool.
    """
    if freeze_setting():
        gc.collect()
        gc.freeze()
