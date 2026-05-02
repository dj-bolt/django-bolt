use actix_web::web::Bytes;
use futures_util::{stream, Stream, StreamExt};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyByteArray, PyDict, PyMemoryView, PyModule, PyString};
use pyo3::IntoPyObjectExt;
use pyo3_async_runtimes::TaskLocals;
use std::ffi::CString;
use std::pin::Pin;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;
use tokio::sync::mpsc::error::TrySendError;

use crate::state::{get_max_sync_streaming_threads, ACTIVE_SYNC_STREAMING_THREADS, TASK_LOCALS};
// Streaming uses direct_stream only in higher-level handler; not directly here

// Buffer pool imports removed (unused)

// Note: buffer pool removed during modularization; reintroduce if needed for micro-alloc tuning

// Reuse the global Python asyncio event loop created at server startup (TASK_LOCALS)

#[inline(always)]
pub fn convert_python_chunk(value: &Bound<'_, PyAny>) -> Option<Bytes> {
    // Zero-copy path: PyBackedBytes holds a reference to the Python bytes object.
    // Bytes::from_owner wraps it without memcpy.
    if let Ok(backed) = value.extract::<PyBackedBytes>() {
        return Some(Bytes::from_owner(backed));
    }
    if let Ok(py_bytearray) = value.cast::<PyByteArray>() {
        // bytearray is mutable so we must copy
        return Some(Bytes::copy_from_slice(unsafe { py_bytearray.as_bytes() }));
    }
    if let Ok(py_str) = value.cast::<PyString>() {
        if let Ok(s) = py_str.to_str() {
            return Some(Bytes::from(s.to_owned()));
        }
        let s = py_str.to_string_lossy().into_owned();
        return Some(Bytes::from(s.into_bytes()));
    }
    if let Ok(memory_view) = value.cast::<PyMemoryView>() {
        if let Ok(bytes_obj) = memory_view.call_method0("tobytes") {
            if let Ok(backed) = bytes_obj.extract::<PyBackedBytes>() {
                return Some(Bytes::from_owner(backed));
            }
        }
    }
    let py = value.py();
    if value
        .hasattr(pyo3::intern!(py, "__bytes__"))
        .unwrap_or(false)
    {
        if let Ok(buffer) = value.call_method0(pyo3::intern!(py, "__bytes__")) {
            if let Ok(backed) = buffer.extract::<PyBackedBytes>() {
                return Some(Bytes::from_owner(backed));
            }
        }
    }
    if let Ok(py_str) = value.str() {
        let s = py_str.to_string_lossy().into_owned();
        return Some(Bytes::from(s.into_bytes()));
    }
    None
}

const ASYNC_STREAM_FORWARDER: &str = r#"
import inspect

async def forward(gen, sender):
    iterator = gen.__aiter__() if hasattr(gen, "__aiter__") else gen
    try:
        while True:
            item = await iterator.__anext__()
            should_continue = sender.send(item)
            if inspect.isawaitable(should_continue):
                should_continue = await should_continue
            if should_continue:
                continue
            break
    except StopAsyncIteration:
        pass
    except Exception:
        # Match the existing behavior of terminating the stream when the Python
        # iterator raises. The response body simply ends instead of surfacing a
        # noisy "Task exception was never retrieved" diagnostic.
        pass
    finally:
        try:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                maybe_awaitable = aclose()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
        except Exception:
            pass
        sender.close()
"#;

#[pyclass]
struct AsyncStreamSender {
    locals: TaskLocals,
    tx: Arc<Mutex<Option<mpsc::Sender<Result<Bytes, std::io::Error>>>>>,
}

