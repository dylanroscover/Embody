"""Phase 4 slice 4: the host POLLS node jobs it handed off.

An async operation (run_tests, save_project) answers with a node-job
handle; the delivery record rests at 'running' and the poll pass mirrors
what the node eventually says. The invariant this suite exists to defend
is the ASYMMETRY between dispatch and poll:

    a dispatch that got no response may have MUTATED something, so it
    is indeterminate; a poll is a READ, so a failed poll teaches nothing
    and MUST leave the job running.

Everything else here -- concurrency, backoff, grace windows, bounded
payloads -- protects that one honesty property from the obvious
"symmetry" refactor.
"""

import json
import threading
import time

import pytest

import convoy_hostapp as ha
import convoy_mcpclient as mcpclient
from conftest import approve_td_python
from test_convoy_hostapp import Server

CONVOY = "studio"
NODE_JOB = "job_1a2b3c4d"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    s.app.poll_backoff_s = 0.0      # pacing is tested explicitly, not
    s.app.drain_backoff_s = 0.0     # smuggled into every other test
    yield s
    s.stop()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(tmp_path, clock):
    """A host app with a controllable clock and no HTTP server -- the
    poll surface is self-locking, so it is directly callable."""
    a = ha.HostApp(str(tmp_path / "state"), now=clock)
    a.poll_backoff_s = 0.0
    a.drain_backoff_s = 0.0
    yield a
    a.stop_drain_loop()
    a.db.close()


# -- helpers ----------------------------------------------------------

def register(app, envoy_port=9800, comp="/Embody", runtime_id="rt1"):
    code, node = app.register_node({
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": comp, "envoy_port": envoy_port,
        "runtime_id": runtime_id})
    assert code == 200, node
    # This polling suite exercises run_tests handles. Opt in explicitly so
    # its transport/state assertions do not bypass the production code gate.
    approve_td_python(app, node["node_id"])
    return node


def enqueue(app, node, operation="run_tests", key="k"):
    with app.lock:
        code, body = app.create_job({
            "idempotency_key": key, "node_id": node["node_id"],
            "operation": operation, "arguments": {},
            "expected_runtime_id": node["runtime_id"]})
    assert code == 200, body
    return body["job"]


def handoff(app, job, node_job_id=NODE_JOB):
    """Dispatch an async job to the point the poller takes over."""
    previous = app.forwarder
    app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"job_id": node_job_id, "status": "running"}}
    code, body = app.dispatch_job(job["delivery_id"])
    app.forwarder = previous
    assert code == 200 and body["job"]["state"] == "running", body
    return body["job"]


def running_job(app, key="k", node_job_id=NODE_JOB, **kw):
    """A delivery record resting at 'running' on a live node job."""
    node = register(app, **kw)
    return handoff(app, enqueue(app, node, key=key), node_job_id), node


def node_record(status="done", job_id=NODE_JOB, **extra):
    """What get_job_status returns for one job (EnvoyExt _job_public)."""
    record = {"id": job_id, "kind": "run_tests", "status": status,
              "started": 1.0, "age_s": 5.0}
    record.update(extra)
    return {"ok": True, "result": record}


def poll_events(app, delivery_id, event=None):
    with app.lock:
        return [e for e in app.db.audit_tail(limit=400)
                if (e["detail"] or {}).get("delivery_id") == delivery_id
                and (event is None or e["event"] == event)]


# =====================================================================
# C1 -- concurrency and the lock, adversarially first
# =====================================================================

