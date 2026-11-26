//! The task store of one MCP mount (the Tasks extension, SEP-2663).
//!
//! A `tools/call` of a task tool returns a task handle at once. The tool
//! runs on. The client reads the state with `tasks/get`, answers input
//! requests with `tasks/update` and stops the task with `tasks/cancel`.
//! The store owns the state of each task: one entry per task id, behind one
//! lock. Only the transitions below change an entry.
//!
//! | From | Event | To |
//! | --- | --- | --- |
//! | (none) | `TaskStore::create` | working |
//! | working | the tool asks for input (`TaskHandle::open_input`) | input_required |
//! | input_required | `tasks/update` answers each open request | working |
//! | working, input_required | the tool returns a result | completed |
//! | working, input_required | the call fails with a JSON-RPC error | failed |
//! | working, input_required | the TTL elapses | failed, and the tool is cancelled |
//! | working, input_required | `tasks/cancel` | cancelled, and the tool is cancelled |
//! | completed, failed, cancelled | one TTL after the change | removed |
//!
//! completed, failed and cancelled are terminal: no event changes them.
//! working and input_required are one phase, `Running`. The status is
//! input_required while an input request is open.
//!
//! Each task belongs to the principal that created it. A call from another
//! principal gets the error of an unknown task id. Thus it cannot learn that
//! the task exists.
//!
//! The store lives in the memory of one process. With more than one
//! process, a `tasks/get` can reach a process that does not have the task.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use rmcp::model::{
    DetailedTask, ErrorData as McpError, InputRequest, JsonObject, Task, TaskPayload, TaskStatus,
};
use rmcp::task_manager::current_timestamp;
use serde_json::Value;
use tokio::sync::oneshot;
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

/// The task settings of one tool.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TaskOptions {
    /// The life of the task from its creation, and the time that Bolt keeps
    /// a finished task.
    pub ttl_ms: u64,
    /// The poll interval that the server suggests to the client.
    pub poll_interval_ms: u64,
}

/// How a task ended.
#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    /// The tool returned a `CallToolResult`, also one with `isError: true`.
    Completed(JsonObject),
    /// The call failed with this JSON-RPC error object.
    Failed(JsonObject),
    Cancelled,
}

impl Outcome {
    pub fn failed(error: &McpError) -> Self {
        let object = match serde_json::to_value(error) {
            Ok(Value::Object(object)) => object,
            _ => JsonObject::new(),
        };
        Self::Failed(object)
    }
}

/// Why a tool cannot open an input request.
#[derive(Debug, PartialEq, Eq)]
pub enum InputError {
    /// The task is terminal or gone.
    NotRunning,
    /// The task used this key before. SEP-2663: a key is unique over the
    /// life of the task.
    KeyUsed,
}

struct OpenInput {
    request: InputRequest,
    reply: oneshot::Sender<Value>,
}

struct Running {
    /// The token of the Python dispatch. It fires when the task settles.
    cancel: CancellationToken,
    inputs: BTreeMap<String, OpenInput>,
    used_keys: HashSet<String>,
}

enum Phase {
    Running(Running),
    Settled { outcome: Outcome, at: Instant },
}

struct Entry {
    owner: String,
    /// The wire metadata. `tasks/get` sets the status from the phase.
    task: Task,
    created: Instant,
    ttl: Duration,
    phase: Phase,
}

impl Entry {
    fn touch(&mut self) {
        self.task.last_updated_at = current_timestamp();
    }

    /// Move a running task to `outcome`. A settled task does not change.
    /// Past the TTL, the task fails, whatever the outcome.
    fn settle(&mut self, outcome: Outcome, now: Instant) {
        if !matches!(self.phase, Phase::Running(_)) {
            return;
        }
        let outcome = if now >= self.created + self.ttl {
            Outcome::failed(&expired_error())
        } else {
            outcome
        };
        if let Outcome::Failed(error) = &outcome
            && let Some(Value::String(message)) = error.get("message")
        {
            // SEP-2663: a failed task SHOULD tell why in statusMessage.
            self.task.status_message = Some(message.clone());
        }
        // The old phase drops its open inputs, which wakes their waiters.
        let old = std::mem::replace(&mut self.phase, Phase::Settled { outcome, at: now });
        if let Phase::Running(running) = old {
            running.cancel.cancel();
        }
        self.touch();
    }