#[pymethods]
impl AsyncStreamSender {
    fn send(&mut self, item: Py<PyAny>) -> PyResult<Py<PyAny>> {
        Python::attach(|py| {
            let bytes = convert_python_chunk(&item.bind(py)).ok_or_else(|| {
                pyo3::exceptions::PyTypeError::new_err(
                    "StreamingResponse async iterator yielded an unsupported chunk type",
                )
            })?;
            let tx = self.tx.lock().unwrap().as_ref().cloned();
            let Some(tx) = tx else {
                return false.into_py_any(py);
            };

            // try_send consumes the value, so we only clone if the channel is
            // full and we need to retry with the async path.
            match tx.try_send(Ok(bytes)) {
                Ok(()) => true.into_py_any(py),
                Err(TrySendError::Full(Ok(bytes))) => {
                    pyo3_async_runtimes::tokio::future_into_py_with_locals(
                        py,
                        self.locals.clone(),
                        async move { Ok(tx.send(Ok(bytes)).await.is_ok()) },
                    )
                    .map(Bound::unbind)
                }
                Err(TrySendError::Full(_) | TrySendError::Closed(_)) => false.into_py_any(py),
            }
        })
    }

    fn close(&mut self) {
        self.tx.lock().unwrap().take();
    }
}

fn get_async_stream_forwarder(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static FORWARDER_MODULE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

    let module = FORWARDER_MODULE.get_or_try_init(py, || -> PyResult<Py<PyAny>> {
        let code = CString::new(ASYNC_STREAM_FORWARDER).expect("valid async stream forwarder");
        let file = CString::new("django_bolt/async_stream_forwarder.py").expect("valid filename");
        let name = CString::new("django_bolt_async_stream_forwarder").expect("valid module name");
        Ok(
            PyModule::from_code(py, code.as_c_str(), file.as_c_str(), name.as_c_str())?
                .into_any()
                .unbind(),
        )
    })?;

    Ok(module.bind(py))
}

fn schedule_async_stream_forwarder(
    py: Python<'_>,
    content: &Py<PyAny>,
    tx: mpsc::Sender<Result<Bytes, std::io::Error>>,
) -> PyResult<()> {
    let locals = TASK_LOCALS
        .get()
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Asyncio loop not initialized"))?
        .clone();
    let sender = Py::new(
        py,
        AsyncStreamSender {
            locals: locals.clone(),
            tx: Arc::new(Mutex::new(Some(tx))),
        },
    )?;
    let forwarder = get_async_stream_forwarder(py)?;
    let coroutine =
        forwarder.call_method1(pyo3::intern!(py, "forward"), (content.bind(py), sender))?;
    let event_loop = locals.event_loop(py);
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "context"), locals.context(py))?;
    event_loop.call_method(
        pyo3::intern!(py, "call_soon_threadsafe"),
        (
            event_loop.getattr(pyo3::intern!(py, "create_task"))?,
            coroutine,
        ),
        Some(&kwargs),
    )?;
    Ok(())
}

/// Create a stream with default batch sizes from environment
pub fn create_python_stream(
    content: Py<PyAny>,
    is_async_generator: bool,
) -> Pin<Box<dyn Stream<Item = Result<Bytes, std::io::Error>> + Send>> {
    let batch_size: usize = std::env::var("DJANGO_BOLT_STREAM_BATCH_SIZE")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(20);
    let sync_batch_size: usize = std::env::var("DJANGO_BOLT_STREAM_SYNC_BATCH_SIZE")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(5);
    create_python_stream_with_config(content, batch_size, sync_batch_size, is_async_generator)
}

/// Create a stream for SSE that sends items immediately (batch_size=1)
/// with optional keep-alive pings when idle.
pub fn create_sse_stream(
    content: Py<PyAny>,
    is_async_generator: bool,
    ping_interval: Option<f64>,
) -> Pin<Box<dyn Stream<Item = Result<Bytes, std::io::Error>> + Send>> {
    let inner = create_python_stream_with_config(content, 1, 1, is_async_generator);

    match ping_interval {
        Some(interval) if interval > 0.0 => Box::pin(keepalive_stream(inner, interval)),
        _ => inner,
    }
}