def test_the_poll_does_not_hold_the_app_lock(server):
    """C16: a poll is I/O against a node whose main thread may be busy
    for minutes. Proven from INSIDE the poll forward -- the app lock is
    acquirable and the server answers /status over HTTP."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    seen = {}

    def probing(port, operation, arguments):
        got = server.app.lock.acquire(timeout=2)
        if got:
            server.app.lock.release()
        seen["lock_free"] = got
        seen["status_code"] = server.call("/status")[0]
        return node_record()

    server.app.forwarder = probing
    code, body = server.call("/poll", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert seen["lock_free"] is True, "the poll ran under the app lock"
    assert seen["status_code"] == 200, "/status blocked during a poll"


def test_a_poll_in_flight_is_skipped_by_a_second_poll(server):
    """C17: two concurrent polls of one job are harmless but wasteful,
    and a late answer could regress state -- so the second one skips.
    The in-flight marker is NOT a claim (a poll is a read): it only
    prevents duplicate I/O."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def slow(port, operation, arguments):
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=10), "test never released the poll"
        return node_record()

    server.app.forwarder = slow
    results = {}
    t = threading.Thread(target=lambda: results.update(
        first=server.call("/poll", {"delivery_id": did})))
    t.start()
    try:
        assert entered.wait(timeout=10), "the first poll never forwarded"
        code, second = server.call("/poll", {"delivery_id": did})
        assert code == 200 and second["polled"] is False
        assert second["reason"] == "poll_in_flight"
        assert second["job"]["state"] == "running", "state changed on a skip"
    finally:
        release.set()
        t.join(timeout=10)
    assert not t.is_alive()
    assert results["first"][1]["job"]["state"] == "succeeded"
    assert calls["n"] == 1, "the in-flight marker let a second poll through"


def test_a_pass_racing_an_in_flight_poll_finishes_the_job_once(server):
    """C18: the drain thread's poll pass and a manual /poll both target
    the same job. Exactly ONE verdict write and ONE poll_finished line --
    a doubled mirror would make the audit trail claim the node finished
    the same run twice."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def slow(port, operation, arguments):
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=10), "test never released the poll"
        return node_record()

    server.app.forwarder = slow
    t = threading.Thread(
        target=lambda: server.call("/poll", {"delivery_id": did}))
    t.start()
    try:
        assert entered.wait(timeout=10)
        summary = server.app.poll_once()
        assert summary["skipped"] == 1 and summary["finished"] == 0
    finally:
        release.set()
        t.join(timeout=10)
    assert calls["n"] == 1
    assert len(poll_events(server.app, did, "poll_finished")) == 1
    # and a pass AFTER the job settled does not touch it again
    assert server.app.poll_once()["examined"] == 0


def test_a_dispatch_during_a_poll_never_reruns_the_operation(server):
    """C19: a running job is past 'queued', so the dispatcher skips it.
    Without that, a /dispatch landing during a poll would start the test
    run a SECOND time on the node."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    seen = {"ops": []}

    def forwarder(port, operation, arguments):
        seen["ops"].append(operation)
        if operation == ha.POLL_OPERATION:
            code, body = server.call("/dispatch", {"delivery_id": did})
            seen["dispatch"] = (code, body["dispatched"])
            return node_record()
        return {"ok": True, "result": {}}

    server.app.forwarder = forwarder
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert seen["dispatch"] == (200, False)
    assert seen["ops"] == [ha.POLL_OPERATION], "the operation was re-run"


