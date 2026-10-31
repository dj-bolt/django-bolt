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
//! When the server stops, `shutdown` stops all lanes before Python exits.
//!
//! A lane has two uses:
//! - `dispatch` runs a complete sync request on a lane, with no asyncio.
//! - `RequestLane` gives an async request one lane for its sync work. asgiref
//!   uses it as the executor of the request's thread-sensitive context.

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender, channel};
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use parking_lot::{Mutex, RwLock};
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyTuple};
use tokio::sync::oneshot;

const DEFAULT_IDLE_SECONDS: f64 = 10.0;

/// How long `shutdown` and `stop_idle_lanes` wait for busy lanes. A lane can
/// still run the last call of an async request after the response.
const STOP_WAIT: Duration = Duration::from_secs(2);

static IDLE: OnceLock<Duration> = OnceLock::new();

/// The idle time after which a lane stops. `DJANGO_BOLT_LANE_IDLE_SECONDS` sets
/// it at startup (see `configure_idle_time`).
fn idle_time() -> Duration {
    *IDLE.get_or_init(|| Duration::from_secs_f64(DEFAULT_IDLE_SECONDS))
}

/// Read `DJANGO_BOLT_LANE_IDLE_SECONDS` once at startup, before a lane exists.
/// An invalid value raises `ImproperlyConfigured`. The first call sets the idle
/// time of the process; a later call only checks the variable.
pub fn configure_idle_time(py: Python<'_>) -> PyResult<()> {
    let raw =
        crate::settings::read_env(py, "DJANGO_BOLT_LANE_IDLE_SECONDS", "a number more than 0")?;
    let idle = parse_idle_time(raw.as_deref())
        .map_err(|message| crate::settings::improperly_configured(py, message))?;
    let _ = IDLE.set(idle);
    Ok(())
}

fn parse_idle_time(raw: Option<&str>) -> Result<Duration, String> {
    let seconds = crate::settings::parse_env(
        "DJANGO_BOLT_LANE_IDLE_SECONDS",
        raw,
        "a number more than 0",
        |seconds: &f64| *seconds > 0.0 && Duration::try_from_secs_f64(*seconds).is_ok(),
    )?;
    Ok(Duration::from_secs_f64(
        seconds.unwrap_or(DEFAULT_IDLE_SECONDS),
    ))
}

enum Job {
    /// A complete sync request. The lane is idle again after it. The server
    /// awaits the result on its Tokio runtime.
    Request {
        callable: Py<PyAny>,
        request: Py<PyAny>,
        done: oneshot::Sender<PyResult<Py<PyAny>>>,
    },
    /// One sync call of an async request. The request keeps the lane until `Release`.
    Call {
        func: Py<PyAny>,
        args: Py<PyTuple>,
        kwargs: Option<Py<PyDict>>,
        future: Py<PyAny>,
    },
    /// The end of an async request. `listed` is true when `RequestLane::release`
    /// put the lane in the idle list.
    Release { listed: bool },
    /// Stop the lane. The sender, if one exists, gets a message after the
    /// lane closed its database connections.
    Stop(Option<Sender<()>>),
}

struct IdleLane {
    id: u64,
    sender: Sender<Job>,
    /// The count of sent calls that the lane did not complete yet.
    pending: Arc<AtomicUsize>,
}

/// Idle lanes, most recently used last. LIFO keeps a small hot set of lanes
/// busy and lets the others reach the idle time. A lane in this list has no
/// owner and no pending call. Only the `Release` of its last async request
/// can be in flight to it, and the lane runs that `Release` before the jobs
/// of its next owner.
static IDLE_LANES: Mutex<Vec<IdleLane>> = Mutex::new(Vec::new());
/// The id and the sender of each lane that is alive, idle or not. A lane
/// leaves this list after it released its Python thread state.
static LANES: Mutex<Vec<(u64, Sender<Job>)>> = Mutex::new(Vec::new());
static NEXT_ID: AtomicU64 = AtomicU64::new(0);
/// True after `shutdown`. It sets the flag under the locks of `IDLE_LANES` and
/// `LANES`. Thus no lane goes idle and no lane starts after it.
static STOPPING: AtomicBool = AtomicBool::new(false);
/// True after `shutdown` stopped its wait for the lanes. Then Python can
/// finalize, and a lane must not attach.
static PYTHON_CLOSED: AtomicBool = AtomicBool::new(false);
/// A lane holds the read lock while it attaches at its start and at its end.
/// `shutdown` takes the write lock after it sets `PYTHON_CLOSED`, so it waits
/// for a lane that read the flag before.
static PYTHON_GATE: RwLock<()> = RwLock::new(());
/// `concurrent.futures.Future`, which `RequestLane.submit` creates for each call.
static FUTURE_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Take an idle lane or start a new one. The caller owns the lane until the
/// lane goes idle again.
fn acquire() -> PyResult<IdleLane> {
    if let Some(lane) = IDLE_LANES.lock().pop() {
        return Ok(lane);
    }
    spawn_lane()
}

