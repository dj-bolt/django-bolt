"""Let handler threads join the transaction of the test that started them.

`TestClient` sends requests through the Rust pipeline. Thus handlers run on
framework threads. Django gives each thread its own database connection. A test
that holds an open transaction does not commit. Django's ``TestCase`` and
pytest-django's plain ``django_db`` are the two usual holders. Thus no handler
can read the rows of such a test. On SQLite the write lock of the test also
stops the query of the handler, which becomes an unclear 500.

Django has an answer for its own live-server tests. It gives the other threads
the connection of the main thread, with ``LiveServerThread`` and
``inc_thread_sharing()``. Bolt does not own the entry point of its threads.
Thus this module replaces the thread-local store of the connection handler
while the client is open. Every thread then resolves to the connection of the
test, and reads the rows of the test.

One connection cannot serve two threads at the same moment. Django gives back
rows from the wrong query if you try. This shows as
``IndexError: list index out of range`` deep in the ORM. Thus each shared
connection gets a lock. The lock is held for the full life of a cursor. Thus
concurrent database work waits in place of corrupting rows. The thread that
makes a request gives its locks back while it waits, because it is blocked in
the Rust pipeline and runs no query. Thus a request inside a
``QuerySet.iterator()`` loop is safe. A second thread of the test that holds a
cursor open cannot be parked in that way. That condition raises
`SharedTestConnectionError`, which names the fixes.
"""

from __future__ import annotations

import logging
import math
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

# The store of the connection handler is process-wide, thus only one thread can
# hold shared connections at a time. A second thread that entered its own client
# would resolve aliases through this store and lend the connection of the first
# test to its own handlers, which mixes the rows of two tests. Nesting on one
# thread is legal, and the depth counts it.
_active_lock = threading.Lock()
_active_owner: int | None = None
_active_depth = 0


class SharedTestConnectionError(RuntimeError):
    """Two threads needed the shared test connection and neither could wait."""


_CROSS_THREAD_MESSAGE = (
    "Another thread already lends its database connection to handler threads.\n"
    "\n"
    "Cause: two threads entered a TestClient at the same time. The connection "
    "store of Django is process-wide, so the second client would lend the "
    "connection of the first test to its own handlers, and the rows of two "
    "tests would mix.\n"
    "\n"
    "Fix — use one of these:\n"
    "\n"
    "  1. Run the tests one after the other. pytest-xdist and manage.py test "
    "--parallel each use a separate process, thus they are safe.\n"
    "\n"
    "  2. Share one client between the threads, which is supported:\n"
    "         with TestClient(api) as client:\n"
    "             run_in_threads(lambda: client.get('/x'))\n"
    "\n"
    "  3. Switch the sharing off in the threads:\n"
    "         TestClient(api, share_db_connection=False)"
)


def lock_timeout() -> float:
    """Seconds to wait for the shared connection before the error is raised.

    Each bad value breaks the lock in its own way, thus none may pass. ``nan``
    and a negative below -1 make ``acquire()`` raise, ``inf`` overflows the
    platform time type, and -1 means wait forever, which hides the deadlock the
    error exists to report.
    """
    raw = os.environ.get("DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT")
    if not raw:
        return DEFAULT_LOCK_TIMEOUT
    try:
        timeout = float(raw)
    except ValueError:
        raise ValueError(f"DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT must be a number of seconds, got {raw!r}.") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT must be a finite number above zero, got {raw!r}.")
    return timeout


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