def test_a_raising_poll_forwarder_does_not_crash_the_pass(server):
    """C20: a broken transport must cost ONE job's poll, never the pass
    -- and the job stays running, because a crashed read is still a read
    that learned nothing."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))

    def boom(port, operation, arguments):
        raise RuntimeError("socket exploded")

    server.app.forwarder = boom
    summary = server.app.poll_once()
    assert summary["examined"] == 1
    assert summary["unreachable"] == 1, summary
    assert summary["errors"] == 0
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "running"
    # the loop survives it too
    assert server.app.poll_once()["examined"] == 1


def test_stop_drain_loop_bounds_cover_a_poll_in_flight(server):
    """C21: one tick can hold a poll forward AND a dispatch forward, so
    the default stop bound must cover both. A stop bounded shorter than
    the in-flight poll must report False and keep its handles rather
    than lie (the lie enabled two concurrent loops)."""
    node = register(server.app)
    handoff(server.app, enqueue(server.app, node))
    entered, release = threading.Event(), threading.Event()

    def slow(port, operation, arguments):
        entered.set()
        assert release.wait(timeout=30), "test never released the poll"
        return node_record(status="running")

    server.app.forwarder = slow
    assert server.app.start_drain_loop(interval_s=0.05) is True
    try:
        assert entered.wait(timeout=10), "the loop never polled"
        assert server.app.stop_drain_loop(timeout_s=0.2) is False
        # self-locking status(): never call it under app.lock (deadlock)
        assert server.app.status()["drain_loop"] is True
        assert server.app.start_drain_loop(interval_s=60) is False
    finally:
        release.set()
    assert server.app.stop_drain_loop(timeout_s=15) is True
    # the default bound covers a poll forward AND a dispatch forward
    import inspect
    source = inspect.getsource(ha.HostApp.stop_drain_loop)
    assert "2 * mcpclient.DEFAULT_TIMEOUT_S" in source


# =====================================================================
# C2 -- poll honesty (A-15)
# =====================================================================

def test_an_unreachable_poll_leaves_the_job_running(server):
    """C22, THE headline. A poll that cannot reach the node must NEVER
    produce indeterminate. The node's job record is durable and will
    answer when it returns; treating an unreachable READ as a
    may-have-run would burn a healthy run -- and would do it every time
    save_project restarted the Envoy server under us."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: mcpclient.UNREACHABLE
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 409 and body["reason"] == "node_unreachable"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "running"
    assert fetched["job"]["node_job_id"] == NODE_JOB
    assert fetched["job"]["verdict_source"] == "node_poll"
    # and the very next poll against a live node finishes it normally
    server.app.forwarder = lambda p, o, a: node_record()
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 200 and body["job"]["state"] == "succeeded"