    fn detailed(&self) -> DetailedTask {
        let payload = match &self.phase {
            Phase::Running(running) if running.inputs.is_empty() => TaskPayload::Working,
            Phase::Running(running) => TaskPayload::InputRequired {
                input_requests: running
                    .inputs
                    .iter()
                    .map(|(key, input)| (key.clone(), input.request.clone()))
                    .collect(),
            },
            Phase::Settled { outcome, .. } => match outcome {
                Outcome::Completed(result) => TaskPayload::Completed {
                    result: result.clone(),
                },
                Outcome::Failed(error) => TaskPayload::Failed {
                    error: error.clone(),
                },
                Outcome::Cancelled => TaskPayload::Cancelled,
            },
        };
        DetailedTask::new(self.task.clone(), payload)
    }
}

type Entries = HashMap<String, Entry>;

struct Shared {
    entries: Mutex<Entries>,
    max_tasks: usize,
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, Entries> {
        self.entries.lock().expect("MCP task store lock poisoned")
    }
}

fn expired_error() -> McpError {
    McpError::internal_error("Task expired: its TTL elapsed before it finished", None)
}

/// The error for an unknown task id, and for a task of another principal.
pub fn unknown_task(task_id: &str) -> McpError {
    McpError::invalid_params(format!("Unknown task: {task_id}"), None)
}

/// Fail each running task past its TTL. Remove each settled task one TTL
/// after it settled.
fn sweep(entries: &mut Entries, now: Instant) {
    for entry in entries.values_mut() {
        if matches!(entry.phase, Phase::Running(_)) && now >= entry.created + entry.ttl {
            entry.settle(Outcome::failed(&expired_error()), now);
        }
    }
    entries.retain(|_, entry| match entry.phase {
        Phase::Running(_) => true,
        Phase::Settled { at, .. } => now < at + entry.ttl,
    });
}

/// The tasks of one mount. Dropping the store cancels each running task.
pub struct TaskStore {
    shared: Arc<Shared>,
}

impl TaskStore {
    /// A store that holds at most `max_tasks` tasks, running or finished.
    pub fn new(max_tasks: usize) -> Self {
        Self {
            shared: Arc::new(Shared {
                entries: Mutex::new(HashMap::new()),
                max_tasks,
            }),
        }
    }

    /// Create a working task of `owner`. A `tasks/get` finds it before this
    /// returns. Returns `None` when the store is full.
    pub fn create(&self, owner: String, options: TaskOptions) -> Option<(Task, TaskHandle)> {
        let now = Instant::now();
        let mut entries = self.shared.lock();
        sweep(&mut entries, now);
        if entries.len() >= self.shared.max_tasks {
            return None;
        }
        let task_id = uuid::Uuid::new_v4().to_string();
        let stamp = current_timestamp();
        let task = Task::new(task_id.clone(), TaskStatus::Working, stamp.clone(), stamp)
            .with_ttl_ms(options.ttl_ms)
            .with_poll_interval_ms(options.poll_interval_ms);
        let ttl = Duration::from_millis(options.ttl_ms);
        let cancel = CancellationToken::new();
        entries.insert(
            task_id.clone(),
            Entry {
                owner,
                task: task.clone(),
                created: now,
                ttl,
                phase: Phase::Running(Running {
                    cancel: cancel.clone(),
                    inputs: BTreeMap::new(),
                    used_keys: HashSet::new(),
                }),
            },
        );
        let handle = TaskHandle {
            task_id,
            shared: self.shared.clone(),
            cancel,
            deadline: now + ttl,
        };
        Some((task, handle))
    }

