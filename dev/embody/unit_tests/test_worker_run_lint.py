"""Static lint: TD's global run() reached from a worker thread.

Derivative confirmed (2026-08-17) that run()/td.run() called off the main
thread does NOT raise on current builds -- it silently corrupts TD state and
crashes frames or minutes later. Nothing catches it at runtime, so Envoy
catches it at WRITE time: execute_python and the two DAT-write tools lint
their submitted source and ride a THREADING WARNING back in _logs.

_worker_run_findings is pure (source string in, findings list out), so the
whole decision is exercised here off-TD, on the same CI matrix as the other
TD-import-free suites. What is worth testing is the two ways this lint could
be useless: missing the hazard (a locally defined worker, a lambda, a helper
one call away), and crying wolf (subprocess.run, x.run, main-thread run(),
a source that does not even parse).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


ENVOY_EXT = (Path(__file__).resolve().parents[1] / "Embody" / "EnvoyExt.py")


def _load_envoy_module():
    spec = importlib.util.spec_from_file_location(
        "worker_run_lint_envoy", ENVOY_EXT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_ENVOY = _load_envoy_module()
findings = _ENVOY._worker_run_findings


# --- positive: the hazard is detected -----------------------------------

def test_thread_target_calling_run_is_flagged():
    got = findings(
        "import threading\n"
        "def worker():\n"
        "    run('op(\\'x\\').par.y = 1', delayFrames=1)\n"
        "threading.Thread(target=worker).start()\n")
    assert len(got) == 1
    assert got[0]["function"] == "worker"
    assert got[0]["call"] == "run()"
    assert got[0]["line"] == 3
    assert got[0]["via"] is None


def test_bare_thread_constructor_is_flagged():
    """Thread(target=...) after `from threading import Thread` -- the lint
    matches the trailing callable name, so the import shape is irrelevant."""
    got = findings(
        "from threading import Thread\n"
        "def worker():\n"
        "    run('pass')\n"
        "Thread(target=worker).start()\n")
    assert [f["function"] for f in got] == ["worker"]


def test_td_run_attribute_call_is_flagged():
    got = findings(
        "import threading\n"
        "def worker():\n"
        "    td.run('pass')\n"
        "threading.Thread(target=worker).start()\n")
    assert len(got) == 1
    assert got[0]["call"] == "td.run()"


def test_enqueue_task_positional_target_is_flagged():
    """ThreadManager takes its callable positionally: EnqueueTask(task, ...)."""
    got = findings(
        "def worker():\n"
        "    run('pass')\n"
        "op.TDResources.ThreadManager.EnqueueTask(worker, standalone=True)\n")
    assert [f["function"] for f in got] == ["worker"]


def test_tdtask_target_keyword_is_flagged():
    got = findings(
        "def worker():\n"
        "    td.run('pass')\n"
        "task = TDTask(target=worker)\n")
    assert [f["function"] for f in got] == ["worker"]


def test_lambda_thread_target_is_flagged():
    got = findings(
        "import threading\n"
        "threading.Thread(target=lambda: run('pass')).start()\n")
    assert len(got) == 1
    assert got[0]["function"] == "<lambda>"
    assert got[0]["line"] == 2


def test_one_level_indirect_call_is_flagged_with_via():
    """The target itself is clean; the helper it calls in the SAME source
    is the one that touches run() -- and it runs on the worker thread too."""
    got = findings(
        "import threading\n"
        "def apply_result(value):\n"
        "    run('op(\\'x\\').par.y = %s' % value)\n"
        "def worker():\n"
        "    apply_result(3)\n"
        "threading.Thread(target=worker).start()\n")
    assert len(got) == 1
    assert got[0]["function"] == "worker"
    assert got[0]["via"] == "apply_result"
    assert got[0]["line"] == 3


def test_locally_defined_worker_inside_a_function_is_flagged():
    got = findings(
        "import threading\n"
        "def start():\n"
        "    def worker():\n"
        "        run('pass')\n"
        "    threading.Thread(target=worker).start()\n")
    assert [f["function"] for f in got] == ["worker"]


def test_method_style_target_resolves_by_attribute_name():
    got = findings(
        "import threading\n"
        "class Poller:\n"
        "    def _loop(self):\n"
        "        run('pass')\n"
        "    def start(self):\n"
        "        threading.Thread(target=self._loop).start()\n")
    assert [f["function"] for f in got] == ["_loop"]


# --- negative: no false positives ---------------------------------------

def test_main_thread_run_is_not_flagged():
    """run() at module level and in an ordinary never-threaded function is
    exactly how TD code is supposed to defer work. Not a finding."""
    assert findings(
        "run('pass', delayFrames=5)\n"
        "def later():\n"
        "    run('pass', delayFrames=1)\n"
        "later()\n") == []


def test_subprocess_run_in_a_thread_target_is_not_flagged():
    assert findings(
        "import subprocess, threading\n"
        "def worker():\n"
        "    subprocess.run(['git', 'status'])\n"
        "threading.Thread(target=worker).start()\n") == []


def test_unrelated_method_named_run_is_not_flagged():
    assert findings(
        "import threading\n"
        "def worker():\n"
        "    server.run(host='127.0.0.1')\n"
        "    self.run()\n"
        "threading.Thread(target=worker).start()\n") == []


def test_thread_target_without_run_is_not_flagged():
    assert findings(
        "import threading, queue\n"
        "def worker(out):\n"
        "    out.put(compute())\n"
        "threading.Thread(target=worker, args=(queue.Queue(),)).start()\n") == []


def test_run_in_a_string_or_comment_is_not_flagged():
    assert findings(
        "import threading\n"
        "def worker():\n"
        "    # never call run() here\n"
        "    note = 'run(\\'pass\\')'\n"
        "    return note\n"
        "threading.Thread(target=worker).start()\n") == []


def test_locally_defined_run_shadows_the_bare_call():
    """A source with its own `run` is calling THAT one; bare run() says
    nothing there. td.run() stays unambiguous and is still caught."""
    assert findings(
        "import threading\n"
        "def run(job):\n"
        "    return job()\n"
        "def worker():\n"
        "    run(compute)\n"
        "threading.Thread(target=worker).start()\n") == []


def test_unparseable_source_lints_to_nothing():
    """Submitted code may be a fragment; the lint must never be the thing
    that fails a write."""
    assert findings("def worker(:\n    run('pass')\n") == []
    assert findings("") == []


def test_non_string_input_is_survivable():
    assert findings(None) == []


# --- panel repros: shadow scope, subclasses, dispatchers, cost bounds ----

def test_thread_subclass_run_method_is_flagged():
    """The other standard spawn idiom: subclass threading.Thread, override
    run(); .start() invokes it on the worker."""
    got = findings(
        "import threading\n"
        "class Worker(threading.Thread):\n"
        "    def run(self):\n"
        "        td.run('op(\\'x\\').par.y = 1')\n"
        "Worker().start()\n")
    assert len(got) == 1
    assert got[0]["function"] == "Worker.run"
    assert got[0]["call"] == "td.run()"


def test_thread_subclass_bare_run_call_is_flagged():
    """A run() METHOD does not rebind module scope -- a bare run() inside
    it still reaches TD's global and is a finding."""
    got = findings(
        "import threading\n"
        "class Worker(threading.Thread):\n"
        "    def run(self):\n"
        "        run('pass')\n")
    assert [f["function"] for f in got] == ["Worker.run"]


