//! Native file-descriptor readiness for `WorkerLoop` (Unix).
//!
//! `add_reader`/`add_writer` register the descriptor with the Tokio reactor
//! directly, so asyncio's `sock_*` helpers, pipes, native transports, and
//! drivers such as psycopg that use the selector-style fd API never leave the
//! worker runtime. Readiness is delivered as a `WorkerLoopCommand::Ready` on
//! the same pump that services `call_soon`, so callbacks stay FIFO with other
//! handles.
//!
//! One registration per descriptor (uvloop's model): the raw fd is registered
//! once with both interests and carries a reader slot and a writer slot, so
//! adding a writer to a watched socket, or psycopg's add/remove per query,
//! costs a slot update rather than a fresh registration, and no `dup(2)` is
//! involved. The registration is dropped (epoll/kqueue `DEL`) once both slots
//! are empty. As with every selector-style loop, a descriptor must be removed
//! before it is closed: the kernel drops the registration with the last
//! reference to the file, so a stale entry for a reused descriptor number
//! would otherwise never fire.
//!
//! asyncio's selector is level-triggered while Tokio's reactor is
//! edge-triggered. After each callback the pump re-checks the descriptor
//! (a zero-timeout `poll(2)` for opaque Handles; the read/write result for
//! native transports) and requeues the callback while the kernel still
//! reports it ready, so a callback that consumes only part of the pending
//! data (a stdlib transport reading one `max_size` chunk) is called again
//! instead of stalling; the watcher only wakes to clear Tokio's cached
//! readiness once the descriptor is drained.

use pyo3::prelude::*;
use std::collections::HashMap;
use std::os::fd::{AsRawFd, RawFd};
use std::sync::{Arc, Mutex};
use tokio::io::unix::AsyncFd;
use tokio::io::Interest;

use crate::worker_loop::WorkerLoopCommand;

/// What the pump runs when a descriptor is ready.
#[derive(Clone)]
pub(crate) enum FdCallback {
    /// An asyncio `Handle` from `add_reader`/`add_writer` (opaque callback:
    /// level triggering is emulated with `poll(2)` afterwards).
    Handle(Arc<Py<PyAny>>),
    /// A native `SocketTransport`; its read/write result says whether the
    /// descriptor is drained, so no `poll(2)` is needed.
    Transport(Arc<Py<crate::worker_transport::SocketTransport>>),
}

impl FdCallback {
    fn same_as(&self, other: &FdCallback) -> bool {
        match (self, other) {
            (FdCallback::Handle(a), FdCallback::Handle(b)) => Arc::ptr_eq(a, b),
            (FdCallback::Transport(a), FdCallback::Transport(b)) => Arc::ptr_eq(a, b),
            _ => false,
        }
    }
}

/// Result of one readiness callback, decided by the pump.
pub(crate) enum ReadyOutcome {
    /// Kernel may still have data/space: requeue without waking the watcher.
    StillReady,
    /// Descriptor drained: the watcher clears Tokio's readiness and re-arms.
    Drained,
    /// Registration is over (cancelled handle, transport closed/paused/eof).
    Stop,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum Direction {
    Read,
    Write,
}

impl Direction {
    fn ready(self) -> tokio::io::Ready {
        match self {
            Direction::Read => tokio::io::Ready::READABLE,
            Direction::Write => tokio::io::Ready::WRITABLE,
        }
    }

    fn poll_events(self) -> libc::c_short {
        match self {
            Direction::Read => libc::POLLIN,
            Direction::Write => libc::POLLOUT,
        }
    }
}

/// The descriptor is owned by Python; the registration never closes it.
struct RawDescriptor(RawFd);

impl AsRawFd for RawDescriptor {
    fn as_raw_fd(&self) -> RawFd {
        self.0
    }
}

#[derive(Default)]
struct Slots {
    read: Option<FdCallback>,
    write: Option<FdCallback>,
}

impl Slots {
    fn get(&self, direction: Direction) -> &Option<FdCallback> {
        match direction {
            Direction::Read => &self.read,
            Direction::Write => &self.write,
        }
    }

    fn get_mut(&mut self, direction: Direction) -> &mut Option<FdCallback> {
        match direction {
            Direction::Read => &mut self.read,
            Direction::Write => &mut self.write,
        }
    }