/// Wrap a stream with keep-alive ping injection.
/// Emits `: ping\n\n` when the inner stream is idle for `interval_secs`.
fn keepalive_stream(
    inner: Pin<Box<dyn Stream<Item = Result<Bytes, std::io::Error>> + Send>>,
    interval_secs: f64,
) -> impl Stream<Item = Result<Bytes, std::io::Error>> + Send {
    let duration = std::time::Duration::from_secs_f64(interval_secs);
    let keepalive_bytes: Bytes = Bytes::from_static(b": ping\n\n");

    stream::unfold(
        (inner, duration, keepalive_bytes),
        |(mut inner, duration, keepalive_bytes)| async move {
            match tokio::time::timeout(duration, inner.next()).await {
                Ok(Some(item)) => Some((item, (inner, duration, keepalive_bytes))),
                Ok(None) => None,
                Err(_) => Some((
                    Ok(keepalive_bytes.clone()),
                    (inner, duration, keepalive_bytes),
                )),
            }
        },
    )
}

/// Internal function with configurable batch sizes
fn create_python_stream_with_config(
    content: Py<PyAny>,
    _async_batch_size: usize,
    sync_batch_size: usize,
    is_async_from_metadata: bool,
) -> Pin<Box<dyn Stream<Item = Result<Bytes, std::io::Error>> + Send>> {
    let channel_capacity: usize = std::env::var("DJANGO_BOLT_STREAM_CHANNEL_CAPACITY")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(32);
    // Note: content is guaranteed to be a generator instance (not a callable)
    // because StreamingResponse validates this in Python at instantiation time.
    // The is_async_generator flag was pre-computed from Python's inspect.
    let resolved_target = Python::attach(|py| content.clone_ref(py));
    let is_async_iter = is_async_from_metadata;

    let (tx, rx) = mpsc::channel::<Result<Bytes, std::io::Error>>(channel_capacity);
    let resolved_target_final = Python::attach(|py| resolved_target.clone_ref(py));
    let is_async_final = is_async_iter;

    if is_async_final {
        let start_result = Python::attach(|py| {
            schedule_async_stream_forwarder(py, &resolved_target_final, tx.clone())
        });
        if let Err(err) = start_result {
            let _ = tx.try_send(Err(std::io::Error::other(format!(
                "Failed to initialize async stream forwarder: {err}"
            ))));
        }
        drop(tx);

        let s = stream::unfold(rx, |mut rx| async move {
            match rx.recv().await {
                Some(item) => Some((item, rx)),
                None => None,
            }
        });
        return Box::pin(s);
    } else {
        let sync_batch = sync_batch_size;

        // OPTION 3: Use std::thread::spawn() instead of spawn_blocking()
        // This avoids Tokio's blocking thread pool limit entirely
        // Each sync SSE connection runs on its own dedicated OS thread

        // Make tx cloneable for the spawn failure case
        let tx_for_spawn = tx.clone();

        // Check connection limits to prevent thread exhaustion DoS
        let max_threads = get_max_sync_streaming_threads();
        let current_threads = ACTIVE_SYNC_STREAMING_THREADS.load(Ordering::Relaxed);

        if current_threads >= max_threads {
            eprintln!(
                "[SSE WARNING] Sync streaming thread limit reached: {} >= {}",
                current_threads, max_threads
            );
            // Spawn async task to send retry directive (can't use blocking_send from runtime)
            tokio::spawn({
                let tx_clone = tx.clone();
                async move {
                    // RFC 6553 Server-Sent Events: send retry directive before closing
                    let retry_directive = b"retry: 30000\n\n";
                    let _ = tx_clone.send(Ok(Bytes::from_static(retry_directive))).await;
                }
            });
            drop(tx);
            let s = stream::unfold(rx, |mut rx| async move {
                match rx.recv().await {
                    Some(item) => Some((item, rx)),
                    None => None,
                }
            });
            return Box::pin(s);
        }

        // Increment active thread counter
        ACTIVE_SYNC_STREAMING_THREADS.fetch_add(1, Ordering::Relaxed);

        // Use Builder::new() to get a Result on thread spawn failure
        match std::thread::Builder::new()
            .name("sync-sse-generator".to_string())
            .spawn(move || {
            let mut iterator: Option<Py<PyAny>> = None;
            let mut batch_buffer = Vec::with_capacity(sync_batch);
            let mut exhausted = false;

            loop {
                batch_buffer.clear();
                let python_exhausted = Python::attach(|py| {
                    if iterator.is_none() {
                        let iter_target = resolved_target_final.clone_ref(py);
                        let bound = iter_target.bind(py);
                        // OPTIMIZATION: Use interned strings for iterator protocol
                        let iter_obj = if bound.hasattr(pyo3::intern!(py, "__next__")).unwrap_or(false) {
                            iter_target
                        } else if bound.hasattr(pyo3::intern!(py, "__iter__")).unwrap_or(false) {
                            match bound.call_method0(pyo3::intern!(py, "__iter__")) {
                                Ok(it) => it.unbind(),
                                Err(_) => return true,
                            }
                        } else {
                            return true;
                        };
                        iterator = Some(iter_obj);
                    }
                    let it = iterator.as_ref().unwrap().bind(py);
                    for _ in 0..sync_batch {
                        match it.call_method0(pyo3::intern!(py, "__next__")) {
                            Ok(value) => {
                                if let Some(bytes) = super::streaming::convert_python_chunk(&value)
                                {
                                    batch_buffer.push(bytes);
                                }
                            }
                            Err(err) => {
                                if err.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                                    return true;
                                }
                                break;
                            }
                        }
                    }
                    false
                });
                if python_exhausted {
                    exhausted = true;
                }
                if batch_buffer.is_empty() && exhausted {
                    break;
                }
                for bytes in batch_buffer.drain(..) {
                    // Use blocking_send which works from non-async context
                    if tx.blocking_send(Ok(bytes)).is_err() {
                        // Client disconnected - close the generator to run cleanup code
                        if let Some(ref iter) = iterator {
                            Python::attach(|py| {
                                // OPTIMIZATION: Use interned string for close
                                match iter.bind(py).call_method0(pyo3::intern!(py, "close")) {
                                    Ok(_) => {},
                                    Err(e) => {
                                        eprintln!("[SSE WARNING] Error during sync generator cleanup on client disconnect: {}", e);
                                    }
                                }
                            });
                        }
                        exhausted = true;
                        break;
                    }
                }
                if exhausted {
                    break;
                }
            }

            // Ensure sync generator cleanup runs
            if let Some(ref iter) = iterator {
                Python::attach(|py| {
                    // OPTIMIZATION: Use interned string for close
                    match iter.bind(py).call_method0(pyo3::intern!(py, "close")) {
                        Ok(_) => {},
                        Err(e) => {
                            eprintln!("[SSE WARNING] Error during sync generator cleanup at end of stream: {}", e);
                        }
                    }
                });
            }
            // Decrement thread counter when thread finishes
            ACTIVE_SYNC_STREAMING_THREADS.fetch_sub(1, Ordering::Relaxed);
        }) {
            Ok(_) => {
                // Thread spawned successfully, SSE will start streaming
            }
            Err(e) => {
                eprintln!("[SSE ERROR] Failed to spawn sync streaming thread: {}", e);
                // Decrement counter since thread spawn failed
                ACTIVE_SYNC_STREAMING_THREADS.fetch_sub(1, Ordering::Relaxed);
                // Spawn async task to send retry directive (can't use blocking_send from runtime)
                tokio::spawn({
                    let tx_clone = tx_for_spawn.clone();
                    async move {
                        // RFC 6553 Server-Sent Events: send retry directive before closing
                        let retry_directive = b"retry: 30000\n\n";
                        let _ = tx_clone.send(Ok(Bytes::from_static(retry_directive))).await;
                    }
                });
                drop(tx_for_spawn);
            }
        }

        // Create simple stream without error state in closure (keeps Stream trait bounds clean)
        let s = stream::unfold(rx, |mut rx| async move {
            match rx.recv().await {
                Some(item) => Some((item, rx)),
                None => None,
            }
        });
        Box::pin(s)
    }
}
