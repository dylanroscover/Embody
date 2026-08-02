"""Phase 4 slice 1: the host dispatches a QUEUED job to the node and
mirrors the verdict. The A-15 invariant holds end to end -- a job leaves
'queued' only by an OBSERVED node result or, on transport failure,
mark_indeterminate; the host never invents a verdict. The forwarder (the
MCP transport, A-46) is injected here so the ORCHESTRATION is tested
without a live node.
"""

import threading
import time

import pytest

import convoy_hostapp as ha
import convoy_hoststore as hs
import convoy_mcpclient as mcpclient
from test_convoy_hostapp import Server


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


CONVOY = "studio"


def register(server, envoy_port=9800, root="/Work/p", comp="/Embody"):
    code, node = server.call("/register", {
        "project_root": root, "convoy_id": CONVOY, "comp_path": comp,
        "envoy_port": envoy_port})
    assert code == 200
    return node


def enqueue(server, node, operation="query_network", key="k",
            arguments=None):
    code, body = server.call("/jobs", {
        "idempotency_key": key, "node_id": node["node_id"],
        "operation": operation, "arguments": arguments or {}})
    assert code == 200
    return body["job"]


# -- envoy_port in registration ---------------------------------------

def test_registration_carries_the_envoy_port(server):
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": 9872})
    assert code == 200 and node["envoy_port"] == 9872
    # and it is on the in-memory node record the dispatcher reads
    with server.app.lock:
        rec = server.app.directory.lookup(node["node_id"])
    assert rec["envoy_port"] == 9872


def test_envoy_port_is_optional(server):
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody"})
    assert code == 200 and node["envoy_port"] is None


@pytest.mark.parametrize("bad", [0, 70000, -1, "9800", 98.6, True])
def test_malformed_envoy_port_refused(server, bad):
    code, body = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": bad})
    assert code == 400 and body["reason"] == "malformed"


def test_a_re_register_without_port_keeps_the_known_port(server):
    node = register(server, envoy_port=9801)
    # TD re-registers (restart) but omits the port this time
    server.call("/register", {"project_root": "/Work/p", "convoy_id": CONVOY,
                              "comp_path": "/Embody"})
    with server.app.lock:
        rec = server.app.directory.lookup(node["node_id"])
    assert rec["envoy_port"] == 9801, "a known port is never cleared"


# -- dispatch: the verdict mirror --------------------------------------

def test_dispatch_mirrors_a_successful_node_result(server):
    node = register(server)
    job = enqueue(server, node, operation="query_network",
                  arguments={"parent_path": "/"})
    seen = {}

    def forwarder(port, operation, arguments):
        seen.update(port=port, operation=operation, arguments=arguments)
        return {"ok": True, "result": {"ops": ["/a", "/b"]}}

    server.app.forwarder = forwarder
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["dispatched"] is True
    updated = body["job"]
    assert updated["state"] == "succeeded"
    assert updated["verdict_source"] == "node_sync"
    assert updated["result"] == {"ops": ["/a", "/b"]}
    assert updated["node_job_id"] is None, "a sync op mints no node job id"
    # the forwarder was handed the node's port + the job's operation
    assert seen == {"port": 9800, "operation": "query_network",
                    "arguments": {"parent_path": "/"}}


def test_dispatch_mirrors_a_node_error(server):
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": False,
                                            "error": "op not found"}
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "failed"
    assert body["job"]["result"] == {"error": "op not found"}
    assert body["job"]["verdict_source"] == "node_sync"


def test_no_response_is_indeterminate_never_a_fake_verdict(server):
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: None   # sent, no response back
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "indeterminate"
    assert body["job"]["result"]["reason"] == "no_response"


def test_a_refused_node_keeps_the_job_queued_to_retry(server):
    """A briefly-down node (connection refused) did NOT run the op, so the
    job must stay QUEUED for the next attempt -- never burned to
    indeterminate."""
    import convoy_mcpclient as mcpclient
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: mcpclient.UNREACHABLE
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 409 and body["reason"] == "node_unreachable"
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "queued", "still queued to retry"
    # and a later dispatch, once the node is up, succeeds normally
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {"n": 1}}
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"


def test_a_raising_forwarder_is_indeterminate_not_a_crash(server):
    node = register(server)
    job = enqueue(server, node)

    def boom(p, o, a):
        raise RuntimeError("socket exploded")

    server.app.forwarder = boom
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "indeterminate"
    assert "RuntimeError" in body["job"]["result"]["detail"]


def test_dispatch_is_idempotent_never_reruns(server):
    node = register(server)
    job = enqueue(server, node)
    calls = {"n": 0}

    def once(p, o, a):
        calls["n"] += 1
        return {"ok": True, "result": {"n": calls["n"]}}

    server.app.forwarder = once
    first = server.call("/dispatch", {"delivery_id": job["delivery_id"]})[1]
    assert first["dispatched"] is True and first["job"]["state"] == "succeeded"
    # a second dispatch must NOT forward again -- the job is terminal
    second = server.call("/dispatch", {"delivery_id": job["delivery_id"]})[1]
    assert second["dispatched"] is False
    assert second["job"]["state"] == "succeeded"
    assert calls["n"] == 1, "a dispatched job is never re-executed"


def test_dispatch_without_a_known_port_leaves_the_job_queued(server):
    _, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody"})           # no envoy_port
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": 1}
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 409 and body["reason"] == "node_endpoint_unknown"
    # the job is untouched -- still queued, ready to dispatch once known
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "queued"


def test_dispatch_unknown_job_is_404(server):
    code, body = server.call("/dispatch", {"delivery_id": "cj_nope"})
    assert code == 404 and body["reason"] == "unknown_job"


