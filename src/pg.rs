//! Run a query on a libpq connection that psycopg opened. The wait for the
//! database is on the Tokio reactor, and no Python code runs during it.
//!
//! psycopg opens the connection, so the connection parameters, TLS and the
//! authentication are those of psycopg. This module calls the functions of
//! the libpq that psycopg loaded, through their addresses.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::os::fd::{BorrowedFd, OwnedFd};
use std::sync::Arc;
use tokio::io::Interest;
use tokio::io::unix::AsyncFd;

type Conn = *mut c_void;
type Res = *mut c_void;

const PGRES_COMMAND_OK: c_int = 1;
const PGRES_TUPLES_OK: c_int = 2;
const PG_DIAG_SQLSTATE: c_int = b'C' as c_int;

#[derive(Clone, Copy)]
struct Api {
    send_query: unsafe extern "C" fn(Conn, *const c_char) -> c_int,
    flush: unsafe extern "C" fn(Conn) -> c_int,
    consume_input: unsafe extern "C" fn(Conn) -> c_int,
    is_busy: unsafe extern "C" fn(Conn) -> c_int,
    get_result: unsafe extern "C" fn(Conn) -> Res,
    result_status: unsafe extern "C" fn(Res) -> c_int,
    ntuples: unsafe extern "C" fn(Res) -> c_int,
    nfields: unsafe extern "C" fn(Res) -> c_int,
    ftype: unsafe extern "C" fn(Res, c_int) -> u32,
    getvalue: unsafe extern "C" fn(Res, c_int, c_int) -> *const c_char,
    getlength: unsafe extern "C" fn(Res, c_int, c_int) -> c_int,
    getisnull: unsafe extern "C" fn(Res, c_int, c_int) -> c_int,
    clear: unsafe extern "C" fn(Res),
    socket: unsafe extern "C" fn(Conn) -> c_int,
    result_error_field: unsafe extern "C" fn(Res, c_int) -> *const c_char,
    result_error_message: unsafe extern "C" fn(Res) -> *const c_char,
    error_message: unsafe extern "C" fn(Conn) -> *const c_char,
}

fn address(addresses: &Bound<'_, PyDict>, name: &str) -> PyResult<usize> {
    let value = addresses
        .get_item(name)?
        .ok_or_else(|| PyValueError::new_err(format!("libpq function {name} has no address")))?;
    let address: usize = value.extract()?;
    if address == 0 {
        return Err(PyValueError::new_err(format!(
            "libpq function {name} has no address"
        )));
    }
    Ok(address)
}

impl Api {
    fn from_addresses(addresses: &Bound<'_, PyDict>) -> PyResult<Self> {
        // SAFETY: each address is the address of the libpq function with this
        // name and this C signature, in the libpq of the connection.
        unsafe {
            Ok(Self {
                send_query: std::mem::transmute::<usize, _>(address(addresses, "PQsendQuery")?),
                flush: std::mem::transmute::<usize, _>(address(addresses, "PQflush")?),
                consume_input: std::mem::transmute::<usize, _>(address(
                    addresses,
                    "PQconsumeInput",
                )?),
                is_busy: std::mem::transmute::<usize, _>(address(addresses, "PQisBusy")?),
                get_result: std::mem::transmute::<usize, _>(address(addresses, "PQgetResult")?),
                result_status: std::mem::transmute::<usize, _>(address(
                    addresses,
                    "PQresultStatus",
                )?),
                ntuples: std::mem::transmute::<usize, _>(address(addresses, "PQntuples")?),
                nfields: std::mem::transmute::<usize, _>(address(addresses, "PQnfields")?),
                ftype: std::mem::transmute::<usize, _>(address(addresses, "PQftype")?),
                getvalue: std::mem::transmute::<usize, _>(address(addresses, "PQgetvalue")?),
                getlength: std::mem::transmute::<usize, _>(address(addresses, "PQgetlength")?),
                getisnull: std::mem::transmute::<usize, _>(address(addresses, "PQgetisnull")?),
                clear: std::mem::transmute::<usize, _>(address(addresses, "PQclear")?),
                socket: std::mem::transmute::<usize, _>(address(addresses, "PQsocket")?),
                result_error_field: std::mem::transmute::<usize, _>(address(
                    addresses,
                    "PQresultErrorField",
                )?),
                result_error_message: std::mem::transmute::<usize, _>(address(
                    addresses,
                    "PQresultErrorMessage",
                )?),
                error_message: std::mem::transmute::<usize, _>(address(
                    addresses,
                    "PQerrorMessage",
                )?),
            })
        }
    }
}

