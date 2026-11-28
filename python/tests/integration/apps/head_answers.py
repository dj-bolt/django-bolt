"""App under test for HEAD answers: a body above the compression threshold and
a file that does not exist. Each route answers GET and HEAD. Compression is
configured in the test's ``settings_extra``.
"""

from __future__ import annotations

from django_bolt import BoltAPI
from django_bolt.responses import FileResponse

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


async def data():
    return {"payload": "x" * 2000}


async def missing():
    return FileResponse("/nonexistent/head-answers.txt")


for path, handler in (("/data", data), ("/missing", missing)):
    api.get(path)(handler)
    api.head(path)(handler)
