"""App under test: routes that write a lot to stderr and stdout.

``/boom`` raises, so the server logs a long traceback to stderr. ``/shout``
prints a long line to stdout. A few dozen requests write more than the pipe
buffer of the OS (about 64 KB). The harness must drain both pipes while the
server runs, or the next write blocks the worker.
"""

from __future__ import annotations

from django_bolt import BoltAPI

api = BoltAPI()

MESSAGE = "noisy-output " * 400  # about 5 KB


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/boom")
def boom():
    raise ValueError(MESSAGE)


@api.get("/shout")
def shout():
    print(MESSAGE, flush=True)
    return {"status": "ok"}