    /// Run `f` on the entry of `task_id` when `owner` created it.
    fn with_entry<R>(
        &self,
        task_id: &str,
        owner: &str,
        f: impl FnOnce(&mut Entry, Instant) -> R,
    ) -> Result<R, McpError> {
        let now = Instant::now();
        let mut entries = self.shared.lock();
        sweep(&mut entries, now);
        match entries.get_mut(task_id) {
            Some(entry) if entry.owner == owner => Ok(f(entry, now)),
            _ => Err(unknown_task(task_id)),
        }
    }

    /// `tasks/get`.
    pub fn get(&self, task_id: &str, owner: &str) -> Result<DetailedTask, McpError> {
        self.with_entry(task_id, owner, |entry, _| entry.detailed())
    }

    /// `tasks/update`: give each answer to its open input request. SEP-2663:
    /// ignore a key that is not open, and accept a part of the answers.
    pub fn update(
        &self,
        task_id: &str,
        owner: &str,
        responses: impl IntoIterator<Item = (String, Value)>,
    ) -> Result<(), McpError> {
        self.with_entry(task_id, owner, |entry, _| {
            let Phase::Running(running) = &mut entry.phase else {
                return;
            };
            let mut answered = false;
            for (key, value) in responses {
                if let Some(input) = running.inputs.remove(&key) {
                    // A dropped receiver means that the tool stopped waiting.
                    let _ = input.reply.send(value);
                    answered = true;
                }
            }
            if answered {
                entry.touch();
            }
        })
    }

    /// `tasks/cancel`: a running task becomes cancelled. A terminal task
    /// does not change.
    pub fn cancel(&self, task_id: &str, owner: &str) -> Result<(), McpError> {
        self.with_entry(task_id, owner, |entry, now| {
            entry.settle(Outcome::Cancelled, now);
        })
    }
}

impl Drop for TaskStore {
    fn drop(&mut self) {
        let mut entries = self.shared.lock();
        for entry in entries.values() {
            if let Phase::Running(running) = &entry.phase {
                running.cancel.cancel();
            }
        }
        entries.clear();
    }
}

/// The side of one task that the running tool uses. Dropping the handle of
/// a running task fails the task.
pub struct TaskHandle {
    task_id: String,
    shared: Arc<Shared>,
    cancel: CancellationToken,
    deadline: Instant,
}

impl TaskHandle {
    pub fn task_id(&self) -> &str {
        &self.task_id
    }

    /// Fires when the task settles: `tasks/cancel`, the TTL, or the end of
    /// the store. Give it to the dispatch, so that it cancels the coroutine.
    pub fn cancel_token(&self) -> &CancellationToken {
        &self.cancel
    }

    /// The end of the TTL of the task.
    pub fn deadline(&self) -> Instant {
        self.deadline
    }

    fn with_running<R>(&self, f: impl FnOnce(&mut Entry) -> R) -> Option<R> {
        let mut entries = self.shared.lock();
        let entry = entries.get_mut(&self.task_id)?;
        matches!(entry.phase, Phase::Running(_)).then(|| f(entry))
    }

    /// Set the status message of a running task.
    pub fn set_status_message(&self, message: String) {
        self.with_running(|entry| {
            entry.task.status_message = Some(message);
            entry.touch();
        });
    }

    /// Open an input request. The task is input_required until
    /// `tasks/update` answers it. The receiver gets the answer. It gets an
    /// error when the task settles first.
    pub fn open_input(
        &self,
        key: String,
        request: InputRequest,
    ) -> Result<oneshot::Receiver<Value>, InputError> {
        self.with_running(|entry| {
            let Phase::Running(running) = &mut entry.phase else {
                return Err(InputError::NotRunning);
            };
            if !running.used_keys.insert(key.clone()) {
                return Err(InputError::KeyUsed);
            }
            let (reply, receiver) = oneshot::channel();
            running.inputs.insert(key, OpenInput { request, reply });
            entry.touch();
            Ok(receiver)
        })
        .unwrap_or(Err(InputError::NotRunning))
    }

