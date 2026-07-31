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
        with server.app.lock:
            status = server.app.status()
        assert status["drain_loop"] is True, "status lied about the loop"
        assert server.app.start_drain_loop(interval_s=60) is False, \
            "a second loop started over the unstopped first"
    finally:
        release.set()
    assert server.app.stop_drain_loop(timeout_s=10) is True
    with server.app.lock:
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
