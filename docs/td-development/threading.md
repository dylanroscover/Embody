# Threading

Long-running Python tasks (network requests, file I/O, MCP servers) must not block TouchDesigner's single main thread, or the UI and cook cycle freeze - and touching a TD object from a worker thread crashes TD outright.

**Choose the lightest mechanism; threading is the last resort.** Prototype synchronously to prove the logic, then: (1) fast TD-only work -> run it inline on the main thread; (2) **HTTP fetch -> the TD-native [Web Client DAT](https://docs.derivative.ca/Web_Client_DAT)**, which does async networking with no hand-rolled threads and delivers `onResponse` on the main thread (parse with a [JSON DAT](https://docs.derivative.ca/JSON_DAT) and bridge to channels with a DAT to CHOP); (3) long main-thread work -> chunk across frames with `run()`; (4) blocking pure-Python work -> the Thread Manager below (prefer the Palette Thread Manager Client); (5) a long-lived loop or a pure-Python sub-task -> a `threading.Thread` that touches no TD object and never calls `run()`. Reach for a thread last, not first.

## Thread Manager

TouchDesigner provides `op.TDResources.ThreadManager` — a wrapper around Python's `threading` module with TD-safe hooks.

!!! danger "Critical Rule"
    **Never access TouchDesigner objects (OPs, COMPs, parameters) from a worker thread.** All TD operations must go through hooks that execute on the main thread.

## Basic Usage

```python
# Create a task
task = op.TDResources.ThreadManager.TDTask(
    target=my_background_function,   # Runs in worker thread (no TD access!)
    args=(arg1, arg2),               # Passed to target
    SuccessHook=on_success,          # Main thread — called when target returns
    ExceptHook=on_error,             # Main thread — called on exception
    RefreshHook=on_refresh,          # Main thread — called every frame while running
)

# Enqueue it (runs in worker pool)
op.TDResources.ThreadManager.EnqueueTask(task)

# Or run in a dedicated thread (outside the pool)
op.TDResources.ThreadManager.EnqueueTask(task, standalone=True)
```

## Key Concepts

### TDTask
A unit of work with a `target` callable and optional hooks:

- `target` — function to run in background (no TD access)
- `SuccessHook` — called on main thread when target returns
- `ExceptHook` — called on main thread on exception
- `RefreshHook` — called every frame on main thread while running

### InfoQueue
Thread-safe channel from worker thread to main thread:

```python
# In worker thread:
worker_thread.InfoQueue.put(data)

# In RefreshHook (main thread):
def on_refresh(task):
    while not task.thread.InfoQueue.empty():
        data = task.thread.InfoQueue.get()
        # Process data with TD access here
```

### Standalone vs Pool

- **Pool** (default): Up to 4 worker threads for short-lived tasks
- **Standalone** (`standalone=True`): Dedicated thread for long-lived tasks like servers

## `run()` — Delayed Execution

The `run()` function defers Python execution — essential for timing-sensitive operations:

```python
# Delay by frames:
run("op('/project1/base1').cook(force=True)", delayFrames=1)

# Delay by time:
run("print('done')", delayMilliSeconds=500)

# End-of-frame execution (after current cook cycle):
run("op.Embody.Update()", endFrame=True)

# With a callable:
run(myFunction, arg1, arg2, delayFrames=5)

# Relative to a specific operator:
run("me.cook(force=True)", fromOP=op('/project1/base1'), delayFrames=1)
```

## Thread Safety Pitfalls

1. **Never access `op.TDResources.ThreadManager` from a worker thread** — it's a TD COMP, and accessing it triggers a THREAD CONFLICT
2. **For sub-tasks spawned INSIDE an existing worker thread** (where `EnqueueTask` is itself off-limits), use a plain `threading.Thread` - and its body, like any worker, must touch ZERO TD objects and must never call `run()`/`td.run()` (which raises `tdError` from a worker). This is NOT a recommendation to start background work with `threading.Thread`; for that, prefer the Web Client DAT (fetches) or the Thread Manager (blocking pure-Python)
3. **Worker pool is limited** to 4 threads (CPU count cap) — use `standalone=True` for long-lived tasks
4. **All data crossing the thread boundary** must go through `InfoQueue` or `Queue` objects