    /// Fail the task because its TTL elapsed.
    pub fn expire(&self) {
        self.settle(Outcome::failed(&expired_error()));
    }

    /// Move the task to `outcome`. A settled task does not change.
    pub fn settle(&self, outcome: Outcome) {
        let mut entries = self.shared.lock();
        if let Some(entry) = entries.get_mut(&self.task_id) {
            entry.settle(outcome, Instant::now());
        }
    }
}

impl Drop for TaskHandle {
    fn drop(&mut self) {
        // The normal end settles the task first, so this has no effect then.
        self.settle(Outcome::failed(&McpError::internal_error(
            "The task stopped before it finished",
            None,
        )));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use rmcp::model::ErrorCode;

    const OPTIONS: TaskOptions = TaskOptions {
        ttl_ms: 1_000,
        poll_interval_ms: 50,
    };

    fn elicit(message: &str) -> InputRequest {
        serde_json::from_value(serde_json::json!({
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": message,
                "requestedSchema": {"type": "object", "properties": {}},
            },
        }))
        .unwrap()
    }

    fn result(text: &str) -> Outcome {
        let value =
            serde_json::json!({"content": [{"type": "text", "text": text}], "isError": false});
        Outcome::Completed(value.as_object().unwrap().clone())
    }

    fn status(store: &TaskStore, handle: &TaskHandle, owner: &str) -> TaskStatus {
        store.get(handle.task_id(), owner).unwrap().status()
    }

    #[tokio::test(start_paused = true)]
    async fn a_new_task_is_working_and_found_at_once() {
        let store = TaskStore::new(10);
        let (task, handle) = store.create("alice".into(), OPTIONS).unwrap();
        assert_eq!(task.status, TaskStatus::Working);
        assert_eq!(task.ttl_ms, Some(1_000));
        assert_eq!(task.poll_interval_ms, Some(50));
        assert_eq!(task.task_id, handle.task_id());
        let detailed = store.get(&task.task_id, "alice").unwrap();
        assert_eq!(detailed.payload, TaskPayload::Working);
    }

    #[tokio::test(start_paused = true)]
    async fn another_principal_gets_the_error_of_an_unknown_id() {
        let store = TaskStore::new(10);
        let (task, _handle) = store.create("alice".into(), OPTIONS).unwrap();
        let id = task.task_id.as_str();
        let foreign = [
            store.get(id, "mallory").unwrap_err(),
            store.update(id, "mallory", []).unwrap_err(),
            store.cancel(id, "mallory").unwrap_err(),
        ];
        for error in foreign {
            assert_eq!(error, unknown_task(id));
            assert_eq!(error.code, ErrorCode::INVALID_PARAMS);
        }
        // The foreign cancel did not reach the task.
        assert_eq!(
            store.get(id, "alice").unwrap().status(),
            TaskStatus::Working
        );
        assert_eq!(
            store.get("no-such-task", "alice").unwrap_err(),
            unknown_task("no-such-task")
        );
    }

