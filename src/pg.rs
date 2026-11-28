//! Send a query on a libpq connection that psycopg opened, and read its result.
//!
//! psycopg opens the connection. Thus the connection parameters, TLS, the
//! authentication and the notice handlers are those of psycopg. This module
//! calls the functions of the libpq that psycopg loaded, through their
//! addresses. The wait for the database is on the Tokio reactor, and no
//! Python code runs during it.
//!
//! A connection is in the pipeline mode of libpq. Many statements can be in
//! flight on it at one time, and their results come back in the order of the
//! statements. Each statement has its own sync point, so each is its own
//! transaction, and an error of one statement does not fail the next one.
//! One task for each connection, `drive`, makes every call into libpq.
//!
//! A slow statement holds back each statement behind it. `stalled` tells the
//! pool when the first statement in flight is slow, and the pool then sends
//! the next statement on another connection.
//!
//! Each call into libpq is in a `step`, which holds the lock of the
//! connection. `close` takes the same lock, so libpq gets no call after
//! `close` returns. Python then frees the connection.
//!
//! libpq can call the notice receiver of psycopg in a step, and the receiver
//! takes the GIL. Thus a thread that holds the GIL must not wait for the
//! lock: `cancel` and `close` release the GIL first.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use std::collections::VecDeque;
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::os::fd::{AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;
use tokio::io::Interest;
use tokio::io::unix::AsyncFd;

type Conn = *mut c_void;
type Res = *mut c_void;

const PGRES_EMPTY_QUERY: c_int = 0;
const PGRES_COMMAND_OK: c_int = 1;
const PGRES_TUPLES_OK: c_int = 2;
const PGRES_COPY_OUT: c_int = 3;
const PGRES_COPY_IN: c_int = 4;
const PGRES_FATAL_ERROR: c_int = 7;
const PGRES_COPY_BOTH: c_int = 8;
const PGRES_PIPELINE_SYNC: c_int = 10;

/// The codes of `PQresultErrorField`: each field of an error of the server.
const ERROR_FIELDS: &[u8] = b"SVCMDHPpqWstcdnFLR";

/// The kinds of result that `query` gives to Python.
const ROWS: i32 = 0;
const NO_ROWS: i32 = 1;
const SERVER_ERROR: i32 = 2;
const CONNECTION_ERROR: i32 = 3;
const STALE: i32 = 4;
const CANCEL_SENT: i32 = 5;

/// The time in nanoseconds since the first call. Never 0: 0 is "no time".
fn clock() -> u64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_nanos() as u64 + 1
}

/// Declare the libpq functions that this module calls: the table of their
/// addresses, and their names for Python.
macro_rules! libpq {
    ($($field:ident = $name:literal: fn($($argument:ty),*) -> $result:ty;)*) => {
        #[derive(Clone, Copy)]
        struct Api {
            $($field: unsafe extern "C" fn($($argument),*) -> $result,)*
        }

        impl Api {
            fn from_addresses(addresses: &Bound<'_, PyDict>) -> PyResult<Self> {
                Ok(Self {
                    $($field: {
                        let address = address(addresses, $name)?;
                        // SAFETY: the caller gives the address of the libpq
                        // function with this name, which has this C signature.
                        unsafe {
                            std::mem::transmute::<
                                usize,
                                unsafe extern "C" fn($($argument),*) -> $result,
                            >(address)
                        }
                    },)*
                })
            }
        }

        const FUNCTIONS: &[&str] = &[$($name),*];
    };
}

