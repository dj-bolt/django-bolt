import threading
from contextlib import asynccontextmanager

import pytest

from django_bolt.api import BoltAPI
from django_bolt.testing import AsyncTestClient, TestClient


def test_no_lifespan_by_default():
    api = BoltAPI()
    assert api._lifespan_context is None
    assert api._has_lifespan is False


def test_lifespan_stored():
    @asynccontextmanager
    async def my_lifespan(app):
        yield

    api = BoltAPI(lifespan=my_lifespan)
    assert api._lifespan_context is my_lifespan
    assert api._has_lifespan is True


def test_lifespan_runs_on_test_client_enter_exit():
    """Lifespan startup runs on TestClient enter, shutdown on exit."""
    called = []

    @asynccontextmanager
    async def lifespan(app):
        called.append("startup")
        yield
        called.append("shutdown")

    api = BoltAPI(lifespan=lifespan)

    @api.get("/health")
    async def health():
        return {"ok": True}

    with TestClient(api) as client:
        assert called == ["startup"]
        response = client.get("/health")
        assert response.status_code == 200

    assert called == ["startup", "shutdown"]


def test_lifespan_receives_app_instance():
    received = []

    @asynccontextmanager
    async def lifespan(app):
        received.append(app)
        yield

    api = BoltAPI(lifespan=lifespan)

    @api.get("/ping")
    async def ping():
        return "pong"

    with TestClient(api):
        pass

    assert received[0] is api


def test_lifespan_shutdown_runs_on_exception():
    called = []

    @asynccontextmanager
    async def lifespan(app):
        called.append("startup")
        try:
            yield
        finally:
            called.append("shutdown")

    api = BoltAPI(lifespan=lifespan)

    @api.get("/fail")
    async def fail():
        raise RuntimeError("boom")

    with TestClient(api, raise_server_exceptions=False) as client:
        client.get("/fail")

    assert called == ["startup", "shutdown"]


def test_no_lifespan_still_works():
    """TestClient works normally when no lifespan is configured."""
    api = BoltAPI()

    @api.get("/hello")
    async def hello():
        return {"message": "world"}

    with TestClient(api) as client:
        response = client.get("/hello")
        assert response.status_code == 200
        assert response.json() == {"message": "world"}


def test_lifespan_startup_failure_prevents_tests():
    """If startup raises, it propagates out of TestClient.__enter__."""

    @asynccontextmanager
    async def bad_lifespan(app):
        raise RuntimeError("startup failed")
        yield  # noqa: RET503

    api = BoltAPI(lifespan=bad_lifespan)

    @api.get("/x")
    async def x():
        return "x"

    with pytest.raises(RuntimeError, match="startup failed"), TestClient(api):
        pass


def _recording_api(events: list[str]) -> BoltAPI:
    @asynccontextmanager
    async def lifespan(app):
        events.append("startup")
        yield
        events.append("shutdown")

    api = BoltAPI(lifespan=lifespan)

    @api.get("/health")
    async def health():
        return {"ok": True}

    return api


def test_a_closed_test_client_does_not_run_the_lifespan_again():
    events: list[str] = []
    client = TestClient(_recording_api(events))
    with client:
        pass

    with pytest.raises(RuntimeError, match="TestClient is closed"), client:
        pass

    assert events == ["startup", "shutdown"]


def test_close_runs_the_lifespan_shutdown_of_an_entered_test_client():
    events: list[str] = []
    client = TestClient(_recording_api(events))
    client.__enter__()
    client.close()
    client.close()

    assert events == ["startup", "shutdown"]


@pytest.mark.asyncio
async def test_a_closed_async_test_client_does_not_run_the_lifespan_again():
    events: list[str] = []
    client = AsyncTestClient(_recording_api(events))
    async with client:
        pass

    with pytest.raises(RuntimeError, match="AsyncTestClient is closed"):
        async with client:
            pass

    assert events == ["startup", "shutdown"]


@pytest.mark.asyncio
async def test_aclose_runs_the_lifespan_shutdown_of_an_entered_async_test_client():
    events: list[str] = []
    client = AsyncTestClient(_recording_api(events))
    await client.__aenter__()
    await client.aclose()
    await client.aclose()

    assert events == ["startup", "shutdown"]


@pytest.mark.django_db
def test_close_of_an_entered_test_client_stops_sharing_the_database_connection():
    # In a transaction, __enter__ shares the connection of this thread. close() must stop sharing it,
    # or a client on another thread raises SharedTestConnectionError.
    api = BoltAPI()

    @api.get("/health")
    async def health():
        return {"ok": True}

    client = TestClient(api)
    client.__enter__()
    client.close()

    statuses: list[int] = []
    errors: list[BaseException] = []

    def other_thread():
        try:
            with TestClient(api, share_db_connection=False) as other:
                statuses.append(other.get("/health").status_code)
        except BaseException as exc:  # noqa: BLE001 - the test reports it below
            errors.append(exc)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()

    assert (statuses, errors) == ([200], [])


def test_an_open_test_client_cannot_be_entered_again():
    events: list[str] = []
    client = TestClient(_recording_api(events))
    with client, pytest.raises(RuntimeError, match="TestClient is already open"), client:
        pass

    assert events == ["startup", "shutdown"]


def test_a_failing_lifespan_shutdown_still_closes_the_test_client():
    @asynccontextmanager
    async def lifespan(app):
        yield
        raise RuntimeError("shutdown failed")

    client = TestClient(BoltAPI(lifespan=lifespan))
    with pytest.raises(RuntimeError, match="shutdown failed"), client:
        pass

    assert client.is_closed
