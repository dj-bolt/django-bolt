"""Form text that is not UTF-8, and the byte limit of a form value.

Django `request.POST` reads an `application/x-www-form-urlencoded` body that is
not UTF-8 as Latin-1, as a whole. `MultiPartParser` decodes each multipart text
field as UTF-8, with U+FFFD for a bad sequence. Each expected value below is
what Django 5.2 and 6.1 give for the same bytes.

`DJANGO_BOLT_MAX_PARAM_LENGTH` counts the bytes that the client sent for a
value: after the percent-decoding of a urlencoded or query value, and as sent
for a multipart value. U+FFFD has three bytes, so the decoded text can be
longer than that count.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Annotated

import pytest

from django_bolt import BoltAPI, WebSocket
from django_bolt.param_functions import Form, Query
from django_bolt.testing import TestClient, WebSocketTestClient

URLENCODED = {"content-type": "application/x-www-form-urlencoded"}
BOUNDARY = "bolt-form-text-boundary"
MULTIPART = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}


def multipart_body(*fields: tuple[bytes, bytes]) -> bytes:
    body = b""
    for name, value in fields:
        body += f"--{BOUNDARY}\r\n".encode()
        body += b'Content-Disposition: form-data; name="' + name + b'"\r\n\r\n' + value + b"\r\n"
    return body + f"--{BOUNDARY}--\r\n".encode()


def form_api() -> BoltAPI:
    api = BoltAPI()

    @api.post("/fields")
    async def fields(request, unused: Annotated[str, Form()] = ""):
        return {key: value if isinstance(value, list) else [value] for key, value in request.form.items()}

    @api.post("/typed")
    async def typed(age: Annotated[int, Form()], name: Annotated[str, Form()]):
        return {"age": age, "name": name}

    @api.post("/value")
    async def value(v: Annotated[str, Form()]):
        return {"v": v}

    @api.get("/query")
    async def query(v: str):
        return {"v": v}

    @api.get("/sequence")
    async def sequence(v: Annotated[list[str], Query()]):
        return {"v": v}

    return api


@pytest.fixture(scope="module")
def client():
    with TestClient(form_api()) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"name=caf\xe9", {"name": ["café"]}),
        (b"caf\xe9=1", {"café": ["1"]}),
        # The fallback applies to the whole body, so a UTF-8 field changes too.
        (b"a=\xc3\xa9&b=\xe9", {"a": ["Ã©"], "b": ["é"]}),
        # An escape gives a byte, and the bytes of the escapes decode as UTF-8.
        (b"a=%C3%A9&b=\xe9", {"a": ["é"], "b": ["é"]}),
        (b"a=%C3\xa9", {"a": ["�©"]}),
        (b"a=caf\xe9+x%zz", {"a": ["café x%zz"]}),
        (b"v=\xc0\xaf", {"v": ["À¯"]}),
        # A UTF-8 body keeps its rules.
        (b"name=caf%E9", {"name": ["caf�"]}),
        (b"name=caf\xc3\xa9", {"name": ["café"]}),
        (b"v=%ED%A0%80", {"v": ["���"]}),
        (b"\xef\xbb\xbfname=x", {"﻿name": ["x"]}),
    ],
)
def test_a_urlencoded_body_decodes_as_django_decodes_it(client, body, expected):
    response = client.post("/fields", content=body, headers=URLENCODED)
    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"caf\xe9", "caf�"),
        (b"\xff" * 3, "�" * 3),
        (b"\xed\xa0\x80", "�" * 3),
        (b"caf%E9", "caf%E9"),
        (b"\xef\xbb\xbfx", "﻿x"),
    ],
)
def test_a_multipart_text_field_decodes_as_django_decodes_it(client, value, expected):
    response = client.post("/fields", content=multipart_body((b"v", value)), headers=MULTIPART)
    assert response.status_code == 200, response.text
    assert response.json() == {"v": [expected]}


def test_a_typed_field_of_a_latin1_body_converts_from_the_decoded_text(client):
    response = client.post("/typed", content=b"age=30&name=caf\xe9", headers=URLENCODED)
    assert response.status_code == 200, response.text
    assert response.json() == {"age": 30, "name": "café"}


def test_a_multipart_field_name_that_is_not_utf8_gets_422(client):
    # Django drops such a part. The multipart parser of Bolt cannot skip it.
    body = multipart_body((b"caf\xe9", b"1"), (b"ok", b"2"))
    response = client.post("/fields", content=body, headers=MULTIPART)
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] == "multipart_error"


LIMIT = 17


@pytest.fixture(scope="module")
def limited_client():
    # The client reads the limit when it starts. The variable is restored at
    # once, so a client that starts later in this module gets the default.
    with ExitStack() as stack:
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("DJANGO_BOLT_MAX_PARAM_LENGTH", str(LIMIT))
            test_client = stack.enter_context(TestClient(form_api()))
        yield test_client


def _post_value(client, kind: str, value: bytes):
    if kind == "multipart":
        return client.post("/value", content=multipart_body((b"v", value)), headers=MULTIPART)
    return client.post("/value", content=b"v=" + value, headers=URLENCODED)


@pytest.mark.parametrize(
    ("kind", "sent"),
    [
        ("multipart", lambda n: b"\xff" * n),
        ("urlencoded", lambda n: b"%FF" * n),
        ("urlencoded", lambda n: b"\xff" * n),
        # A UTF-8 character counts its bytes, not one character.
        ("multipart", lambda n: "é".encode() * (n // 2) + b"a" * (n % 2)),
        ("urlencoded", lambda n: b"%C3%A9" * (n // 2) + b"a" * (n % 2)),
    ],
    ids=["multipart-ff", "urlencoded-escaped-ff", "urlencoded-raw-ff", "multipart-utf8", "urlencoded-utf8"],
)
def test_a_form_value_at_the_byte_limit_passes_and_one_more_byte_gets_422(limited_client, kind, sent):
    response = _post_value(limited_client, kind, sent(LIMIT))
    assert response.status_code == 200, response.text

    response = _post_value(limited_client, kind, sent(LIMIT + 1))
    assert response.status_code == 422, response.text
    message = f"Invalid value for field 'v': Parameter too long: {LIMIT + 1} bytes (max {LIMIT} bytes)"
    assert response.json()["detail"][0]["msg"] == message


@pytest.mark.parametrize("path", ["/query?v=", "/sequence?v=a&v="])
def test_a_query_value_at_the_byte_limit_passes_and_one_more_byte_gets_422(limited_client, path):
    response = limited_client.get(path + "%FF" * LIMIT)
    assert response.status_code == 200, response.text
    assert response.json()["v"] in ("�" * LIMIT, ["a", "�" * LIMIT])

    response = limited_client.get(path + "%FF" * (LIMIT + 1))
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        f"Query parameter 'v': Parameter too long: {LIMIT + 1} bytes (max {LIMIT} bytes)"
    )


@pytest.mark.asyncio
async def test_a_websocket_query_value_counts_its_bytes_after_the_percent_decoding(monkeypatch):
    monkeypatch.setenv("DJANGO_BOLT_MAX_PARAM_LENGTH", str(LIMIT))
    api = BoltAPI()

    @api.websocket("/ws")
    async def echo(websocket: WebSocket, v: str):
        await websocket.accept()
        await websocket.send_text(v)

    query = "v=" + "%FF" * LIMIT
    async with WebSocketTestClient(api, "/ws", query_string=query, read_django_settings=False) as ws:
        assert await ws.receive_text() == "\ufffd" * LIMIT

    message = rf"Query parameter 'v': Parameter too long: {LIMIT + 1} bytes \(max {LIMIT} bytes\)"
    with pytest.raises(ValueError, match=message):
        async with WebSocketTestClient(api, "/ws", query_string=query + "%FF", read_django_settings=False):
            pass