libpq! {
    flush = "PQflush": fn(Conn) -> c_int;
    consume_input = "PQconsumeInput": fn(Conn) -> c_int;
    is_busy = "PQisBusy": fn(Conn) -> c_int;
    get_result = "PQgetResult": fn(Conn) -> Res;
    socket = "PQsocket": fn(Conn) -> c_int;
    error_message = "PQerrorMessage": fn(Conn) -> *const c_char;
    result_status = "PQresultStatus": fn(Res) -> c_int;
    ntuples = "PQntuples": fn(Res) -> c_int;
    nfields = "PQnfields": fn(Res) -> c_int;
    ftype = "PQftype": fn(Res, c_int) -> u32;
    getvalue = "PQgetvalue": fn(Res, c_int, c_int) -> *const c_char;
    getlength = "PQgetlength": fn(Res, c_int, c_int) -> c_int;
    getisnull = "PQgetisnull": fn(Res, c_int, c_int) -> c_int;
    result_error_field = "PQresultErrorField": fn(Res, c_int) -> *const c_char;
    result_error_message = "PQresultErrorMessage": fn(Res) -> *const c_char;
    clear = "PQclear": fn(Res) -> ();
    get_cancel = "PQgetCancel": fn(Conn) -> *mut c_void;
    cancel = "PQcancel": fn(*mut c_void, *mut c_char, c_int) -> c_int;
    free_cancel = "PQfreeCancel": fn(*mut c_void) -> ();
    enter_pipeline_mode = "PQenterPipelineMode": fn(Conn) -> c_int;
    send_query_params = "PQsendQueryParams": fn(Conn, *const c_char, c_int, *const c_void, *const c_void, *const c_void, *const c_void, c_int) -> c_int;
    pipeline_sync = "PQpipelineSync": fn(Conn) -> c_int;
}

fn address(addresses: &Bound<'_, PyDict>, name: &str) -> PyResult<usize> {
    let missing = || PyValueError::new_err(format!("libpq function {name} has no address"));
    let address: usize = addresses.get_item(name)?.ok_or_else(missing)?.extract()?;
    if address == 0 {
        return Err(missing());
    }
    Ok(address)
}

/// The address of a connection or of a result.
#[derive(Clone, Copy)]
struct Pointer(usize);

/// The results of one query. Each result is cleared one time, also when the
/// task of the query stops before its end.
struct Results {
    api: Api,
    list: Vec<Pointer>,
}

impl Drop for Results {
    fn drop(&mut self) {
        for result in &self.list {
            // SAFETY: the result came from PQgetResult. A result stays valid
            // after its connection closes, until this call.
            unsafe { (self.api.clear)(result.0 as Res) };
        }
    }
}

/// Why a query has no results.
enum Failure {
    /// `close` ran. Nothing waits for this query.
    Closed,
    /// The connection ended while it was idle. The server did not get the statement.
    Stale(String),
    Connection(String),
}

struct Link {
    /// The watch of a duplicate of the socket. None after `close`.
    watcher: Option<Arc<AsyncFd<OwnedFd>>>,
    /// The task that drives the connection.
    task: Option<tokio::task::AbortHandle>,
}

struct Shared {
    api: Api,
    conn: Pointer,
    link: Mutex<Link>,
    /// The time at which the first statement in flight became the first, or 0 with no statement in flight.
    head_since: AtomicU64,
    runtime: tokio::runtime::Handle,
    /// `call_soon` of the event loop. Each thread can call it.
    call_soon: Py<PyAny>,
    /// `deliver(token, result)`, which runs on the event loop.
    deliver: Py<PyAny>,
    /// The psycopg connection. It frees the libpq connection when Python
    /// frees it, so it lives as long as a task can use the connection.
    _owner: Py<PyAny>,
}

impl Shared {
    /// Call libpq with the connection. No step starts after `close`.
    fn step<T>(&self, step: impl FnOnce(&Api, Conn) -> Result<T, Failure>) -> Result<T, Failure> {
        let link = self.link.lock().unwrap();
        if link.watcher.is_none() {
            return Err(Failure::Closed);
        }
        step(&self.api, self.conn.0 as Conn)
    }
}

fn connection_failure(api: &Api, conn: Conn, action: &str) -> Failure {
    // SAFETY: `conn` is a live connection. The message is a C string of libpq.
    let message = unsafe {
        let message = (api.error_message)(conn);
        if message.is_null() {
            String::new()
        } else {
            CStr::from_ptr(message).to_string_lossy().trim().to_owned()
        }
    };
    Failure::Connection(format!("{action} failed: {message}"))
}

fn watch_failure(error: std::io::Error) -> Failure {
    Failure::Connection(format!("the watch of the database socket failed: {error}"))
}

/// The events of `events` that the socket has now.
///
/// The reactor reports an event one time, and tokio keeps it until a caller
/// clears it. libpq does the reads and the writes, so this module cannot see
/// which call found the socket empty. It asks the kernel.
fn socket_events(fd: RawFd, events: libc::c_short) -> libc::c_short {
    let mut pollfd = libc::pollfd {
        fd,
        events,
        revents: 0,
    };
    // SAFETY: `pollfd` is a valid, exclusively borrowed array of one entry.
    let count = unsafe { libc::poll(&mut pollfd, 1, 0) };
    if count < 0 {
        // An error such as EINTR. A call that finds no data is harmless, and a lost event is not.
        return events;
    }
    pollfd.revents
}

