//! Request lanes for routes with Django middleware.
//!
//! A lane is one OS thread with a pinned Python thread state. One lane serves
//! one request at a time. Thread-local state, such as a tenant database
//! connection, thus stays with its request. Lanes stay alive between
//! requests, so their database connections stay open, as with WSGI worker
//! threads.
//!
//! Lanes size themselves. A request takes an idle lane or starts a new one.
//! A lane that stays idle for `idle_time()` closes its connections and stops.
//!
//! A lane has two uses:
//! - `dispatch` runs a complete sync request on a lane, with no asyncio.
//! - `RequestLane` gives an async request one lane for its sync work. asgiref
//!   uses it as the executor of the request's thread-sensitive context.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::OnceLock;
use std::time::Duration;

use parking_lot::Mutex;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyTuple};
use tokio::sync::oneshot;

const DEFAULT_IDLE_SECONDS: f64 = 10.0;

/// The idle time after which a lane stops. `DJANGO_BOLT_LANE_IDLE_SECONDS` sets it.
fn idle_time() -> Duration {
    static IDLE: OnceLock<Duration> = OnceLock::new();
    *IDLE.get_or_init(|| parse_idle_time(std::env::var("DJANGO_BOLT_LANE_IDLE_SECONDS").ok()))
}

/// A value that is not a positive number in the range of `Duration` gives the default.
fn parse_idle_time(raw: Option<String>) -> Duration {
    raw.and_then(|raw| raw.parse::<f64>().ok())
        .filter(|seconds| *seconds > 0.0)
        .and_then(|seconds| Duration::try_from_secs_f64(seconds).ok())
        .unwrap_or(Duration::from_secs_f64(DEFAULT_IDLE_SECONDS))
}

enum Job {
    /// A complete sync request. The lane is idle again after it.
    Request {
        callable: Py<PyAny>,
        request: Py<PyAny>,
        done: Done,
    },
    /// One sync call of an async request. The request keeps the lane until `Release`.
    Call {
        func: Py<PyAny>,
        args: Py<PyTuple>,
        kwargs: Option<Py<PyDict>>,
        future: Py<PyAny>,
    },
    Release,
    Stop,
}

/// Where the result of a complete request goes.
enum Done {
    /// The server awaits the result on its Tokio runtime.
    Async(oneshot::Sender<PyResult<Py<PyAny>>>),
    /// The TestClient blocks its thread. Tokio channels cannot block inside a runtime.
    Blocking(Sender<PyResult<Py<PyAny>>>),
}

struct IdleLane {
    id: u64,
    sender: Sender<Job>,
}

/// Idle lanes, most recently used last. LIFO keeps a small hot set of lanes
/// busy and lets the others reach the idle time. A lane in this list has no
/// owner, so no job can be in flight to it.
static IDLE_LANES: Mutex<Vec<IdleLane>> = Mutex::new(Vec::new());
static NEXT_ID: AtomicU64 = AtomicU64::new(0);
static STOPPING: AtomicBool = AtomicBool::new(false);
/// `concurrent.futures.Future`, which `RequestLane.submit` creates for each call.
static FUTURE_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Take an idle lane or start a new one. The caller owns the lane until the
/// lane goes idle again.
fn acquire() -> PyResult<Sender<Job>> {
    if let Some(lane) = IDLE_LANES.lock().pop() {
        return Ok(lane.sender);
    }
    spawn_lane()
}

fn spawn_lane() -> PyResult<Sender<Job>> {
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let (sender, receiver) = channel::<Job>();
    let lane_sender = sender.clone();
    std::thread::Builder::new()
        .name(format!("bolt-lane-{id}"))
        .spawn(move || lane_main(id, lane_sender, receiver))
        .map_err(|err| PyRuntimeError::new_err(format!("could not start a request lane: {err}")))?;
    Ok(sender)
}