def test_a_poll_transport_failure_leaves_the_job_running(server):
    """C23: the ASYMMETRY with dispatch, stated. A dispatch that got no
    response may have MUTATED something -- indeterminate. A poll that
    got no response only failed to LOOK; the node still owns the run."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: None
    for _ in range(3):
        code, body = server.call("/poll", {"delivery_id": did})
        assert code == 409 and body["reason"] == "poll_no_response"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "running", "a failed READ burned a job"
    assert len(poll_events(server.app, did, "poll_no_response")) == 1


def test_a_finished_node_job_is_mirrored_with_its_body(server):
    """C24: the node's fetch-by-id window closes at 24h, so the terminal
    payload is COPIED into the delivery record -- the host's copy is what
    survives."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    summary = {"passed": 357, "failed": 0, "suites": 12}
    server.app.forwarder = lambda p, o, a: node_record(
        status="done", result=summary, finished=99.0)
    code, body = server.call("/poll", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["polled"] is True
    mirrored = body["job"]
    assert mirrored["state"] == "succeeded"
    assert mirrored["verdict_source"] == "node_poll"
    assert mirrored["node_job_id"] == NODE_JOB
    assert mirrored["observed_at"] is not None
    assert mirrored["result"]["result"] == summary
    assert len(poll_events(server.app, job["delivery_id"],
                           "poll_finished")) == 1


def test_a_node_error_job_is_mirrored_as_failed(server):
    """C25: 'error' is the node's own verdict on its own run -- mirrored
    as failed, with the node's message intact."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    server.app.forwarder = lambda p, o, a: node_record(
        status="error", error="Test run failed to start: no unit_tests")
    code, body = server.call("/poll", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "failed"
    assert body["job"]["verdict_source"] == "node_poll"
    assert "unit_tests" in body["job"]["result"]["error"]


def test_a_forgotten_node_job_terminalises_only_after_the_grace(app, clock):
    """C26: 'no job with id ...' is NOT immediately proof the run is
    lost -- a node restart before a flush, the 24h retention, and the
    transient dark-job layer all produce one-off unknowns. Only a
    repeated AND aged unknown terminalises, with the node's own message
    as evidence."""
    job, _node = running_job(app)
    did = job["delivery_id"]
    app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"error": "no job with id %r" % NODE_JOB,
                               "jobs": []}}

    code, body = app.poll_job(did)
    assert code == 409 and body["reason"] == "node_forgot_job_pending"
    assert app.db.get_job(did)["state"] == "running", "one unknown is not proof"

    clock.advance(30.0)                 # aged, but not enough observations
    assert app.poll_job(did)[0] == 409
    assert app.db.get_job(did)["state"] == "running"

    clock.advance(31.0)                 # third observation, past the grace
    code, body = app.poll_job(did)
    assert code == 200 and body["job"]["state"] == "indeterminate"
    evidence = body["job"]["result"]
    assert evidence["reason"] == "node_forgot_job"
    assert evidence["node_job_id"] == NODE_JOB
    assert evidence["observations"] == 3
    assert evidence["first_unknown_at"] == 1000.0
    assert NODE_JOB in evidence["detail"]
    assert len(poll_events(app, did, "poll_node_forgot_job")) == 1


def test_a_fresh_unknown_counter_needs_the_full_grace_again(app, clock):
    """The grace is a WINDOW, not a countdown that survives recovery: a
    poll that sees the job again clears the evidence, so a later unknown
    starts over rather than terminalising on its first sighting."""
    job, _node = running_job(app)
    did = job["delivery_id"]
    unknown = {"ok": True, "result": {"error": "no job with id x",
                                      "jobs": []}}
    app.forwarder = lambda p, o, a: unknown
    for _ in range(2):
        assert app.poll_job(did)[0] == 409
        clock.advance(30.0)
    # the node answers again before the third aged observation lands
    app.forwarder = lambda p, o, a: node_record(status="running")
    assert app.poll_job(did)[1]["job"]["state"] == "running"
    with app.lock:
        assert did not in app._poll_unknown, "stale evidence survived"
    app.forwarder = lambda p, o, a: unknown
    clock.advance(600.0)
    assert app.poll_job(did)[0] == 409
    assert app.db.get_job(did)["state"] == "running"


def test_a_stale_node_record_terminalises_with_the_node_payload(server):
    """C27: 'running' + stale=True is the NODE's own derived verdict --
    its completion poll chain died. The host mirrors that as
    indeterminate carrying the node's evidence, and does NOT invent a
    second host-side staleness horizon."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: node_record(
        status="running", age_s=2400.0, stale=True,
        hint="running far longer than expected")
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 200 and body["job"]["state"] == "indeterminate"
    evidence = body["job"]["result"]
    assert evidence["reason"] == "node_reported_stale"
    assert evidence["node_job_id"] == NODE_JOB
    assert evidence["node_record"]["stale"] is True
    assert evidence["node_record"]["age_s"] == 2400.0
    assert len(poll_events(server.app, did, "poll_stale_indeterminate")) == 1