fn spawn_lane() -> PyResult<IdleLane> {
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let (sender, receiver) = channel::<Job>();
    {
        let mut lanes = LANES.lock();
        // `shutdown` does not wait for a lane that starts after it.
        if STOPPING.load(Ordering::Relaxed) {
            return Err(PyRuntimeError::new_err("request lanes stopped"));
        }
        lanes.push((id, sender.clone()));
    }
    let lane_sender = sender.clone();
    let pending = Arc::new(AtomicUsize::new(0));
    let lane_pending = Arc::clone(&pending);
    std::thread::Builder::new()
        .name(format!("bolt-lane-{id}"))
        .spawn(move || lane_main(id, lane_sender, lane_pending, receiver))
        .map_err(|err| {
            unlist(id);
            PyRuntimeError::new_err(format!("could not start a request lane: {err}"))
        })?;
    Ok(IdleLane {
        id,
        sender,
        pending,
    })
}

fn lane_main(id: u64, sender: Sender<Job>, pending: Arc<AtomicUsize>, receiver: Receiver<Job>) {
    let started = while_python_open(|| {
        crate::state::pin_python_thread_state();
        call_concurrency("mark_lane_thread");
    });
    if !started {
        unlist(id);
        return;
    }
    let idle = idle_time();
    // The spawner owns a new lane, so the first job arrives with no idle wait.
    let mut owned = true;
    let mut stopped = None;
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
        // The server stopped, so no request waits for a result. Do not run
        // the job: it could attach to Python after `shutdown` stopped its wait.
        if STOPPING.load(Ordering::Relaxed) {
            if let Job::Stop(ack) = job {
                stopped = ack;
            }
            break;
        }
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
                let stopping = !go_idle(id, &sender, &pending);
                // The receiver is gone when the client disconnected.
                drop(done.send(result));
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
                run_call(py, func, args, kwargs, future, &pending);
            }),
            Job::Release { listed } => {
                // A `Release` follows at least one `Call`.
                request_open = false;
                call_concurrency("close_lane_request");
                owned = false;
                // A listed lane can have jobs of its next owner after this one,
                // so it must not stop here. A lane that `RequestLane::release`
                // did not list goes idle now, after the calls of its request.
                if !listed && !go_idle(id, &sender, &pending) {
                    break;
                }
            }
            Job::Stop(ack) => {
                stopped = ack;
                break;
            }
        }
    }
    while_python_open(|| {
        call_concurrency("close_lane_connections");
        crate::state::unpin_python_thread_state();
    });
    unlist(id);
    if let Some(ack) = stopped {
        let _ = ack.send(());
    }
}

/// Run `work` unless `shutdown` stopped its wait for the lanes. Return false
/// when the work did not run.
fn while_python_open(work: impl FnOnce()) -> bool {
    let _gate = PYTHON_GATE.read();
    let open = !PYTHON_CLOSED.load(Ordering::SeqCst);
    if open {
        work();
    }
    open
}

fn unlist(id: u64) {
    LANES.lock().retain(|(lane, _)| *lane != id);
}

