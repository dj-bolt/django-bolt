//! Native TCP transport for `WorkerLoop` (Unix).
//!
//! `WorkerLoop._make_socket_transport` returns this instead of asyncio's
//! `_SelectorSocketTransport`. The Python one costs a `Handle._run` →
//! `_read_ready` → `sock.recv` → `data_received` chain per readable event, a
//! Python `write()` per send, and — because the callback is opaque to the
//! reactor — a `poll(2)` after every event to emulate level triggering. Here
//! the pump calls `on_readable` directly: `recv(2)` runs in Rust, the read
//! result says whether the socket is drained (short read / `EAGAIN`) so no
//! `poll(2)` is needed, and `write()` is a Rust method that calls `send(2)`
//! and only buffers on `EAGAIN`.
//!
//! Semantics follow `asyncio.selector_events._SelectorSocketTransport` +
//! `transports._FlowControlMixin` (3.12–3.14): connection_made before the
//! first read, EOF handling via `eof_received`, `BufferedProtocol` support,
//! write-buffer water marks driving `pause_writing`/`resume_writing`,
//! `write_eof`, `close`/`abort`/`connection_lost` ordering, `Server`
//! attach/detach, `start_tls` compatibility, and the private hooks
//! `loop.sendfile` needs. TLS layers `sslproto._SSLProtocolTransport` on top
//! exactly as it does over the stdlib transport.
//!
//! State lives behind a `Mutex` (the pump may run on any Tokio worker
//! thread; every access is under the GIL, so the lock is uncontended) and is
//! never held across a call into Python — protocol callbacks re-enter
//! `write()`/`close()`/`pause_reading()`.

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDict};
use std::collections::VecDeque;
use std::os::fd::RawFd;
use std::sync::{Arc, Mutex};

use crate::worker_fd::{Direction, FdCallback, ReadyOutcome};

/// asyncio's `_SelectorTransport.max_size`: bytes per `recv`.
const MAX_SIZE: usize = 256 * 1024;
/// `transports._FlowControlMixin` defaults.
const DEFAULT_HIGH_WATER: usize = 64 * 1024;
/// `constants.LOG_THRESHOLD_FOR_CONNLOST_WRITES`.
const LOG_THRESHOLD_FOR_CONNLOST_WRITES: usize = 5;

static ASYNCIO_LOGGER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static SET_RESULT_UNLESS_CANCELLED: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static BUFFERED_PROTOCOL: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn cached<'py>(
    py: Python<'py>,
    slot: &'static PyOnceLock<Py<PyAny>>,
    module: &str,
    name: &str,
) -> PyResult<&'py Bound<'py, PyAny>> {
    slot.get_or_try_init(py, || {
        Ok::<_, PyErr>(py.import(module)?.getattr(name)?.unbind())
    })
    .map(|obj| obj.bind(py))
}

std::thread_local! {
    /// Scratch `recv` buffer per pump thread; the bytes handed to the protocol
    /// are a fresh `PyBytes` of the read length (as uvloop does).
    static RECV_BUFFER: std::cell::RefCell<Vec<u8>> = std::cell::RefCell::new(vec![0u8; MAX_SIZE]);
}

struct Inner {
    loop_obj: Py<PyAny>,
    sock: Option<Py<PyAny>>,
    fd: RawFd,
    protocol: Py<PyAny>,
    protocol_connected: bool,
    buffered_protocol: bool,
    server: Option<Py<PyAny>>,
    server_attach_takes_transport: bool,
    extra: Py<PyDict>,
    /// Pending outbound bytes; `send(2)` consumes from the front.
    write_buffer: VecDeque<u8>,
    writer_registered: bool,
    conn_lost: usize,
    closing: bool,
    paused: bool,
    eof: bool,
    protocol_paused: bool,
    high_water: usize,
    low_water: usize,
    empty_waiter: Option<Py<PyAny>>,
}

impl Inner {
    fn is_reading(&self) -> bool {
        !self.closing && !self.paused
    }
}

/// asyncio's `_SelectorSocketTransport`, driven by the WorkerLoop pump.
#[pyclass(
    name = "SocketTransport",
    module = "django_bolt._core",
    weakref,
    frozen
)]
pub(crate) struct SocketTransport {
    inner: Mutex<Inner>,
}

enum SendOutcome {
    Sent(usize),
    WouldBlock,
    Failed(std::io::Error),
}

fn send_now(fd: RawFd, data: &[u8]) -> SendOutcome {
    #[cfg(target_os = "linux")]
    let flags = libc::MSG_NOSIGNAL;
    #[cfg(not(target_os = "linux"))]
    let flags = 0;
    // SAFETY: `data` is a valid, live buffer for the call's duration.
    let rc = unsafe { libc::send(fd, data.as_ptr().cast(), data.len(), flags) };
    if rc >= 0 {
        return SendOutcome::Sent(rc as usize);
    }
    let error = std::io::Error::last_os_error();
    match error.raw_os_error() {
        Some(libc::EAGAIN) | Some(libc::EINTR) => SendOutcome::WouldBlock,
        #[allow(unreachable_patterns)]
        Some(libc::EWOULDBLOCK) => SendOutcome::WouldBlock,
        _ => SendOutcome::Failed(error),
    }
}