/// A statement and the token that Python gets back with its result.
type Statement = (CString, Py<PyAny>);

/// How a statement ended.
enum Outcome {
    Results(Results),
    /// The server did not get the statement. Another connection can send it.
    Stale(String),
    /// The connection ended after the send. The server can have run the statement.
    Failed(String),
}

/// A statement that ended: its token, its outcome and its service time.
///
/// The service time is in nanoseconds, from the send to the result. Only a
/// statement that went out on an idle connection has one. The time of a
/// statement behind another one includes the wait for that one.
type Ended = (Py<PyAny>, Outcome, Option<u64>);

/// Give each ended statement to the event loop as
/// `deliver(token, result, seconds)`: see `payload`.
fn deliver(shared: &Shared, done: &mut Vec<Ended>) {
    if done.is_empty() {
        return;
    }
    Python::attach(|py| {
        for (token, outcome, service) in done.drain(..) {
            let message = |kind: i32, message: &str| {
                message
                    .into_pyobject(py)
                    .map_err(PyErr::from)
                    .and_then(|message| triple(py, kind, none(py), message.into_any()))
            };
            let result = match &outcome {
                Outcome::Results(results) => payload(py, &shared.api, &results.list),
                Outcome::Stale(text) => message(STALE, text),
                Outcome::Failed(text) => message(CONNECTION_ERROR, text),
            };
            drop(outcome);
            let seconds = service.map(|nanoseconds| nanoseconds as f64 / 1e9);
            let delivered = result.and_then(|result| {
                shared
                    .call_soon
                    .call1(py, (&shared.deliver, token, result, seconds))
            });
            if let Err(error) = delivered {
                error.print(py);
            }
        }
    });
}

/// Take the first statement in flight, which has all its results.
fn end_first(
    shared: &Shared,
    in_flight: &mut VecDeque<(Py<PyAny>, u64)>,
    results: Results,
) -> Ended {
    let (token, sent) = in_flight.pop_front().expect("a statement is in flight");
    let now = clock();
    // The server starts the next statement now.
    let head = if in_flight.is_empty() { 0 } else { now };
    shared.head_since.store(head, Ordering::Relaxed);
    let service = (sent != 0).then(|| now.saturating_sub(sent));
    (token, Outcome::Results(results), service)
}