/// Put the lane on the idle list. Return false when the server stops.
fn go_idle(id: u64, sender: &Sender<Job>, pending: &Arc<AtomicUsize>) -> bool {
    let mut idle = IDLE_LANES.lock();
    // Read the flag under the lock: `shutdown` empties the list under it.
    if STOPPING.load(Ordering::Relaxed) {
        return false;
    }
    idle.push(IdleLane {
        id,
        sender: sender.clone(),
        pending: Arc::clone(pending),
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
    pending: &AtomicUsize,
) {
    let future = future.bind(py);
    let called = match future.call_method0(intern!(py, "set_running_or_notify_cancel")) {
        Ok(running) if !running.is_truthy().unwrap_or(false) => Ok(None),
        Ok(_) => Ok(Some(func.call(
            py,
            args.bind(py),
            kwargs.as_ref().map(|k| k.bind(py)),
        ))),
        Err(err) => Err(err),
    };
    // The call ended. Count it before the future completes. Thus the request
    // that awaits the future sees no pending call when it releases the lane.
    pending.fetch_sub(1, Ordering::Release);
    let outcome = match called {
        Ok(None) => return,
        Ok(Some(Ok(value))) => future.call_method1(intern!(py, "set_result"), (value,)),
        Ok(Some(Err(err))) => {
            future.call_method1(intern!(py, "set_exception"), (err.into_value(py),))
        }
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

fn submit_request(
    callable: Py<PyAny>,
    request: Py<PyAny>,
    done: oneshot::Sender<PyResult<Py<PyAny>>>,
) -> PyResult<()> {
    acquire()?
        .sender
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
    let submitted = submit_request(callable, request, done);
    async move {
        submitted?;
        result.await.unwrap_or_else(lane_stopped)
    }
}

/// Stop all lanes. Wait until each one closed its database connections and
/// released its Python thread state, up to `STOP_WAIT`. The server calls this
/// after it stopped, before Python exits. After this, a lane does not attach
/// to Python again. Return the count of lanes that did not stop in the wait.
///
/// A busy lane stops after its current job. A lane can also stay with an
/// async request that never ends, for example when its worker stopped.
pub fn shutdown() -> usize {
    let lanes: Vec<Sender<Job>> = {
        let mut idle = IDLE_LANES.lock();
        let lanes = LANES.lock();
        STOPPING.store(true, Ordering::Relaxed);
        idle.clear();
        lanes.iter().map(|(_, sender)| sender.clone()).collect()
    };
    for sender in lanes {
        // A send fails only when the lane thread is gone. Then no stop is necessary.
        let _ = sender.send(Job::Stop(None));
    }
    let deadline = Instant::now() + STOP_WAIT;
    while !LANES.lock().is_empty() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(1));
    }
    // A lane that reads the flag after this does not attach. Wait, up to the
    // same limit, for a lane that read it before. That lane is still in
    // `LANES` if it does not complete in time.
    PYTHON_CLOSED.store(true, Ordering::SeqCst);
    drop(PYTHON_GATE.try_write_until(deadline));
    LANES.lock().len()
}

/// Stop all lanes and wait until each one closed its database connections.
/// The test clients call this at exit. If not, a lane connection stays open
/// for the idle time, and it blocks the drop of the test database.
///
/// This waits for busy lanes, up to `STOP_WAIT`. A lane that a different
/// client still uses stays alive after that time.
#[pyfunction]
pub fn stop_idle_lanes(py: Python<'_>) {
    py.detach(|| {
        let deadline = Instant::now() + STOP_WAIT;
        loop {
            let idle = std::mem::take(&mut *IDLE_LANES.lock());
            let (ack, stopped) = channel();
            for lane in idle {
                let _ = lane.sender.send(Job::Stop(Some(ack.clone())));
            }
            drop(ack);
            // The channel closes after the last stopped lane sent its message.
            while stopped.recv().is_ok() {}
            if LANES.lock().is_empty() || Instant::now() >= deadline {
                return;
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    });
}

/// The lane of one async request. It takes a lane at the first `submit`, so a
/// request with no sync work costs nothing. asgiref uses it as an executor.
#[pyclass(module = "django_bolt._core")]
pub struct RequestLane {
    lane: Mutex<Option<IdleLane>>,
}

#[pymethods]
impl RequestLane {
    #[new]
    fn new() -> Self {
        Self {
            lane: Mutex::new(None),
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
        let mut owned = self.lane.lock();
        let lane = match owned.take() {
            Some(lane) => lane,
            None => acquire()?,
        };
        let lane = owned.insert(lane);
        lane.pending.fetch_add(1, Ordering::Relaxed);
        lane.sender
            .send(Job::Call {
                func,
                args,
                kwargs,
                future: future.clone_ref(py),
            })
            .map_err(|_| PyRuntimeError::new_err("request lane stopped"))?;
        Ok(future)
    }

    /// Give the lane back. The request calls this at its end, before the
    /// response leaves.
    ///
    /// A lane with no pending call goes into the idle list here, not when its
    /// thread runs the `Release`. The next request of the client can then
    /// take this lane at once. The channel keeps the order: the lane closes
    /// this request before it runs the next one.
    ///
    /// A lane that still runs a call of this request goes to no other
    /// request. Its thread puts it in the idle list after that call.
    fn release(&self) {
        let Some(lane) = self.lane.lock().take() else {
            return;
        };
        // Hold the lock from the send to the push. Thus the lane cannot reach
        // its idle time before it is in the list. This send does not block.
        let mut idle = IDLE_LANES.lock();
        let stopping = STOPPING.load(Ordering::Relaxed);
        let listed = !stopping && lane.pending.load(Ordering::Acquire) == 0;
        // A send fails only when the lane thread is gone. Then the lane is not reusable.
        if lane.sender.send(Job::Release { listed }).is_err() {
            return;
        }
        if listed {
            idle.push(lane);
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

    /// The tests that use `STOPPING` or `IDLE_LANES` run one at a time.
    static GLOBALS: Mutex<()> = Mutex::new(());

    /// A request lane with no lane thread. The receiver gets its jobs.
    fn request_lane(id: u64, pending: usize) -> (RequestLane, Receiver<Job>) {
        let (sender, receiver) = channel::<Job>();
        let lane = RequestLane {
            lane: Mutex::new(Some(IdleLane {
                id,
                sender,
                pending: Arc::new(AtomicUsize::new(pending)),
            })),
        };
        (lane, receiver)
    }

    /// Remove the lane from the idle list. Return true when it was there.
    fn take_idle(id: u64) -> bool {
        let mut idle = IDLE_LANES.lock();
        let index = idle.iter().position(|lane| lane.id == id);
        index.map(|index| idle.remove(index)).is_some()
    }

    #[test]
    fn release_lists_a_lane_with_no_pending_call() {
        let _globals = GLOBALS.lock();
        let (lane, receiver) = request_lane(u64::MAX - 1, 0);
        lane.release();

        assert!(matches!(
            receiver.try_recv(),
            Ok(Job::Release { listed: true })
        ));
        assert!(receiver.try_recv().is_err());
        assert!(take_idle(u64::MAX - 1));
    }

    /// A request can stop waiting for a call that its lane still runs.
    #[test]
    fn release_does_not_list_a_lane_with_a_pending_call() {
        let _globals = GLOBALS.lock();
        let (lane, receiver) = request_lane(u64::MAX - 2, 1);
        lane.release();

        assert!(matches!(
            receiver.try_recv(),
            Ok(Job::Release { listed: false })
        ));
        assert!(receiver.try_recv().is_err());
        assert!(!take_idle(u64::MAX - 2));
    }

    /// Shutdown can take the idle list before a request releases its lane.
    /// The lane then stops at the `Release`, so it must not go into the list.
    #[test]
    fn release_during_shutdown_does_not_list_the_lane() {
        let _globals = GLOBALS.lock();
        let (lane, receiver) = request_lane(u64::MAX, 0);
        STOPPING.store(true, Ordering::Relaxed);
        lane.release();
        STOPPING.store(false, Ordering::Relaxed);

        assert!(matches!(
            receiver.try_recv(),
            Ok(Job::Release { listed: false })
        ));
        assert!(receiver.try_recv().is_err());
        assert!(!take_idle(u64::MAX));
    }

    /// A request that ends with no `release` call, for example when its task
    /// is cancelled, still gives its lane back. Else the lane waits for a job
    /// of that request forever and never reaches its idle time.
    #[test]
    fn drop_releases_the_lane() {
        let _globals = GLOBALS.lock();
        let (lane, receiver) = request_lane(u64::MAX - 3, 0);
        drop(lane);

        assert!(matches!(
            receiver.try_recv(),
            Ok(Job::Release { listed: true })
        ));
        assert!(take_idle(u64::MAX - 3));
    }

    /// `shutdown` waits until `LANES` is empty, so a stopped lane must leave it.
    #[test]
    fn unlist_removes_only_its_lane() {
        let _globals = GLOBALS.lock();
        let (sender, _receiver) = channel::<Job>();
        LANES.lock().push((u64::MAX - 4, sender.clone()));
        LANES.lock().push((u64::MAX - 5, sender));

        unlist(u64::MAX - 4);
        let ids: Vec<u64> = LANES.lock().iter().map(|(id, _)| *id).collect();
        unlist(u64::MAX - 5);

        assert_eq!(ids, vec![u64::MAX - 5]);
        assert!(LANES.lock().is_empty());
    }

    /// `shutdown` waits only for the lanes that started before it.
    #[test]
    fn no_lane_starts_during_shutdown() {
        let _globals = GLOBALS.lock();
        STOPPING.store(true, Ordering::Relaxed);
        let spawned = spawn_lane();
        STOPPING.store(false, Ordering::Relaxed);

        assert!(spawned.is_err());
        assert!(LANES.lock().is_empty());
    }

    #[test]
    fn idle_time_uses_a_valid_value() {
        assert_eq!(
            parse_idle_time(Some("1.5")),
            Ok(Duration::from_millis(1500))
        );
    }

    #[test]
    fn idle_time_defaults_when_unset_or_empty() {
        let default = Duration::from_secs_f64(DEFAULT_IDLE_SECONDS);
        assert_eq!(parse_idle_time(None), Ok(default));
        assert_eq!(parse_idle_time(Some("")), Ok(default));
    }

    #[test]
    fn idle_time_rejects_an_invalid_value() {
        for raw in ["1e100", "inf", "nan", "0", "-1", "soon"] {
            assert_eq!(
                parse_idle_time(Some(raw)),
                Err(format!(
                    "DJANGO_BOLT_LANE_IDLE_SECONDS must be a number more than 0, got '{raw}'."
                )),
                "{raw}"
            );
        }
    }
}
