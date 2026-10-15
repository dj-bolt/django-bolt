"""App that reports if the server's event loop thread started.

``start_server`` starts one thread to run the server's asyncio loop. The first
call of that thread is ``asyncio.set_event_loop``. When
``BOLT_TEST_PROBE_LOOP_THREAD`` is set, this module wraps that function. At
exit, it waits a short time for the call and prints ``PROBE_MARKER`` if it
came from a thread other than the main thread.

The test that holds the port uses this. A bind failure must stop startup before
that thread starts: a thread that attaches to Python after finalization panics,
and the release build aborts.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import sys
import threading

from django_bolt import BoltAPI

PROBE_MARKER = "probe: the event loop thread started"

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/hello")
async def hello():
    return {"message": "hello"}


def _install_probe() -> None:
    started = threading.Event()
    set_event_loop = asyncio.set_event_loop
    main_thread_id = threading.main_thread().ident

    def recording_set_event_loop(loop):
        if threading.get_ident() != main_thread_id:
            started.set()
        set_event_loop(loop)

    def report() -> None:
        # The wait releases the GIL, so a started thread can make its call.
        if started.wait(timeout=2):
            print(PROBE_MARKER, file=sys.stderr, flush=True)

    asyncio.set_event_loop = recording_set_event_loop
    atexit.register(report)


if os.environ.get("BOLT_TEST_PROBE_LOOP_THREAD") == "1":
    _install_probe()