class _Gate:
    """The lock of one shared connection, and the depth this thread holds it at.

    The depth lives in a `threading.local`, thus only the owning thread reads
    or writes its own count. This lets a thread give its locks back while it
    waits for a response, and take them again after.
    """

    __slots__ = ("local", "lock")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.local = threading.local()

    def depth(self) -> int:
        return getattr(self.local, "held", 0)

    def took(self) -> None:
        self.local.held = self.depth() + 1

    def gave_back(self) -> None:
        self.local.held = self.depth() - 1


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
        self._gates: list[_Gate] = []
        self._restores: list[Callable[[], None]] = []
        self._previous_store: Any = None
        self._installed = False

    # -- lifecycle ---------------------------------------------------------
    def install(self) -> bool:
        """Share the connections that hold an open transaction.

        Returns True when sharing was installed. Nothing is shared when no
        connection is in a transaction, because then the rows of the test are
        committed already and the handler threads can read them by themselves.

        Raises `SharedTestConnectionError` when another thread already shares
        its connections. See the note on `_active_owner`.
        """
        global _active_owner, _active_depth

        if connections is None:
            return False
        with _active_lock:
            if _active_owner is not None and _active_owner != threading.get_ident():
                raise SharedTestConnectionError(_CROSS_THREAD_MESSAGE)
            installed = self._install_locked()
            if installed:
                _active_owner = threading.get_ident()
                _active_depth += 1
            return installed

    def _install_locked(self) -> bool:
        """Do the installation. The caller holds ``_active_lock``."""
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
        global _active_owner, _active_depth

        if not self._installed:
            return
        with _active_lock:
            self._installed = False
            connections._connections = self._previous_store
            for restore in self._restores:
                restore()
            for conn in self._connections:
                conn.dec_thread_sharing()
            self._connections.clear()
            self._gates.clear()
            self._restores.clear()
            _active_depth -= 1
            if _active_depth == 0:
                _active_owner = None

    # -- guarding ----------------------------------------------------------
    def _guard(self, conn: Any) -> Callable[[], None]:
        """Serialize cursor use on one connection. Returns the undo callable.

        ``chunked_cursor`` is wrapped as well as ``cursor``. Django's
        ``QuerySet.iterator()`` takes that second door, and on Postgres it is a
        different door: a server-side cursor that would otherwise skip the lock.
        """
        gate = _Gate()
        self._gates.append(gate)
        originals = {}
        for name in ("cursor", "chunked_cursor"):
            original = getattr(conn, name, None)
            if original is None:
                continue
            originals[name] = original
            setattr(conn, name, self._make_guarded(conn, original, gate))

        def restore() -> None:
            for name, original in originals.items():
                setattr(conn, name, original)

        return restore

    def _make_guarded(self, conn: Any, original: Callable[..., Any], gate: _Gate) -> Callable[..., Any]:
        def release() -> None:
            gate.gave_back()
            gate.lock.release()

        def guarded(*args: Any, **kwargs: Any) -> _CursorProxy:
            if not gate.lock.acquire(timeout=self.timeout):
                self.conflict = conn.alias
                raise SharedTestConnectionError(self._conflict_message(conn.alias))
            gate.took()
            try:
                cursor = original(*args, **kwargs)
            except BaseException:
                release()
                raise
            return _CursorProxy(cursor, release)

        return guarded

    # -- parking ------------------------------------------------------------
    def park(self) -> list[int] | None:
        """Give back the locks of this thread while it waits for a response.

        A thread that waits in ``client.get()`` is blocked in the Rust
        pipeline. It cannot run a query, thus the cursors it holds open are
        idle and its locks block a handler for no reason. Before this, a test
        that made a request inside a ``QuerySet.iterator()`` loop deadlocked
        against its own request, and the wait ended in an error.

        The thread takes its locks again in `unpark`. Returns the depth of each
        gate, or None when this thread holds nothing, which is the usual case.
        """
        counts = [gate.depth() for gate in self._gates]
        if not any(counts):
            return None
        for gate, held in zip(self._gates, counts, strict=True):
            for _ in range(held):
                gate.lock.release()
        return counts

    def unpark(self, counts: list[int] | None) -> None:
        """Take the locks back, in the numbers `park` gave back."""
        if not counts:
            return
        for gate, held in zip(self._gates, counts, strict=True):
            for _ in range(held):
                if not gate.lock.acquire(timeout=self.timeout):
                    self.conflict = "default"
                    raise SharedTestConnectionError(self._conflict_message("default"))

    def _conflict_message(self, alias: str) -> str:
        """State the cause, then give the fix as code the reader can copy."""
        return (
            f"The test and a handler both needed the {alias!r} database connection, and "
            f"neither could wait ({self.timeout:g}s).\n"
            "\n"
            "Cause: another thread holds a cursor open on this connection, and it is "
            "not waiting for a response. TestClient lends its connection to the handler "
            "threads, so that plain TestCase and django_db see the rows of the test, and "
            "one connection can serve one cursor at a time. The thread that makes the "
            "request gives its own locks back while it waits, thus a request inside a "
            "QuerySet.iterator() loop is safe. A second thread of the test that iterates "
            "is not.\n"
            "\n"
            "Fix — use one of these:\n"
            "\n"
            "  1. Read the rows in that thread before the request:\n"
            "         for row in list(Model.objects.all()):\n"
            "             ...\n"
            "\n"
            "  2. Switch the sharing off, and commit the rows instead:\n"
            "         @pytest.mark.django_db(transaction=True)   # or TransactionTestCase\n"
            "         def test_x():\n"
            "             with TestClient(api, share_db_connection=False) as client:\n"
            "                 ...\n"
            "\n"
            "  3. Give the wait more time, if the other thread is only slow:\n"
            "         DJANGO_BOLT_TEST_DB_LOCK_TIMEOUT=60"
        )

    def raise_if_conflict(self) -> None:
        """Re-raise a conflict on the thread of the test, not as a bare 500."""
        alias = self.conflict
        if alias is None:
            return
        self.conflict = None
        raise SharedTestConnectionError(self._conflict_message(alias))