/// Drive one connection: send each statement of `queue`, and read the results in their order.
async fn drive(
    shared: Arc<Shared>,
    watcher: Arc<AsyncFd<OwnedFd>>,
    mut queue: tokio::sync::mpsc::UnboundedReceiver<Statement>,
) {
    let fd = watcher.get_ref().as_raw_fd();
    // The statements that libpq did not get yet.
    let mut waiting: VecDeque<Statement> = VecDeque::new();
    // Each statement that libpq got, in the order of the results: its token,
    // and the time of its send when the connection was idle then, else 0.
    let mut in_flight: VecDeque<(Py<PyAny>, u64)> = VecDeque::new();
    // The results of the first statement in flight that are here.
    let mut current = Results {
        api: shared.api,
        list: Vec::with_capacity(1),
    };
    let mut done: Vec<Ended> = Vec::new();
    // True while libpq holds data that the socket did not take.
    let mut unsent = false;
    // False after Python dropped the connection object.
    let mut open = true;
    // A failure of the watch of the socket, for the next turn of the loop.
    let mut watch: Result<(), Failure> = Ok(());
    loop {
        while let Ok(statement) = queue.try_recv() {
            waiting.push_back(statement);
        }
        let step = std::mem::replace(&mut watch, Ok(())).and_then(|()| {
            shared.step(|api, conn| {
                // SAFETY: `conn` is a live connection in pipeline mode, and a step is its only user.
                unsafe {
                    if !waiting.is_empty() && in_flight.is_empty() {
                        // An idle connection has no input. Input before the
                        // statement is a message that no query asked for, and
                        // then usually the end of the connection: a server that
                        // stops sends a last message and closes a moment later.
                        if socket_events(fd, libc::POLLIN) != 0 {
                            return Err(Failure::Stale(
                                "the server sent data or closed the connection while it was idle"
                                    .to_owned(),
                            ));
                        }
                    }
                    while let Some((sql, token)) = waiting.pop_front() {
                        let null = std::ptr::null();
                        let sent = if in_flight.is_empty() {
                            let now = clock();
                            shared.head_since.store(now, Ordering::Relaxed);
                            now
                        } else {
                            0
                        };
                        // From here, a failure is a failure of this statement. libpq
                        // sends a large statement in parts, so the server can have
                        // a statement that libpq then reports as not sent.
                        in_flight.push_back((token, sent));
                        unsent = true;
                        if (api.send_query_params)(conn, sql.as_ptr(), 0, null, null, null, null, 0)
                            == 0
                            || (api.pipeline_sync)(conn) == 0
                        {
                            return Err(connection_failure(api, conn, "sending query"));
                        }
                    }
                    if unsent {
                        match (api.flush)(conn) {
                            0 => unsent = false,
                            1 => {}
                            _ => return Err(connection_failure(api, conn, "sending query")),
                        }
                    }
                    if !in_flight.is_empty()
                        && socket_events(fd, libc::POLLIN) != 0
                        && (api.consume_input)(conn) == 0
                    {
                        return Err(connection_failure(api, conn, "consuming input"));
                    }
                    // PQgetResult gives a null result after the results of each
                    // statement. The count stops a loop on null results.
                    let mut ends = 0;
                    while !in_flight.is_empty() && ends < 3 && (api.is_busy)(conn) == 0 {
                        let result = (api.get_result)(conn);
                        if result.is_null() {
                            ends += 1;
                            continue;
                        }
                        ends = 0;
                        let status = (api.result_status)(result);
                        if status == PGRES_PIPELINE_SYNC {
                            // The sync point of the first statement: its results are complete.
                            (api.clear)(result);
                            let results = std::mem::replace(
                                &mut current,
                                Results {
                                    api: *api,
                                    list: Vec::with_capacity(1),
                                },
                            );
                            done.push(end_first(&shared, &mut in_flight, results));
                            continue;
                        }
                        current.list.push(Pointer(result as usize));
                        if matches!(status, PGRES_COPY_OUT | PGRES_COPY_IN | PGRES_COPY_BOTH) {
                            // The connection stays in the copy. Python reports
                            // the statement, and the connection ends here.
                            let results = std::mem::replace(
                                &mut current,
                                Results {
                                    api: *api,
                                    list: Vec::new(),
                                },
                            );
                            done.push(end_first(&shared, &mut in_flight, results));
                            return Err(Failure::Connection(
                                "the connection is in a COPY".to_owned(),
                            ));
                        }
                    }
                }
                Ok(())
            })
        });
        match step {
            Ok(()) => {}
            // `close` ran. Nothing waits for these statements.
            Err(Failure::Closed) => return,
            Err(failure) => {
                let (stale, text) = match failure {
                    Failure::Stale(text) => (true, text),
                    Failure::Connection(text) => (false, text),
                    Failure::Closed => unreachable!("handled above"),
                };
                for (token, _sent) in in_flight.drain(..) {
                    done.push((token, Outcome::Failed(text.clone()), None));
                }
                // No statement comes after this point, and the server got none of these.
                queue.close();
                while let Ok(statement) = queue.try_recv() {
                    waiting.push_back(statement);
                }
                let reason = if stale {
                    text
                } else {
                    format!("the connection ended: {text}")
                };
                for (_sql, token) in waiting.drain(..) {
                    done.push((token, Outcome::Stale(reason.clone()), None));
                }
                deliver(&shared, &mut done);
                return;
            }
        }
        deliver(&shared, &mut done);
        if !open && in_flight.is_empty() && waiting.is_empty() {
            return;
        }
        // tokio keeps an event until a caller clears it. libpq does the reads
        // and the writes, so the kernel tells if the event is still true.
        let clear = |guard: std::io::Result<tokio::io::unix::AsyncFdReadyGuard<'_, OwnedFd>>,
                     events: libc::c_short| {
            guard.map_err(watch_failure).map(|mut guard| {
                if socket_events(fd, events) == 0 {
                    guard.clear_ready();
                }
            })
        };
        watch = tokio::select! {
            biased;
            statement = queue.recv(), if open => {
                match statement {
                    Some(statement) => waiting.push_back(statement),
                    None => open = false,
                }
                Ok(())
            }
            guard = watcher.readable(), if !in_flight.is_empty() => clear(guard, libc::POLLIN),
            guard = watcher.writable(), if unsent => clear(guard, libc::POLLOUT),
        };
    }
}