fn lane_main(id: u64, sender: Sender<Job>, receiver: Receiver<Job>) {
    crate::state::pin_python_thread_state();
    call_concurrency("mark_lane_thread");
    let idle = idle_time();
    // The spawner owns a new lane, so the first job arrives with no idle wait.
    let mut owned = true;
    // An async request is open from its first `Call` to its `Release`.
    let mut request_open = false;
    loop {
        let job = if owned {
            match receiver.recv() {
                Ok(job) => job,
                Err(_) => break,
            }
        } else {
            match receiver.recv_timeout(idle) {
                Ok(job) => job,
                Err(RecvTimeoutError::Timeout) => {
                    let mut idle = IDLE_LANES.lock();
                    match idle.iter().position(|lane| lane.id == id) {
                        // Still in the list under the lock: no owner, no job in flight.
                        Some(index) => {
                            idle.remove(index);
                            break;
                        }
                        // An owner took this lane. Its job arrives soon.
                        None => {
                            owned = true;
                            continue;
                        }
                    }
                }
                Err(RecvTimeoutError::Disconnected) => break,
            }
        };
        owned = true;
        match job {
            Job::Request {
                callable,
                request,
                done,
            } => {
                let result = Python::attach(|py| callable.call1(py, (request,)));
                // Go idle before the result leaves. The next request of this
                // client can then take this lane, and does not start a new one.
                owned = false;
                let stopping = !go_idle(id, &sender);
                // The receiver is gone when the client disconnected.
                match done {
                    Done::Async(done) => drop(done.send(result)),
                    Done::Blocking(done) => drop(done.send(result)),
                }
                if stopping {
                    break;
                }
            }
            Job::Call {
                func,
                args,
                kwargs,
                future,
            } => Python::attach(|py| {
                if !request_open {
                    request_open = true;
                    call_concurrency_attached(py, "open_lane_request");
                }
                run_call(py, func, args, kwargs, future);
            }),
            Job::Release => {
                // A `Release` follows at least one `Call`.
                request_open = false;
                call_concurrency("close_lane_request");
                owned = false;
                if !go_idle(id, &sender) {
                    break;
                }
            }
            Job::Stop => break,
        }
    }
    call_concurrency("close_lane_connections");
    crate::state::unpin_python_thread_state();
}

/// Put the lane on the idle list. Return false when the server stops.
fn go_idle(id: u64, sender: &Sender<Job>) -> bool {
    if STOPPING.load(Ordering::Relaxed) {
        return false;
    }
    IDLE_LANES.lock().push(IdleLane {
        id,
        sender: sender.clone(),
    });
    true
}

/// Complete a `concurrent.futures.Future` with the result of `func(*args, **kwargs)`.
fn run_call(
    py: Python<'_>,
    func: Py<PyAny>,
    args: Py<PyTuple>,
    kwargs: Option<Py<PyDict>>,
    future: Py<PyAny>,
) {
    let future = future.bind(py);
    let outcome = match future.call_method0(intern!(py, "set_running_or_notify_cancel")) {
        Ok(running) if !running.is_truthy().unwrap_or(false) => return,
        Ok(_) => match func.call(py, args.bind(py), kwargs.as_ref().map(|k| k.bind(py))) {
            Ok(value) => future.call_method1(intern!(py, "set_result"), (value,)),
            Err(err) => future.call_method1(intern!(py, "set_exception"), (err.into_value(py),)),
        },
        Err(err) => Err(err),
    };
    if let Err(err) = outcome {
        log::error!("request lane could not complete a future: {err}");
    }
}

/// Call a no-argument function of `django_bolt.concurrency` on the lane thread.
fn call_concurrency(function: &str) {
    Python::attach(|py| call_concurrency_attached(py, function));
}

fn call_concurrency_attached(py: Python<'_>, function: &str) {
    let called = py
        .import(intern!(py, "django_bolt.concurrency"))
        .and_then(|module| module.call_method0(function));
    if let Err(err) = called {
        log::warn!("request lane: {function} failed: {err}");
    }
}