    #[tokio::test(start_paused = true)]
    async fn an_input_request_waits_for_its_answer() {
        let store = TaskStore::new(10);
        let (_task, handle) = store.create("alice".into(), OPTIONS).unwrap();
        let receiver = handle.open_input("name".into(), elicit("Name?")).unwrap();
        match store.get(handle.task_id(), "alice").unwrap().payload {
            TaskPayload::InputRequired { input_requests } => {
                assert_eq!(input_requests.keys().collect::<Vec<_>>(), ["name"]);
                assert_eq!(input_requests["name"], elicit("Name?"));
            }
            other => panic!("expected input_required, got {other:?}"),
        }
        let answer = serde_json::json!({"action": "accept", "content": {"name": "Ada"}});
        store
            .update(
                handle.task_id(),
                "alice",
                [
                    ("unknown".to_owned(), serde_json::json!(1)),
                    ("name".to_owned(), answer.clone()),
                ],
            )
            .unwrap();
        assert_eq!(receiver.await.unwrap(), answer);
        assert_eq!(status(&store, &handle, "alice"), TaskStatus::Working);
        // SEP-2663: a key is unique over the life of the task.
        assert_eq!(
            handle
                .open_input("name".into(), elicit("Again?"))
                .unwrap_err(),
            InputError::KeyUsed
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_result_completes_the_task_and_cancel_after_it_changes_nothing() {
        let store = TaskStore::new(10);
        let (_task, handle) = store.create("alice".into(), OPTIONS).unwrap();
        handle.settle(result("42"));
        assert!(handle.cancel_token().is_cancelled());
        store.cancel(handle.task_id(), "alice").unwrap();
        store
            .update(handle.task_id(), "alice", [("k".to_owned(), Value::Null)])
            .unwrap();
        let detailed = store.get(handle.task_id(), "alice").unwrap();
        assert_eq!(detailed.status(), TaskStatus::Completed);
        let Outcome::Completed(expected) = result("42") else {
            unreachable!()
        };
        assert_eq!(
            detailed.payload,
            TaskPayload::Completed { result: expected }
        );
        assert_eq!(
            handle
                .open_input("late".into(), elicit("Late?"))
                .unwrap_err(),
            InputError::NotRunning
        );
    }

    #[tokio::test(start_paused = true)]
    async fn cancel_settles_the_task_and_wakes_the_tool() {
        let store = TaskStore::new(10);
        let (_task, handle) = store.create("alice".into(), OPTIONS).unwrap();
        let receiver = handle.open_input("k".into(), elicit("Wait")).unwrap();
        store.cancel(handle.task_id(), "alice").unwrap();
        assert!(handle.cancel_token().is_cancelled());
        assert!(receiver.await.is_err(), "the open input must wake");
        assert_eq!(status(&store, &handle, "alice"), TaskStatus::Cancelled);
        // The tool ends later. The task stays cancelled.
        handle.settle(result("too late"));
        assert_eq!(status(&store, &handle, "alice"), TaskStatus::Cancelled);
    }

    #[tokio::test(start_paused = true)]
    async fn a_failure_keeps_its_json_rpc_error() {
        let store = TaskStore::new(10);
        let (_task, handle) = store.create("alice".into(), OPTIONS).unwrap();
        handle.settle(Outcome::failed(&McpError::invalid_params("bad", None)));
        let detailed = store.get(handle.task_id(), "alice").unwrap();
        assert_eq!(detailed.task.status_message.as_deref(), Some("bad"));
        match detailed.payload {
            TaskPayload::Failed { error } => {
                assert_eq!(error["code"], -32602);
                assert_eq!(error["message"], "bad");
            }
            other => panic!("expected failed, got {other:?}"),
        }
    }

    #[tokio::test(start_paused = true)]
    async fn the_ttl_fails_a_running_task_then_removes_it() {
        let store = TaskStore::new(10);
        let (_task, handle) = store.create("alice".into(), OPTIONS).unwrap();
        assert_eq!(
            handle.deadline(),
            Instant::now() + Duration::from_millis(1_000)
        );
        tokio::time::advance(Duration::from_millis(999)).await;
        assert_eq!(status(&store, &handle, "alice"), TaskStatus::Working);
        tokio::time::advance(Duration::from_millis(1)).await;
        let detailed = store.get(handle.task_id(), "alice").unwrap();
        assert_eq!(detailed.status(), TaskStatus::Failed);
        assert!(handle.cancel_token().is_cancelled());
        let message = detailed.task.status_message.unwrap();
        assert!(message.contains("TTL"), "{message}");
        // Kept for one TTL after it failed, then removed.
        tokio::time::advance(Duration::from_millis(999)).await;
        assert_eq!(status(&store, &handle, "alice"), TaskStatus::Failed);
        tokio::time::advance(Duration::from_millis(1)).await;
        assert_eq!(
            store.get(handle.task_id(), "alice").unwrap_err(),
            unknown_task(handle.task_id())
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_result_after_the_ttl_fails_the_task() {
        let store = TaskStore::new(10);
        let (_task, late) = store.create("alice".into(), OPTIONS).unwrap();
        let (_task, early) = store.create("alice".into(), OPTIONS).unwrap();
        tokio::time::advance(Duration::from_millis(999)).await;
        early.settle(result("in time"));
        tokio::time::advance(Duration::from_millis(1)).await;
        // No tasks/get ran since the deadline, so no sweep failed the task first.
        late.settle(result("too late"));
        assert_eq!(status(&store, &early, "alice"), TaskStatus::Completed);
        let detailed = store.get(late.task_id(), "alice").unwrap();
        assert_eq!(detailed.status(), TaskStatus::Failed);
        assert!(detailed.task.status_message.unwrap().contains("TTL"));
    }

    #[tokio::test(start_paused = true)]
    async fn a_full_store_refuses_until_a_task_is_removed() {
        let store = TaskStore::new(2);
        let (_a, first) = store.create("alice".into(), OPTIONS).unwrap();
        let (_b, _second) = store.create("bob".into(), OPTIONS).unwrap();
        assert!(store.create("carol".into(), OPTIONS).is_none());
        // A finished task still counts until the store removes it.
        first.settle(result("done"));
        assert!(store.create("carol".into(), OPTIONS).is_none());
        tokio::time::advance(Duration::from_millis(1_000)).await;
        // The TTL failed the second task, and removed the first one.
        assert!(store.create("carol".into(), OPTIONS).is_some());
    }

    #[tokio::test(start_paused = true)]
    async fn a_dropped_handle_fails_its_running_task() {
        let store = TaskStore::new(10);
        let (task, handle) = store.create("alice".into(), OPTIONS).unwrap();
        let token = handle.cancel_token().clone();
        drop(handle);
        assert!(token.is_cancelled());
        let detailed = store.get(&task.task_id, "alice").unwrap();
        assert_eq!(detailed.status(), TaskStatus::Failed);
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_the_store_cancels_each_running_task() {
        let store = TaskStore::new(10);
        let (_a, running) = store.create("alice".into(), OPTIONS).unwrap();
        let (_b, done) = store.create("alice".into(), OPTIONS).unwrap();
        done.settle(result("done"));
        drop(store);
        assert!(running.cancel_token().is_cancelled());
        // The tool ends after the store: nothing to settle.
        running.settle(result("late"));
    }

    /// One step of a random run against the store.
    #[derive(Clone, Debug)]
    enum Step {
        Create(usize),
        Get(usize, usize),
        Update(usize, usize, usize),
        Cancel(usize, usize),
        OpenInput(usize, usize),
        Settle(usize, bool),
        Advance(u64),
    }

    fn step() -> impl Strategy<Value = Step> {
        prop_oneof![
            (0..2usize).prop_map(Step::Create),
            (0..6usize, 0..2usize).prop_map(|(t, o)| Step::Get(t, o)),
            (0..6usize, 0..2usize, 0..3usize).prop_map(|(t, o, k)| Step::Update(t, o, k)),
            (0..6usize, 0..2usize).prop_map(|(t, o)| Step::Cancel(t, o)),
            (0..6usize, 0..3usize).prop_map(|(t, k)| Step::OpenInput(t, k)),
            (0..6usize, any::<bool>()).prop_map(|(t, ok)| Step::Settle(t, ok)),
            (0..700u64).prop_map(Step::Advance),
        ]
    }

    fn task_id(tasks: &[(TaskHandle, usize)], index: usize) -> Option<String> {
        tasks
            .get(index)
            .map(|(handle, _)| handle.task_id().to_owned())
    }

    const OWNERS: [&str; 2] = ["alice", "bob"];
    const KEYS: [&str; 3] = ["a", "b", "c"];

    /// Check the invariants of each task after a step. `seen` holds the
    /// first terminal state that the owner read, and whether the task is gone.
    fn check(
        store: &TaskStore,
        tasks: &[(TaskHandle, usize)],
        seen: &mut [(Option<DetailedTask>, bool)],
    ) {
        for (index, (handle, owner)) in tasks.iter().enumerate() {
            let id = handle.task_id();
            let other = OWNERS[1 - owner];
            assert_eq!(store.get(id, other).unwrap_err(), unknown_task(id));
            let (terminal, gone) = &mut seen[index];
            match store.get(id, OWNERS[*owner]) {
                Err(error) => {
                    assert_eq!(error, unknown_task(id));
                    *gone = true;
                }
                Ok(detailed) => {
                    assert!(!*gone, "a removed task came back");
                    let settled = detailed.status().is_terminal();
                    assert_eq!(handle.cancel_token().is_cancelled(), settled);
                    if let Some(first) = terminal {
                        assert_eq!(first.payload, detailed.payload, "a terminal state changed");
                    } else if settled {
                        *terminal = Some(detailed);
                    }
                }
            }
        }
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(64))]

        #[test]
        fn random_runs_keep_the_state_machine_invariants(steps in prop::collection::vec(step(), 1..40)) {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_time()
                .start_paused(true)
                .build()
                .unwrap();
            runtime.block_on(async {
                let store = TaskStore::new(4);
                let mut tasks: Vec<(TaskHandle, usize)> = Vec::new();
                let mut seen: Vec<(Option<DetailedTask>, bool)> = Vec::new();
                let mut waiting = Vec::new();
                for step in steps {
                    match step {
                        Step::Create(owner) => {
                            if let Some((_, handle)) = store.create(OWNERS[owner].into(), OPTIONS) {
                                tasks.push((handle, owner));
                                seen.push((None, false));
                            }
                        }
                        Step::Get(t, owner) => {
                            if let Some(id) = task_id(&tasks, t) {
                                let _ = store.get(&id, OWNERS[owner]);
                            }
                        }
                        Step::Update(t, owner, key) => {
                            if let Some(id) = task_id(&tasks, t) {
                                let _ = store.update(&id, OWNERS[owner], [(KEYS[key].to_owned(), Value::Null)]);
                            }
                        }
                        Step::Cancel(t, owner) => {
                            if let Some(id) = task_id(&tasks, t) {
                                let _ = store.cancel(&id, OWNERS[owner]);
                            }
                        }
                        Step::OpenInput(t, key) => {
                            if let Some((handle, _)) = tasks.get(t)
                                && let Ok(receiver) = handle.open_input(KEYS[key].into(), elicit(KEYS[key]))
                            {
                                waiting.push(receiver);
                            }
                        }
                        Step::Settle(t, ok) => {
                            if let Some((handle, _)) = tasks.get(t) {
                                handle.settle(if ok { result("ok") } else { Outcome::Cancelled });
                            }
                        }
                        Step::Advance(ms) => tokio::time::advance(Duration::from_millis(ms)).await,
                    }
                    check(&store, &tasks, &mut seen);
                    let entries = store.shared.lock();
                    assert!(entries.len() <= 4, "the store holds more than max_tasks");
                    for entry in entries.values() {
                        if let Phase::Running(running) = &entry.phase {
                            let detailed = entry.detailed();
                            let expected = if running.inputs.is_empty() {
                                TaskStatus::Working
                            } else {
                                TaskStatus::InputRequired
                            };
                            assert_eq!(detailed.status(), expected);
                        }
                    }
                }
            });
        }
    }
}