def test_a_poll_answer_about_a_different_job_is_ignored(server):
    """C28: the answer must be about the job we asked for. A mismatched
    id is debris (a confused node, a crossed response) -- mirroring it
    would file another run's verdict against this delivery."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: node_record(
        status="done", job_id="job_ffffffff", result={"other": True})
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 409 and body["reason"] == "poll_id_mismatch"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "running"
    assert fetched["job"]["node_job_id"] == NODE_JOB
    assert len(poll_events(server.app, did, "poll_id_mismatch")) == 1


def test_a_job_without_node_provenance_is_never_polled(server):
    """C29: the poll argument is the node's own job id. A running record
    without one (only reachable by hand-edited state) is an anomaly --
    refused loudly, never forwarded with a guessed id."""
    node = register(server.app)
    job = enqueue(server.app, node)
    did = job["delivery_id"]
    # _apply_state now refuses a provenance-less 'running' outright
    # (A-15 enforced by state), which is this anomaly's whole point: it
    # is only reachable by HAND-EDITED state -- so the test constructs
    # it exactly that way, on disk.
    import json as _json
    path = server.app.db._job_path(did)
    with open(path, "r", encoding="utf-8") as f:
        rec = _json.load(f)
    rec["state"] = "running"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        _json.dump(rec, f)
    calls = {"n": 0}

    def counting(port, operation, arguments):
        calls["n"] += 1
        return node_record()

    server.app.forwarder = counting
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 409 and body["reason"] == "no_node_provenance"
    assert calls["n"] == 0, "polled with no id to poll for"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "running"
    assert len(poll_events(server.app, did, "poll_missing_provenance")) == 1
    # and a pass counts it as the anomaly it is, without wedging
    assert server.app.poll_once()["errors"] == 1


def test_a_failed_verdict_write_during_a_poll_leaves_the_job_running(
        server, monkeypatch):
    """C30: contrast _downgrade_failed_recording. A dispatch holds a
    CLAIM, so a failed phase-c write must resolve it or it strands. A
    poll holds NOTHING -- so a failed write simply leaves the record as
    the node last left it, and the next poll retries."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: node_record()

    def explode(*args, **kwargs):
        raise OSError("disk says no")

    monkeypatch.setattr(server.app.db, "record_node_verdict", explode)
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 500 and body["reason"] == "poll_recording_failed"
    monkeypatch.undo()
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "running", "nothing claimed, nothing lost"
    with server.app.lock:
        assert did not in server.app._polls_in_flight
    # the retry lands the verdict
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 200 and body["job"]["state"] == "succeeded"