def test_thread_subclass_without_hazard_is_clean():
    assert findings(
        "import threading\n"
        "class Worker(threading.Thread):\n"
        "    def run(self):\n"
        "        self.result = compute()\n") == []


def test_class_method_named_run_does_not_disable_the_lint():
    """Panel repro: the old whole-tree shadow scan let ANY method named run
    (every Thread subclass has one) suppress bare-run detection globally --
    silencing the lint on exactly the threading-heavy sources it exists
    for. Only a MODULE-scope binding of `run` suppresses now."""
    got = findings(
        "import threading\n"
        "class Worker(threading.Thread):\n"
        "    def run(self):\n"
        "        pass\n"
        "def other():\n"
        "    run('op(\\'x\\').par.y = 1')\n"
        "threading.Thread(target=other).start()\n")
    assert [f["function"] for f in got] == ["other"]


def test_local_variable_named_run_does_not_disable_the_lint():
    got = findings(
        "import threading\n"
        "def unrelated():\n"
        "    run = 5\n"
        "    return run\n"
        "def worker():\n"
        "    run('pass')\n"
        "threading.Thread(target=worker).start()\n")
    assert [f["function"] for f in got] == ["worker"]


def test_executor_submit_target_is_flagged():
    got = findings(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "def job():\n"
        "    run('pass')\n"
        "pool = ThreadPoolExecutor()\n"
        "pool.submit(job)\n")
    assert [f["function"] for f in got] == ["job"]


def test_repeated_spawn_sites_yield_one_finding_set():
    """30 spawn sites of the same worker are one scan and one finding --
    the dedupe that bounds the repeated-target cost pathology."""
    got = findings(
        "import threading\n"
        "def worker():\n"
        "    run('pass')\n"
        + "threading.Thread(target=worker).start()\n" * 30)
    assert [f["function"] for f in got] == ["worker"]


def test_oversized_source_is_skipped_before_parsing():
    """The byte cap keeps ast.parse off the main thread's frame budget; an
    oversized source lints to nothing even when it contains the hazard."""
    src = ("import threading\n"
           "def worker():\n"
           "    run('pass')\n"
           "threading.Thread(target=worker).start()\n")
    pad_line = "# " + "x" * 200 + "\n"
    pad_count = (_ENVOY._WORKER_RUN_LINT_MAX_BYTES // len(pad_line)) + 1
    padded = src + pad_line * pad_count
    assert len(padded) > _ENVOY._WORKER_RUN_LINT_MAX_BYTES
    assert findings(padded) == []


def test_source_without_run_substring_skips_the_parse():
    """A payload with no 'run(' at all (a JSON blob, a table dump) cannot
    produce a finding; the substring prefilter rejects it pre-parse."""
    assert findings('x = {"a": 1}\n' * 1000) == []