    fn interest(&self) -> Option<Interest> {
        match (self.read.is_some(), self.write.is_some()) {
            (true, true) => Some(Interest::READABLE | Interest::WRITABLE),
            (true, false) => Some(Interest::READABLE),
            (false, true) => Some(Interest::WRITABLE),
            (false, false) => None,
        }
    }
}

struct Registration {
    async_fd: AsyncFd<RawDescriptor>,
    /// Identity of the open file at registration time (see `add`).
    file: Option<(libc::dev_t, libc::ino_t)>,
    slots: Mutex<Slots>,
    /// Wakes the watcher when the slots (and thus the interest set) change.
    changed: tokio::sync::Notify,
    /// Set when `add` replaced this registration because the descriptor
    /// number now names a different file: the kernel already dropped our
    /// epoll/kqueue entry with the old file, and a `DEL` on exit would hit
    /// the new file's registration instead. The watcher leaks the reactor
    /// handle rather than deregistering it.
    stale: std::sync::atomic::AtomicBool,
}

fn file_identity(fd: RawFd) -> Option<(libc::dev_t, libc::ino_t)> {
    // SAFETY: `stat` is a plain C struct; zeroed is a valid initial value.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: valid fd (or EBADF), valid out-pointer.
    if unsafe { libc::fstat(fd, &mut stat) } == 0 {
        Some((stat.st_dev, stat.st_ino))
    } else {
        None
    }
}

#[derive(Default)]
pub(crate) struct FdWatchers {
    registrations: Mutex<HashMap<RawFd, Arc<Registration>>>,
}

impl FdWatchers {
    /// Install `callback` for `direction` on `fd`, replacing any previous
    /// callback for that direction. The caller (Python) has already cancelled
    /// a replaced Handle, so a readiness event still in flight for it is a
    /// no-op.
    pub(crate) fn add(
        &self,
        runtime: &tokio::runtime::Handle,
        tx: &tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
        fd: RawFd,
        direction: Direction,
        callback: FdCallback,
    ) -> PyResult<()> {
        let mut registrations = self.registrations.lock().unwrap();
        if let Some(registration) = registrations.get(&fd) {
            let mut slots = registration.slots.lock().unwrap();
            // A registration whose slots are all empty is on its way out (its
            // watcher removes it once it wakes). Reuse it while the descriptor
            // still names the same file — the reactor registration is intact,
            // and a second one would be rejected (EEXIST). If the caller closed
            // and reopened the number in between, the kernel already dropped
            // the old registration, so start fresh instead of a stale one.
            let reusable = slots.interest().is_some() || file_identity(fd) == registration.file;
            if reusable {
                *slots.get_mut(direction) = Some(callback);
                drop(slots);
                registration.changed.notify_one();
                return Ok(());
            }
            drop(slots);
            registration
                .stale
                .store(true, std::sync::atomic::Ordering::SeqCst);
            registrations.remove(&fd);
        }
        let async_fd = {
            let _guard = runtime.enter();
            AsyncFd::with_interest(RawDescriptor(fd), Interest::READABLE | Interest::WRITABLE)
                .map_err(PyErr::from)?
        };
        let registration = Arc::new(Registration {
            async_fd,
            file: file_identity(fd),
            slots: Mutex::new(Slots::default()),
            changed: tokio::sync::Notify::new(),
            stale: std::sync::atomic::AtomicBool::new(false),
        });
        *registration.slots.lock().unwrap().get_mut(direction) = Some(callback);
        registrations.insert(fd, Arc::clone(&registration));
        runtime.spawn(watch(registration, tx.clone()));
        Ok(())
    }

    /// Number of registrations with a live reader or writer (test hook).
    pub(crate) fn live_count(&self) -> usize {
        self.registrations
            .lock()
            .unwrap()
            .values()
            .filter(|registration| registration.slots.lock().unwrap().interest().is_some())
            .count()
    }

    pub(crate) fn remove(&self, fd: RawFd, direction: Direction) -> bool {
        let registrations = self.registrations.lock().unwrap();
        let Some(registration) = registrations.get(&fd) else {
            return false;
        };
        let existed = registration
            .slots
            .lock()
            .unwrap()
            .get_mut(direction)
            .take()
            .is_some();
        // The watcher re-evaluates its interest and, once both slots are
        // empty, drops the registration (deregistering the descriptor).
        registration.changed.notify_one();
        existed
    }

    /// Clear `direction` on `fd` if it still holds `callback` (the pump
    /// reported that callback as finished).
    fn clear_if_same(&self, fd: RawFd, direction: Direction, callback: &FdCallback) {
        let registrations = self.registrations.lock().unwrap();
        let Some(registration) = registrations.get(&fd) else {
            return;
        };
        let mut slots = registration.slots.lock().unwrap();
        if slots
            .get(direction)
            .as_ref()
            .is_some_and(|current| current.same_as(callback))
        {
            *slots.get_mut(direction) = None;
        }
    }

