# Threading

Long-running Python tasks (network requests, file I/O, MCP servers) must run in background threads to avoid freezing TouchDesigner's UI and cook cycle.

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
2. **For sub-tasks inside worker threads**, use plain `threading.Thread` instead of ThreadManager
3. **Worker pool is limited** to 4 threads (CPU count cap) — use `standalone=True` for long-lived tasks
4. **All data crossing the thread boundary** must go through `InfoQueue` or `Queue` objects
