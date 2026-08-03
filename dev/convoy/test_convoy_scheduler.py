"""Bounded rolling poll/drain scheduling at the 30-node product ceiling."""

import threading
import time

import convoy_controllers as controllers
import convoy_hostapp as hostapp


class FakeStore:
    def __init__(self, jobs):
        self._jobs = [dict(job) for job in jobs]
        self.audits = []

    def jobs(self, state=None):
        rows = [dict(job) for job in self._jobs]
        if state is not None:
            rows = [job for job in rows if job.get("state") == state]
        return rows

    def get_job(self, delivery_id):
        for job in self._jobs:
            if job.get("delivery_id") == delivery_id:
                return dict(job)
        return None

    def job_file_exists(self, _delivery_id):
        return False

    def audit(self, actor, event, detail=None):
        self.audits.append((actor, event, detail))


def scheduler_app(jobs, workers=hostapp.PASS_MAX_WORKERS):
    """The real pass methods with only their store/bookkeeping seams."""
    app = object.__new__(hostapp.HostApp)
    app.lock = threading.Lock()
    app.db = FakeStore(jobs)
    app._now = time.time
    app.pass_max_workers = workers
    app.poll_backoff_s = 0.0
    app.drain_backoff_s = 0.0
    app._poll_backoff = {}
    app._poll_noted = {}
    app._poll_unknown = {}
    app._drain_backoff = {}
    app._drain_noted = {}
    app._drain_map_cap = hostapp.DRAIN_MAP_FLOOR
    app._pending_release = {}
    app._pending_handoff = {}
    app._in_flight = {}
    app.leases = controllers.LeaseRegistry()
    return app


def jobs(count, state="queued", node_for=None):
    rows = []
    for index in range(count):
        node_id = (node_for(index) if node_for is not None
                   else "node-%02d" % index)
        rows.append({
            "delivery_id": "cj_%032x" % index,
            "node_id": node_id,
            "state": state,
        })
    return rows


def dispatched(state="succeeded"):
    return 200, {"ok": True, "dispatched": True,
                 "job": {"state": state}}


def test_poll_overlaps_distinct_nodes_at_the_hard_eight_worker_cap():
    app = scheduler_app(jobs(16, state="running"), workers=999)
    lock = threading.Lock()
    release = threading.Event()
    active = {"now": 0, "max": 0}

    def poll(_delivery_id):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            if active["now"] == hostapp.PASS_MAX_WORKERS:
                release.set()
        assert release.wait(timeout=10)
        time.sleep(0.005)
        with lock:
            active["now"] -= 1
        return 200, {"ok": True, "polled": True,
                     "job": {"state": "running"}}

    app.poll_job = poll
    summary = app.poll_once()
    assert summary == {
        "examined": 16, "finished": 0, "failed": 0, "running": 16,
        "indeterminate": 0, "unreachable": 0, "deferred": 0,
        "skipped": 0, "errors": 0, "backoff": 0, "aborted": False,
    }
    assert active == {"now": 0, "max": hostapp.PASS_MAX_WORKERS}


def test_rolling_replenishment_isolates_a_hung_target_and_serializes_it():
    rows = jobs(
        12, node_for=lambda index: "node-hung" if index < 2
        else "node-%02d" % index)
    app = scheduler_app(rows, workers=3)
    hung_entered = threading.Event()
    release_hung = threading.Event()
    healthy_done = threading.Event()
    lock = threading.Lock()
    calls = []
    healthy = {"count": 0}

    def dispatch(delivery_id):
        index = int(delivery_id[3:], 16)
        with lock:
            calls.append(index)
        if index == 0:
            hung_entered.set()
            assert release_hung.wait(timeout=15)
        elif index >= 2:
            with lock:
                healthy["count"] += 1
                if healthy["count"] == 10:
                    healthy_done.set()
        return dispatched()

    app.dispatch_job = dispatch
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("summary", app.drain_once()))
    thread.start()
    try:
        assert hung_entered.wait(timeout=10)
        assert healthy_done.wait(timeout=10), (
            "healthy targets did not roll through free worker slots")
        with lock:
            assert 1 not in calls, (
                "a second call to the hung target occupied another slot")
    finally:
        release_hung.set()
        thread.join(timeout=15)
    assert not thread.is_alive()
    assert result["summary"]["dispatched"] == 12
    assert result["summary"]["errors"] == 0
    with lock:
        assert calls[-1] == 1


def test_stop_submits_no_more_work_marks_aborted_and_finishes_active_calls():
    app = scheduler_app(jobs(20), workers=4)
    stop = threading.Event()
    first_wave = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = []
    finished = []

    def dispatch(delivery_id):
        with lock:
            calls.append(delivery_id)
            if len(calls) == 4:
                first_wave.set()
        assert release.wait(timeout=10)
        with lock:
            finished.append(delivery_id)
        return dispatched()

    app.dispatch_job = dispatch
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("summary", app.drain_once(stop)))
    thread.start()
    try:
        assert first_wave.wait(timeout=10)
        stop.set()
    finally:
        release.set()
        thread.join(timeout=15)
    assert not thread.is_alive()
    assert result["summary"]["aborted"] is True
    assert result["summary"]["examined"] == 20
    assert result["summary"]["dispatched"] == 4
    assert len(calls) == 4, "stop allowed rolling replenishment"
    assert sorted(finished) == sorted(calls), "active calls did not finish"


def test_thirty_node_drain_has_stable_counts_and_exception_isolation():
    app = scheduler_app(jobs(30), workers=8)

    def dispatch(delivery_id):
        index = int(delivery_id[3:], 16)
        # Scramble completion order without making the test timing-sensitive.
        time.sleep(((29 - index) % 5) * 0.001)
        if index < 10:
            return dispatched()
        if index < 15:
            return dispatched("running")
        if index < 20:
            return 409, {"ok": False, "dispatched": False,
                         "reason": "node_unreachable"}
        if index < 25:
            return 200, {"ok": True, "dispatched": False}
        raise RuntimeError("one target failed")

    app.dispatch_job = dispatch
    expected = {
        "examined": 30, "dispatched": 15, "started": 5,
        "indeterminate": 0, "unreachable": 5, "no_handle": 0,
        "deferred": 0, "skipped": 5, "errors": 5, "backoff": 0,
        "refused": 0, "stranded": 0, "requeued": 0, "handoffs": 0,
        "aborted": False,
    }
    assert app.drain_once() == expected
    assert app.drain_once() == expected


def test_background_tick_keeps_poll_before_drain():
    app = object.__new__(hostapp.HostApp)
    stop = threading.Event()
    order = []

    def poll_once(stop=None):
        order.append("poll")
        return {"errors": 0}

    def drain_once(stop=None):
        order.append("drain")
        stop.set()
        return {"errors": 0}

    app.poll_once = poll_once
    app.drain_once = drain_once
    app._audit_pass = lambda kind, summary: None
    app._maybe_reap = lambda: None
    app._drain_loop(stop, 0.001)
    assert order == ["poll", "drain"]