fn os_error(_py: Python<'_>, error: std::io::Error) -> PyErr {
    PyErr::from(error)
}

fn is_system_exit_like(py: Python<'_>, error: &PyErr) -> bool {
    error.is_instance_of::<pyo3::exceptions::PySystemExit>(py)
        || error.is_instance_of::<pyo3::exceptions::PyKeyboardInterrupt>(py)
}

impl SocketTransport {
    fn watch(&self, slf: &Py<Self>, py: Python<'_>, direction: Direction) -> PyResult<()> {
        let fd = self.inner.lock().unwrap().fd;
        crate::worker_loop::add_fd_watch(
            py,
            fd,
            direction,
            FdCallback::Transport(Arc::new(slf.clone_ref(py))),
        )
    }

    fn unwatch(&self, direction: Direction) {
        let fd = self.inner.lock().unwrap().fd;
        crate::worker_loop::remove_fd_watch(fd, direction);
    }

    fn call_soon<'py>(
        &self,
        py: Python<'py>,
        args: impl pyo3::call::PyCallArgs<'py>,
    ) -> PyResult<()> {
        let loop_obj = self.inner.lock().unwrap().loop_obj.clone_ref(py);
        loop_obj
            .bind(py)
            .call_method1(intern!(py, "call_soon"), args)?;
        Ok(())
    }

    /// `_SelectorTransport._fatal_error`: report non-OSError failures to the
    /// loop's exception handler, then force-close.
    fn fatal_error(&self, slf: &Py<Self>, py: Python<'_>, error: PyErr, message: &str) {
        if !error.is_instance_of::<pyo3::exceptions::PyOSError>(py) {
            let (loop_obj, protocol) = {
                let inner = self.inner.lock().unwrap();
                (inner.loop_obj.clone_ref(py), inner.protocol.clone_ref(py))
            };
            let context = PyDict::new(py);
            let report = context
                .set_item("message", message)
                .and_then(|_| context.set_item("exception", error.value(py)))
                .and_then(|_| context.set_item("transport", slf))
                .and_then(|_| context.set_item("protocol", protocol))
                .and_then(|_| {
                    loop_obj
                        .bind(py)
                        .call_method1(intern!(py, "call_exception_handler"), (context,))
                });
            if let Err(report_error) = report {
                report_error.print(py);
            }
        }
        self.force_close(slf, py, Some(error));
    }

    /// `_SelectorTransport._force_close`.
    fn force_close(&self, slf: &Py<Self>, py: Python<'_>, error: Option<PyErr>) {
        let (fd, unwatch_writer, unwatch_reader) = {
            let mut inner = self.inner.lock().unwrap();
            if inner.conn_lost > 0 {
                return;
            }
            let unwatch_writer = !inner.write_buffer.is_empty() || inner.writer_registered;
            inner.write_buffer.clear();
            inner.writer_registered = false;
            let unwatch_reader = !inner.closing;
            inner.closing = true;
            inner.conn_lost += 1;
            (inner.fd, unwatch_writer, unwatch_reader)
        };
        if unwatch_writer {
            crate::worker_loop::remove_fd_watch(fd, Direction::Write);
        }
        if unwatch_reader {
            crate::worker_loop::remove_fd_watch(fd, Direction::Read);
        }
        let exc = error.map(|e| e.into_value(py));
        if let Err(schedule_error) = slf
            .getattr(py, intern!(py, "_call_connection_lost"))
            .and_then(|method| self.call_soon(py, (method, exc)))
        {
            schedule_error.print(py);
        }
    }

    /// `_FlowControlMixin._maybe_pause_protocol`.
    fn maybe_pause_protocol(&self, slf: &Py<Self>, py: Python<'_>) {
        let protocol = {
            let mut inner = self.inner.lock().unwrap();
            if inner.write_buffer.len() <= inner.high_water || inner.protocol_paused {
                return;
            }
            inner.protocol_paused = true;
            inner.protocol.clone_ref(py)
        };
        if let Err(error) = protocol.bind(py).call_method0(intern!(py, "pause_writing")) {
            self.report_protocol_error(slf, py, error, "protocol.pause_writing() failed");
        }
    }

    /// `_FlowControlMixin._maybe_resume_protocol`.
    fn maybe_resume_protocol(&self, slf: &Py<Self>, py: Python<'_>) {
        let protocol = {
            let mut inner = self.inner.lock().unwrap();
            if !inner.protocol_paused || inner.write_buffer.len() > inner.low_water {
                return;
            }
            inner.protocol_paused = false;
            inner.protocol.clone_ref(py)
        };
        if let Err(error) = protocol
            .bind(py)
            .call_method0(intern!(py, "resume_writing"))
        {
            self.report_protocol_error(slf, py, error, "protocol.resume_writing() failed");
        }
    }

    fn report_protocol_error(&self, slf: &Py<Self>, py: Python<'_>, error: PyErr, message: &str) {
        if is_system_exit_like(py, &error) {
            error.print(py);
            return;
        }
        let (loop_obj, protocol) = {
            let inner = self.inner.lock().unwrap();
            (inner.loop_obj.clone_ref(py), inner.protocol.clone_ref(py))
        };
        let context = PyDict::new(py);
        let report = context
            .set_item("message", message)
            .and_then(|_| context.set_item("exception", error.value(py)))
            .and_then(|_| context.set_item("transport", slf))
            .and_then(|_| context.set_item("protocol", protocol))
            .and_then(|_| {
                loop_obj
                    .bind(py)
                    .call_method1(intern!(py, "call_exception_handler"), (context,))
            });
        if let Err(report_error) = report {
            report_error.print(py);
        }
    }

    /// Flush the write buffer once (`_write_ready`). Returns whether the
    /// writer must stay registered.
    fn flush(&self, slf: &Py<Self>, py: Python<'_>) -> bool {
        let outcome = {
            let mut inner = self.inner.lock().unwrap();
            if inner.conn_lost > 0 || inner.write_buffer.is_empty() {
                return false;
            }
            let fd = inner.fd;
            let outcome = send_now(fd, inner.write_buffer.as_slices().0);
            if let SendOutcome::Sent(n) = outcome {
                inner.write_buffer.drain(..n);
            }
            outcome
        };
        match outcome {
            SendOutcome::Sent(_) => {}
            SendOutcome::WouldBlock => return true,
            SendOutcome::Failed(error) => {
                let waiter = {
                    let mut inner = self.inner.lock().unwrap();
                    inner.write_buffer.clear();
                    inner.writer_registered = false;
                    inner.empty_waiter.as_ref().map(|w| w.clone_ref(py))
                };
                self.unwatch(Direction::Write);
                let error = os_error(py, error);
                let exc = error.clone_ref(py);
                self.fatal_error(slf, py, error, "Fatal write error on socket transport");
                if let Some(waiter) = waiter {
                    let _ = waiter
                        .bind(py)
                        .call_method1(intern!(py, "set_exception"), (exc.into_value(py),));
                }
                return false;
            }
        }
        // May append to the buffer.
        self.maybe_resume_protocol(slf, py);
        let (empty, closing, eof, waiter, sock) = {
            let mut inner = self.inner.lock().unwrap();
            let empty = inner.write_buffer.is_empty();
            if empty {
                inner.writer_registered = false;
            }
            (
                empty,
                inner.closing,
                inner.eof,
                if empty {
                    inner.empty_waiter.as_ref().map(|w| w.clone_ref(py))
                } else {
                    None
                },
                inner.sock.as_ref().map(|s| s.clone_ref(py)),
            )
        };
        if !empty {
            return true;
        }
        if let Some(waiter) = waiter {
            let _ = waiter
                .bind(py)
                .call_method1(intern!(py, "set_result"), (py.None(),));
        }
        if closing {
            if let Err(error) = self.call_connection_lost(slf, py, None) {
                error.print(py);
            }
        } else if eof {
            if let Some(sock) = sock {
                let _ = sock
                    .bind(py)
                    .call_method1(intern!(py, "shutdown"), (libc::SHUT_WR,));
            }
        }
        false
    }

    fn call_connection_lost(
        &self,
        slf: &Py<Self>,
        py: Python<'_>,
        exc: Option<PyErr>,
    ) -> PyResult<()> {
        let (protocol, connected, sock, server, takes_transport, waiter) = {
            let mut inner = self.inner.lock().unwrap();
            // Every later write is dropped as "connection lost" (the stdlib
            // relies on `_sock = None` raising into `_fatal_error` here).
            if inner.conn_lost == 0 {
                inner.conn_lost = 1;
            }
            // Break the protocol <-> transport cycle like `_SelectorTransport`
            // (`self._protocol = None`); the loop is process-lived.
            let protocol = std::mem::replace(&mut inner.protocol, py.None());
            let connected = std::mem::replace(&mut inner.protocol_connected, false);
            (
                protocol,
                connected,
                inner.sock.take(),
                inner.server.take(),
                inner.server_attach_takes_transport,
                inner.empty_waiter.take(),
            )
        };
        let result = if connected {
            let exc = exc.map(|e| e.into_value(py));
            protocol
                .bind(py)
                .call_method1(intern!(py, "connection_lost"), (exc,))
                .map(|_| ())
        } else {
            Ok(())
        };
        if let Some(sock) = sock {
            let _ = sock.bind(py).call_method0(intern!(py, "close"));
        }
        if let Some(server) = server {
            let detach = if takes_transport {
                server.bind(py).call_method1(intern!(py, "_detach"), (slf,))
            } else {
                server.bind(py).call_method0(intern!(py, "_detach"))
            };
            if let Err(error) = detach {
                error.print(py);
            }
        }
        if let Some(waiter) = waiter {
            let error =
                pyo3::exceptions::PyConnectionError::new_err("Connection is closed by peer");
            let _ = waiter
                .bind(py)
                .call_method1(intern!(py, "set_exception"), (error.into_value(py),));
        }
        result
    }

    /// Readable event from the pump.
    pub(crate) fn on_readable(&self, slf: &Py<Self>, py: Python<'_>) -> ReadyOutcome {
        let (fd, protocol, buffered) = {
            let inner = self.inner.lock().unwrap();
            if inner.conn_lost > 0 || !inner.is_reading() {
                return ReadyOutcome::Stop;
            }
            (
                inner.fd,
                inner.protocol.clone_ref(py),
                inner.buffered_protocol,
            )
        };
        let protocol = protocol.bind(py);
        if buffered {
            return self.read_into_protocol_buffer(slf, py, fd, protocol);
        }
        let read = RECV_BUFFER.with(|scratch| {
            let mut scratch = scratch.borrow_mut();
            // SAFETY: `scratch` is a live, exclusively borrowed MAX_SIZE buffer.
            let rc = unsafe { libc::recv(fd, scratch.as_mut_ptr().cast(), MAX_SIZE, 0) };
            if rc < 0 {
                Err(std::io::Error::last_os_error())
            } else {
                let n = rc as usize;
                Ok((
                    n,
                    if n == 0 {
                        None
                    } else {
                        Some(PyBytes::new(py, &scratch[..n]).unbind())
                    },
                ))
            }
        });
        match read {
            Err(error) => match error.raw_os_error() {
                Some(libc::EAGAIN) | Some(libc::EINTR) => ReadyOutcome::Drained,
                #[allow(unreachable_patterns)]
                Some(libc::EWOULDBLOCK) => ReadyOutcome::Drained,
                _ => {
                    self.fatal_error(
                        slf,
                        py,
                        os_error(py, error),
                        "Fatal read error on socket transport",
                    );
                    ReadyOutcome::Stop
                }
            },
            Ok((0, _)) => self.on_eof(slf, py, protocol),
            Ok((n, Some(data))) => {
                if let Err(error) = protocol.call_method1(intern!(py, "data_received"), (data,)) {
                    if is_system_exit_like(py, &error) {
                        error.print(py);
                    } else {
                        self.fatal_error(
                            slf,
                            py,
                            error,
                            "Fatal error: protocol.data_received() call failed.",
                        );
                    }
                    return ReadyOutcome::Stop;
                }
                self.after_read(n)
            }
            Ok((_, None)) => unreachable!("non-empty read without data"),
        }
    }

    fn after_read(&self, n: usize) -> ReadyOutcome {
        if !self.inner.lock().unwrap().is_reading() {
            // data_received paused or closed the transport.
            ReadyOutcome::Stop
        } else if n == MAX_SIZE {
            // A full read: the kernel may hold more (asyncio's selector is
            // level-triggered and would fire again).
            ReadyOutcome::StillReady
        } else {
            ReadyOutcome::Drained
        }
    }

    fn read_into_protocol_buffer(
        &self,
        slf: &Py<Self>,
        py: Python<'_>,
        fd: RawFd,
        protocol: &Bound<'_, PyAny>,
    ) -> ReadyOutcome {
        let buffer = match protocol
            .call_method1(intern!(py, "get_buffer"), (-1,))
            .and_then(|buf| PyBuffer::<u8>::get(&buf))
        {
            Ok(buffer) if buffer.len_bytes() > 0 && !buffer.readonly() => buffer,
            Ok(_) => {
                let error = PyRuntimeError::new_err("get_buffer() returned an empty buffer");
                self.fatal_error(
                    slf,
                    py,
                    error,
                    "Fatal error: protocol.get_buffer() call failed.",
                );
                return ReadyOutcome::Stop;
            }
            Err(error) => {
                if is_system_exit_like(py, &error) {
                    error.print(py);
                } else {
                    self.fatal_error(
                        slf,
                        py,
                        error,
                        "Fatal error: protocol.get_buffer() call failed.",
                    );
                }
                return ReadyOutcome::Stop;
            }
        };
        let capacity = buffer.len_bytes();
        // SAFETY: the buffer is writable and stays alive (we hold `buffer`)
        // for the duration of the call.
        let rc = unsafe { libc::recv(fd, buffer.buf_ptr().cast(), capacity, 0) };
        if rc < 0 {
            let error = std::io::Error::last_os_error();
            return match error.raw_os_error() {
                Some(libc::EAGAIN) | Some(libc::EINTR) => ReadyOutcome::Drained,
                #[allow(unreachable_patterns)]
                Some(libc::EWOULDBLOCK) => ReadyOutcome::Drained,
                _ => {
                    self.fatal_error(
                        slf,
                        py,
                        os_error(py, error),
                        "Fatal read error on socket transport",
                    );
                    ReadyOutcome::Stop
                }
            };
        }
        let n = rc as usize;
        drop(buffer);
        if n == 0 {
            return self.on_eof(slf, py, protocol);
        }
        if let Err(error) = protocol.call_method1(intern!(py, "buffer_updated"), (n,)) {
            if is_system_exit_like(py, &error) {
                error.print(py);
            } else {
                self.fatal_error(
                    slf,
                    py,
                    error,
                    "Fatal error: protocol.buffer_updated() call failed.",
                );
            }
            return ReadyOutcome::Stop;
        }
        if !self.inner.lock().unwrap().is_reading() {
            ReadyOutcome::Stop
        } else if n == capacity {
            ReadyOutcome::StillReady
        } else {
            ReadyOutcome::Drained
        }
    }

    /// `_read_ready__on_eof`.
    fn on_eof(&self, slf: &Py<Self>, py: Python<'_>, protocol: &Bound<'_, PyAny>) -> ReadyOutcome {
        match protocol.call_method0(intern!(py, "eof_received")) {
            Ok(keep_open) => {
                if keep_open.is_truthy().unwrap_or(false) {
                    // Keep the connection open for writes; nothing more to read.
                    // Stopping the watcher is `_remove_reader` here.
                } else {
                    self.close_impl(slf, py);
                }
                ReadyOutcome::Stop
            }
            Err(error) => {
                if is_system_exit_like(py, &error) {
                    error.print(py);
                } else {
                    self.fatal_error(
                        slf,
                        py,
                        error,
                        "Fatal error: protocol.eof_received() call failed.",
                    );
                }
                ReadyOutcome::Stop
            }
        }
    }

    /// Writable event from the pump.
    pub(crate) fn on_writable(&self, slf: &Py<Self>, py: Python<'_>) -> ReadyOutcome {
        if self.flush(slf, py) {
            ReadyOutcome::Drained
        } else {
            ReadyOutcome::Stop
        }
    }

    fn close_impl(&self, slf: &Py<Self>, py: Python<'_>) {
        let (fd, schedule_lost) = {
            let mut inner = self.inner.lock().unwrap();
            if inner.closing {
                return;
            }
            inner.closing = true;
            let schedule_lost = inner.write_buffer.is_empty();
            if schedule_lost {
                inner.conn_lost += 1;
                inner.writer_registered = false;
            }
            (inner.fd, schedule_lost)
        };
        crate::worker_loop::remove_fd_watch(fd, Direction::Read);
        if schedule_lost {
            crate::worker_loop::remove_fd_watch(fd, Direction::Write);
            if let Err(error) = slf
                .getattr(py, intern!(py, "_call_connection_lost"))
                .and_then(|method| self.call_soon(py, (method, py.None())))
            {
                error.print(py);
            }
        }
    }

    fn write_bytes(&self, slf: &Py<Self>, py: Python<'_>, data: &[u8]) -> PyResult<()> {
        let mut data = data;
        let (fd, buffer_empty) = {
            let inner = self.inner.lock().unwrap();
            (inner.fd, inner.write_buffer.is_empty())
        };
        if buffer_empty {
            // Optimization: try to send now.
            match send_now(fd, data) {
                SendOutcome::Sent(n) => {
                    data = &data[n..];
                    if data.is_empty() {
                        return Ok(());
                    }
                }
                SendOutcome::WouldBlock => {}
                SendOutcome::Failed(error) => {
                    self.fatal_error(
                        slf,
                        py,
                        os_error(py, error),
                        "Fatal write error on socket transport",
                    );
                    return Ok(());
                }
            }
            // Not all was written; register the write handler.
            self.watch(slf, py, Direction::Write)?;
            self.inner.lock().unwrap().writer_registered = true;
        }
        self.inner.lock().unwrap().write_buffer.extend(data);
        self.maybe_pause_protocol(slf, py);
        Ok(())
    }

    fn conn_lost_write(&self, py: Python<'_>) -> PyResult<bool> {
        let warn = {
            let mut inner = self.inner.lock().unwrap();
            if inner.conn_lost == 0 {
                return Ok(false);
            }
            let warn = inner.conn_lost >= LOG_THRESHOLD_FOR_CONNLOST_WRITES;
            inner.conn_lost += 1;
            warn
        };
        if warn {
            cached(py, &ASYNCIO_LOGGER, "asyncio.log", "logger")?
                .call_method1(intern!(py, "warning"), ("socket.send() raised exception.",))?;
        }
        Ok(true)
    }
}