/// The addresses of a connection and of its results. Only one task uses a
/// connection at a time, as libpq requires.
#[derive(Clone, Copy)]
struct Pointer(usize);

// SAFETY: a libpq connection can move between threads. One thread uses it at a time.
unsafe impl Send for Pointer {}

struct State {
    api: Api,
    conn: Pointer,
    /// A duplicate of the socket of the connection, in the reactor of the runtime of the first query.
    watcher: tokio::sync::Mutex<Option<AsyncFd<OwnedFd>>>,
    /// The psycopg connection. It owns the libpq connection, so it must live as long as this object.
    _owner: Py<PyAny>,
}

#[pyclass(frozen)]
pub struct LibpqConnection {
    state: Arc<State>,
}

fn connection_error(api: &Api, conn: Conn) -> String {
    // SAFETY: `conn` is a live connection. The message is a C string of libpq.
    unsafe {
        let message = (api.error_message)(conn);
        if message.is_null() {
            String::new()
        } else {
            CStr::from_ptr(message).to_string_lossy().trim_end().to_owned()
        }
    }
}

/// Send `sql` and wait for its results. Returns the address of each result.
async fn run(state: &State, sql: CString) -> Result<Vec<Pointer>, String> {
    let api = state.api;
    let conn = state.conn;
    let mut slot = state.watcher.lock().await;
    if slot.is_none() {
        // SAFETY: libpq keeps the socket open while the connection is open.
        let socket = unsafe { BorrowedFd::borrow_raw((api.socket)(conn.0 as Conn)) };
        let duplicate = socket.try_clone_to_owned().map_err(|e| e.to_string())?;
        // SAFETY: `duplicate` is our own duplicate of the socket. The AsyncFd
        // owns it, so it stays open and keeps its number until the AsyncFd drops.
        let watcher = unsafe {
            AsyncFd::register_with_interest(duplicate, Interest::READABLE | Interest::WRITABLE)
        }
        .map_err(|e| std::io::Error::from(e).to_string())?;
        *slot = Some(watcher);
    }
    let watcher = slot.as_ref().expect("the watcher is set above");

    // SAFETY: `conn` is a live connection, and this task is its only user.
    if unsafe { (api.send_query)(conn.0 as Conn, sql.as_ptr()) } == 0 {
        return Err(connection_error(&api, conn.0 as Conn));
    }
    loop {
        // SAFETY: as above.
        match unsafe { (api.flush)(conn.0 as Conn) } {
            0 => break,
            1 => {
                let mut ready = watcher.writable().await.map_err(|e| e.to_string())?;
                ready.clear_ready();
            }
            _ => return Err(connection_error(&api, conn.0 as Conn)),
        }
    }

    let mut results = Vec::with_capacity(1);
    loop {
        // SAFETY: as above.
        while unsafe { (api.is_busy)(conn.0 as Conn) } != 0 {
            let mut ready = watcher.readable().await.map_err(|e| e.to_string())?;
            // SAFETY: as above.
            if unsafe { (api.consume_input)(conn.0 as Conn) } == 0 {
                let message = connection_error(&api, conn.0 as Conn);
                for result in results {
                    let result: Pointer = result;
                    // SAFETY: the result came from PQgetResult and is cleared one time.
                    unsafe { (api.clear)(result.0 as Res) };
                }
                return Err(message);
            }
            // SAFETY: as above.
            if unsafe { (api.is_busy)(conn.0 as Conn) } != 0 {
                ready.clear_ready();
            }
        }
        // SAFETY: as above. The connection is not busy, so the call does not block.
        let result = unsafe { (api.get_result)(conn.0 as Conn) };
        if result.is_null() {
            return Ok(results);
        }
        results.push(Pointer(result as usize));
    }
}

