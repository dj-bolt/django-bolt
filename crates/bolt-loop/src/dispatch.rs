//! Running a Python callable on the WorkerLoop from Rust.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;

use crate::scheduler::{get_loop, get_worker_fn};

static WORKER_DISPATCH_START: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_DISPATCH_START_CANCELLABLE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static WORKER_SYNC_DISPATCH: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

#[pyclass(frozen)]
struct WorkerDispatchResolver {
    tx: std::sync::Mutex<Option<tokio::sync::oneshot::Sender<PyResult<Py<PyAny>>>>>,
}

#[pymethods]
impl WorkerDispatchResolver {
    fn set_result(&self, value: Py<PyAny>) {
        if let Some(tx) = self.tx.lock().unwrap().take() {
            let _ = tx.send(Ok(value));
        }
    }

    fn set_exception(&self, exc: Bound<'_, PyAny>) {
        if let Some(tx) = self.tx.lock().unwrap().take() {
            let _ = tx.send(Err(PyErr::from_value(exc)));
        }
    }
}

/// Deliberate: if the caller's future is dropped (client disconnect), the
/// Python task is NOT cancelled — it runs to completion and its result is
/// discarded, consistent with tasks being allowed to outlive responses.
pub async fn dispatch(dispatch: Py<PyAny>, request: Py<PyAny>) -> PyResult<Py<PyAny>> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    Python::attach(|py| {
        let resolver = Py::new(
            py,
            WorkerDispatchResolver {
                tx: std::sync::Mutex::new(Some(tx)),
            },
        )?;
        let start = get_worker_fn(py, &WORKER_DISPATCH_START, "worker_dispatch_start")?;
        start.call1(py, (dispatch, request, get_loop(py)?, resolver))?;
        Ok::<_, PyErr>(())
    })?;

    match rx.await {
        Ok(result) => result,
        Err(_) => Err(pyo3::exceptions::PyRuntimeError::new_err(
            "worker dispatch resolver dropped without a result",
        )),
    }
}

/// Holds the asyncio Task created by `worker_dispatch_start_cancellable` so a
/// later cancellation can reach it. Filled from the WorkerLoop thread.
#[pyclass(frozen)]
struct WorkerTaskHandle {
    task: std::sync::Mutex<Option<Py<PyAny>>>,
    cancelled: std::sync::atomic::AtomicBool,
}

#[pymethods]
impl WorkerTaskHandle {
    fn set_task(&self, task: Bound<'_, PyAny>) {
        let mut slot = self.task.lock().unwrap();
        if self.cancelled.load(std::sync::atomic::Ordering::SeqCst) {
            drop(slot);
            let _ = task.call_method0("cancel");
        } else {
            *slot = Some(task.unbind());
        }
    }
}

/// Cancellable variant of [`dispatch`], used by MCP tool calls: rmcp fires the
/// request's `CancellationToken` when the client closes the SSE response
/// stream (the 2026-07-28 cancellation signal) or sends
/// `notifications/cancelled` (legacy sessions). Plain routes keep the
/// non-cancelling `dispatch` semantics deliberately (tasks may outlive
/// responses); MCP semantics require the tool to actually stop.
pub async fn dispatch_cancellable(
    dispatch: Py<PyAny>,
    payload: Py<PyAny>,
    ct: tokio_util::sync::CancellationToken,
) -> PyResult<Py<PyAny>> {
    let (tx, mut rx) = tokio::sync::oneshot::channel();
    let handle = Python::attach(|py| {
        let resolver = Py::new(
            py,
            WorkerDispatchResolver {
                tx: std::sync::Mutex::new(Some(tx)),
            },
        )?;
        let handle = Py::new(
            py,
            WorkerTaskHandle {
                task: std::sync::Mutex::new(None),
                cancelled: std::sync::atomic::AtomicBool::new(false),
            },
        )?;
        let start = get_worker_fn(
            py,
            &WORKER_DISPATCH_START_CANCELLABLE,
            "worker_dispatch_start_cancellable",
        )?;
        start.call1(
            py,
            (
                dispatch,
                payload,
                get_loop(py)?,
                resolver,
                handle.clone_ref(py),
            ),
        )?;
        Ok::<_, PyErr>(handle)
    })?;

    tokio::select! {
        result = &mut rx => match result {
            Ok(result) => result,
            Err(_) => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "worker dispatch resolver dropped without a result",
            )),
        },
        _ = ct.cancelled() => {
            Python::attach(|py| {
                let inner = handle.bind(py).get();
                inner.cancelled.store(true, std::sync::atomic::Ordering::SeqCst);
                if let Some(task) = inner.task.lock().unwrap().take() {
                    // Schedule `.cancel()` on the task's own loop; calling it
                    // from this thread directly is not loop-safe.
                    let task = task.bind(py);
                    if let (Ok(task_loop), Ok(cancel)) =
                        (task.call_method0("get_loop"), task.getattr("cancel"))
                    {
                        let _ = task_loop.call_method1("call_soon_threadsafe", (cancel,));
                    }
                }
            });
            Err(pyo3::exceptions::PyRuntimeError::new_err(
                "MCP request cancelled by client",
            ))
        }
    }
}

pub fn dispatch_sync(
    py: Python<'_>,
    dispatch: &Py<PyAny>,
    request: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let call = get_worker_fn(py, &WORKER_SYNC_DISPATCH, "worker_sync_dispatch")?;
    call.call1(py, (dispatch, request, get_loop(py)?))
}