fn with_buffer<R>(data: &Bound<'_, PyAny>, f: impl FnOnce(&[u8]) -> R) -> PyResult<R> {
    if let Ok(bytes) = data.cast::<PyBytes>() {
        return Ok(f(bytes.as_bytes()));
    }
    let buffer = PyBuffer::<u8>::get(data)?;
    if !buffer.is_c_contiguous() {
        return Err(PyValueError::new_err("data must be a contiguous buffer"));
    }
    // SAFETY: a C-contiguous u8 buffer of len_bytes() bytes, alive while
    // `buffer` is; no Python code runs while the slice is borrowed.
    let slice =
        unsafe { std::slice::from_raw_parts(buffer.buf_ptr().cast::<u8>(), buffer.len_bytes()) };
    let result = f(slice);
    drop(buffer);
    Ok(result)
}

#[pymethods]
impl SocketTransport {
    #[new]
    #[pyo3(signature = (loop_obj, sock, protocol, waiter, extra, server, server_attach_takes_transport))]
    fn new(
        py: Python<'_>,
        loop_obj: Py<PyAny>,
        sock: Py<PyAny>,
        protocol: Py<PyAny>,
        waiter: Option<Py<PyAny>>,
        extra: Py<PyDict>,
        server: Option<Py<PyAny>>,
        server_attach_takes_transport: bool,
    ) -> PyResult<Py<Self>> {
        let fd: RawFd = sock
            .bind(py)
            .call_method0(intern!(py, "fileno"))?
            .extract()?;
        let buffered_protocol = protocol.bind(py).is_instance(cached(
            py,
            &BUFFERED_PROTOCOL,
            "asyncio.protocols",
            "BufferedProtocol",
        )?)?;
        // Disable Nagle: small writes go out without waiting for the ACK.
        // asyncio ignores errors here (`_set_nodelay`), e.g. for Unix sockets.
        let socket_mod = py.import("socket")?;
        let _ = sock.bind(py).call_method1(
            intern!(py, "setsockopt"),
            (
                socket_mod.getattr("IPPROTO_TCP")?,
                socket_mod.getattr("TCP_NODELAY")?,
                1,
            ),
        );
        let transport = Py::new(
            py,
            Self {
                inner: Mutex::new(Inner {
                    loop_obj: loop_obj.clone_ref(py),
                    sock: Some(sock),
                    fd,
                    protocol: protocol.clone_ref(py),
                    protocol_connected: true,
                    buffered_protocol,
                    server: server.as_ref().map(|s| s.clone_ref(py)),
                    server_attach_takes_transport,
                    extra,
                    write_buffer: VecDeque::new(),
                    writer_registered: false,
                    conn_lost: 0,
                    closing: false,
                    paused: false,
                    eof: false,
                    protocol_paused: false,
                    high_water: DEFAULT_HIGH_WATER,
                    low_water: DEFAULT_HIGH_WATER / 4,
                    empty_waiter: None,
                }),
            },
        )?;
        if let Some(server) = server {
            if server_attach_takes_transport {
                server
                    .bind(py)
                    .call_method1(intern!(py, "_attach"), (&transport,))?;
            } else {
                server.bind(py).call_method0(intern!(py, "_attach"))?;
            }
        }
        let loop_bound = loop_obj.bind(py);
        loop_bound.call_method1(
            intern!(py, "call_soon"),
            (
                protocol.bind(py).getattr(intern!(py, "connection_made"))?,
                &transport,
            ),
        )?;
        // Only start reading once connection_made() has run.
        loop_bound.call_method1(
            intern!(py, "call_soon"),
            (transport.getattr(py, intern!(py, "_start_reading"))?,),
        )?;
        if let Some(waiter) = waiter {
            loop_bound.call_method1(
                intern!(py, "call_soon"),
                (
                    cached(
                        py,
                        &SET_RESULT_UNLESS_CANCELLED,
                        "asyncio.futures",
                        "_set_result_unless_cancelled",
                    )?,
                    waiter,
                    py.None(),
                ),
            )?;
        }
        Ok(transport)
    }