fn submit_request(callable: Py<PyAny>, request: Py<PyAny>, done: Done) -> PyResult<()> {
    acquire()?
        .send(Job::Request {
            callable,
            request,
            done,
        })
        .map_err(|_| PyRuntimeError::new_err("request lane stopped"))
}

fn lane_stopped<T>(_: T) -> PyResult<Py<PyAny>> {
    Err(PyRuntimeError::new_err("request lane stopped"))
}

/// Run `callable(request)` on a lane. The future resolves with the result.
pub fn dispatch(
    callable: Py<PyAny>,
    request: Py<PyAny>,
) -> impl std::future::Future<Output = PyResult<Py<PyAny>>> + Send + 'static {
    let (done, result) = oneshot::channel();
    let submitted = submit_request(callable, request, Done::Async(done));
    async move {
        submitted?;
        result.await.unwrap_or_else(lane_stopped)
    }
}

/// Run `callable(request)` on a lane and block the calling thread. The GIL is
/// released during the wait. The TestClient uses this form.
pub fn dispatch_blocking(
    py: Python<'_>,
    callable: Py<PyAny>,
    request: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
    let (done, result) = channel();
    submit_request(callable, request, Done::Blocking(done))?;
    py.detach(move || result.recv())
        .unwrap_or_else(lane_stopped)
}

/// Stop all idle lanes. A busy lane stops after its current request.
pub fn shutdown() {
    STOPPING.store(true, Ordering::Relaxed);
    let idle = std::mem::take(&mut *IDLE_LANES.lock());
    for lane in idle {
        let _ = lane.sender.send(Job::Stop);
    }
}

/// The lane of one async request. It takes a lane at the first `submit`, so a
/// request with no sync work costs nothing. asgiref uses it as an executor.
#[pyclass(module = "django_bolt._core")]
pub struct RequestLane {
    sender: Mutex<Option<Sender<Job>>>,
}

#[pymethods]
impl RequestLane {
    #[new]
    fn new() -> Self {
        Self {
            sender: Mutex::new(None),
        }
    }

    /// Run `func(*args, **kwargs)` on the lane. Return a `concurrent.futures.Future`.
    #[pyo3(signature = (func, *args, **kwargs))]
    fn submit(
        &self,
        py: Python<'_>,
        func: Py<PyAny>,
        args: Py<PyTuple>,
        kwargs: Option<Py<PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let future = FUTURE_CLASS
            .get_or_try_init(py, || {
                py.import(intern!(py, "concurrent.futures"))?
                    .getattr(intern!(py, "Future"))
                    .map(Bound::unbind)
            })?
            .call0(py)?;
        let mut sender = self.sender.lock();
        let lane = match sender.take() {
            Some(lane) => lane,
            None => acquire()?,
        };
        sender
            .insert(lane)
            .send(Job::Call {
                func,
                args,
                kwargs,
                future: future.clone_ref(py),
            })
            .map_err(|_| PyRuntimeError::new_err("request lane stopped"))?;
        Ok(future)
    }

    /// Give the lane back. The request calls this at its end.
    fn release(&self) {
        if let Some(sender) = self.sender.lock().take() {
            let _ = sender.send(Job::Release);
        }
    }
}

impl Drop for RequestLane {
    fn drop(&mut self) {
        self.release();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn idle_time_uses_a_valid_value() {
        assert_eq!(
            parse_idle_time(Some("1.5".into())),
            Duration::from_millis(1500)
        );
    }

    #[test]
    fn idle_time_falls_back_to_the_default() {
        let default = Duration::from_secs_f64(DEFAULT_IDLE_SECONDS);
        for raw in ["1e100", "inf", "nan", "0", "-1", "soon"] {
            assert_eq!(parse_idle_time(Some(raw.into())), default, "{raw}");
        }
        assert_eq!(parse_idle_time(None), default);
    }
}