def test_the_poll_result_body_is_bounded(server):
    """C31: a large node verdict is preserved as a private artifact.

    The delivery record and every /jobs response stay bounded without
    destroying the full node-authored result.
    """
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    server.app.forwarder = lambda p, o, a: node_record(
        status="done", result={"log": "x" * (ha.MAX_RESULT_BYTES + 4096)})
    code, body = server.call("/poll", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert body["job"]["verdict_source"] == "node_poll"
    result = body["job"]["result"]
    assert result["spilled"] is True
    assert result["result_bytes"] > ha.MAX_RESULT_BYTES
    assert len(json.dumps(result)) < 4096
    reference = result["artifact"]
    code, payload, lease, _headers = server.app.artifact_open_local_download(
        reference["convoy_id"], reference["artifact_id"])
    assert code == 200 and payload is None
    try:
        restored = json.loads(b"".join(lease).decode("utf-8"))
    finally:
        lease.close()
    assert restored["result"]["log"] == "x" * (
        ha.MAX_RESULT_BYTES + 4096)


# =====================================================================
# C3 -- pass mechanics and end to end
# =====================================================================

def test_poll_once_touches_only_running_jobs(server):
    """C32: a poll pass is about node-owned work. Queued, claimed and
    terminal records belong to the dispatcher or to nobody."""
    node = register(server.app)
    enqueue(server.app, node, key="queued")
    running = handoff(server.app, enqueue(server.app, node, key="running"))
    done = enqueue(server.app, node, key="done")
    with server.app.lock:
        server.app.db.record_sync_result(done["delivery_id"], True,
                                         observed_at=5.0)
    claimed = enqueue(server.app, node, key="claimed")
    polled = []

    def forwarder(port, operation, arguments):
        polled.append(arguments["job_id"])
        return node_record(status="running")

    with server.app.lock:
        server.app.db.claim_for_dispatch(claimed["delivery_id"])
    server.app.forwarder = forwarder
    summary = server.app.poll_once()
    assert summary["examined"] == 1 and summary["running"] == 1
    assert polled == [NODE_JOB]
    _, fetched = server.call(f"/jobs/{running['delivery_id']}")
    assert fetched["job"]["state"] == "running"


def test_poll_backoff_paces_the_node_and_manual_poll_is_exempt(server):
    """C33: a still-running job must not be re-asked every tick. The
    pass backs off; an explicit /poll is its own authority, exactly like
    /dispatch."""
    node = register(server.app)
    handoff(server.app, enqueue(server.app, node))
    calls = {"n": 0}

    def counting(port, operation, arguments):
        calls["n"] += 1
        return node_record(status="running")

    server.app.forwarder = counting
    server.app.poll_backoff_s = 3600.0
    summary = server.app.poll_once()
    assert summary["running"] == 1 and calls["n"] == 1
    summary = server.app.poll_once()
    assert summary["backoff"] == 1 and calls["n"] == 1
    code, _ = server.call("/poll", {"delivery_id":
                                    server.app.db.jobs(state="running")[0]
                                    ["delivery_id"]})
    assert code == 200 and calls["n"] == 2


def test_repeated_poll_failures_audit_once_per_transition(server):
    """C34: an unreachable node is today's NORMAL state between TD
    restarts. Auditing every tick would grow audit.jsonl without bound;
    the trail records the TRANSITION."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: mcpclient.UNREACHABLE
    for _ in range(4):
        server.app.poll_once()
    assert len(poll_events(server.app, did, "poll_unreachable")) == 1
    # a CHANGED failure is a real transition and audits afresh
    server.app.forwarder = lambda p, o, a: None
    for _ in range(2):
        server.app.poll_once()
    assert len(poll_events(server.app, did, "poll_no_response")) == 1
    # ... and so is a change of CAUSE within the same event: a dead
    # transport and the node refusing the status call are different
    # facts, and deduping them together asserted a stale one
    server.app.forwarder = lambda p, o, a: {"ok": False,
                                            "error": "tool exploded"}
    for _ in range(2):
        server.app.poll_once()
    causes = [(e["detail"] or {}).get("reason")
              for e in poll_events(server.app, did, "poll_no_response")]
    assert causes == ["transport", "node_error"], causes
    assert server.app.db.get_job(did)["state"] == "running"


def test_a_poll_after_a_host_restart_finishes_a_running_job(tmp_path):
    """C35, CRASH RECOVERY. The host dies while a node job runs. On
    restart the mirror is intact (the load sweep resolves CLAIMS, not
    node-owned work), the node re-registers its port, and the first poll
    collects the verdict."""
    state = str(tmp_path / "state")
    first = ha.HostApp(state)
    first.poll_backoff_s = 0.0
    node = register(first)
    job = handoff(first, enqueue(first, node))
    did = job["delivery_id"]
    first.db.close()

    second = ha.HostApp(state)          # the host restarts
    second.poll_backoff_s = 0.0
    revived = second.db.get_job(did)
    assert revived["state"] == "running" and revived["node_job_id"] == NODE_JOB
    # envoy_port is per-launch and never persisted: until the node
    # re-registers, the poll defers rather than inventing an endpoint
    second.forwarder = lambda p, o, a: node_record()
    code, body = second.poll_job(did)
    assert code == 409 and body["reason"] == "node_endpoint_unknown"
    assert second.db.get_job(did)["state"] == "running"

    register(second)                    # the node comes back
    code, body = second.poll_job(did)
    assert code == 200 and body["job"]["state"] == "succeeded"
    second.db.close()


def test_the_drain_loop_polls_before_it_dispatches(server):
    """C36: order matters. Learning what already-running node jobs did
    BEFORE starting new work keeps the running set from growing across
    a tick, and keeps a finished job from being polled again next tick."""
    node = register(server.app)
    handoff(server.app, enqueue(server.app, node, key="already-running"))
    enqueue(server.app, node, key="to-dispatch")
    order = []
    done = threading.Event()

    def forwarder(port, operation, arguments):
        order.append(operation)
        if operation == ha.POLL_OPERATION:
            return node_record()
        done.set()
        return {"ok": True, "result": {"job_id": "job_00000002",
                                       "status": "running"}}

    server.app.forwarder = forwarder
    assert server.app.start_drain_loop(interval_s=0.05) is True
    try:
        assert done.wait(timeout=10), "the loop never dispatched"
    finally:
        assert server.app.stop_drain_loop(timeout_s=15) is True
    assert order[0] == ha.POLL_OPERATION, order
    assert "run_tests" in order


def test_an_async_operation_completes_end_to_end_under_the_loop(server):
    """C37: the whole slice, with ZERO manual calls -- queued, handed
    off to a node job, polled, and mirrored succeeded by the autonomous
    loop alone."""
    node = register(server.app)
    phase = {"started": False}

    def forwarder(port, operation, arguments):
        if operation == ha.POLL_OPERATION:
            assert arguments["job_id"] == NODE_JOB
            return node_record(status="done", result={"passed": 1})
        assert arguments["background"] is True
        phase["started"] = True
        return {"ok": True, "result": {"job_id": NODE_JOB,
                                       "status": "running"}}

    server.app.forwarder = forwarder
    assert server.app.start_drain_loop(interval_s=0.05) is True
    try:
        job = enqueue(server.app, node)
        deadline = time.time() + 15
        state = None
        while time.time() < deadline:
            _, fetched = server.call(f"/jobs/{job['delivery_id']}")
            state = fetched["job"]["state"]
            if state in ("succeeded", "failed", "indeterminate"):
                break
            time.sleep(0.05)
        assert state == "succeeded", f"never completed (state={state})"
        assert phase["started"] is True
    finally:
        assert server.app.stop_drain_loop(timeout_s=15) is True
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["verdict_source"] == "node_poll"
    assert fetched["job"]["result"]["result"] == {"passed": 1}


def test_poll_requires_the_token_and_names_a_malformed_id(server):
    """C38: /poll joins the authenticated surface, and a type-confused
    body is an audited 400 -- never a TypeError-500 the trail cannot
    classify (A-39)."""
    code, body = server.call("/poll", {"delivery_id": "cj_x"}, token=None)
    assert code == 401 and body["reason"] == "unauthenticated"
    for bad in (5, 98.6, True, ["cj_x"], {"a": 1}, None):
        code, body = server.call("/poll", {"delivery_id": bad})
        assert code == 400 and body["reason"] == "malformed"
    code, body = server.call("/poll", {"delivery_id": "cj_nosuchjob"})
    assert code == 404 and body["reason"] == "unknown_job"


def test_the_status_route_reports_running_jobs_and_polls(server):
    """Node-side work is invisible in jobs_queued -- status() names it
    so an operator can see the host is waiting on a node, not idle."""
    node = register(server.app)
    handoff(server.app, enqueue(server.app, node))
    enqueue(server.app, node, key="still-queued")
    _, status = server.call("/status")
    assert status["jobs_queued"] == 1
    assert status["jobs_running"] == 1
    assert status["polls_in_flight"] == 0


# =====================================================================
# Panel regressions (2026-08-01)
# =====================================================================

def test_a_running_poll_does_not_rewrite_an_unchanged_mirror(server,
                                                             monkeypatch):
    """PANEL, measured: every successful running-poll rewrote the whole
    job file for a record whose state and handle had not changed -- 6-38
    ms each, 200 atomic rewrites per backoff window at 200 running jobs,
    forever. Nothing durable changed, so nothing is written."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    writes = {"n": 0}
    real = server.app.db._apply_state

    def counting(*a, **kw):
        writes["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(server.app.db, "_apply_state", counting)
    server.app.forwarder = lambda p, o, a: node_record(status="running")
    for _ in range(5):
        code, body = server.call("/poll", {"delivery_id": did})
        assert code == 200 and body["job"]["state"] == "running"
        assert body["refreshed"] is False
    assert writes["n"] == 0, "an unchanged mirror was rewritten"
    # a REAL change always writes immediately -- the skip is about
    # observed_at moving, never about an outcome
    server.app.forwarder = lambda p, o, a: node_record(
        status="done", result={"passed": 1})
    code, body = server.call("/poll", {"delivery_id": did})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert writes["n"] == 1


def test_a_running_mirror_is_refreshed_on_the_horizon(app, clock):
    """The skip is bounded: past POLL_MIRROR_REFRESH_S the durable
    record is refreshed, so observed_at can lag its last observation by
    a known amount rather than indefinitely."""
    job, _node = running_job(app)
    did = job["delivery_id"]
    app.forwarder = lambda p, o, a: node_record(status="running")
    assert app.poll_job(did)[1]["refreshed"] is False
    clock.advance(ha.POLL_MIRROR_REFRESH_S + 1)
    code, body = app.poll_job(did)
    assert code == 200 and body["refreshed"] is True
    assert body["job"]["observed_at"] == clock.t


def test_an_unrecognised_node_error_never_becomes_node_forgot_job(app, clock):
    """PANEL, minor: RULE 2's terminalising trigger fired on ANY ok=True
    payload carrying an `error` key, so a node reporting its OWN trouble
    ('Job records unavailable') terminalised a healthy running job as
    'the node has no record of it' -- a claim the node never made. It is
    gated on the node's actual not-found answer now."""
    job, _node = running_job(app)
    did = job["delivery_id"]
    app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"error": "Job records unavailable (project "
                                        "root not resolved yet)"}}
    for _ in range(6):
        code, body = app.poll_job(did)
        assert code == 409 and body["reason"] == "poll_no_response"
        clock.advance(60.0)
    assert app.db.get_job(did)["state"] == "running"
    assert poll_events(app, did, "poll_node_forgot_job") == []
    with app.lock:
        assert did not in app._poll_unknown, "unknown evidence accrued"
    # the node's ACTUAL not-found answer still terminalises on the grace
    app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"error": "no job with id %r" % NODE_JOB,
                               "jobs": []}}
    for _ in range(3):
        app.poll_job(did)
        clock.advance(31.0)
    assert app.db.get_job(did)["state"] == "indeterminate"
    assert app.db.get_job(did)["result"]["reason"] == "node_forgot_job"