    #[classattr]
    fn _start_tls_compatible() -> bool {
        true
    }

    #[classattr]
    fn _sendfile_compatible(py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py
            .import("asyncio.constants")?
            .getattr("_SendfileMode")?
            .getattr("TRY_NATIVE")?
            .unbind())
    }

    #[classattr]
    fn max_size() -> usize {
        MAX_SIZE
    }

    fn __repr__(slf: &Bound<'_, Self>) -> String {
        let inner = slf.get().inner.lock().unwrap();
        let mut info = vec!["SocketTransport".to_string()];
        if inner.sock.is_none() {
            info.push("closed".into());
        } else if inner.closing {
            info.push("closing".into());
        }
        info.push(format!("fd={}", inner.fd));
        info.push(if inner.is_reading() {
            "read=polling".into()
        } else {
            "read=idle".into()
        });
        info.push(format!(
            "write=<{}, bufsize={}>",
            if inner.writer_registered {
                "polling"
            } else {
                "idle"
            },
            inner.write_buffer.len()
        ));
        format!("<{}>", info.join(" "))
    }

    /// Registers the read watcher (scheduled from the constructor after
    /// connection_made, and by resume_reading).
    fn _start_reading(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        if !slf.get().inner.lock().unwrap().is_reading() {
            return Ok(());
        }
        slf.get().watch(&slf.clone().unbind(), py, Direction::Read)
    }

    // ---- BaseTransport -------------------------------------------------

    #[pyo3(signature = (name, default=None))]
    fn get_extra_info(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let extra = self.inner.lock().unwrap().extra.clone_ref(py);
        match extra.bind(py).get_item(name)? {
            Some(value) => Ok(value.unbind()),
            None => Ok(default.unwrap_or_else(|| py.None())),
        }
    }

    fn is_closing(&self) -> bool {
        self.inner.lock().unwrap().closing
    }

    fn close(slf: &Bound<'_, Self>) {
        let py = slf.py();
        slf.get().close_impl(&slf.clone().unbind(), py);
    }

    fn set_protocol(&self, py: Python<'_>, protocol: Py<PyAny>) -> PyResult<()> {
        let buffered = protocol.bind(py).is_instance(cached(
            py,
            &BUFFERED_PROTOCOL,
            "asyncio.protocols",
            "BufferedProtocol",
        )?)?;
        let mut inner = self.inner.lock().unwrap();
        inner.protocol = protocol;
        inner.buffered_protocol = buffered;
        inner.protocol_connected = true;
        Ok(())
    }

    fn get_protocol(&self, py: Python<'_>) -> Py<PyAny> {
        self.inner.lock().unwrap().protocol.clone_ref(py)
    }

    // ---- ReadTransport -------------------------------------------------

    fn is_reading(&self) -> bool {
        self.inner.lock().unwrap().is_reading()
    }

    fn pause_reading(&self) {
        {
            let mut inner = self.inner.lock().unwrap();
            if !inner.is_reading() {
                return;
            }
            inner.paused = true;
        }
        self.unwatch(Direction::Read);
    }

    fn resume_reading(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        {
            let mut inner = slf.get().inner.lock().unwrap();
            if inner.closing || !inner.paused {
                return Ok(());
            }
            inner.paused = false;
        }
        slf.get().watch(&slf.clone().unbind(), py, Direction::Read)
    }

    // ---- WriteTransport ------------------------------------------------

    #[pyo3(signature = (high=None, low=None))]
    fn set_write_buffer_limits(
        slf: &Bound<'_, Self>,
        high: Option<usize>,
        low: Option<usize>,
    ) -> PyResult<()> {
        let py = slf.py();
        let high = match (high, low) {
            (Some(high), _) => high,
            (None, Some(low)) => 4 * low,
            (None, None) => DEFAULT_HIGH_WATER,
        };
        let low = low.unwrap_or(high / 4);
        if low > high {
            return Err(PyValueError::new_err(format!(
                "high ({high}) must be >= low ({low}) must be >= 0"
            )));
        }
        {
            let mut inner = slf.get().inner.lock().unwrap();
            inner.high_water = high;
            inner.low_water = low;
        }
        slf.get().maybe_pause_protocol(&slf.clone().unbind(), py);
        Ok(())
    }

    fn get_write_buffer_limits(&self) -> (usize, usize) {
        let inner = self.inner.lock().unwrap();
        (inner.low_water, inner.high_water)
    }

    fn get_write_buffer_size(&self) -> usize {
        self.inner.lock().unwrap().write_buffer.len()
    }

    fn write(slf: &Bound<'_, Self>, data: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        if !(data.is_instance_of::<PyBytes>()
            || data.is_instance_of::<pyo3::types::PyByteArray>()
            || data.is_instance_of::<pyo3::types::PyMemoryView>())
        {
            return Err(PyTypeError::new_err(format!(
                "data argument must be a bytes, bytearray, or memoryview object, not '{}'",
                data.get_type().name()?
            )));
        }
        {
            let inner = this.inner.lock().unwrap();
            if inner.eof {
                return Err(PyRuntimeError::new_err(
                    "Cannot call write() after write_eof()",
                ));
            }
            if inner.empty_waiter.is_some() {
                return Err(PyRuntimeError::new_err(
                    "unable to write; sendfile is in progress",
                ));
            }
        }
        if data.len()? == 0 {
            return Ok(());
        }
        if this.conn_lost_write(py)? {
            return Ok(());
        }
        let owner = slf.clone().unbind();
        with_buffer(data, |bytes| this.write_bytes(&owner, py, bytes))?
    }

    fn writelines(slf: &Bound<'_, Self>, list_of_data: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let this = slf.get();
        {
            let inner = this.inner.lock().unwrap();
            if inner.eof {
                return Err(PyRuntimeError::new_err(
                    "Cannot call writelines() after write_eof()",
                ));
            }
            if inner.empty_waiter.is_some() {
                return Err(PyRuntimeError::new_err(
                    "unable to writelines; sendfile is in progress",
                ));
            }
        }
        let mut chunks: Vec<Vec<u8>> = Vec::new();
        for item in list_of_data.try_iter()? {
            let item = item?;
            chunks.push(with_buffer(&item, |bytes| bytes.to_vec())?);
        }
        if chunks.iter().all(|chunk| chunk.is_empty()) {
            return Ok(());
        }
        if this.conn_lost_write(py)? {
            return Ok(());
        }
        let owner = slf.clone().unbind();
        {
            let mut inner = this.inner.lock().unwrap();
            for chunk in &chunks {
                inner.write_buffer.extend(chunk);
            }
        }
        if this.flush(&owner, py) {
            let registered = {
                let mut inner = this.inner.lock().unwrap();
                std::mem::replace(&mut inner.writer_registered, true)
            };
            if !registered {
                this.watch(&owner, py, Direction::Write)?;
            }
            this.maybe_pause_protocol(&owner, py);
        }
        Ok(())
    }

    fn write_eof(&self, py: Python<'_>) -> PyResult<()> {
        let sock = {
            let mut inner = self.inner.lock().unwrap();
            if inner.closing || inner.eof {
                return Ok(());
            }
            inner.eof = true;
            if inner.write_buffer.is_empty() {
                inner.sock.as_ref().map(|s| s.clone_ref(py))
            } else {
                None
            }
        };
        if let Some(sock) = sock {
            sock.bind(py)
                .call_method1(intern!(py, "shutdown"), (libc::SHUT_WR,))?;
        }
        Ok(())
    }

    fn can_write_eof(&self) -> bool {
        true
    }

    fn abort(slf: &Bound<'_, Self>) {
        let py = slf.py();
        slf.get().force_close(&slf.clone().unbind(), py, None);
    }

    // ---- private hooks used by asyncio (sslproto, sendfile, Server) ----

    fn _call_connection_lost(slf: &Bound<'_, Self>, exc: Option<Bound<'_, PyAny>>) -> PyResult<()> {
        let py = slf.py();
        let exc = exc.map(|value| PyErr::from_value(value));
        slf.get()
            .call_connection_lost(&slf.clone().unbind(), py, exc)
    }

    fn _make_empty_waiter(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let (loop_obj, empty) = {
            let inner = self.inner.lock().unwrap();
            if inner.empty_waiter.is_some() {
                return Err(PyRuntimeError::new_err("Empty waiter is already set"));
            }
            (inner.loop_obj.clone_ref(py), inner.write_buffer.is_empty())
        };
        let waiter = loop_obj
            .bind(py)
            .call_method0(intern!(py, "create_future"))?;
        if empty {
            waiter.call_method1(intern!(py, "set_result"), (py.None(),))?;
        }
        self.inner.lock().unwrap().empty_waiter = Some(waiter.clone().unbind());
        Ok(waiter.unbind())
    }

    fn _reset_empty_waiter(&self) {
        self.inner.lock().unwrap().empty_waiter = None;
    }

    #[getter]
    fn _sock(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.inner
            .lock()
            .unwrap()
            .sock
            .as_ref()
            .map(|s| s.clone_ref(py))
    }

    #[getter]
    fn _sock_fd(&self) -> RawFd {
        self.inner.lock().unwrap().fd
    }

    #[getter]
    fn _loop(&self, py: Python<'_>) -> Py<PyAny> {
        self.inner.lock().unwrap().loop_obj.clone_ref(py)
    }

    #[getter]
    fn _protocol_paused(&self) -> bool {
        self.inner.lock().unwrap().protocol_paused
    }

    #[getter]
    fn _closing(&self) -> bool {
        self.inner.lock().unwrap().closing
    }

    #[getter]
    fn _conn_lost(&self) -> usize {
        self.inner.lock().unwrap().conn_lost
    }

    fn __traverse__(&self, visit: pyo3::gc::PyVisit<'_>) -> Result<(), pyo3::gc::PyTraverseError> {
        // try_lock: a traversal must never block, and the GIL already
        // serializes us with every other user of the lock.
        if let Ok(inner) = self.inner.try_lock() {
            visit.call(&inner.protocol)?;
            visit.call(&inner.loop_obj)?;
            visit.call(&inner.extra)?;
            if let Some(sock) = &inner.sock {
                visit.call(sock)?;
            }
            if let Some(server) = &inner.server {
                visit.call(server)?;
            }
            if let Some(waiter) = &inner.empty_waiter {
                visit.call(waiter)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        if let Ok(mut inner) = self.inner.try_lock() {
            Python::attach(|py| {
                inner.protocol = py.None();
                inner.protocol_connected = false;
            });
            inner.sock = None;
            inner.server = None;
            inner.empty_waiter = None;
        }
    }
}
