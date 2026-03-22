use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use pyo3::prelude::*;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
struct ReloadFilter {
    ignore_dir_names: HashSet<String>,
    ignore_paths: Vec<PathBuf>,
}

impl ReloadFilter {
    fn new(ignore_dir_names: Vec<String>, ignore_paths: Vec<String>) -> Self {
        Self {
            ignore_dir_names: ignore_dir_names.into_iter().collect(),
            ignore_paths: ignore_paths
                .into_iter()
                .map(|path| normalize_path(PathBuf::from(path)))
                .collect(),
        }
    }

    fn is_relevant(&self, path: &Path) -> bool {
        !self.is_ignored(path) && !is_temporary_path(path)
    }

    fn is_ignored(&self, path: &Path) -> bool {
        let path = normalize_path(path.to_path_buf());

        if self
            .ignore_paths
            .iter()
            .any(|prefix| path.starts_with(prefix))
        {
            return true;
        }

        path.components().any(|component| match component {
            std::path::Component::Normal(name) => self
                .ignore_dir_names
                .contains(name.to_string_lossy().as_ref()),
            _ => false,
        })
    }
}

fn normalize_path(path: PathBuf) -> PathBuf {
    if path.is_absolute() {
        path
    } else if let Ok(cwd) = std::env::current_dir() {
        cwd.join(path)
    } else {
        path
    }
}

fn is_temporary_path(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };

    name.ends_with('~')
        || name.ends_with(".swp")
        || name.ends_with(".swo")
        || name.ends_with(".swx")
        || name.ends_with(".tmp")
        || name == "4913"
        || name.starts_with(".#")
        || (name.starts_with('#') && name.ends_with('#'))
}

fn first_relevant_path(event: &Event, filter: &ReloadFilter) -> Option<PathBuf> {
    if matches!(event.kind, EventKind::Access(_)) {
        return None;
    }

    event
        .paths
        .iter()
        .find(|path| filter.is_relevant(path))
        .cloned()
}

fn py_runtime_error(message: impl Into<String>) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(message.into())
}

// Keep in sync with _ENV_DEV_WORKER / _ENV_DEV_RELOAD_COUNT in runbolt.py
fn spawn_worker(command: &[String], reload_count: u64) -> PyResult<Child> {
    let Some(program) = command.first() else {
        return Err(py_runtime_error("Dev worker command cannot be empty"));
    };

    let mut child = Command::new(program);
    for arg in &command[1..] {
        child.arg(arg);
    }

    child
        .env("DJANGO_BOLT_DEV_WORKER", "1")
        .env("DJANGO_BOLT_DEV_RELOAD_COUNT", reload_count.to_string())
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());

    child.spawn().map_err(|err| {
        py_runtime_error(format!("Failed to start dev worker '{}': {}", program, err))
    })
}

fn wait_for_child_exit(
    child: &mut Child,
    timeout: Duration,
) -> std::io::Result<Option<std::process::ExitStatus>> {
    let deadline = Instant::now() + timeout;

    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(Some(status));
        }

        if Instant::now() >= deadline {
            return Ok(None);
        }

        std::thread::sleep(Duration::from_millis(25));
    }
}

fn stop_worker(worker: &mut Option<Child>) -> PyResult<()> {
    let Some(child) = worker.as_mut() else {
        return Ok(());
    };

    #[cfg(unix)]
    {
        let pid = child.id() as i32;

        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }

        if wait_for_child_exit(child, Duration::from_millis(1200))
            .map_err(|err| py_runtime_error(format!("Failed while stopping dev worker: {}", err)))?
            .is_none()
        {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
            child
                .wait()
                .map_err(|err| py_runtime_error(format!("Failed to kill dev worker: {}", err)))?;
        }
    }

    #[cfg(not(unix))]
    {
        child
            .kill()
            .map_err(|err| py_runtime_error(format!("Failed to stop dev worker: {}", err)))?;
        child
            .wait()
            .map_err(|err| py_runtime_error(format!("Failed to wait for dev worker: {}", err)))?;
    }

    *worker = None;
    Ok(())
}

fn recv_change(
    rx: &Receiver<notify::Result<Event>>,
    filter: &ReloadFilter,
    debounce: Duration,
    poll_interval: Duration,
) -> Result<Option<PathBuf>, String> {
    match rx.recv_timeout(poll_interval) {
        Ok(Ok(event)) => {
            let Some(mut changed_path) = first_relevant_path(&event, filter) else {
                return Ok(None);
            };

            let deadline = Instant::now() + debounce;
            while Instant::now() < deadline {
                match rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
                    Ok(Ok(next_event)) => {
                        if let Some(next_path) = first_relevant_path(&next_event, filter) {
                            changed_path = next_path;
                        }
                    }
                    Ok(Err(err)) => return Err(err.to_string()),
                    Err(RecvTimeoutError::Timeout) => break,
                    Err(RecvTimeoutError::Disconnected) => break,
                }
            }

            Ok(Some(changed_path))
        }
        Ok(Err(err)) => Err(err.to_string()),
        Err(RecvTimeoutError::Timeout) => Ok(None),
        Err(RecvTimeoutError::Disconnected) => Err("File watcher disconnected".to_string()),
    }
}

fn format_exit_status(status: std::process::ExitStatus) -> String {
    match status.code() {
        Some(code) => format!("exit code {}", code),
        None => "terminated by signal".to_string(),
    }
}