def test_the_poll_backoff_map_is_bounded(server):
    """PANEL, minor: it was the only one of the three poll maps with no
    cap, and the drain loop -- the thing that prunes it -- is off by
    default, so a controller driving /poll by hand grew it forever."""
    with server.app.lock:
        for n in range(2048):
            server.app._poll_backoff[f"cj_fill{n:05d}"] = 9e12
        server.app._set_poll_backoff("cj_new", 9e12)
        assert len(server.app._poll_backoff) <= 2048
        assert "cj_new" in server.app._poll_backoff
        server.app._poll_backoff.clear()


def test_the_poll_maps_are_pruned_but_keep_live_entries(server):
    """The poll maps are SEPARATE from the drain maps on purpose (a
    drain prune's notion of 'live' is the queued set, and would wipe
    them). They prune against the RUNNING set, and -- like the drain
    prune -- never drop an entry whose job is still live or merely
    unreadable."""
    node = register(server.app)
    job = handoff(server.app, enqueue(server.app, node))
    did = job["delivery_id"]
    with server.app.lock:
        server.app._poll_backoff["cj_ghost"] = 9e12
        server.app._poll_noted["cj_ghost"] = ("poll_unreachable", None)
        server.app._poll_unknown["cj_ghost"] = {"count": 1, "first": 0.0}
        server.app._poll_backoff[did] = 9e12
    server.app.poll_once()
    with server.app.lock:
        assert "cj_ghost" not in server.app._poll_backoff
        assert "cj_ghost" not in server.app._poll_noted
        assert "cj_ghost" not in server.app._poll_unknown
        assert did in server.app._poll_backoff, "live entry pruned"
    # and the drain pass must not wipe the poll maps either -- its own
    # prune's notion of "live" is the QUEUED set, which never contains a
    # running job (drain_once self-locks, so it is called lock-free)
    server.app.drain_once()
    with server.app.lock:
        assert did in server.app._poll_backoff
