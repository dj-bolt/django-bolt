"""An error in the body of a StreamingResponse ends the body and is logged.

The status and headers are already sent when the body raises. Bolt cannot send
a 500, so it ends the body and logs the error with its traceback.
"""

from __future__ import annotations

import logging

import pytest

from django_bolt import BoltAPI, StreamingResponse
from django_bolt.testing import TestClient

STREAM_LOGGER = "django_bolt.streaming"


class FailsOnceIterator:
    """An iterator, not a generator: it can go on after an error."""

    def __init__(self):
        self.steps = iter(["first,", ValueError("boom in iterator"), "after-error,"])

    def __iter__(self):
        return self

    def __next__(self):
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step


class FailsInIter:
    def __iter__(self):
        raise ValueError("boom in __iter__")


def sync_body():
    yield "first,"
    raise ValueError("boom in sync body")


async def async_body():
    yield "first,"
    raise ValueError("boom in async body")


@pytest.fixture
def api():
    api = BoltAPI()

    @api.get("/sync")
    async def sync_stream():
        return StreamingResponse(sync_body(), media_type="text/plain")

    @api.get("/async")
    async def async_stream():
        return StreamingResponse(async_body(), media_type="text/plain")

    @api.get("/iterator")
    async def iterator_stream():
        return StreamingResponse(FailsOnceIterator(), media_type="text/plain")

    @api.get("/iter-fails")
    async def iter_fails():
        return StreamingResponse(FailsInIter(), media_type="text/plain")

    return api


def _stream_errors(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == STREAM_LOGGER]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/sync", "boom in sync body"),
        ("/async", "boom in async body"),
        ("/iterator", "boom in iterator"),
    ],
)
def test_body_error_ends_the_body_and_is_logged(api, caplog, path, message):
    with caplog.at_level(logging.ERROR, logger=STREAM_LOGGER), TestClient(api) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.content == b"first,"
    [record] = _stream_errors(caplog)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == message


def test_iter_error_gives_an_empty_body_and_is_logged(api, caplog):
    with caplog.at_level(logging.ERROR, logger=STREAM_LOGGER), TestClient(api) as client:
        response = client.get("/iter-fails")

    assert response.status_code == 200
    assert response.content == b""
    [record] = _stream_errors(caplog)
    assert str(record.exc_info[1]) == "boom in __iter__"
