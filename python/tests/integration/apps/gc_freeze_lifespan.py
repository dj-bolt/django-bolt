"""An app whose lifespan makes an object at startup, as an application makes a client or a cache.

``runbolt`` starts the server in another branch for an app with a lifespan.
"""

from __future__ import annotations

import contextlib
import gc
from collections.abc import AsyncIterator

from django_bolt import BoltAPI


class Client:
    """An object that the garbage collector tracks. It does not track a dict of strings."""


made_at_startup: list[Client] = []


@contextlib.asynccontextmanager
async def lifespan(app: BoltAPI) -> AsyncIterator[None]:
    made_at_startup.append(Client())
    yield


api = BoltAPI(lifespan=lifespan)


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/collector")
async def collector():
    startup_object = made_at_startup[0]
    # The collector does not list a frozen object.
    listed = any(tracked is startup_object for tracked in gc.get_objects())
    return {"frozen": gc.get_freeze_count(), "collector_reads_the_startup_object": listed}