fn triple<'py>(
    py: Python<'py>,
    kind: i32,
    first: Bound<'py, PyAny>,
    second: Bound<'py, PyAny>,
) -> PyResult<Py<PyAny>> {
    let kind = kind.into_pyobject(py)?.into_any();
    Ok(PyTuple::new(py, [kind, first, second])?.into_any().unbind())
}

fn none(py: Python<'_>) -> Bound<'_, PyAny> {
    py.None().into_bound(py)
}

/// The result of a query for Python:
///
/// - `(ROWS, oids, rows)`: the type of each column, and each row as a tuple
///   of `bytes` or `None`.
/// - `(NO_ROWS, status, None)`: a result with no rows, with its libpq status.
/// - `(SERVER_ERROR, fields, message)`: the error fields as `{code: bytes}`,
///   and the error message of libpq as `bytes`.
///
/// An error in one statement fails the query, as in psycopg. In other
/// conditions the result of the first statement is the result of the query.
fn payload(py: Python<'_>, api: &Api, results: &[Pointer]) -> PyResult<Py<PyAny>> {
    let Some(first) = results.first() else {
        let message = "the query gave no result".into_pyobject(py)?.into_any();
        return triple(py, CONNECTION_ERROR, none(py), message);
    };
    // SAFETY: each result is live until its `Results` drops, and each index is in its range.
    unsafe {
        let status = |result: &Pointer| (api.result_status)(result.0 as Res);
        let chosen = results
            .iter()
            .find(|result| {
                !matches!(
                    status(result),
                    PGRES_EMPTY_QUERY | PGRES_COMMAND_OK | PGRES_TUPLES_OK
                )
            })
            .unwrap_or(first);
        let result = chosen.0 as Res;
        match status(chosen) {
            PGRES_TUPLES_OK => {
                let columns = (api.nfields)(result);
                let oids =
                    PyTuple::new(py, (0..columns).map(|column| (api.ftype)(result, column)))?;
                let rows = PyList::empty(py);
                for row in 0..(api.ntuples)(result) {
                    let values = (0..columns).map(|column| {
                        if (api.getisnull)(result, row, column) != 0 {
                            return none(py);
                        }
                        let start = (api.getvalue)(result, row, column) as *const u8;
                        let length = (api.getlength)(result, row, column) as usize;
                        PyBytes::new(py, std::slice::from_raw_parts(start, length)).into_any()
                    });
                    rows.append(PyTuple::new(py, values)?)?;
                }
                triple(py, ROWS, oids.into_any(), rows.into_any())
            }
            PGRES_FATAL_ERROR => {
                let fields = PyDict::new(py);
                for code in ERROR_FIELDS {
                    let value = (api.result_error_field)(result, c_int::from(*code));
                    if !value.is_null() {
                        fields
                            .set_item(*code, PyBytes::new(py, CStr::from_ptr(value).to_bytes()))?;
                    }
                }
                let message = (api.result_error_message)(result);
                let message = if message.is_null() {
                    PyBytes::new(py, b"")
                } else {
                    PyBytes::new(py, CStr::from_ptr(message).to_bytes())
                };
                triple(py, SERVER_ERROR, fields.into_any(), message.into_any())
            }
            status => triple(py, NO_ROWS, status.into_pyobject(py)?.into_any(), none(py)),
        }
    }
}

/// A libpq connection of psycopg, with its queries sent from Rust.
#[pyclass(frozen)]
pub struct LibpqConnection {
    shared: Arc<Shared>,
    /// The statements for `drive`.
    queue: tokio::sync::mpsc::UnboundedSender<Statement>,
}

#[pymethods]
impl LibpqConnection {
    /// The names of the libpq functions that `addresses` must have.
    #[classattr]
    #[pyo3(name = "FUNCTIONS")]
    fn functions() -> Vec<&'static str> {
        FUNCTIONS.to_vec()
    }

    #[classattr]
    #[pyo3(name = "ROWS")]
    fn rows() -> i32 {
        ROWS
    }

    #[classattr]
    #[pyo3(name = "NO_ROWS")]
    fn no_rows() -> i32 {
        NO_ROWS
    }

    #[classattr]
    #[pyo3(name = "SERVER_ERROR")]
    fn server_error() -> i32 {
        SERVER_ERROR
    }

    #[classattr]
    #[pyo3(name = "CONNECTION_ERROR")]
    fn connection_error() -> i32 {
        CONNECTION_ERROR
    }

    #[classattr]
    #[pyo3(name = "STALE")]
    fn stale() -> i32 {
        STALE
    }

    #[classattr]
    #[pyo3(name = "CANCEL_SENT")]
    fn cancel_sent() -> i32 {
        CANCEL_SENT
    }

    /// `addresses` maps the name of each libpq function to its address, in
    /// the libpq that holds the connection. `connection` is the address of
    /// the `PGconn`, and `socket` is its socket. `owner` keeps the connection
    /// open. A result goes to the event loop as
    /// `call_soon(deliver, token, result, seconds)`.
    ///
    /// Call this function on a thread of the Tokio runtime of the event loop.
    #[new]
    fn new(
        addresses: &Bound<'_, PyDict>,
        connection: usize,
        socket: RawFd,
        owner: Py<PyAny>,
        call_soon: Py<PyAny>,
        deliver: Py<PyAny>,
    ) -> PyResult<Self> {
        if connection == 0 {
            return Err(PyValueError::new_err("the connection has no address"));
        }
        let api = Api::from_addresses(addresses)?;
        // SAFETY: `connection` is the address of a live connection. A libpq
        // other than the one of the connection gives a different socket here.
        if unsafe { (api.socket)(connection as Conn) } != socket {
            return Err(PyValueError::new_err(
                "the libpq functions are not those of the connection",
            ));
        }
        let runtime = tokio::runtime::Handle::try_current().map_err(|_| {
            PyRuntimeError::new_err("a connection needs the Tokio runtime of an event loop")
        })?;
        // SAFETY: libpq keeps the socket open while the connection is open.
        let duplicate = unsafe { BorrowedFd::borrow_raw(socket) }
            .try_clone_to_owned()
            .map_err(PyErr::from)?;
        let watcher = {
            let _guard = runtime.enter();
            // SAFETY: `duplicate` is our own duplicate of the socket. The
            // AsyncFd owns it, so it stays open and keeps its number until
            // the AsyncFd drops.
            unsafe {
                AsyncFd::register_with_interest(duplicate, Interest::READABLE | Interest::WRITABLE)
            }
            .map_err(|error| PyErr::from(std::io::Error::from(error)))?
        };
        // SAFETY: `connection` is a live, idle connection, and no task uses it yet.
        if unsafe { (api.enter_pipeline_mode)(connection as Conn) } != 1 {
            return Err(PyRuntimeError::new_err(
                "the connection did not enter the pipeline mode",
            ));
        }
        let watcher = Arc::new(watcher);
        let shared = Arc::new(Shared {
            api,
            conn: Pointer(connection),
            link: Mutex::new(Link {
                watcher: Some(Arc::clone(&watcher)),
                task: None,
            }),
            head_since: AtomicU64::new(0),
            runtime,
            call_soon,
            deliver,
            _owner: owner,
        });
        let (queue, statements) = tokio::sync::mpsc::unbounded_channel();
        let task = shared
            .runtime
            .spawn(drive(Arc::clone(&shared), watcher, statements));
        shared.link.lock().unwrap().task = Some(task.abort_handle());
        Ok(Self { shared, queue })
    }

    /// Send `sql` after the statements that are in flight. Returns false
    /// when the connection ended: the server does not get the statement.
    ///
    /// The event loop gets `deliver(token, result, seconds)` when the
    /// statement ends: see `payload` and `Ended`. A connection that ended after the send gives
    /// `(CONNECTION_ERROR, None, message)`. A connection that ended before
    /// the send gives `(STALE, None, message)`. A statement that `close`
    /// stops gives no call.
    fn query(&self, sql: &[u8], token: Py<PyAny>) -> PyResult<bool> {
        let sql = CString::new(sql)
            .map_err(|_| PyValueError::new_err("a query cannot have a NUL character"))?;
        Ok(self.queue.send((sql, token)).is_ok())
    }

    /// True when the first statement in flight is the first for more than
    /// `after` seconds, and the socket has no data. A statement behind it
    /// waits for it.
    ///
    /// The event loop and the task of the connection can share one thread.
    /// The task then does not run while the loop runs Python code, and a
    /// result waits in the socket. Such a statement is not slow.
    fn stalled(&self, after: f64) -> bool {
        let head = self.shared.head_since.load(Ordering::Relaxed);
        if head == 0 || clock().saturating_sub(head) as f64 <= after * 1e9 {
            return false;
        }
        // A task that holds the lock reads the socket now.
        let Ok(link) = self.shared.link.try_lock() else {
            return false;
        };
        link.watcher
            .as_ref()
            .is_some_and(|watcher| socket_events(watcher.get_ref().as_raw_fd(), libc::POLLIN) == 0)
    }

    /// Call `function(*arguments)` while no task calls libpq with the
    /// connection. psycopg reads the connection to escape a parameter.
    #[pyo3(signature = (function, *arguments))]
    fn exclusive(
        &self,
        py: Python<'_>,
        function: Bound<'_, PyAny>,
        arguments: Bound<'_, PyTuple>,
    ) -> PyResult<Py<PyAny>> {
        if let Ok(_link) = self.shared.link.try_lock() {
            return function.call1(arguments).map(Bound::unbind);
        }
        // A task is in libpq, and it can need the GIL: see the module text.
        let (function, arguments) = (function.unbind(), arguments.unbind());
        py.detach(|| {
            let _link = self.shared.link.lock().unwrap();
            Python::attach(|py| function.call1(py, arguments.into_bound(py)))
        })
    }

    /// Ask the server to stop the statement that it runs on this connection.
    /// The caller makes sure that only its statement is in flight. Returns
    /// false when the connection is closed or libpq gives no cancel request.
    ///
    /// The request opens a connection of its own and blocks, so a blocking
    /// thread of the runtime sends it, with no GIL. When the request ended,
    /// the event loop gets `deliver(token, (CANCEL_SENT, None, error), None)`.
    /// `error` is None, or the message of libpq.
    fn cancel(&self, py: Python<'_>, token: Py<PyAny>) -> bool {
        let shared = Arc::clone(&self.shared);
        let request = py.detach(|| {
            let link = shared.link.lock().unwrap();
            // None after `close`.
            link.watcher.as_ref()?;
            // SAFETY: the connection is live, and the lock keeps the task of the query out of libpq.
            let request = unsafe { (shared.api.get_cancel)(shared.conn.0 as Conn) };
            (!request.is_null()).then_some(Pointer(request as usize))
        });
        let Some(request) = request else {
            return false;
        };
        let runtime = shared.runtime.clone();
        runtime.spawn_blocking(move || {
            let api = shared.api;
            let mut buffer = [0 as c_char; 256];
            // SAFETY: the request came from PQgetCancel. It has its own copy
            // of the address of the server, so it stays valid when the
            // connection closes. This thread is its only user, and frees it one time.
            let error = unsafe {
                let sent = (api.cancel)(request.0 as *mut c_void, buffer.as_mut_ptr(), 256);
                (api.free_cancel)(request.0 as *mut c_void);
                (sent == 0).then(|| {
                    CStr::from_ptr(buffer.as_ptr())
                        .to_string_lossy()
                        .trim()
                        .to_owned()
                })
            };
            Python::attach(move |py| {
                let delivered = error
                    .into_pyobject(py)
                    .map_err(PyErr::from)
                    .and_then(|error| triple(py, CANCEL_SENT, none(py), error.into_any()))
                    .and_then(|result| {
                        shared
                            .call_soon
                            .call1(py, (&shared.deliver, token, result, py.None()))
                    });
                if let Err(error) = delivered {
                    error.print(py);
                }
            });
        });
        true
    }

    /// Stop the query in flight and the watch of the socket. After this call
    /// returns, this object makes no call into libpq, and the caller can
    /// close the connection.
    fn close(&self, py: Python<'_>) {
        let task = py.detach(|| {
            let mut link = self.shared.link.lock().unwrap();
            link.watcher = None;
            link.task.take()
        });
        if let Some(task) = task {
            task.abort();
        }
    }
}