    /// Called by a watcher that found both slots empty: drop the map entry
    /// unless `add` refilled it (or replaced it) in the meantime.
    fn retire(&self, fd: RawFd, registration: &Arc<Registration>) -> bool {
        let mut registrations = self.registrations.lock().unwrap();
        let Some(current) = registrations.get(&fd) else {
            return true;
        };
        if !Arc::ptr_eq(current, registration) {
            return true;
        }
        if registration.slots.lock().unwrap().interest().is_some() {
            return false;
        }
        registrations.remove(&fd);
        true
    }
}

/// One readiness delivery. The pump owns the level-trigger re-check: after
/// the callback it decides whether the descriptor may still be ready
/// (requeue), is drained (wake the watcher to clear readiness), or the
/// registration is over.
pub(crate) struct Ready {
    callback: FdCallback,
    fd: RawFd,
    direction: Direction,
    done: tokio::sync::oneshot::Sender<ReadyOutcome>,
}

impl Ready {
    /// Run the callback on the pump thread (GIL held), then requeue or
    /// report back to the watcher.
    pub(crate) fn run(
        self,
        py: Python<'_>,
        loop_obj: &Py<PyAny>,
        tx: &tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
    ) {
        let outcome = match &self.callback {
            FdCallback::Handle(handle) => {
                if crate::worker_loop::run_handle(py, loop_obj, handle) {
                    if still_ready(self.fd, self.direction) {
                        ReadyOutcome::StillReady
                    } else {
                        ReadyOutcome::Drained
                    }
                } else {
                    ReadyOutcome::Stop
                }
            }
            FdCallback::Transport(transport) => match self.direction {
                Direction::Read => transport.get().on_readable(transport, py),
                Direction::Write => transport.get().on_writable(transport, py),
            },
        };
        match outcome {
            ReadyOutcome::StillReady => {
                let _ = tx.send(WorkerLoopCommand::Ready(self));
            }
            outcome => {
                let _ = self.done.send(outcome);
            }
        }
    }
}

fn still_ready(fd: RawFd, direction: Direction) -> bool {
    let mut pollfd = libc::pollfd {
        fd,
        events: direction.poll_events(),
        revents: 0,
    };
    // SAFETY: `pollfd` is a valid, exclusively borrowed array of one entry.
    let rc = unsafe { libc::poll(&mut pollfd, 1, 0) };
    // On error (EINTR) report ready: an extra callback is harmless, a lost
    // wakeup is not.
    rc != 0
}

/// Deliver one direction's readiness and wait for the pump's verdict.
/// Returns whether Tokio's cached readiness for that direction should be
/// cleared.
async fn deliver(
    fd: RawFd,
    direction: Direction,
    callback: FdCallback,
    tx: &tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
) -> bool {
    let (done_tx, done_rx) = tokio::sync::oneshot::channel();
    let ready = Ready {
        callback: callback.clone(),
        fd,
        direction,
        done: done_tx,
    };
    if tx.send(WorkerLoopCommand::Ready(ready)).is_err() {
        return true;
    }
    match done_rx.await {
        Ok(ReadyOutcome::Drained) | Err(_) => true,
        Ok(ReadyOutcome::Stop) => {
            // The callback is finished (cancelled Handle, transport paused or
            // closed): drop it from its slot unless it was already replaced,
            // as the stdlib selector loop drops cancelled readers. Readiness
            // stays cached so a replacement callback fires immediately.
            crate::worker_loop::fd_watchers().clear_if_same(fd, direction, &callback);
            false
        }
        Ok(ReadyOutcome::StillReady) => unreachable!("StillReady is requeued by the pump"),
    }
}

async fn watch(
    registration: Arc<Registration>,
    tx: tokio::sync::mpsc::UnboundedSender<WorkerLoopCommand>,
) {
    let fd = registration.async_fd.get_ref().as_raw_fd();
    loop {
        let interest = registration.slots.lock().unwrap().interest();
        let Some(interest) = interest else {
            // Both slots empty: retire (deregister) unless `add` raced us.
            if crate::worker_loop::fd_watchers().retire(fd, &registration) {
                if registration.stale.load(std::sync::atomic::Ordering::SeqCst) {
                    if let Ok(registration) = Arc::try_unwrap(registration) {
                        std::mem::forget(registration.async_fd);
                    }
                }
                return;
            }
            continue;
        };
        let mut guard = tokio::select! {
            biased;
            _ = registration.changed.notified() => continue,
            ready = registration.async_fd.ready(interest) => match ready {
                Ok(guard) => guard,
                Err(_) => return,
            },
        };
        let events = guard.ready();
        for direction in [Direction::Read, Direction::Write] {
            let hit = match direction {
                Direction::Read => events.is_readable(),
                Direction::Write => events.is_writable(),
            };
            if !hit {
                continue;
            }
            let callback = registration.slots.lock().unwrap().get(direction).clone();
            let Some(callback) = callback else {
                guard.clear_ready_matching(direction.ready());
                continue;
            };
            if deliver(fd, direction, callback, &tx).await {
                guard.clear_ready_matching(direction.ready());
            }
        }
    }
}
