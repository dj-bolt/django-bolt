"""Let handler threads join the transaction of the test that started them.

`TestClient` sends requests through the Rust pipeline, thus handlers run on
framework threads. Django gives each thread its own database connection, so a
test that holds an open transaction (Django's ``TestCase``, or pytest-django's
plain ``django_db``) makes rows that no handler can read. On SQLite the write
lock of the test also stops the query of the handler, which becomes an unclear
500.

The answer Django uses for its own live-server tests is to give the other
threads the connection of the main thread (``LiveServerThread`` plus
``inc_thread_sharing()``). Bolt does not own the entry point of its threads, so
this module replaces the thread-local store of the connection handler for the
time that the client is open. Every thread then resolves to the connection of
the test, and thus reads the rows of the test.

One connection cannot serve two threads at the same moment. Django gives back
rows from the wrong query if you try, which shows as
``IndexError: list index out of range`` deep in the ORM. Thus each shared
connection gets a lock that is held for the full life of a cursor, which makes
concurrent database work wait instead of corrupt. A test that holds a cursor
open across a request cannot be made to wait, because it is the holder. That
one condition raises `SharedTestConnectionError`, which names the escape hatch.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    from django.db import connections
except ImportError:  # Django is optional for parts of the test suite
    connections = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TIMEOUT = 10.0


class SharedTestConnectionError(RuntimeError):
    """Two threads needed the shared test connection and neither could wait."""


def lock_timeout() -> float:
    """Seconds to wait for the shared connection before the error is raised."""
    raw = os.environ.get("DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT")
    if not raw:
        return DEFAULT_LOCK_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LOCK_TIMEOUT


class _SharedStore:
    """Store that gives all threads the shared connections, and no others.

    ``ConnectionHandler`` reads and writes connections as attributes of
    ``_connections``, which is normally an ``asgiref`` Local with
    ``thread_critical=True``. Shared aliases must resolve to one object for
    every thread. Every other alias must stay thread-local: a connection that
    Django did not mark as shareable raises ``DatabaseError`` when a second
    thread uses it, thus an alias this client did not share is handed back to
    the original store.
    """

    def __init__(self, shared: dict[str, Any], fallback: Any) -> None:
        object.__setattr__(self, "_shared", shared)
        object.__setattr__(self, "_fallback", fallback)

    def __getattr__(self, name: str) -> Any:
        shared = object.__getattribute__(self, "_shared")
        if name in shared:
            return shared[name]
        return getattr(object.__getattribute__(self, "_fallback"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        shared = object.__getattribute__(self, "_shared")
        if name in shared:
            shared[name] = value
        else:
            setattr(object.__getattribute__(self, "_fallback"), name, value)

    def __delattr__(self, name: str) -> None:
        shared = object.__getattribute__(self, "_shared")
        if name in shared:
            del shared[name]
        else:
            delattr(object.__getattribute__(self, "_fallback"), name)


class _CursorProxy:
    """Holds the connection lock while the cursor of one query is open.

    The lock must cover more than ``execute()``. Django reads the rows after
    the call returns, thus a lock around execution alone still lets a second
    thread take the rows of the first. Measured: 133 corrupt reads survived
    that narrower guard.
    """

    __slots__ = ("_cursor", "_release", "_released")

    def __init__(self, cursor: Any, release: Callable[[], None]) -> None:
        self._cursor = cursor
        self._release = release
        self._released = False

    def _finish(self) -> None:
        if not self._released:
            self._released = True
            self._release()

    def close(self) -> None:
        try:
            self._cursor.close()
        finally:
            self._finish()

    def __enter__(self) -> _CursorProxy:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class SharedConnections:
    """Installs, guards, and removes the shared connections of one client."""

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = lock_timeout() if timeout is None else timeout
        self.aliases: list[str] = []
        self.conflict: str | None = None
        self._connections: list[Any] = []
        self._restores: list[Callable[[], None]] = []
        self._previous_store: Any = None
        self._installed = False

    # -- lifecycle ---------------------------------------------------------
    def install(self) -> bool:
        """Share the connections that hold an open transaction.

        Returns True when sharing was installed. Nothing is shared when no
        connection is in a transaction, because then the rows of the test are
        committed already and the handler threads can read them by themselves.
        """
        if connections is None:
            return False
        try:
            initialized = list(connections.all(initialized_only=True))
        except Exception as exc:  # noqa: BLE001 - unconfigured settings, no databases
            logger.debug("Could not inspect database connections: %s", exc)
            return False
        if not any(conn.in_atomic_block for conn in initialized):
            return False

        # Every open connection is shared, and not only the ones in a
        # transaction. A test can read one alias and write another, and a
        # connection reached from a second thread without inc_thread_sharing()
        # raises DatabaseError.
        shared: dict[str, Any] = {}
        for conn in initialized:
            conn.inc_thread_sharing()
            self._connections.append(conn)
            self.aliases.append(conn.alias)
            self._restores.append(self._guard(conn))
            shared[conn.alias] = conn

        self._previous_store = connections._connections
        connections._connections = _SharedStore(shared, self._previous_store)
        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        connections._connections = self._previous_store
        for restore in self._restores:
            restore()
        for conn in self._connections:
            conn.dec_thread_sharing()
        self._connections.clear()
        self._restores.clear()

    # -- guarding ----------------------------------------------------------
    def _guard(self, conn: Any) -> Callable[[], None]:
        """Serialize cursor use on one connection. Returns the undo callable.

        ``chunked_cursor`` is wrapped as well as ``cursor``. Django's
        ``QuerySet.iterator()`` takes that second door, and on Postgres it is a
        different door: a server-side cursor that would otherwise skip the lock.
        """
        lock = threading.RLock()
        originals = {}
        for name in ("cursor", "chunked_cursor"):
            original = getattr(conn, name, None)
            if original is None:
                continue
            originals[name] = original
            setattr(conn, name, self._make_guarded(conn, original, lock))

        def restore() -> None:
            for name, original in originals.items():
                setattr(conn, name, original)

        return restore

    def _make_guarded(self, conn: Any, original: Callable[..., Any], lock: threading.RLock) -> Callable[..., Any]:
        def guarded(*args: Any, **kwargs: Any) -> _CursorProxy:
            if not lock.acquire(timeout=self.timeout):
                self.conflict = conn.alias
                raise SharedTestConnectionError(self._conflict_message(conn.alias))
            try:
                cursor = original(*args, **kwargs)
            except BaseException:
                lock.release()
                raise
            return _CursorProxy(cursor, lock.release)

        return guarded

    def _conflict_message(self, alias: str) -> str:
        """State the cause, then give the fix as code the reader can copy."""
        return (
            f"The test and a handler both needed the {alias!r} database connection, and "
            f"neither could wait ({self.timeout:g}s).\n"
            "\n"
            "Cause: this test holds a cursor open across a request. TestClient lends its "
            "connection to the handler threads, so that plain TestCase and django_db see "
            "the rows of the test, and a cursor that stays open cannot be lent. A loop "
            "over QuerySet.iterator() that makes a request in its body is the usual cause.\n"
            "\n"
            "Fix — use one of these:\n"
            "\n"
            "  1. Read the rows before the request:\n"
            "         for row in list(Model.objects.all()):\n"
            "             client.get(f'/x/{row.pk}')\n"
            "\n"
            "  2. Switch the sharing off, and commit the rows instead:\n"
            "         @pytest.mark.django_db(transaction=True)   # or TransactionTestCase\n"
            "         def test_x():\n"
            "             with TestClient(api, share_db_connection=False) as client:\n"
            "                 ...\n"
            "\n"
            "  3. Give the wait more time, if the request is only slow:\n"
            "         DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT=60"
        )

    def raise_if_conflict(self) -> None:
        """Re-raise a conflict on the thread of the test, not as a bare 500."""
        alias = self.conflict
        if alias is None:
            return
        self.conflict = None
        raise SharedTestConnectionError(self._conflict_message(alias))