/// The payload for Python: `(0, oids, rows)` for rows, `(1, None, None)` for
/// a statement with no rows, `(2, sqlstate, message)` for an error.
fn payload(py: Python<'_>, api: &Api, results: &[Pointer]) -> PyResult<Py<PyAny>> {
    let mut chosen: Option<Res> = None;
    for result in results {
        let result = result.0 as Res;
        // SAFETY: the result is live until `clear` below.
        let status = unsafe { (api.result_status)(result) };
        if status != PGRES_COMMAND_OK && status != PGRES_TUPLES_OK {
            chosen = Some(result);
            break;
        }
        if chosen.is_none() {
            chosen = Some(result);
        }
    }
    let Some(result) = chosen else {
        return Ok((1, py.None(), py.None()).into_pyobject(py)?.into_any().unbind());
    };
    // SAFETY: the result is live, and each index is in its range.
    unsafe {
        let status = (api.result_status)(result);
        if status == PGRES_COMMAND_OK {
            return Ok((1, py.None(), py.None()).into_pyobject(py)?.into_any().unbind());
        }
        if status != PGRES_TUPLES_OK {
            let state = (api.result_error_field)(result, PG_DIAG_SQLSTATE);
            let state = if state.is_null() {
                None
            } else {
                Some(CStr::from_ptr(state).to_string_lossy().into_owned())
            };
            let message = (api.result_error_message)(result);
            let message = if message.is_null() {
                String::new()
            } else {
                CStr::from_ptr(message).to_string_lossy().trim_end().to_owned()
            };
            return Ok((2, state, message).into_pyobject(py)?.into_any().unbind());
        }
        let columns = (api.nfields)(result);
        let count = (api.ntuples)(result);
        let oids = PyTuple::new(py, (0..columns).map(|column| (api.ftype)(result, column)))?;
        let rows = PyList::empty(py);
        for row in 0..count {
            let mut values: Vec<Py<PyAny>> = Vec::with_capacity(columns as usize);
            for column in 0..columns {
                if (api.getisnull)(result, row, column) != 0 {
                    values.push(py.None());
                } else {
                    let start = (api.getvalue)(result, row, column) as *const u8;
                    let length = (api.getlength)(result, row, column) as usize;
                    let bytes = std::slice::from_raw_parts(start, length);
                    values.push(PyBytes::new(py, bytes).into_any().unbind());
                }
            }
            rows.append(PyTuple::new(py, values)?)?;
        }
        Ok((0, oids, rows).into_pyobject(py)?.into_any().unbind())
    }
}

#[pymethods]
impl LibpqConnection {
    /// `addresses` maps the name of each libpq function to its address.
    /// `connection` is the address of the `PGconn`. `owner` keeps it open.
    #[new]
    fn new(addresses: &Bound<'_, PyDict>, connection: usize, owner: Py<PyAny>) -> PyResult<Self> {
        if connection == 0 {
            return Err(PyValueError::new_err("the connection has no address"));
        }
        Ok(Self {
            state: Arc::new(State {
                api: Api::from_addresses(addresses)?,
                conn: Pointer(connection),
                watcher: tokio::sync::Mutex::new(None),
                _owner: owner,
            }),
        })
    }

    /// Send `sql`. When the results are here, `call_soon(done, payload)` runs
    /// on the event loop. See `payload` for its forms. A connection error
    /// gives `(3, None, message)`.
    fn query(&self, sql: &[u8], call_soon: Py<PyAny>, done: Py<PyAny>) -> PyResult<()> {
        let sql = CString::new(sql)
            .map_err(|_| PyValueError::new_err("a query cannot have a NUL character"))?;
        let runtime = tokio::runtime::Handle::try_current().map_err(|_| {
            PyRuntimeError::new_err("a query needs the Tokio runtime of a worker thread")
        })?;
        let state = Arc::clone(&self.state);
        runtime.spawn(async move {
            let outcome = run(&state, sql).await;
            Python::attach(|py| {
                let payload = match &outcome {
                    Ok(results) => payload(py, &state.api, results),
                    Err(message) => (3, py.None(), message.as_str())
                        .into_pyobject(py)
                        .map(|value| value.into_any().unbind()),
                };
                if let Ok(results) = &outcome {
                    for result in results {
                        // SAFETY: each result came from PQgetResult and is cleared one time.
                        unsafe { (state.api.clear)(result.0 as Res) };
                    }
                }
                let delivered = payload.and_then(|payload| call_soon.call1(py, (done, payload)));
                if let Err(error) = delivered {
                    error.print(py);
                }
            });
        });
        Ok(())
    }

    /// Stop the watch of the socket. Call this before the connection closes.
    fn release(&self) {
        if let Ok(mut slot) = self.state.watcher.try_lock() {
            *slot = None;
        }
    }
}