fn run_dev_reloader_inner(
    command: Vec<String>,
    watch_paths: Vec<String>,
    ignore_dir_names: Vec<String>,
    ignore_paths: Vec<String>,
    debounce_ms: u64,
) -> PyResult<i32> {
    if watch_paths.is_empty() {
        return Err(py_runtime_error(
            "At least one watch path is required for dev reload",
        ));
    }

    let filter = ReloadFilter::new(ignore_dir_names, ignore_paths);
    let debounce = Duration::from_millis(debounce_ms.max(50));
    let poll_interval = Duration::from_millis(200);
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_flag = shutdown.clone();

    ctrlc::set_handler(move || {
        shutdown_flag.store(true, Ordering::SeqCst);
    })
    .map_err(|err| {
        py_runtime_error(format!(
            "Failed to install dev reload signal handler: {}",
            err
        ))
    })?;

    let (tx, rx) = mpsc::channel();
    let mut watcher = RecommendedWatcher::new(
        move |result| {
            let _ = tx.send(result);
        },
        Config::default(),
    )
    .map_err(|err| py_runtime_error(format!("Failed to initialize file watcher: {}", err)))?;

    let mut watched = 0usize;
    for path in watch_paths {
        let watch_path = PathBuf::from(path);
        if !watch_path.exists() {
            continue;
        }

        watcher
            .watch(&watch_path, RecursiveMode::Recursive)
            .map_err(|err| {
                py_runtime_error(format!(
                    "Failed to watch '{}': {}",
                    watch_path.display(),
                    err
                ))
            })?;
        watched += 1;
    }

    if watched == 0 {
        return Err(py_runtime_error(
            "No valid watch paths were found for dev reload",
        ));
    }

    let mut reload_count = 0u64;
    let mut worker = Some(spawn_worker(&command, reload_count)?);
    let mut worker_exited = false;

    loop {
        if shutdown.load(Ordering::SeqCst) {
            stop_worker(&mut worker)?;
            return Ok(0);
        }

        if let Some(child) = worker.as_mut() {
            if let Some(status) = child
                .try_wait()
                .map_err(|err| py_runtime_error(format!("Failed to poll dev worker: {}", err)))?
            {
                let exit_detail = format_exit_status(status);
                eprintln!(
                    "[django-bolt] Dev worker exited with {}. Waiting for changes...",
                    exit_detail
                );
                worker_exited = true;
                worker = None;
            }
        }

        match recv_change(&rx, &filter, debounce, poll_interval) {
            Ok(Some(changed_path)) => {
                eprintln!(
                    "[django-bolt] 🔄 Reloading ({})",
                    changed_path.display()
                );
                reload_count += 1;
                stop_worker(&mut worker)?;
                worker = Some(spawn_worker(&command, reload_count)?);
                worker_exited = false;
            }
            Ok(None) => {}
            Err(err) => {
                if shutdown.load(Ordering::SeqCst) {
                    stop_worker(&mut worker)?;
                    return Ok(0);
                }

                if worker_exited {
                    continue;
                }

                eprintln!("[django-bolt] Dev reload watcher error: {}", err);
            }
        }
    }
}

#[pyfunction]
pub fn run_dev_reloader(
    py: Python<'_>,
    command: Vec<String>,
    watch_paths: Vec<String>,
    ignore_dir_names: Vec<String>,
    ignore_paths: Vec<String>,
    debounce_ms: Option<u64>,
) -> PyResult<i32> {
    py.detach(|| {
        run_dev_reloader_inner(
            command,
            watch_paths,
            ignore_dir_names,
            ignore_paths,
            debounce_ms.unwrap_or(125),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::{first_relevant_path, is_temporary_path, ReloadFilter};
    use notify::{event::CreateKind, Event, EventKind};
    use std::path::PathBuf;

    #[test]
    fn ignores_paths_inside_ignored_directories() {
        let filter = ReloadFilter::new(vec!["target".to_string()], vec![]);
        assert!(!filter.is_relevant(&PathBuf::from("/tmp/project/target/output.txt")));
        assert!(filter.is_relevant(&PathBuf::from("/tmp/project/src/app.py")));
    }

    #[test]
    fn temporary_editor_files_do_not_trigger_reload() {
        assert!(is_temporary_path(&PathBuf::from(
            "/tmp/project/main.py.swp"
        )));
        assert!(is_temporary_path(&PathBuf::from("/tmp/project/.#main.py")));
        assert!(!is_temporary_path(&PathBuf::from("/tmp/project/main.py")));
    }

    #[test]
    fn ignores_access_only_events() {
        let filter = ReloadFilter::new(vec![], vec![]);
        let event = Event {
            kind: EventKind::Access(notify::event::AccessKind::Close(
                notify::event::AccessMode::Read,
            )),
            paths: vec![PathBuf::from("/tmp/project/main.py")],
            attrs: Default::default(),
        };

        assert!(first_relevant_path(&event, &filter).is_none());
    }

    #[test]
    fn returns_first_relevant_path_for_change_events() {
        let filter = ReloadFilter::new(vec!["__pycache__".to_string()], vec![]);
        let event = Event {
            kind: EventKind::Create(CreateKind::File),
            paths: vec![
                PathBuf::from("/tmp/project/__pycache__/main.pyc"),
                PathBuf::from("/tmp/project/main.py"),
            ],
            attrs: Default::default(),
        };

        assert_eq!(
            first_relevant_path(&event, &filter),
            Some(PathBuf::from("/tmp/project/main.py"))
        );
    }
}