def test_dispatch_requires_the_token(server):
    code, body = server.call("/dispatch", {"delivery_id": "cj_x"},
                             token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


def test_the_default_forwarder_is_the_mcp_client(server):
    """The host app dispatches through the real minimal MCP client by
    default; a node whose Envoy is unreachable fails SAFE to
    indeterminate (proven with an injected transport failure elsewhere)."""
    import convoy_mcpclient as mcpclient
    assert server.app.forwarder is mcpclient.forward


# -- record_sync_result unit contract ----------------------------------

def test_record_sync_result_contract(tmp_path):
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    did = job["delivery_id"]

    ok = db.record_sync_result(did, True, observed_at=1000.0,
                               result={"v": 1})
    assert ok["state"] == "succeeded" and ok["verdict_source"] == "node_sync"
    assert ok["observed_at"] == 1000.0 and ok["node_job_id"] is None

    job2, _ = db.create_job("k2", "n", "x", {}, "cv")
    bad = db.record_sync_result(job2["delivery_id"], False, observed_at=1.0,
                                result={"error": "boom"})
    assert bad["state"] == "failed"

    with pytest.raises(ValueError):
        db.record_sync_result(did, True, observed_at=None)
    with pytest.raises(KeyError):
        db.record_sync_result("cj_missing", True, observed_at=1.0)
    db.close()


# =====================================================================
# The drain-loop concurrency slice: claim CAS, lock-free forward,
# crash recovery, drain_once, and the background loop.
# =====================================================================

def test_dispatching_is_a_known_host_originable_state():
    """'dispatching' is the host's transient CLAIM -- host-originated by
    definition (no node was observed yet), never a verdict."""
    assert "dispatching" in hs.JOB_STATES
    assert "dispatching" in hs._HOST_ORIGINABLE_STATES
    # and the verdict states remain node-only
    for verdict in ("running", "succeeded", "failed"):
        assert verdict not in hs._HOST_ORIGINABLE_STATES


def test_claim_cas_only_one_winner(tmp_path):
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    did = job["delivery_id"]
    first = db.claim_for_dispatch(did)
    assert first is not None and first["state"] == "dispatching"
    assert db.claim_for_dispatch(did) is None, "second claim must lose"
    # a terminal job can never be claimed
    db.record_sync_result(did, True, observed_at=1.0, result={})
    assert db.claim_for_dispatch(did) is None
    # an unknown id claims nothing (and does not raise: the drain loop
    # snapshots ids that may finish or vanish before it reaches them)
    assert db.claim_for_dispatch("cj_missing") is None
    db.close()


def test_release_claim_returns_the_job_to_queued(tmp_path):
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    did = job["delivery_id"]
    assert db.claim_for_dispatch(did)["state"] == "dispatching"
    released = db.release_claim(did)
    assert released is not None and released["state"] == "queued"
    # release is CAS too: only a dispatching job releases
    assert db.release_claim(did) is None
    assert db.release_claim("cj_missing") is None
    # and the released job is claimable again -- the retry path
    assert db.claim_for_dispatch(did)["state"] == "dispatching"
    db.close()


def test_host_restart_sweeps_dispatching_to_indeterminate(tmp_path):
    """A job still 'dispatching' at store load was claimed by a host that
    died before recording an outcome. The forward MAY have happened, so
    the only honest state is indeterminate -- never back to queued (a
    mutation could double-run), never a fabricated verdict."""
    state_dir = str(tmp_path / "s")
    db = hs.HostStore(state_dir)
    job, _ = db.create_job("k", "n", "set_op_position", {}, "cv")
    claimed = db.claim_for_dispatch(job["delivery_id"])
    assert claimed["state"] == "dispatching"
    db.close()

    db2 = hs.HostStore(state_dir)          # the host restarts
    revived = db2.get_job(job["delivery_id"])
    assert revived["state"] == "indeterminate"
    assert revived["verdict_source"] == "host"
    assert revived["result"]["reason"] == "host_exited_mid_dispatch"
    # queued jobs are NOT touched by the sweep
    other, _ = db2.create_job("k2", "n", "query_network", {}, "cv")
    db2.close()
    db3 = hs.HostStore(state_dir)
    assert db3.get_job(other["delivery_id"])["state"] == "queued"
    db3.close()


def test_a_dispatching_job_is_skipped_by_a_second_dispatch(server):
    """The claim is what makes concurrent dispatchers safe: while one
    holds the job through a slow forward, any other dispatch call must
    see 'dispatching' and skip -- the forwarder runs exactly once."""
    node = register(server)
    job = enqueue(server, node)
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def slow(port, operation, arguments):
        calls["n"] += 1
        entered.set()
        assert release.wait(timeout=10), "test never released the forward"
        return {"ok": True, "result": {"n": calls["n"]}}

    server.app.forwarder = slow
    results = {}
    t = threading.Thread(target=lambda: results.update(
        first=server.call("/dispatch", {"delivery_id": job["delivery_id"]})))
    t.start()
    try:
        assert entered.wait(timeout=10), "first dispatch never forwarded"
        # while the forward is in flight the job is visibly claimed and a
        # second dispatch is a clean skip, not a second execution
        code, second = server.call("/dispatch",
                                   {"delivery_id": job["delivery_id"]})
        assert code == 200 and second["dispatched"] is False
        assert second["job"]["state"] == "dispatching"
    finally:
        release.set()
        t.join(timeout=10)
    assert not t.is_alive()
    code, first = results["first"]
    assert code == 200 and first["job"]["state"] == "succeeded"
    assert calls["n"] == 1, "the claim kept the second dispatcher out"


def test_the_forward_does_not_hold_the_app_lock(server):
    """The whole point of the slice: up to 30s of forward I/O must not
    freeze the host app. Proven from INSIDE the forward -- the app lock
    is acquirable, and the server answers /status over HTTP."""
    node = register(server)
    job = enqueue(server, node)
    seen = {}

    def probing(port, operation, arguments):
        got = server.app.lock.acquire(timeout=2)
        if got:
            server.app.lock.release()
        seen["lock_free"] = got
        seen["status_code"] = server.call("/status")[0]
        return {"ok": True, "result": {}}

    server.app.forwarder = probing
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert seen["lock_free"] is True, "the forward ran under the app lock"
    assert seen["status_code"] == 200, "/status blocked during a forward"


def test_an_unreachable_forward_releases_the_claim(server):
    """UNREACHABLE = the request was never delivered: the claim must be
    RELEASED so the job is queued (not dispatching, not indeterminate)
    and the next drain retries it."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: mcpclient.UNREACHABLE
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 409 and body["reason"] == "node_unreachable"
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "queued"
    # the retry actually works: claimable and dispatchable again
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {"up": 1}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"


def test_drain_once_dispatches_every_queued_job(server):
    server.app.drain_backoff_s = 0.0    # retries are the point here
    a = register(server, envoy_port=9801, comp="/A")
    _, b = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/B"})                    # no envoy_port yet
    c = register(server, envoy_port=9999, comp="/C")   # will refuse

    j1 = enqueue(server, a, key="k1")
    j2 = enqueue(server, a, key="k2")
    j3 = enqueue(server, b, key="k3")
    j4 = enqueue(server, c, key="k4")

    def forwarder(port, operation, arguments):
        if port == 9999:
            return mcpclient.UNREACHABLE
        return {"ok": True, "result": {"port": port}}

    server.app.forwarder = forwarder
    code, body = server.call("/drain", {})
    assert code == 200 and body["ok"] is True
    assert body["examined"] == 4
    assert body["dispatched"] == 2      # both of a's jobs
    assert body["deferred"] == 1        # b has no port -> stays queued
    assert body["unreachable"] == 1     # c refused -> stays queued
    for job, want in ((j1, "succeeded"), (j2, "succeeded"),
                      (j3, "queued"), (j4, "queued")):
        _, fetched = server.call(f"/jobs/{job['delivery_id']}")
        assert fetched["job"]["state"] == want
    # the next drain retries ONLY the retryables; c is up now
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    code, body = server.call("/drain", {})
    assert body["examined"] == 2
    assert body["dispatched"] == 1      # c's job succeeded on retry
    assert body["deferred"] == 1        # b is still portless
    _, fetched = server.call(f"/jobs/{j4['delivery_id']}")
    assert fetched["job"]["state"] == "succeeded"


def test_drain_requires_the_token(server):
    code, body = server.call("/drain", {}, token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


def test_the_background_drain_loop_dispatches_automatically(server):
    """The autonomous slice end to end: a queued job is dispatched with
    NO manual /dispatch call, by the opt-in background loop."""
    node = register(server)
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {"auto": 1}}
    assert server.app.start_drain_loop(interval_s=0.05) is True
    try:
        job = enqueue(server, node)
        deadline = time.time() + 10
        state = None
        while time.time() < deadline:
            _, fetched = server.call(f"/jobs/{job['delivery_id']}")
            state = fetched["job"]["state"]
            if state == "succeeded":
                break
            time.sleep(0.05)
        assert state == "succeeded", f"never auto-dispatched (state={state})"
    finally:
        # Capture the thread BEFORE stopping: stop_drain_loop clears the
        # handle on success, so asserting on the attribute afterwards
        # proved nothing (panel-caught: the assert could never fail).
        thread = server.app._drain_thread
        stopped = server.app.stop_drain_loop()
        assert stopped is True, "stop_drain_loop timed out on a fast loop"
        assert thread is not None and not thread.is_alive(), \
            "loop outlived a stop that reported success"
        # starting is refused while alive, restartable after a real stop
        assert server.app.start_drain_loop(interval_s=60) is True
        assert server.app.start_drain_loop(interval_s=60) is False
        assert server.app.stop_drain_loop() is True


def test_stop_drain_loop_reports_a_loop_it_could_not_stop(server):
    """A stop bounded shorter than the in-flight forward must say so --
    keep the handles, keep status() truthful, refuse a second loop --
    never clear state it did not actually retire (panel-caught: the old
    version lied, and the lie enabled TWO concurrent loops)."""
    node = register(server)
    server.app.drain_backoff_s = 0.0
    entered, release = threading.Event(), threading.Event()

    def slow(port, operation, arguments):
        entered.set()
        assert release.wait(timeout=30), "test never released the forward"
        return {"ok": True, "result": {}}

    server.app.forwarder = slow
    assert server.app.start_drain_loop(interval_s=0.05) is True
    job = enqueue(server, node)
    try:
        assert entered.wait(timeout=10), "loop never picked up the job"
        # Mid-forward, a too-short stop times out and says so.
        assert server.app.stop_drain_loop(timeout_s=0.2) is False
        # status() is SELF-LOCKING since the /status starvation fix;
        # holding app.lock around it deadlocks (threading.Lock is not
        # reentrant) -- the old wrapper here hung the ENTIRE suite.
        status = server.app.status()
        assert status["drain_loop"] is True, "status lied about the loop"
        assert server.app.start_drain_loop(interval_s=60) is False, \
            "a second loop started over the unstopped first"
    finally:
        release.set()
    assert server.app.stop_drain_loop(timeout_s=10) is True
    assert server.app.status()["drain_loop"] is False
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "succeeded"


def test_a_failed_verdict_write_downgrades_to_indeterminate(server,
                                                            monkeypatch):
    """Phase c failing must NOT strand the claim: the job downgrades to
    indeterminate with the recording failure as evidence, and the caller
    gets a named 500 (panel-caught: any phase-c raise stranded the job
    in 'dispatching' until a host restart)."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {"v": 1}}

    def explode(*args, **kwargs):
        raise OSError("disk says no")

    monkeypatch.setattr(server.app.db, "record_sync_result", explode)
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 500 and body["reason"] == "recording_failed"
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "indeterminate"
    assert fetched["job"]["result"]["reason"] == "verdict_recording_failed"


def test_drain_reaps_a_stranded_claim(server):
    """A 'dispatching' job with no forward in flight in this process is
    debris (a failed recording, a killed thread) -- the drain pass must
    resolve it to indeterminate instead of leaving it invisible to every
    route until a restart."""
    node = register(server)
    job = enqueue(server, node)
    with server.app.lock:
        assert server.app.db.claim_for_dispatch(job["delivery_id"])
    code, body = server.call("/drain", {})
    assert code == 200 and body["stranded"] == 1
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "indeterminate"
    assert fetched["job"]["result"]["reason"] == "claim_stranded"


@pytest.mark.parametrize("garbage", [
    {}, {"result": {"x": 1}}, {"error": "gw timeout"}, {"ok": None},
    {"ok": "yes"}, ["nope"], "nope", 5,
])
def test_a_forwarder_return_without_a_verdict_is_indeterminate(server,
                                                               garbage):
    """A-15: only a real boolean 'ok' is a node verdict. A dict without
    one must resolve indeterminate -- recording it as failed would
    fabricate a verdict the node never produced (panel-caught: {} became
    'failed' with node provenance)."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: garbage
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "indeterminate"
    assert body["job"]["verdict_source"] == "host"
    assert "ok" in body["job"]["result"]["detail"]


def test_an_unserializable_node_result_keeps_its_verdict(server):
    """A real node verdict whose payload cannot ride JSON keeps the
    VERDICT and sanitizes the payload (panel-caught: json.dumps raised
    in phase c and the claim stranded)."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": True,
                                            "result": {"pixels": {1, 2}}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert "sanitized" in body["job"]["result"]["detail"]


@pytest.mark.parametrize("bad", [5, 98.6, True, ["cj_x"], {"a": 1}, None])
def test_a_malformed_delivery_id_is_a_named_400(server, bad):
    """A type-confused /dispatch body is an audited 400, never a
    TypeError-500 the audit trail cannot classify (A-39)."""
    code, body = server.call("/dispatch", {"delivery_id": bad})
    assert code == 400 and body["reason"] == "malformed"


def test_dispatch_rechecks_the_runtime_at_execution_time(server):
    """A-22 must hold when the job RUNS, not just when it was accepted:
    the queue spans node restarts, which is exactly when the runtime
    changes (panel-caught: a mutating job deferred across a restart
    dispatched into the new runtime unchecked)."""
    server.app.operations["query_network"]["runtime_required"] = True
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/RT", "envoy_port": 9800, "runtime_id": "rt1"})
    assert code == 200
    code, body = server.call("/jobs", {
        "idempotency_key": "krt", "node_id": node["node_id"],
        "operation": "query_network", "arguments": {},
        "expected_runtime_id": "rt1"})
    assert code == 200
    job = body["job"]
    assert job["expected_runtime_id"] == "rt1"
    # TD restarts: same identity, NEW runtime
    server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/RT", "envoy_port": 9800, "runtime_id": "rt2"})
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 409 and body["reason"] == "runtime_changed"
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "queued", "never forwarded"


def test_the_drain_loop_backs_off_a_refused_job(server):
    """A refused/deferred job is not re-attempted every tick: the drain
    skips it for drain_backoff_s (panel-caught: zero backoff re-forwarded
    and re-audited stuck jobs on every pass, unbounded). A manual
    /dispatch stays exempt -- an explicit call is its own authority."""
    node = register(server)
    job = enqueue(server, node)
    calls = {"n": 0}

    def refusing(port, operation, arguments):
        calls["n"] += 1
        return mcpclient.UNREACHABLE

    server.app.forwarder = refusing
    server.app.drain_backoff_s = 3600.0
    code, body = server.call("/drain", {})
    assert body["unreachable"] == 1 and calls["n"] == 1
    # the very next pass skips it -- no forward, no audit churn
    code, body = server.call("/drain", {})
    assert body["backoff"] == 1 and calls["n"] == 1
    # but an explicit dispatch still tries immediately
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 409 and calls["n"] == 2


def test_a_stale_attempt_cannot_clear_a_live_flight_marker(server):
    """Round-2 panel: _in_flight must be ATTEMPT-scoped. A finished
    attempt's cleanup runs after a release back to queued -- by then a
    second attempt may own the job, and a job-scoped discard erased the
    live marker, letting the reaper burn an undelivered job to
    indeterminate."""
    node = register(server)
    job = enqueue(server, node)
    did = job["delivery_id"]
    with server.app.lock:
        assert server.app.db.claim_for_dispatch(did) is not None
        server.app._flight_counter += 1
        live_attempt = server.app._flight_counter
        server.app._in_flight[did] = live_attempt
    # A stale attempt (older token) finishing must NOT remove the marker
    with server.app.lock:
        if server.app._in_flight.get(did) == live_attempt - 1:
            del server.app._in_flight[did]
    assert server.app._in_flight.get(did) == live_attempt
    # and the reaper must leave the in-flight claim alone
    summary = server.app.drain_once()
    assert summary["stranded"] == 0
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "dispatching"
    # cleanup: the owning attempt's discard works, then the reaper may act
    with server.app.lock:
        if server.app._in_flight.get(did) == live_attempt:
            del server.app._in_flight[did]
    summary = server.app.drain_once()
    assert summary["stranded"] == 1


def test_a_failing_claim_audit_does_not_divert_the_dispatch(server,
                                                            monkeypatch):
    """Round-2 panel: the dispatch_claimed audit sat between the claim
    and the try/finally -- an audit raise leaked the in-flight marker
    forever and blinded the reaper. An audit failure must never alter
    dispatch state."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {"v": 1}}
    real_audit = server.app.db.audit

    def flaky_audit(actor, event, detail=None):
        if event == "dispatch_claimed":
            raise OSError("audit disk full")
        return real_audit(actor, event, detail)

    monkeypatch.setattr(server.app.db, "audit", flaky_audit)
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert server.app._in_flight == {}, "marker leaked past the dispatch"


def test_a_failing_claim_write_is_a_named_503(server, monkeypatch):
    """Round-2 panel: a store failure in phase a escaped as an unaudited
    TypeError-class 500. It must be a named refusal that leaves the job
    queued (os.replace is atomic -- the claim never landed)."""
    node = register(server)
    job = enqueue(server, node)

    def explode(delivery_id):
        raise PermissionError("sharing violation outlived retries")

    monkeypatch.setattr(server.app.db, "claim_for_dispatch", explode)
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 503 and body["reason"] == "store_unavailable"
    monkeypatch.undo()
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "queued"


def test_a_changed_refusal_reason_is_audited_as_a_transition(server):
    """Round-2 panel: deduping on the event name alone swallowed a
    reason CHANGE -- the trail asserted a stale cause (A-40)."""
    _, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/TR"})               # portless -> deferred
    job = enqueue(server, node)
    did = job["delivery_id"]
    server.call("/dispatch", {"delivery_id": did})   # dispatch_deferred
    server.call("/dispatch", {"delivery_id": did})   # deduped
    # the operation vanishes from the registry: a DIFFERENT refusal
    with server.app.lock:
        removed = server.app.operations.pop("query_network")
    try:
        server.call("/dispatch", {"delivery_id": did})
    finally:
        with server.app.lock:
            server.app.operations["query_network"] = removed
    with server.app.lock:
        events = [e["event"] for e in server.app.db.audit_tail(limit=200)
                  if (e["detail"] or {}).get("delivery_id") == did
                  and e["event"] in ("dispatch_deferred",
                                     "dispatch_refused")]
    assert events == ["dispatch_deferred", "dispatch_refused"], events


def test_the_prune_keeps_entries_for_live_jobs(server):
    """Round-2 panel: pruning against the pass's start snapshot dropped
    backoff/dedupe entries set mid-pass for live jobs -- defeating the
    backoff and re-auditing the refusal."""
    node = register(server)
    job = enqueue(server, node)
    did = job["delivery_id"]
    # a live claimed job (protected from the reaper by its marker) with
    # bookkeeping entries -- exactly what a mid-pass manual dispatch makes
    with server.app.lock:
        assert server.app.db.claim_for_dispatch(did) is not None
        server.app._flight_counter += 1
        server.app._in_flight[did] = server.app._flight_counter
        server.app._drain_backoff[did] = 9e12
        server.app._drain_noted[did] = ("dispatch_unreachable", None)
    try:
        server.app.drain_once()      # snapshot does not contain did queued
        with server.app.lock:
            assert did in server.app._drain_backoff, "live entry pruned"
            assert did in server.app._drain_noted
    finally:
        with server.app.lock:
            server.app._in_flight.pop(did, None)
            server.app._drain_backoff.pop(did, None)
            server.app._drain_noted.pop(did, None)


def test_ghost_delivery_ids_leave_no_bookkeeping_entries(server):
    """Round-2 panel: unknown ids inserted dedupe-map entries that only
    the drain pass prunes -- and the loop is off by default, so ghosts
    accumulated forever. They are audited plainly instead."""
    for n in range(3):
        code, _ = server.call("/dispatch", {"delivery_id": f"cj_ghost{n}"})
        assert code == 404
    with server.app.lock:
        assert not any(k.startswith("cj_ghost")
                       for k in server.app._drain_noted)
        ghosts = [e for e in server.app.db.audit_tail(limit=50)
                  if e["event"] == "dispatch_refused"
                  and str((e["detail"] or {}).get("delivery_id", ""))
                  .startswith("cj_ghost")]
    assert len(ghosts) == 3, "each ghost dispatch is audited"


def test_a_result_failing_only_the_stores_dumps_options_is_sanitized(
        server):
    """Round-2 panel: _json_safe validated laxer than the store writes
    (no sort_keys), so mixed-type dict keys passed the guard and blew up
    the store write, downgrading a REAL verdict to indeterminate."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"rows": {1: "a", "b": 2}}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    assert "sanitized" in body["job"]["result"]["detail"]


def test_a_failed_requeue_write_never_becomes_indeterminate(server,
                                                            monkeypatch):
    """Round-3 BLOCKER: release_claim raising inside the UNREACHABLE
    branch funneled into the downgrade path and burned a NEVER-DELIVERED
    job to indeterminate. It must park the release (503), keep the claim
    protected from the reaper, and the next drain pass must requeue it."""
    node = register(server)
    job = enqueue(server, node)
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: mcpclient.UNREACHABLE
    real_release = server.app.db.release_claim
    calls = {"n": 0}

    def failing_release(delivery_id):
        calls["n"] += 1
        raise PermissionError("sharing violation outlived retries")

    monkeypatch.setattr(server.app.db, "release_claim", failing_release)
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 503 and body["reason"] == "store_unavailable"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "dispatching", "claim parked"
    # the reaper must NOT resolve the parked claim
    monkeypatch.setattr(server.app.db, "release_claim",
                        lambda d: (_ for _ in ()).throw(
                            PermissionError("still failing")))
    summary = server.app.drain_once()
    assert summary["stranded"] == 0 and summary["requeued"] == 0
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "dispatching"
    # disk heals: the next pass requeues it and the job can retry
    monkeypatch.setattr(server.app.db, "release_claim", real_release)
    summary = server.app.drain_once()
    assert summary["requeued"] == 1
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "queued", "recovered to retry"
    with server.app.lock:
        assert did not in server.app._pending_release
        assert did not in server.app._in_flight


def test_an_audit_failure_after_a_verdict_never_destroys_it(server,
                                                            monkeypatch):
    """Round-3 panel: the post-write 'dispatched' audit raising funneled
    a durably-recorded NODE verdict into mark_indeterminate -- the host
    destroyed a real verdict and originated its own (A-15). Audits must
    never alter dispatch state."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": True,
                                            "result": {"nodes": ["/geo1"]}}
    real_audit = server.app.db.audit

    def flaky(actor, event, detail=None):
        if event == "dispatched":
            raise OSError(28, "No space left on device")
        return real_audit(actor, event, detail)

    monkeypatch.setattr(server.app.db, "audit", flaky)
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    monkeypatch.undo()
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "succeeded"
    assert fetched["job"]["verdict_source"] == "node_sync"
    assert fetched["job"]["result"] == {"nodes": ["/geo1"]}


def test_an_audit_failure_after_a_release_keeps_the_job_queued(server,
                                                               monkeypatch):
    """Round-3 panel: an audit raise AFTER a successful release flipped
    the already-queued job to indeterminate. The refusal must come back
    normally and the job must stay queued."""
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: mcpclient.UNREACHABLE
    real_audit = server.app.db.audit

    def flaky(actor, event, detail=None):
        if event == "dispatch_unreachable":
            raise OSError("audit locked")
        return real_audit(actor, event, detail)

    monkeypatch.setattr(server.app.db, "audit", flaky)
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 409 and body["reason"] == "node_unreachable"
    monkeypatch.undo()
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "queued"


def test_the_downgrade_path_refuses_to_clobber_a_resolved_record(server):
    """Round-3 panel: _downgrade_failed_recording marked indeterminate
    unconditionally. It may only touch a job still 'dispatching'."""
    node = register(server)
    job = enqueue(server, node)
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {"v": 1}}
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert body["job"]["state"] == "succeeded"
    code, payload = server.app._downgrade_failed_recording(
        did, "query_network", RuntimeError("late bookkeeping failure"))
    assert code == 200
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "succeeded", "verdict destroyed"
    assert fetched["job"]["result"] == {"v": 1}


def test_a_phase_a_audit_failure_is_still_a_named_refusal(server,
                                                          monkeypatch):
    """Round-3 panel: an audit raise on a phase-a refusal escaped as an
    unnamed 500 AND poisoned the dedupe so the next attempt's audit was
    swallowed. The refusal must come back named, and the line must be
    written by the next healthy attempt."""
    _, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/AF"})               # portless -> deferred
    job = enqueue(server, node)
    did = job["delivery_id"]
    real_audit = server.app.db.audit

    def flaky(actor, event, detail=None):
        if event == "dispatch_deferred":
            raise OSError("audit locked")
        return real_audit(actor, event, detail)

    monkeypatch.setattr(server.app.db, "audit", flaky)
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 409 and body["reason"] == "node_endpoint_unknown"
    monkeypatch.undo()
    server.call("/dispatch", {"delivery_id": did})
    with server.app.lock:
        events = [e for e in server.app.db.audit_tail(limit=100)
                  if e["event"] == "dispatch_deferred"
                  and (e["detail"] or {}).get("delivery_id") == did]
    assert len(events) == 1, "the healthy retry must write the line"


def test_the_prune_keeps_entries_for_unreadable_job_files(server,
                                                          monkeypatch):
    """Round-3 panel: get_job returns None for absent AND unreadable
    alike, so a transient sharing violation pruned a live job's
    bookkeeping. Unreadable-but-present must be kept."""
    node = register(server)
    job = enqueue(server, node)
    did = job["delivery_id"]
    with server.app.lock:
        server.app._drain_backoff[did] = 9e12
    real_get = server.app.db.get_job

    def blind(delivery_id):
        if delivery_id == did:
            return None            # transiently unreadable
        return real_get(delivery_id)

    monkeypatch.setattr(server.app.db, "get_job", blind)
    try:
        server.app.drain_once()
        with server.app.lock:
            assert did in server.app._drain_backoff, \
                "unreadable pruned as absent"
    finally:
        monkeypatch.undo()
        with server.app.lock:
            server.app._drain_backoff.pop(did, None)


def test_distinct_runtime_mismatches_each_audit(server):
    """Round-3 panel: the (event, reason) dedupe had no discriminator
    for runtime_changed, so successive DIFFERENT mismatches collapsed
    into one audit line."""
    server.app.operations["query_network"]["runtime_required"] = True
    try:
        code, node = server.call("/register", {
            "project_root": "/Work/p", "convoy_id": CONVOY,
            "comp_path": "/RTX", "envoy_port": 9800, "runtime_id": "rt1"})
        code, body = server.call("/jobs", {
            "idempotency_key": "krtx", "node_id": node["node_id"],
            "operation": "query_network", "arguments": {},
            "expected_runtime_id": "rt1"})
        did = body["job"]["delivery_id"]
        for new_rt in ("rt2", "rt3"):
            server.call("/register", {
                "project_root": "/Work/p", "convoy_id": CONVOY,
                "comp_path": "/RTX", "envoy_port": 9800,
                "runtime_id": new_rt})
            code, body = server.call("/dispatch", {"delivery_id": did})
            assert code == 409 and body["reason"] == "runtime_changed"
        with server.app.lock:
            events = [e for e in server.app.db.audit_tail(limit=100)
                      if e["event"] == "dispatch_runtime_changed"
                      and (e["detail"] or {}).get("delivery_id") == did]
        assert len(events) == 2, [e["detail"] for e in events]
    finally:
        server.app.operations["query_network"]["runtime_required"] = False


def test_the_noted_map_is_bounded(server):
    """Round-3 panel: on a loop-off host nothing prunes the dedupe map;
    it must be bounded."""
    with server.app.lock:
        for n in range(2048):
            server.app._drain_noted[f"cj_fill{n:05d}"] = ("x", None)
    node = register(server, comp="/BND")
    job = enqueue(server, node, key="kbnd")
    with server.app.lock:
        server.app._note_dispatch_event(job["delivery_id"], "dispatch_x",
                                        {"reason": "bound-test"})
        assert len(server.app._drain_noted) <= 2048
        server.app._drain_noted = {
            k: v for k, v in server.app._drain_noted.items()
            if not k.startswith("cj_fill")}


def test_an_unknown_job_refusal_survives_an_audit_failure(server,
                                                          monkeypatch):
    """Round-4 panel: the unknown_job audit was the last unwrapped one
    in phase a -- an append failure turned the named 404 into an
    unclassifiable 500 (A-39)."""
    def dead_audit(actor, event, detail=None):
        raise PermissionError("audit locked")

    monkeypatch.setattr(server.app.db, "audit", dead_audit)
    code, body = server.call("/dispatch", {"delivery_id": "cj_nosuchjob"})
    assert code == 404 and body["reason"] == "unknown_job"
    code, body = server.call("/dispatch", {"delivery_id": 12345})
    assert code == 400 and body["reason"] == "malformed"


def test_an_unreadable_release_cas_parks_instead_of_claim_lost(server,
                                                               monkeypatch):
    """Round-4 panel: get_job swallows read errors into None, so a
    transiently unreadable job file made release_claim return None and
    the claim_lost path handed a NEVER-DELIVERED job to the reaper.
    Unreadable-but-present must park, exactly like a failed write."""
    node = register(server)
    job = enqueue(server, node)
    did = job["delivery_id"]
    real_release = server.app.db.release_claim
    real_get = server.app.db.get_job

    def forwarder(port, operation, arguments):
        # Install the blindness AFTER phase a claimed: from here the
        # store cannot read this job (a sharing violation), though the
        # file provably exists.
        monkeypatch.setattr(server.app.db, "release_claim",
                            lambda d: None if d == did else real_release(d))
        monkeypatch.setattr(server.app.db, "get_job",
                            lambda d: None if d == did else real_get(d))
        return mcpclient.UNREACHABLE

    server.app.forwarder = forwarder
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 503 and body["reason"] == "store_unavailable"
    monkeypatch.undo()
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "dispatching", "claim parked"
    # the disk heals AND the node comes back: the drain pass requeues
    # (never reaps) and then dispatches the recovered job in the same
    # pass -- the full recovery, end to end
    server.app.drain_backoff_s = 0.0
    server.app.forwarder = lambda p, o, a: {"ok": True,
                                            "result": {"healed": 1}}
    summary = server.app.drain_once()
    assert summary["requeued"] == 1 and summary["stranded"] == 0
    assert summary["dispatched"] == 1
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "succeeded"


def test_one_bad_job_cannot_wedge_a_drain_pass(server, monkeypatch):
    """Round-4 panel: an unguarded dispatch_job raise aborted the whole
    pass, and the same leading job wedged every subsequent pass."""
    node = register(server)
    bad = enqueue(server, node, key="kbad")
    good = enqueue(server, node, key="kgood")
    server.app.drain_backoff_s = 0.0
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    real_dispatch = server.app.dispatch_job

    def flaky(delivery_id):
        if delivery_id == bad["delivery_id"]:
            raise RuntimeError("wedge attempt")
        return real_dispatch(delivery_id)

    monkeypatch.setattr(server.app, "dispatch_job", flaky)
    summary = server.app.drain_once()
    assert summary["errors"] == 1
    assert summary["dispatched"] == 1, "the healthy job must not starve"
    _, fetched = server.call(f"/jobs/{good['delivery_id']}")
    assert fetched["job"]["state"] == "succeeded"


def test_a_landed_reap_is_counted_even_if_its_audit_fails(server,
                                                          monkeypatch):
    """Round-4 panel: the stranded counter incremented after the audit,
    so a failing append made a REAL reap invisible in the summary."""
    node = register(server)
    job = enqueue(server, node)
    with server.app.lock:
        assert server.app.db.claim_for_dispatch(job["delivery_id"])
    real_audit = server.app.db.audit

    def flaky(actor, event, detail=None):
        if event == "stranded_claim_reaped":
            raise OSError("audit locked")
        return real_audit(actor, event, detail)

    monkeypatch.setattr(server.app.db, "audit", flaky)
    summary = server.app.drain_once()
    assert summary["stranded"] == 1, "a landed reap must be counted"
    monkeypatch.undo()
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "indeterminate"


# =====================================================================
# The ASYNC HANDOFF: an operation that mints a node-side job returns a
# HANDLE, not a result. Dispatch records 'running' with the node's
# provenance and hands the job to the poller -- it never blocks a drain
# pass on a test run, and it never fabricates the outcome of one.
# =====================================================================

def register_rt(server, envoy_port=9800, comp="/RT", runtime_id="rt1"):
    """A node with a runtime -- the async operations are
    runtime_required, so a job for one must name the run it addressed."""
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY, "comp_path": comp,
        "envoy_port": envoy_port, "runtime_id": runtime_id})
    assert code == 200
    return node


def enqueue_async(server, node, operation="run_tests", key="ka",
                  arguments=None):
    code, body = server.call("/jobs", {
        "idempotency_key": key, "node_id": node["node_id"],
        "operation": operation, "arguments": arguments or {},
        "expected_runtime_id": node["runtime_id"]})
    assert code == 200, body
    return body["job"]


def started_handle(job_id="job_1a2b3c4d"):
    """What _startTestsJob returns on a successful start."""
    return {"ok": True, "result": {"job_id": job_id, "status": "running",
                                   "hint": "Poll get_job_status(...)"}}


def test_an_async_op_is_recorded_running_with_node_provenance(server):
    """B6, the headline: the node answered with a job HANDLE, so the host
    mirrors 'running' carrying the node's own job id -- the state that
    tells the poller this delivery is owned by a live node job."""
    node = register_rt(server)
    job = enqueue_async(server, node)
    server.app.forwarder = lambda p, o, a: started_handle()
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["dispatched"] is True
    assert body["started"] is True
    updated = body["job"]
    assert updated["state"] == "running"
    assert updated["node_job_id"] == "job_1a2b3c4d"
    assert updated["verdict_source"] == "node_poll"
    assert updated["observed_at"] is not None
    assert updated["result"]["status"] == "running"


def test_a_running_handoff_leaves_dispatching_before_any_reaper_pass(server):
    """The reaper resolves 'dispatching' claims with no forward in flight.
    An async handoff must therefore LEAVE dispatching inside phase c --
    if it parked at 'dispatching' waiting for a poll, the very next drain
    pass would burn a healthy node job to indeterminate."""
    node = register_rt(server)
    job = enqueue_async(server, node)
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: started_handle()
    server.call("/dispatch", {"delivery_id": did})
    with server.app.lock:
        assert did not in server.app._in_flight, "flight marker leaked"
    summary = server.app.drain_once()
    assert summary["stranded"] == 0, "the reaper touched a running node job"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "running"


def test_the_delivery_idempotency_key_rides_to_the_node(server):
    """B7: the node's own 16.5 index is what makes a retry recover a lost
    handle, and it is keyed by the idempotency_key WE send. Sending the
    delivery's key (not the caller's, not a fresh one) is what anchors
    every redelivery of this job to the same node run."""
    node = register_rt(server)
    job = enqueue_async(server, node, key="the-run-key")
    seen = {}

    def forwarder(port, operation, arguments):
        seen.update(operation=operation, arguments=dict(arguments))
        return started_handle()

    server.app.forwarder = forwarder
    server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert seen["operation"] == "run_tests"
    assert seen["arguments"]["idempotency_key"] == "the-run-key"
    assert seen["arguments"]["background"] is True
    # and the merged arguments are NOT persisted onto the job record
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["arguments"] == {}


def test_injected_arguments_override_a_hostile_caller(server):
    """B8: host policy WINS over caller-supplied arguments. background
    False would block the forward for the whole run (a fabricated
    indeterminate), a caller idempotency_key would break the 16.5 anchor,
    and override True would bypass the node's own multi-session
    destructive gate."""
    node = register_rt(server)
    job = enqueue_async(server, node, key="honest-key", arguments={
        "background": False, "override": True,
        "idempotency_key": "attacker-key", "suite_name": "test_sandbox"})
    seen = {}
    server.app.forwarder = lambda p, o, a: (seen.update(a) or
                                            started_handle())
    server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert seen["background"] is True
    assert seen["override"] is False
    assert seen["idempotency_key"] == "honest-key"
    # a benign caller argument still rides through untouched
    assert seen["suite_name"] == "test_sandbox"


def test_save_project_gets_only_its_idempotency_key(server):
    """B9: save_project's MCP signature accepts idempotency_key ONLY, so
    anything else on the wire is a validation error at the node -- a
    failure the host would have invented.

    Enqueued WITH hostile and junk arguments on purpose: an empty
    `inject` overrides nothing, so this passed for years-in-miniature on
    an empty-arguments job while the property its name asserts was false
    (panel finding, 2026-08-01). The empty `caller_args` is what
    actually delivers it."""
    node = register_rt(server)
    job = enqueue_async(server, node, operation="save_project", key="ks",
                        arguments={"background": True, "override": True,
                                   "junk": "surprise"})
    seen = {}
    server.app.forwarder = lambda p, o, a: (seen.update(args=dict(a)) or
                                            started_handle("job_00ff00ff"))
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "running"
    assert seen["args"] == {"idempotency_key": "ks"}, seen["args"]


def test_run_tests_keeps_its_own_arguments_and_drops_the_rest(server):
    """The other half of the same contract: a field the node's tool
    signature accepts rides through, one it does not is DROPPED rather
    than forwarded for the node to reject -- and the drop is audited, so
    a caller can see why its argument vanished."""
    node = register_rt(server)
    job = enqueue_async(server, node, key="kt", arguments={
        "suite_name": "test_sandbox", "junk": 1, "override": True})
    did = job["delivery_id"]
    seen = {}
    server.app.forwarder = lambda p, o, a: (seen.update(args=dict(a)) or
                                            started_handle())
    assert server.call("/dispatch", {"delivery_id": did})[0] == 200
    assert seen["args"] == {"suite_name": "test_sandbox",
                            "background": True, "override": False,
                            "idempotency_key": "kt"}, seen["args"]
    with server.app.lock:
        claimed = [e for e in server.app.db.audit_tail(limit=300)
                   if e["event"] == "dispatch_claimed"
                   and (e["detail"] or {}).get("delivery_id") == did]
    assert claimed[0]["detail"]["dropped"] == ["junk", "override"]


def test_a_sync_op_gets_no_injected_arguments(server):
    """B10: injection is per-operation, from the registry entry. A plain
    relayed read must reach the node byte-identical to what was enqueued."""
    node = register(server)
    job = enqueue(server, node, operation="query_network",
                  arguments={"parent_path": "/"})
    seen = {}
    server.app.forwarder = lambda p, o, a: (seen.update(args=dict(a)) or
                                            {"ok": True, "result": {}})
    server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert seen["args"] == {"parent_path": "/"}


@pytest.mark.parametrize("status,state", [("done", "succeeded"),
                                          ("error", "failed")])
def test_an_async_handle_that_is_already_terminal_is_recorded_terminal(
        server, status, state):
    """B11: an idempotency reconcile can hand back a FINISHED run, and a
    mint-then-fail-to-start answers 'error' with its handle. Both are
    node verdicts with provenance -- terminal immediately, no poll."""
    node = register_rt(server)
    job = enqueue_async(server, node, key=f"k-{status}")
    server.app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"job_id": "job_abcdef01", "status": status,
                               "error": "Test run failed to start: boom"}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == state
    assert body["started"] is False
    assert body["job"]["verdict_source"] == "node_poll"
    assert body["job"]["node_job_id"] == "job_abcdef01"


def test_an_async_answer_without_a_handle_requeues_never_terminalises(server):
    """B12: the node refused before minting ({'error': ...}, NO job_id) --
    but it may also have started work whose id we lost (the 30s
    _execute_in_td timeout). Neither 'failed' nor 'indeterminate' is
    honest: the job goes back to QUEUED, and the redelivery carries the
    same idempotency_key so the node's 16.5 index hands back the original
    handle."""
    node = register_rt(server)
    job = enqueue_async(server, node)
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"error": "A test run is already in progress"}}
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 409 and body["reason"] == "no_node_job_handle"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "queued", "never burned"
    assert fetched["job"]["verdict_source"] is None
    # audited once per transition, and paced
    server.call("/dispatch", {"delivery_id": did})
    with server.app.lock:
        events = [e for e in server.app.db.audit_tail(limit=200)
                  if e["event"] == "dispatch_no_handle"
                  and (e["detail"] or {}).get("delivery_id") == did]
        assert did in server.app._drain_backoff
    assert len(events) == 1, events


def test_a_requeued_async_delivery_recovers_the_lost_handle(server):
    """B13: the recovery the requeue is BETTING on. The retry sends the
    same idempotency_key, the node reconciles to the run it already
    started, and the host adopts that handle -- the lost id comes back."""
    node = register_rt(server)
    job = enqueue_async(server, node, key="recover-me")
    did = job["delivery_id"]
    attempts = {"n": 0}

    def forwarder(port, operation, arguments):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # the answer whose handle we lost
            return {"ok": True, "result": {"error": "Operation timed out "
                                                    "after 30 seconds"}}
        assert arguments["idempotency_key"] == "recover-me"
        return {"ok": True, "result": {
            "job_id": "job_beadfeed", "status": "running",
            "hint": "Reconciled to the original run for this "
                    "idempotency_key (calls are idempotent)."}}

    server.app.forwarder = forwarder
    assert server.call("/dispatch", {"delivery_id": did})[0] == 409
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 200 and body["job"]["state"] == "running"
    assert body["job"]["node_job_id"] == "job_beadfeed"
    assert attempts["n"] == 2


def test_an_async_op_with_no_response_is_still_indeterminate(server):
    """B14: the async branch changes what a HANDLE means, not what
    silence means. A forward that got no response may have started the
    run, and that stays indeterminate."""
    node = register_rt(server)
    job = enqueue_async(server, node)
    server.app.forwarder = lambda p, o, a: None
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "indeterminate"
    assert body["job"]["result"]["reason"] == "no_response"


def test_the_drain_summary_counts_started_and_no_handle(server):
    """B15: the pass summary must name both new outcomes. A handoff is
    not a completed dispatch and a handle-less answer is not an error --
    burying either in 'dispatched' would make the summary lie."""
    server.app.drain_backoff_s = 0.0
    node = register_rt(server)
    good = enqueue_async(server, node, key="k-good")
    lost = enqueue_async(server, node, key="k-lost")

    def forwarder(port, operation, arguments):
        if arguments["idempotency_key"] == "k-good":
            return started_handle()
        return {"ok": True, "result": {"error": "Job records unavailable"}}

    server.app.forwarder = forwarder
    code, body = server.call("/drain", {})
    assert code == 200
    assert body["examined"] == 2
    assert body["started"] == 1
    assert body["no_handle"] == 1
    assert body["dispatched"] == 1, "a handoff is counted as dispatched too"
    assert body["errors"] == 0
    _, fetched = server.call(f"/jobs/{good['delivery_id']}")
    assert fetched["job"]["state"] == "running"
    _, fetched = server.call(f"/jobs/{lost['delivery_id']}")
    assert fetched["job"]["state"] == "queued"


def test_the_async_registry_entries_are_honestly_gated(server):
    """The two entries are MUTATING and runtime_required: a run_tests job
    is refused without the A-22 precondition, exactly like any other
    stale-state-sensitive operation.

    They are also the two operations that are NOT remotely exposed --
    run_tests because TestRunnerExt execs every test file it finds on
    disk (so "the project's own code" is a loopback assumption), and
    save_project because it blocks TD's main thread for 15+ seconds
    while show protection is still Phase 4 work. Both stay fully
    available LOCALLY; only a future remote peer is refused.
    """
    node = register_rt(server)
    code, body = server.call("/jobs", {
        "idempotency_key": "no-rt", "node_id": node["node_id"],
        "operation": "run_tests", "arguments": {}})
    assert code == 400 and body["reason"] == "runtime_id_required"
    for name in ("run_tests", "save_project"):
        entry = ha.PHASE1_OPERATIONS[name]
        assert ha.gating_of(entry) == {"executes_arbitrary_code": False,
                                       "mutating": True,
                                       "runtime_required": True,
                                       "remote_exposed": False}
        assert entry["async_job"]["key_arg"] == "idempotency_key"


# =====================================================================
# Panel regressions (2026-08-01, four-lens review of the polling slice)
#
# The BLOCKER: `arguments` was type-checked nowhere, and the async merge
# `dict(arguments)` sat between the durable claim and the try/finally
# that clears the in-flight marker. A list/str/int reached it, raised,
# and left the delivery claimed on disk with a phantom marker -- so the
# stranded-claim reaper skipped it FOREVER, /dispatch skipped it (not
# queued), /poll skipped it (not running), and a host restart finally
# wrote a FALSE indeterminate for an operation that never left phase a.
# =====================================================================

@pytest.mark.parametrize("bad", [["a", "b"], "suite=x", 5, True, 98.6,
                                 [], "", 0])
def test_non_dict_arguments_are_refused_at_enqueue(server, bad):
    """Layer 1: `arguments` becomes the node's tool kwargs, so a non-dict
    was never meaningful. It is a named, audited 400 at the door, exactly
    like every other caller-supplied field of the wrong type (A-39)."""
    node = register(server)
    code, body = server.call("/jobs", {
        "idempotency_key": f"bad-{bad!r}", "node_id": node["node_id"],
        "operation": "query_network", "arguments": bad})
    assert code == 400 and body["reason"] == "malformed"
    assert "arguments" in body["detail"]
    _, status = server.call("/status")
    assert status["jobs_queued"] == 0, "a refused enqueue created a job"


def test_absent_or_empty_arguments_are_still_fine(server):
    """The refusal is about TYPE, not emptiness: no arguments at all and
    an empty dict must both keep working."""
    node = register(server)
    for key, args in (("none", None), ("empty", {})):
        code, body = server.call("/jobs", {
            "idempotency_key": key, "node_id": node["node_id"],
            "operation": "query_network", "arguments": args})
        assert code == 200, body
        assert body["job"]["arguments"] == {}


def test_a_legacy_non_dict_arguments_record_never_wedges_a_claim(server):
    """THE PROBE, verbatim. A record whose arguments are a list (an older
    build, or hand-edited state -- the door refuses them now) must
    resolve as a NAMED refusal that releases the claim, never a raise
    that leaks the flight marker and hides the job from every route."""
    node = register_rt(server)
    # Written straight to the store: the enqueue gate would refuse it,
    # so this is the only way such a record can exist at all.
    with server.app.lock:
        job, _ = server.app.db.create_job(
            "legacy-args", node["node_id"], "run_tests", ["not", "a", "dict"],
            convoy_id=CONVOY, expected_runtime_id=node["runtime_id"])
    did = job["delivery_id"]
    assert job["arguments"] == ["not", "a", "dict"]
    calls = {"n": 0}

    def counting(port, operation, arguments):
        calls["n"] += 1
        return started_handle()

    server.app.forwarder = counting
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 409, body
    assert body["reason"] == "malformed_arguments"
    assert calls["n"] == 0, "malformed arguments went on the wire"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "queued", "the claim was not released"
    with server.app.lock:
        assert did not in server.app._in_flight, "flight marker leaked"
    # the job is visible to every route again -- paced, not wedged
    summary = server.app.drain_once()
    assert summary["backoff"] == 1 and summary["errors"] == 0
    assert calls["n"] == 0
    # and the SYNC path refuses identically -- one authority, not two
    with server.app.lock:
        sync_job, _ = server.app.db.create_job(
            "legacy-sync", node["node_id"], "query_network", ["x"],
            convoy_id=CONVOY)
    code, body = server.call("/dispatch",
                             {"delivery_id": sync_job["delivery_id"]})
    assert code == 409 and body["reason"] == "malformed_arguments"


def test_any_raise_after_the_claim_still_clears_the_flight_marker(server,
                                                                  monkeypatch):
    """Layer 3, STRUCTURAL. The claim and its cleanup now share ONE
    failure domain: the try/finally opens BEFORE the claim, so no future
    edit to the post-claim tail can reopen the leak class. Proven with a
    raise injected into that tail."""
    node = register_rt(server)
    job = enqueue_async(server, node)
    did = job["delivery_id"]

    def boom(*args, **kwargs):
        raise RuntimeError("a future edit raises here")

    monkeypatch.setattr(server.app, "_merged_arguments", boom)
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 500 and body["reason"] == "internal_error"
    monkeypatch.undo()
    with server.app.lock:
        assert did not in server.app._in_flight, "flight marker leaked"
    # the claim is debris, but VISIBLE debris: the reaper resolves it
    # instead of skipping it forever
    summary = server.app.drain_once()
    assert summary["stranded"] == 1
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "indeterminate"


def test_a_failed_handoff_write_parks_instead_of_burning_the_handle(
        server, monkeypatch):
    """PANEL, important: the node IS running the job and the host holds
    its handle -- and a single transient failure of the running-mirror
    write used to fall into the indeterminate downgrade, terminalising a
    run the host provably observed AND throwing away the only key to an
    outcome the node can still answer for 24h. It parks, exactly like a
    failed requeue, and the drain pass recovers it."""
    node = register_rt(server)
    job = enqueue_async(server, node)
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: started_handle()
    real = server.app.db.record_node_verdict
    calls = {"n": 0}

    def flaky(delivery_id, node_status, node_job_id, observed_at,
              result=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("sharing violation outlived retries")
        return real(delivery_id, node_status, node_job_id=node_job_id,
                    observed_at=observed_at, result=result)

    monkeypatch.setattr(server.app.db, "record_node_verdict", flaky)
    code, body = server.call("/dispatch", {"delivery_id": did})
    assert code == 503 and body["reason"] == "store_unavailable"
    assert "job_1a2b3c4d" in body["detail"], "the handle is not even named"
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "dispatching", "claim parked"
    with server.app.lock:
        assert did in server.app._pending_handoff
        assert did in server.app._in_flight, "marker handed to the reaper"
        # the handle is DURABLE in the trail even though the write failed
        # -- that is why the audit now precedes the write
        started = [e for e in server.app.db.audit_tail(limit=300)
                   if e["event"] == "dispatch_node_job_started"
                   and (e["detail"] or {}).get("delivery_id") == did]
    assert started and started[0]["detail"]["node_job_id"] == "job_1a2b3c4d"
    # the reaper must NOT burn it, and the same pass recovers the mirror
    summary = server.app.drain_once()
    assert summary["stranded"] == 0 and summary["handoffs"] == 1
    _, fetched = server.call(f"/jobs/{did}")
    assert fetched["job"]["state"] == "running"
    assert fetched["job"]["node_job_id"] == "job_1a2b3c4d"
    assert fetched["job"]["verdict_source"] == "node_poll"
    with server.app.lock:
        assert did not in server.app._pending_handoff
        assert did not in server.app._in_flight


def test_a_requeued_delivery_records_its_attempts_on_the_record(server):
    """PANEL, important: a node refusal that can never resolve (a key
    already bound to another operation) requeues forever by design --
    the host does not terminalise it, that stays the deferred reaper's
    call. But the audit line is deduped to ONE, so without a counter on
    the record the retry loop was completely invisible: the job looked
    freshly enqueued, no matter how many times it had been refused."""
    server.app.drain_backoff_s = 0.0
    node = register_rt(server)
    job = enqueue_async(server, node)
    did = job["delivery_id"]
    server.app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"error": "idempotency_key is already bound "
                                        "to a different operation"}}
    for _ in range(3):
        code, body = server.call("/dispatch", {"delivery_id": did})
        assert code == 409 and body["reason"] == "no_node_job_handle"
    _, fetched = server.call(f"/jobs/{did}")
    record = fetched["job"]
    assert record["state"] == "queued", "never terminalised (A-15)"
    assert record["attempts"] == 3
    assert record["last_attempt"]["reason"] == "no_node_job_handle"
    assert record["last_attempt"]["at"] is not None
    # bookkeeping ONLY: it must never touch the verdict fields
    assert record["verdict_source"] is None
    assert record["result"] is None and record["node_job_id"] is None
    # and the audit stays deduped -- the RECORD is what makes it visible
    with server.app.lock:
        events = [e for e in server.app.db.audit_tail(limit=300)
                  if e["event"] == "dispatch_no_handle"
                  and (e["detail"] or {}).get("delivery_id") == did]
    assert len(events) == 1


def test_a_terminal_reconcile_handle_says_it_carries_no_outcome(server):
    """PANEL, minor: a 16.5 reconcile hands back {'job_id','status':
    'done','hint'} with NO result body, and the delivery is terminal --
    so the poller never revisits it and the host's permanent record of a
    completed run held no outcome at all. The record now says so, and
    names where the outcome still lives for the node's 24h window."""
    node = register_rt(server)
    job = enqueue_async(server, node, key="reconcile")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {
        "job_id": "job_beadfeed", "status": "done",
        "hint": "Reconciled to the original run for this idempotency_key"}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded"
    result = body["job"]["result"]
    assert result["job_id"] == "job_beadfeed"
    assert "no result body" in result["detail"]
    assert "get_job_status" in result["detail"]
    # a terminal handle that DOES carry an outcome is mirrored verbatim
    job2 = enqueue_async(server, node, key="witherror")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {
        "job_id": "job_abcdef01", "status": "error",
        "error": "Test run failed to start: boom"}}
    code, body = server.call("/dispatch", {"delivery_id": job2["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "failed"
    assert "detail" not in body["job"]["result"]


def test_repeated_refusals_audit_once_per_transition(server):
    """The steady failing state must not grow audit.jsonl without bound:
    the same refusal audits on TRANSITION, not on every attempt."""
    _, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/NP"})               # portless: every pass defers
    job = enqueue(server, node)
    for _ in range(3):
        server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    with server.app.lock:
        events = [e for e in server.app.db.audit_tail(limit=200)
                  if e["event"] == "dispatch_deferred"
                  and e["detail"].get("delivery_id") == job["delivery_id"]]
    assert len(events) == 1, f"expected one deferred audit, got {events}"


# -- the drain bookkeeping maps scale with the queue -------------------

def test_drain_maps_scale_past_the_floor_without_eviction_cascade(
        server, monkeypatch):
    """At a FIXED 2048 cap, a backlog one entry over made every insertion
    evict another LIVE entry -- and the eviction CASCADED: each
    re-attempt evicted the next job's pacing, so one pass over the cap
    unpaced and re-audited the ENTIRE queue (measured: 2100 queued
    refusals -> second pass backoff=0, all 2100 re-attempted inside the
    30s window). The cap now scales to 2x the live queue; the floor is
    exercised here shrunk to 8 so the test stays fast."""
    monkeypatch.setattr(ha, "DRAIN_MAP_FLOOR", 8)
    for i in range(20):
        with server.app.lock:
            server.app.db.create_job("cap%d" % i, "nd_missing",
                                     "query_network", {}, CONVOY)
    first = server.app.drain_once()
    assert first["examined"] == 20
    assert server.app._drain_map_cap == 40, "cap must scale to 2x queue"
    assert len(server.app._drain_backoff) == 20, (
        "every queued job must keep its pacing entry -- eviction "
        "cascaded: %d" % len(server.app._drain_backoff))
    assert len(server.app._drain_noted) == 20
    second = server.app.drain_once()
    assert second["backoff"] == 20 and second["errors"] == 0, (
        "the second pass must skip ALL 20 via backoff, not re-attempt "
        "evicted ones: %r" % second)
    # ... and ONE audit line per job, not one per attempt
    with server.app.lock:
        refusals = [e for e in server.app.db.audit_tail(limit=400)
                    if e["event"] == "dispatch_refused"
                    and str(e["detail"].get("delivery_id", "")).startswith(
                        "cj_")]
    assert len(refusals) == 20, (
        "dedupe must hold at scale: %d lines" % len(refusals))


def test_the_end_of_pass_prune_reads_stale_entries_lock_free(
        server, monkeypatch):
    """Panel MAJOR (scale/locks lens). F4's cap rescale let the
    end-of-pass prune read O(backlog) job files under self.lock; on the
    pass after a mass-terminalise that wedged every route but /health
    (the exact starvation status() and the drain snapshot were fixed
    for). The get_job reads must run LOCK-FREE.

    The stale set is seeded with entries whose jobs DO NOT EXIST -- so
    the pass's snapshot reads nothing (its get_job calls are what a
    naive test would miscount) and EVERY get_job here is a PRUNE read.
    A live thread must be able to take the app lock from inside one."""
    with server.app.lock:
        for i in range(30):
            gid = "cj_ghost%02d" % i
            server.app._drain_backoff[gid] = 0.0        # past -> not held
            server.app._drain_noted[gid] = ("dispatch_refused", "x")
    seen = {"lock_free": None, "reads": 0}
    real_get = server.app.db.get_job

    def probe_get(delivery_id):
        seen["reads"] += 1
        if seen["reads"] == 1:
            got = server.app.lock.acquire(timeout=2)
            if got:
                server.app.lock.release()
            seen["lock_free"] = got
        return real_get(delivery_id)

    monkeypatch.setattr(server.app.db, "get_job", probe_get)
    server.app.drain_once()          # queue empty -> prune scans the ghosts
    assert seen["reads"] >= 30, (
        "the prune did not read every stale entry: %d" % seen["reads"])
    assert seen["lock_free"] is True, (
        "the end-of-pass prune held self.lock across get_job -- the "
        "O(backlog)-under-lock stall F4 must not reintroduce")
    with server.app.lock:
        assert not any(k.startswith("cj_ghost")
                       for k in server.app._drain_backoff), "ghosts not reaped"
        assert not any(k.startswith("cj_ghost")
                       for k in server.app._drain_noted)


def test_a_mid_pass_addition_survives_the_lock_free_prune(server,
                                                          monkeypatch):
    """The lock-free prune drops by an explicit `drop` set, never a
    keep-rebuild -- so an entry added BETWEEN its two lock holds (a
    concurrent /dispatch pacing another job) survives. A keep-rebuild
    would silently discard it, defeating its backoff and re-auditing the
    refusal (the round-3 invariant, now under a two-lock-hold prune)."""
    with server.app.lock:
        for i in range(5):
            server.app._drain_backoff["cj_dead%d" % i] = 0.0   # ghosts
    injected = "cj_injected_mid_prune"
    real_get = server.app.db.get_job

    def inject_once(delivery_id):
        if not getattr(inject_once, "done", False):
            inject_once.done = True
            with server.app.lock:
                server.app._drain_backoff[injected] = server.app._now() + 30
        return real_get(delivery_id)

    monkeypatch.setattr(server.app.db, "get_job", inject_once)
    server.app.drain_once()
    assert injected in server.app._drain_backoff, (
        "an entry added mid-prune was wrongly dropped -- its job would "
        "re-dispatch immediately and re-audit the refusal")
    assert not any(k.startswith("cj_dead")
                   for k in server.app._drain_backoff), (
        "the examined-dead ghosts must still be reaped")
