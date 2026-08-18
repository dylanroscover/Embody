"""Panel findings from the slice 2 adversarial review, as regressions.

Every test here was written to FAIL against the first cut and was
observed failing before the fix landed. Two blockers and four majors,
all reachable with no I/O error and no race:

  B1  observe-only had ZERO dispatch-time enforcement -- the one-shot
      revocation sweep cannot cover work that is in flight when the
      narrowing lands, and a requeue puts it straight back on the wire.
  B2  remote_exposed was dead data: run_tests (which exec_module's every
      test_*.py on disk) and save_project (15s main-thread block) were
      both remotely submittable and dispatchable.
  M3  a peer-chosen controller_id let a peer's own revocation release
      the LOCAL operator's lease.
  M4  db.jobs() swallows unreadable records, so a revocation that read
      NOTHING was byte-identical to one that found nothing.
  M5  the idempotency marker excluded origin: a peer could bind to a
      local caller's record (and get it burned by its own revocation),
      or have its operation silently replaced and still be told 200.
  M6  the killswitch -- the EMERGENCY STOP -- ran one full job scan per
      peer inside the global lock.
"""

import json
import os
import time

import pytest

import convoy_controllers as controllers
from conftest import approve_td_python
import convoy_hostapp as ha
import convoy_hoststore as hs
import convoy_peers as cp
import convoy_protocol as protocol
from test_convoy_hostapp import Server
from test_convoy_revocation import (CONVOY, PEER, PEER_FP, OTHER, OTHER_FP,
                                    admit, envelope_for, peer_job, psk_for,
                                    register, submit_as_peer, write_denylist)


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


def audit_events(server):
    with server.app.lock:
        return [e["event"] for e in server.app.db.audit_tail(limit=600)]


# =====================================================================
# BLOCKER 1 -- observe-only must be enforced AT DISPATCH, every pass
# =====================================================================

def test_a_narrowed_peers_requeued_mutation_is_never_forwarded(server):
    """THE reproduction, end to end, with no I/O error and no race.

    A peer's mutation is DISPATCHING when /peers/observe lands. The
    one-shot sweep can only count it (`left_in_flight`) -- terminalising
    an in-flight forward would invent an outcome. The node is down, so
    the forward is UNREACHABLE, which correctly returns the job to
    QUEUED... and the next drain pass forwarded the mutation for a peer
    that may no longer mutate.

    The queue must be RE-AUTHORIZED on every pass. A one-shot sweep is
    not containment.
    """
    node = register(server)
    admit(server)
    job = peer_job(server, node, operation="set_op_position", key="b1")
    delivery_id = job["delivery_id"]

    with server.app.lock:
        assert server.app.db.claim_for_dispatch(delivery_id) is not None

    code, body = server.call("/peers/observe", {"host_id": PEER})
    assert code == 200
    assert body["revocation"]["left_in_flight"] == 1, (
        "an in-flight forward is left alone -- that is correct, and it is "
        "exactly why the sweep cannot be the containment")

    # the forward was never delivered: back to queued, honestly
    with server.app.lock:
        assert server.app.db.release_claim(delivery_id)["state"] == "queued"

    forwarded = []
    server.app.forwarder = lambda p, o, a: (forwarded.append(o)
                                            or {"ok": True, "result": {}})
    with server.app.lock:
        server.app._drain_backoff.clear()
    summary = server.app.drain_once()

    assert forwarded == [], (
        "a narrowed peer's mutation reached the node -- observe-only has "
        "no dispatch-time enforcement")
    assert summary["dispatched"] == 0
    after = server.call("/jobs/" + delivery_id)[1]["job"]
    assert after["state"] == "refused", (
        "the job can never be served, so leaving it queued makes /jobs "
        "lie -- verbatim the failure `refused` was added to prevent")
    assert after["result"]["peer_reason"] == cp.REASON_OBSERVE_ONLY


def test_a_narrowed_peers_READ_still_dispatches(server):
    """POSITIVE CONTROL for B1: the narrowing is per-operation, not a
    blanket stop. Observe-only means X0 keeps working."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, operation="query_network", key="b1r")
    server.call("/peers/observe", {"host_id": PEER})
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    summary = server.app.drain_once()
    assert summary["dispatched"] == 1
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "succeeded"


def test_a_blocked_peers_requeued_job_is_terminalised(server):
    """The same hole for a BLOCKED peer: the sweep ran while the job was
    in flight, so nothing terminalised it when it came back."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="b1b")
    delivery_id = job["delivery_id"]
    with server.app.lock:
        server.app.db.claim_for_dispatch(delivery_id)
    server.call("/peers/block", {"host_id": PEER})
    with server.app.lock:
        server.app.db.release_claim(delivery_id)
        server.app._drain_backoff.clear()

    forwarded = []
    server.app.forwarder = lambda p, o, a: (forwarded.append(o)
                                            or {"ok": True, "result": {}})
    summary = server.app.drain_once()
    assert forwarded == []
    assert summary["refused"] == 1, (
        "a terminalised job is not 'deferred' -- the pass summary must "
        "say what happened to it")
    assert server.call("/jobs/" + delivery_id)[1][
        "job"]["state"] == "refused"


def test_a_reversible_refusal_still_only_SKIPS(server):
    """The other side of the same coin, so the fix cannot overshoot: a
    killswitch and a denylist entry lift without any membership change,
    so their work is skipped and paced, never burnt."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="b1rev")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    write_denylist(server, {"host_ids": [PEER]})
    summary = server.app.drain_once()
    assert summary["deferred"] == 1 and summary["refused"] == 0
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "queued"

    server.call("/lan/killswitch", {"engaged": True})
    write_denylist(server, {"host_ids": []})
    with server.app.lock:
        server.app._drain_backoff.clear()
    summary = server.app.drain_once()
    assert summary["deferred"] == 1 and summary["refused"] == 0
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "queued"


def test_authorize_origin_hands_back_the_whole_decision(server):
    """The root cause, pinned directly: the helper returned None for an
    observe-only peer, which the dispatcher reads as 'go ahead'."""
    admit(server)
    server.call("/peers/observe", {"host_id": PEER})
    with server.app.lock:
        decision = server.app._authorize_origin(PEER)
    assert decision is not None, (
        "an allowed-but-narrowed peer must not come back as None")
    assert decision.allowed is True and decision.may_mutate is False


# =====================================================================
# BLOCKER 2 -- the reviewed remote surface and per-node code gate bind
# =====================================================================

def test_peer_run_tests_is_never_relayed_whether_or_not_approved(server):
    """run_tests exec_module's every test file on disk, so it is NOT relayable
    to a remote peer at all (remote_exposed=False, A-1/R-2) -- it returns to the
    remote surface only in a later phase, gated by A-30/A-31 show protection,
    which does not exist yet. A peer submitting it is REFUSED whether or not the
    node has TD-Python approval; the refusal reason differs by gate order (the
    TD-Python gate is checked before the remote-exposed gate), but it is never
    executed (review 2026-08-02, finding 544)."""
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": 9800,
        "runtime_id": "rt_live"})
    assert code == 200
    admit(server)
    envelope = envelope_for(server, node, psk_for(server),
                            operation="run_tests", idempotency_key="b2",
                            expected_runtime_id="rt_live")
    code, body = submit_as_peer(server, envelope)
    assert code == 403
    assert body["reason"] == "td_python_not_approved"

    # With TD-Python approved the earlier gate passes, but run_tests is still
    # refused by the remote-exposed gate -- it never reaches execution.
    approve_td_python(server.app, node["node_id"])
    envelope = envelope_for(server, node, psk_for(server),
                            operation="run_tests", idempotency_key="b2-ok",
                            expected_runtime_id="rt_live")
    code, body = submit_as_peer(server, envelope)
    assert code == 403
    assert body["reason"] == "operation_not_remote_exposed"


def test_peer_save_project_is_not_on_the_remote_surface(server):
    """save_project blocks TD's main thread 15+s and, without A-30/A-31 show
    protection, is NOT relayable to a remote peer (remote_exposed=False). A peer
    submitting it is refused as not remote-exposed (finding 544). It remains
    available on the LOCAL path (see test_the_local_path_keeps_both_operations)."""
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": 9800,
        "runtime_id": "rt_live"})
    assert code == 200
    admit(server)
    envelope = envelope_for(server, node, psk_for(server),
                            operation="save_project", idempotency_key="b2-save",
                            expected_runtime_id="rt_live")
    code, body = submit_as_peer(server, envelope)
    assert code == 403
    assert body["reason"] == "operation_not_remote_exposed"


@pytest.mark.parametrize("operation", ["run_tests", "save_project"])
def test_the_local_path_keeps_both_operations(server, operation):
    """Both operations remain available locally under the same node policy;
    run_tests needs TD-Python approval regardless of transport."""
    node = register(server)
    code, body = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": 9800,
        "runtime_id": "rt_live"})
    runtime = body.get("runtime_id") or "rt_live"
    if operation == "run_tests":
        approve_td_python(server.app, node["node_id"])
    code, body = server.call("/jobs", {
        "idempotency_key": "local-" + operation,
        "node_id": node["node_id"], "operation": operation,
        "expected_runtime_id": runtime})
    assert code == 200, body


def test_a_queued_unapproved_run_tests_job_is_never_dispatched(server):
    """Defence in depth: a legacy/directly-created code job is re-gated at
    dispatch and terminally refused while TD-Python approval is off."""
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": 9800,
        "runtime_id": "rt_live"})
    assert code == 200
    admit(server)
    with server.app.lock:
        # expected_runtime_id MATCHES the registered runtime, so the A-22
        # precondition passes -- and the job carries the CURRENT
        # admission lineage, so the stale_admission fence passes too.
        # Both stops short of the gate under test would leave forwarded
        # empty for the WRONG reason.
        job, _ = server.app.db.create_job(
            "b2d", node["node_id"], "run_tests", {}, CONVOY,
            expected_runtime_id="rt_live", origin_host_id=PEER,
            controller_id="peer:%s:ctl" % PEER,
            origin_admission_id=server.app.peers.get(PEER)["admission_id"])
    forwarded = []
    server.app.forwarder = lambda p, o, a: (forwarded.append(o)
                                            or {"ok": True, "result": {}})
    server.app.drain_once()
    assert forwarded == [], "a peer's run_tests reached the node at dispatch"
    # The refusal REASON is the evidence, never absence alone: it must be
    # the node code-permission gate that fired, not some earlier fence.
    after = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert after["state"] == "refused"
    assert after["result"]["reason"] == "td_python_not_approved", (
        after["result"])


def test_remote_exposed_has_a_real_consumer():
    """The finding was found by grep, so it is closed by grep: a flag
    nothing reads is a comment, not a boundary."""
    import ast
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "convoy_hostapp.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    reads = 0
    for node in ast.walk(tree):
        # gating["remote_exposed"] / gating.get("remote_exposed") -- a
        # READ, as opposed to the dict literals that merely define it.
        if isinstance(node, ast.Subscript):
            value = getattr(node.slice, "value", None)
            if value == "remote_exposed":
                reads += 1
    assert reads >= 2, (
        "remote_exposed is read nowhere -- it must gate BOTH the "
        "submission path and the dispatch path")


# =====================================================================
# MAJOR 3 -- a peer must not be able to name a LOCAL controller
# =====================================================================

def test_a_peer_cannot_release_the_local_operators_lease(server):
    """The measured escalation: a stranger's mutation goes from refused
    (node_leased) to ALLOWED, and the attacker's own revocation is the
    trigger."""
    node = register(server)
    admit(server)
    admit(server, host_id=OTHER, fingerprint=OTHER_FP)

    # the LOCAL operator holds the node exclusively
    code, _ = server.call("/leases", {"controller_id": "ctl-local",
                                      "node_id": node["node_id"],
                                      "mode": "exclusive"})
    assert code == 200

    # the peer sends ONE harmless read, naming the local controller
    envelope = envelope_for(server, node, psk_for(server),
                            operation="query_network",
                            controller_id="ctl-local", idempotency_key="m3")
    assert submit_as_peer(server, envelope)[0] == 200

    # ... and then gets itself blocked
    server.call("/peers/block", {"host_id": PEER})

    assert len(server.call("/leases")[1]["leases"]) == 1, (
        "a peer named the LOCAL controller and its own revocation "
        "released the local operator's exclusive lease")

    # the lock still holds against a THIRD party's mutation
    envelope = envelope_for(server, node, psk_for(server),
                            operation="set_op_position",
                            controller_id="ctl-other", idempotency_key="m3b")
    code, body = submit_as_peer(server, envelope, OTHER, OTHER_FP)
    assert code == 409 and body["reason"] == "node_leased"


def test_a_peer_controller_id_is_namespaced_by_origin(server):
    node = register(server)
    admit(server)
    admit(server, host_id=OTHER, fingerprint=OTHER_FP)
    a = peer_job(server, node, key="ns-a", controller_id="ctl")
    b = peer_job(server, node, key="ns-b", controller_id="ctl",
                 host_id=OTHER, fingerprint=OTHER_FP)
    assert a["controller_id"] != b["controller_id"], (
        "two peers naming the same controller_id must not share a lease "
        "identity")
    for record, host in ((a, PEER), (b, OTHER)):
        assert host in record["controller_id"]
        assert record["controller_id"].startswith(cp.CONTROLLER_NAMESPACE)


def test_two_peers_cannot_take_each_others_leases(server):
    """The namespacing has to reach the LEASE gate, not just the record:
    peer B naming peer A's controller_id must not inherit A's rights."""
    node = register(server)
    admit(server)
    admit(server, host_id=OTHER, fingerprint=OTHER_FP)
    envelope = envelope_for(server, node, psk_for(server),
                            operation="set_op_position",
                            controller_id="shared", idempotency_key="x1")
    assert submit_as_peer(server, envelope)[0] == 200
    with server.app.lock:
        namespaced = cp.namespaced_controller(PEER, "shared")
        server.app.leases.acquire(node["node_id"], namespaced,
                                  controllers.LEASE_EXCLUSIVE,
                                  server.app._now())
    envelope = envelope_for(server, node, psk_for(server),
                            operation="set_op_position",
                            controller_id="shared", idempotency_key="x2")
    code, body = submit_as_peer(server, envelope, OTHER, OTHER_FP)
    assert code == 409 and body["reason"] == "node_leased"


# =====================================================================
# MAJOR 4 -- unreadable is not absent, in the revocation sweep
# =====================================================================

def test_a_revocation_that_could_not_read_the_records_says_so(server,
                                                              monkeypatch):
    """A revocation that read NOTHING was byte-identical to one that
    found nothing: errors stayed 0 and the operator was told the peer
    was contained."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="m4")

    real_get = server.app.db.get_job

    def unreadable(delivery_id):
        if delivery_id == job["delivery_id"]:
            return None             # exactly what an OSError looks like
        return real_get(delivery_id)

    monkeypatch.setattr(server.app.db, "get_job", unreadable)
    code, body = server.call("/peers/block", {"host_id": PEER})
    monkeypatch.undo()

    assert code == 200
    assert body["revocation"]["unreadable"] == 1, (
        "the sweep skipped a record it could not read and reported a "
        "clean revocation")
    assert "peer_revocation_incomplete" in audit_events(server)
    # and the job is still there, still queued -- the honest state
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "queued"


def test_a_failed_directory_listing_is_not_an_empty_queue(server,
                                                          monkeypatch):
    def boom(path):
        raise OSError("the jobs directory is unreadable")

    monkeypatch.setattr(ha.os, "listdir", boom)
    admit(server)
    code, body = server.call("/peers/block", {"host_id": PEER})
    monkeypatch.undo()
    assert code == 200
    assert body["revocation"]["errors"] >= 1, (
        "a scan that failed outright must never read as 'no work found'")


def test_scan_jobs_separates_unreadable_from_absent(tmp_path):
    db = hs.HostStore(str(tmp_path / "s"))
    good, _ = db.create_job("k1", "n", "query_network", {}, "cv")
    bad, _ = db.create_job("k2", "n", "query_network", {}, "cv")
    with open(db._job_path(bad["delivery_id"]), "w", encoding="utf-8") as f:
        f.write("{ truncated")
    jobs, unreadable = db.scan_jobs()
    assert [j["delivery_id"] for j in jobs] == [good["delivery_id"]]
    assert unreadable == [bad["delivery_id"]]
    db.close()


# =====================================================================
# MAJOR 5 -- the idempotency key space must be scoped to the origin
# =====================================================================

def test_a_peer_cannot_bind_to_a_local_callers_record(server):
    """Reproduced in the review: the peer's submission bound to the
    LOCAL record, its requested operation was silently replaced, and it
    was still answered 200."""
    node = register(server)
    admit(server)
    local = server.call("/jobs", {"idempotency_key": "nightly",
                                  "node_id": node["node_id"],
                                  "operation": "query_network",
                                  "controller_id": "ctl-local"})[1]["job"]
    envelope = envelope_for(server, node, psk_for(server),
                            operation="set_op_position",
                            idempotency_key="nightly")
    code, body = submit_as_peer(server, envelope)
    if code == 200:
        assert body["job"]["delivery_id"] != local["delivery_id"], (
            "the peer was handed the LOCAL caller's delivery record")
        assert body["job"]["operation"] == "set_op_position", (
            "the peer was told 200 for an operation that was replaced")
        assert body["job"]["origin_host_id"] == PEER
    else:
        assert body["reason"] == "idempotency_origin_conflict"


def test_a_peer_revocation_cannot_burn_a_local_callers_job(server):
    """The other direction, and the worse one: a 200-acknowledged LOCAL
    job terminalised by a peer's revocation."""
    node = register(server)
    admit(server)
    envelope = envelope_for(server, node, psk_for(server),
                            operation="query_network",
                            idempotency_key="shared-key")
    assert submit_as_peer(server, envelope)[0] == 200
    code, body = server.call("/jobs", {"idempotency_key": "shared-key",
                                       "node_id": node["node_id"],
                                       "operation": "query_network",
                                       "controller_id": "ctl-local"})
    assert code == 200, body
    local = body["job"]
    assert local["origin_host_id"] == server.app.host_id, (
        "a local caller was handed a record owned by a PEER")

    server.call("/peers/block", {"host_id": PEER})
    assert server.call("/jobs/" + local["delivery_id"])[1][
        "job"]["state"] == "queued", (
        "a peer's revocation burned a local caller's acknowledged job")


def test_a_marker_and_a_record_that_disagree_on_origin_are_refused(tmp_path):
    """Origin scoping makes a live marker and its record agree by
    construction -- so the conflict guard is only reachable when they
    have STOPPED agreeing: a job file whose origin changed underneath
    its marker (a hand edit, a restored backup, a partial copy of a
    state directory). Handing the key's owner that record would
    attribute someone else's work to them, and their revocation would
    then terminalise it."""
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv",
                           origin_host_id=PEER)
    path = db._job_path(job["delivery_id"])
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    record["origin_host_id"] = OTHER          # the record drifts
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f)

    with pytest.raises(hs.IdempotencyOriginConflict) as e:
        db.create_job("k", "n", "query_network", {}, "cv",
                      origin_host_id=PEER)
    assert e.value.existing_origin == OTHER
    assert e.value.requested_origin == PEER
    assert e.value.reason == "idempotency_origin_conflict"
    db.close()


def test_the_origin_conflict_surfaces_as_a_named_refusal(server):
    """...and it must reach the caller as a named 409, never a 500."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="drift")
    path = server.app.db._job_path(job["delivery_id"])
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    record["origin_host_id"] = OTHER
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f)

    envelope = envelope_for(server, node, psk_for(server),
                            idempotency_key="drift")
    code, body = submit_as_peer(server, envelope)
    assert code == 409
    assert body["reason"] == "idempotency_origin_conflict"


def test_idempotency_still_holds_within_one_origin(server):
    """POSITIVE CONTROL: scoping must not break idempotency itself."""
    node = register(server)
    admit(server)
    first = peer_job(server, node, key="same")
    envelope = envelope_for(server, node, psk_for(server),
                            idempotency_key="same")
    code, body = submit_as_peer(server, envelope)
    assert code == 200 and body["created"] is False
    assert body["job"]["delivery_id"] == first["delivery_id"]


def test_a_legacy_marker_is_honoured_for_a_local_retry(tmp_path):
    """The upgrade hazard, closed: markers written before delivery
    records carried an origin used a 3-part scope. A local retry must
    find its own job, not mint a duplicate of acknowledged work."""
    db = hs.HostStore(str(tmp_path / "s"))
    legacy, created = db.create_job("k", "n", "query_network", {}, "cv")
    assert created
    again, created = db.create_job("k", "n", "query_network", {}, "cv",
                                   origin_host_id=db.host_id())
    assert created is False, "a local retry re-minted an acknowledged job"
    assert again["delivery_id"] == legacy["delivery_id"]
    db.close()


def test_a_peer_never_inherits_a_legacy_marker(tmp_path):
    """The back-compat path is for LOCAL submissions only: an
    origin-less record is local by definition."""
    db = hs.HostStore(str(tmp_path / "s"))
    legacy, _ = db.create_job("k", "n", "query_network", {}, "cv")
    peer, created = db.create_job("k", "n", "query_network", {}, "cv",
                                  origin_host_id=PEER)
    assert created is True
    assert peer["delivery_id"] != legacy["delivery_id"]
    db.close()


def test_a_refused_key_can_be_used_again_after_re_admission(server):
    """A terminalised delivery must not wedge its idempotency key: after
    re-admitting the peer, the same key has to be able to produce work
    again -- and a plain 200 over a refused record is a lie."""
    node = register(server)
    admit(server)
    first = peer_job(server, node, key="wedged")
    server.call("/peers/block", {"host_id": PEER})
    assert server.call("/jobs/" + first["delivery_id"])[1][
        "job"]["state"] == "refused"

    admit(server)               # re-admitted after comparing out of band
    envelope = envelope_for(server, node, psk_for(server),
                            idempotency_key="wedged")
    code, body = submit_as_peer(server, envelope)
    assert code == 200
    assert body["created"] is True, (
        "the key was permanently wedged by a refused record")
    assert body["job"]["state"] == "queued"
    assert body["job"]["delivery_id"] != first["delivery_id"]


# =====================================================================
# MAJOR 6 -- the emergency stop must be the FASTEST route, not the slowest
# =====================================================================

def test_the_killswitch_scans_the_job_store_at_most_once(server,
                                                         monkeypatch):
    """Measured at 36.45s / 3000 jobs / 20 peers, inside the global lock,
    while the operator's own client timed out and RETRIED it. drain_once
    was explicitly rewritten to keep this scan out of the lock."""
    node = register(server)
    for i in range(6):
        host = "%02x" % i * 16
        fingerprint = "cvfp1-%s" % "-".join(["%04d" % i] * 8)
        server.call("/peers/admit", {"host_id": host,
                                     "fingerprint": fingerprint,
                                     "convoy_ids": [CONVOY]})
    scans = []
    real_scan = server.app.db.scan_jobs
    monkeypatch.setattr(server.app.db, "scan_jobs",
                        lambda: (scans.append(1), real_scan())[1])
    real_jobs = server.app.db.jobs
    monkeypatch.setattr(server.app.db, "jobs",
                        lambda state=None: (scans.append(1),
                                            real_jobs(state))[1])
    code, _ = server.call("/lan/killswitch", {"engaged": True})
    assert code == 200
    assert len(scans) <= 1, (
        "the emergency stop scanned the whole job store %d times -- once "
        "per peer" % len(scans))


def test_the_killswitch_does_not_hold_the_lock_across_the_scan(server):
    """It is the EMERGENCY route: a /status must stay answerable while it
    runs, and the switch itself must take effect immediately."""
    import threading
    node = register(server)
    admit(server)
    for i in range(40):
        peer_job(server, node, key="ks%d" % i)

    slow = threading.Event()

    real_scan = server.app.db.scan_jobs

    def slow_scan():
        slow.set()
        time.sleep(0.6)
        return real_scan()

    server.app.db.scan_jobs = slow_scan
    result = {}

    def fire():
        result["killswitch"] = server.call("/lan/killswitch",
                                           {"engaged": True})

    thread = threading.Thread(target=fire, daemon=True)
    thread.start()
    assert slow.is_set() or slow.wait(5), "the scan never started"

    # Measure THE LOCK, not an HTTP round trip: the claim is about the
    # global lock, and timing it through the server would fold in the
    # client's own retry/backoff and make the test lie in both directions.
    started = time.time()
    with server.app.lock:
        held = time.time() - started
    # ... and the switch must ALREADY be in force, mid-scan.
    code, body = server.call("/status")
    thread.join(timeout=20)
    server.app.db.scan_jobs = real_scan

    assert held < 0.2, (
        "the global lock was held for %.2fs across the killswitch's job "
        "scan -- every route and the drain loop block behind it" % held)
    assert code == 200
    assert body["lan_killswitch"] is True, (
        "the switch must be in force from the instant it is set, not "
        "after the lease cleanup finishes")
    assert result["killswitch"][0] == 200


def test_revoke_scans_the_job_store_once(server, monkeypatch):
    node = register(server)
    admit(server)
    peer_job(server, node, key="one-scan")
    scans = []
    real_scan = server.app.db.scan_jobs
    monkeypatch.setattr(server.app.db, "scan_jobs",
                        lambda: (scans.append(1), real_scan())[1])
    real_jobs = server.app.db.jobs
    monkeypatch.setattr(server.app.db, "jobs",
                        lambda state=None: (scans.append(1),
                                            real_jobs(state))[1])
    server.call("/peers/block", {"host_id": PEER})
    assert len(scans) == 1, "the revocation scanned the job store twice"


# =====================================================================
# MINORS
# =====================================================================

@pytest.mark.parametrize("body,why", [
    ('{"version": 0, "host_ids": []}', "version 0"),
    ('{"version": -1, "host_ids": []}', "a negative version"),
    ('{"host_ids": null}', "a null list"),
    ('{"fingerprints": null}', "a null fingerprint list"),
    ('{"host_ids": ["%s"], "host_ids": []}' % PEER, "a DUPLICATE key"),
], ids=["v0", "v-1", "null-hosts", "null-fps", "duplicate-key"])
def test_minor_a_every_remaining_denylist_shape_fails_closed(tmp_path, body,
                                                             why):
    """The duplicate-key cell is the 2am-plausible one: an operator
    appends a second host_ids line and json keeps only the last."""
    store = cp.PeerStore(str(tmp_path / "state"))
    store.admit(PEER, PEER_FP)
    path = store.denylist.path
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    old = time.time() - 5
    os.utime(path, (old, old))
    decision = store.authorize_peer(PEER, PEER_FP)
    assert decision.allowed is False, (
        f"a denylist with {why} ADMITTED -- 'every failure refuses "
        f"everything' has an exception in it")


def test_minor_b_a_size_and_mtime_preserving_rewrite_is_still_seen(tmp_path):
    """rsync -t, cp -p, a backup restore, a backward clock step: an edit
    that preserves BOTH mtime and size was invisible forever, not just
    inside the 1s window."""
    store = cp.PeerStore(str(tmp_path / "state"))
    store.admit(PEER, PEER_FP)
    path = store.denylist.path
    first = json.dumps({"host_ids": [PEER]})
    second = json.dumps({"host_ids": [OTHER]})
    assert len(first) == len(second), "the probe needs equal lengths"

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(first)
    stamp = time.time() - 600
    os.utime(path, (stamp, stamp))
    assert store.authorize_peer(PEER, PEER_FP).allowed is False

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(second)
    os.utime(path, (stamp, stamp))          # mtime AND size preserved
    store._now = lambda: time.time() + cp.DENYLIST_REVALIDATE_S + 1
    store.denylist._now = store._now
    assert store.authorize_peer(PEER, PEER_FP).allowed is True, (
        "a rewrite that preserved mtime and size was never noticed")


def test_minor_c_an_unreadable_store_reports_the_truth(tmp_path):
    """Authorization stayed closed, but /status told the operator the
    opposite: killswitch false, and set_killswitch then refused."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(PEER, PEER_FP)
    store.set_killswitch(True, reason="incident")
    with open(os.path.join(directory, cp.PEERS_FILE), "w",
              encoding="utf-8") as f:
        f.write("{ truncated")
    reloaded = cp.PeerStore(directory)
    assert reloaded.unreadable
    assert reloaded.killswitch()["engaged"] is True, (
        "an engaged killswitch read as DISENGAGED after the store went "
        "unreadable -- the operator is told the opposite of the truth")
    assert reloaded.authorize_peer(PEER, PEER_FP).allowed is False


def test_minor_c_a_repaired_store_is_picked_up_without_a_restart(tmp_path):
    """`unreadable` was sticky for the process lifetime: fixing the file
    did nothing until the daemon restarted."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(PEER, PEER_FP)
    path = os.path.join(directory, cp.PEERS_FILE)
    good = open(path, encoding="utf-8").read()
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ truncated")
    broken = cp.PeerStore(directory)
    assert broken.authorize_peer(PEER, PEER_FP).allowed is False

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(good)
    old = time.time() - 5
    os.utime(path, (old, old))
    assert broken.authorize_peer(PEER, PEER_FP).allowed is True, (
        "a repaired peers.json was ignored until a restart")
    assert broken.unreadable is None


def test_minor_d_omitting_the_origin_is_an_error_not_a_bypass(server):
    """The whole slice's security rested on slice 3 remembering to pass
    origin=. Omission must CRASH, not silently authorize."""
    node = register(server)
    envelope = envelope_for(server, node, psk_for(server))
    with pytest.raises(TypeError):
        with server.app.lock:
            server.app.submit_envelope({"envelope": envelope})


def test_minor_d_an_explicit_none_origin_is_refused(server):
    """None is not the loopback sentinel: a caller that computed its
    origin and got None must be refused, not trusted."""
    node = register(server)
    envelope = envelope_for(server, node, psk_for(server))
    with server.app.lock:
        code, body = server.app.submit_envelope({"envelope": envelope},
                                                origin=None)
    assert code == 403 and body["reason"] == cp.REASON_UNKNOWN


def test_minor_e_the_peer_controller_map_evicts_the_oldest(server,
                                                           monkeypatch):
    """First-inserted eviction let a chatty peer evict an older peer's
    attribution, which is the one that revocation needs."""
    monkeypatch.setattr(ha, "MAX_PEER_CONTROLLERS", 4)
    clock = [1000.0]
    monkeypatch.setattr(server.app, "_now", lambda: clock[0])
    with server.app.lock:
        # PEER's bucket is the FIRST-INSERTED one, and its entries are
        # then refreshed to be the NEWEST of all. Eviction must take the
        # globally oldest ENTRY (a chatty peer's stale one), not whatever
        # happens to sit in the first-inserted HOST's bucket.
        server.app._note_peer_controller(PEER, "p1")
        clock[0] += 1
        server.app._note_peer_controller(PEER, "p2")
        clock[0] += 1
        server.app._note_peer_controller(OTHER, "stale-o1")
        clock[0] += 1
        server.app._note_peer_controller(OTHER, "o2")
        clock[0] = 2000.0
        server.app._note_peer_controller(PEER, "p1")     # refreshed
        clock[0] += 1
        server.app._note_peer_controller(PEER, "p2")     # refreshed
        clock[0] += 1
        server.app._note_peer_controller(OTHER, "o3")    # overflow
        kept = set(server.app._peer_controllers.get(PEER) or ())
        others = set(server.app._peer_controllers.get(OTHER) or ())
    assert kept == {"p1", "p2"}, (
        "a chatty peer evicted a quiet peer's freshly-refreshed "
        "attribution -- which is the one revocation needs")
    assert "stale-o1" not in others, "the oldest ENTRY must be the one to go"


def test_minor_g_refused_evidence_does_not_claim_it_was_never_dispatched(
        server):
    """A requeued job HAS attempts >= 1 and a forward WAS attempted. Only
    'it never ran' is true."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="g1")
    with server.app.lock:
        server.app.db.record_dispatch_note(job["delivery_id"],
                                           "node_unreachable", 1.0)
    server.call("/peers/block", {"host_id": PEER})
    result = server.call("/jobs/" + job["delivery_id"])[1]["job"]["result"]
    text = json.dumps(result)
    assert "never dispatched" not in text, (
        "the evidence claims something the attempts counter contradicts")
    assert "never ran" in text
    assert result.get("attempts") == 1


# =====================================================================
# SECOND AND THIRD ROUND OF PANEL FINDINGS
# =====================================================================

def test_new1_a_refused_peer_envelope_still_registers_its_controller(server):
    """`_gate_operation` heartbeats the controller BEFORE the lease
    check, but `_note_peer_controller` ran only AFTER the gate passed --
    so a peer whose request was REFUSED landed in the lease registry and
    NOT in the revocation map, invisible to `_controllers_for_origin`."""
    node = register(server)
    admit(server)
    admit(server, host_id=OTHER, fingerprint=OTHER_FP)
    # OTHER holds the node exclusively, so PEER's mutation is refused
    with server.app.lock:
        server.app.leases.acquire(
            node["node_id"], cp.namespaced_controller(OTHER, "ctl-other"),
            controllers.LEASE_EXCLUSIVE, server.app._now())
    envelope = envelope_for(server, node, psk_for(server),
                            operation="set_op_position",
                            controller_id="ctl-refused",
                            idempotency_key="n1")
    code, body = submit_as_peer(server, envelope)
    assert code == 409 and body["reason"] == "node_leased"

    with server.app.lock:
        tracked = server.app._controllers_for_origin(PEER)
    assert cp.namespaced_controller(PEER, "ctl-refused") in tracked, (
        "a refused peer's controller is in the lease registry but not in "
        "the revocation map -- revocation cannot see it")


def test_new1_a_peer_cannot_resurrect_a_dead_local_controller(server):
    """Measured: a local controller idle 5000s reads alive=False, and one
    peer envelope naming its controller_id flipped it back to alive."""
    node = register(server)
    admit(server)
    with server.app.lock:
        server.app.leases.heartbeat("ctl-local", 0.0)
        server.app.leases.acquire(node["node_id"], "ctl-local",
                                  controllers.LEASE_EXCLUSIVE, 0.0)
        assert server.app.leases.controller_alive("ctl-local", 5000.0) \
            is False

    # a MUTATING operation, because that is the one _gate_operation
    # heartbeats for ("issuing a mutation proves the controller is alive")
    envelope = envelope_for(server, node, psk_for(server),
                            operation="set_op_position",
                            controller_id="ctl-local", idempotency_key="n1b")
    submit_as_peer(server, envelope)

    with server.app.lock:
        alive = server.app.leases.controller_alive("ctl-local",
                                                   server.app._now())
    assert alive is False, (
        "a peer naming a LOCAL controller_id kept a dead local "
        "controller's lease standing")


def test_new2_the_refused_count_only_counts_real_transitions(server,
                                                             monkeypatch):
    """The summary is returned to the operator AND written into the
    `peer_revoked` audit line. A count that can claim refusals
    mark_refused declined to make is the wrong thing to make evidence."""
    node = register(server)
    admit(server)
    peer_job(server, node, key="n2")
    monkeypatch.setattr(server.app.db, "mark_refused",
                        lambda delivery_id, evidence: None)
    code, body = server.call("/peers/block", {"host_id": PEER})
    assert code == 200
    assert body["revocation"]["refused"] == 0, (
        "the count claimed a refusal that never landed")


@pytest.mark.parametrize("field,value", [
    ("cert_pem", "x" * 200000),
    ("clock_offset_s", "not-a-number"),
    ("display_name", "y" * 5000),
    ("admitted_via", "z" * 5000),
    ("pin_first_seen", "yesterday"),
    ("last_seen", ["nope"]),
    ("admitted_at", {"a": 1}),
], ids=["huge-cert", "text-offset", "huge-name", "huge-via",
        "text-pin-time", "list-last-seen", "dict-admitted-at"])
def test_new3_known_fields_are_validated_on_the_READ_path(tmp_path, field,
                                                          value):
    """`_coerce_record` rejected unknown fields, then copied seven KNOWN
    ones verbatim. Slice 3 reads cert_pem to build a trust decision."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(PEER, PEER_FP)
    path = os.path.join(directory, cp.PEERS_FILE)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["peers"][PEER][field] = value
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    reloaded = cp.PeerStore(directory)
    assert reloaded.unreadable, (
        f"a peer record carrying {field}={type(value).__name__} loaded "
        f"clean and would be written straight back out")
    assert reloaded.authorize_peer(PEER, PEER_FP).allowed is False


def test_new5_the_killswitch_can_be_engaged_while_the_store_is_unreadable(
        tmp_path):
    """Measured recovery routes were {admit, block, forget, killswitch}
    = all refused. The operator could not even engage the emergency
    stop, and the only fix was hand-editing a host-private file."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(PEER, PEER_FP)
    with open(os.path.join(directory, cp.PEERS_FILE), "w",
              encoding="utf-8") as f:
        f.write("{ truncated")
    broken = cp.PeerStore(directory)
    # engaging is already true in effect, and must report so rather than
    # raising at the operator
    assert broken.set_killswitch(True, reason="incident")["engaged"] is True
    # ... and DISENGAGING must still refuse: that would be a real change
    with pytest.raises(cp.PeerStoreUnreadable):
        broken.set_killswitch(False)


def test_new5_an_unreadable_store_can_be_quarantined_in_band(server):
    """In-band recovery: the operator must be able to get the host
    working again without hand-editing a host-private file."""
    admit(server)
    path = os.path.join(server.app.data_dir, cp.PEERS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ truncated")
    with server.app.lock:
        server.app.peers = cp.PeerStore(server.app.data_dir)

    code, body = server.call("/peers")
    assert body["peers_unreadable"]
    code, body = server.call("/peers/quarantine", {"confirm": True})
    assert code == 200, body
    assert body["quarantined"].endswith(".corrupt") or ".corrupt" in \
        body["quarantined"]
    assert os.path.exists(body["quarantined"]), (
        "the damaged file must be KEPT -- it is the only record of what "
        "was admitted")
    # the host is operable again, with an empty (not a guessed) store
    code, body = server.call("/peers")
    assert code == 200 and body["peers"] == [] and \
        body["peers_unreadable"] is None
    assert server.call("/peers/admit", {"host_id": PEER,
                                        "fingerprint": PEER_FP,
                                        "convoy_ids": [CONVOY]})[0] == 200


def test_new5_quarantine_refuses_a_READABLE_store(server):
    """It destroys membership. It may only ever run against a file the
    host has already refused to use."""
    admit(server)
    code, body = server.call("/peers/quarantine", {"confirm": True})
    assert code == 409 and body["reason"] == "peers_readable"
    assert server.call("/peers")[1]["peers"] != []


def test_new6_a_revocation_does_not_stall_the_whole_host(server):
    """300 jobs / 1 peer measured an 8.1-SECOND total host stall with the
    global lock held -- every route and the drain loop blocked."""
    import threading
    node = register(server)
    admit(server)
    jobs = 250
    delivery_ids = []
    with server.app.lock:
        for i in range(jobs):
            job, _ = server.app.db.create_job("n6-%d" % i, node["node_id"],
                                              "query_network", {}, CONVOY,
                                              origin_host_id=PEER,
                                              controller_id="peer:%s:c" % PEER)
            delivery_ids.append(job["delivery_id"])
    worst = [0.0]
    stop = threading.Event()

    def poll_lock():
        # The LOCK is the claim. A realistic monitor interval, not a
        # starvation fuzzer: hammering the store with concurrent reads
        # makes Windows os.replace lose its own retry race, which would
        # measure the filesystem rather than the lock discipline.
        while not stop.is_set():
            started = time.time()
            with server.app.lock:
                worst[0] = max(worst[0], time.time() - started)
            time.sleep(0.02)

    watcher = threading.Thread(target=poll_lock, daemon=True)
    watcher.start()
    code, body = server.call("/peers/block", {"host_id": PEER})
    stop.set()
    watcher.join(timeout=10)

    assert code == 200
    summary = body["revocation"]
    # Every queued job must actually transition -- but WHICH ACTOR
    # terminalises each one is a race this test must not assert. On a
    # stalled runner (windows-latest, 2026-08-18) the autonomous drain
    # lawfully refused 241 of the 250 at dispatch-time re-authorization
    # BEFORE the sweep snapshotted the store (already_terminal, state
    # 'refused'), and the sweep's read-then-CAS lost 8 more records to
    # that same race mid-write (mark_refused declined -> 'errors'). The
    # invariant is the OUTCOME, asserted on the STORE: every job ends
    # refused whoever won, nothing is left live, and the lock was never
    # held long.
    for name in ("left_queued", "left_in_flight", "left_running"):
        assert summary[name] == 0, (
            "revocation left live work behind: %r" % summary)
    with server.app.lock:
        end_states = {
            delivery_id: (server.app.db.get_job(delivery_id) or {}).get(
                "state")
            for delivery_id in delivery_ids}
    not_refused = {k: v for k, v in end_states.items() if v != "refused"}
    assert not not_refused, (
        "every job must END refused whichever actor won the race: "
        "%r ... (sweep summary %r)"
        % (dict(list(not_refused.items())[:8]), summary))
    # AND THE SUMMARY MUST ADD UP, which is what made the one CI failure
    # of this test so hard to read: it reported examined 250 against
    # buckets summing to 14 and said nothing about the other 236, so the
    # sharing violations it DID measure could not be told apart from
    # records the sweep simply never counted. If this ever goes red
    # again, the printed summary now names every state it saw.
    assert sum(summary[name] for name in
               ("refused", "errors", "left_in_flight", "left_running",
                "already_terminal", "left_queued", "unknown_state")
               ) == summary["examined"], (
        "the sweep lost records out of its own summary: %r" % summary)
    assert worst[0] < 1.0, (
        "the global lock was held for %.2fs by the revocation sweep -- it "
        "must not be held across O(jobs) file writes" % worst[0])


def test_new7_the_host_originable_states_constant_is_ENFORCED(tmp_path):
    """It was read by no production code: definition plus test
    assertions only. A tautology over a documentation constant is not
    evidence for the A-15 amendment."""
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    for verdict in ("running", "succeeded", "failed"):
        with pytest.raises(ValueError) as e:
            db._apply_state(job["delivery_id"], verdict,
                            host_originated=True)
        assert "A-15" in str(e.value)
    # and the three the host MAY originate still work
    assert db.mark_refused(job["delivery_id"], {"r": 1})["state"] == "refused"
    db.close()


def test_new7b_the_a15_guard_fires_by_STATE_not_by_the_flag(tmp_path):
    """The first cut of the guard fired only when host_originated=True
    was PASSED -- omitting the flag wrote a terminal 'succeeded' with
    verdict_source=host and no node provenance at all (measured). The
    guard now keys off the STATE: an execution verdict lands only with a
    node verdict_source, observed_at, and (for the poll path) the
    node-minted job id."""
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    did = job["delivery_id"]
    for kwargs in ({"verdict_source": "host"},        # the measured hole
                   {},                                # no source at all
                   {"verdict_source": "node"},        # not a node source
                   {"verdict_source": "node_sync"},   # missing observed_at
                   {"verdict_source": "node_poll",    # missing node_job_id
                    "observed_at": 1.0}):
        with pytest.raises(ValueError) as e:
            db._apply_state(did, "succeeded", result={"ok": True}, **kwargs)
        assert "A-15" in str(e.value)
    # ... and the record is untouched by all five refusals
    assert db.get_job(did)["state"] == "queued"
    # the two REAL node paths still land
    assert db.record_node_verdict(did, "done", "job_1234abcd", 2.0,
                                  result={"ok": True})["state"] == "succeeded"
    job2, _ = db.create_job("k2", "n", "query_network", {}, "cv")
    assert db.record_sync_result(job2["delivery_id"], False, 3.0,
                                 result={"err": 1})["state"] == "failed"
    db.close()


def test_new8_an_unadmitted_origin_is_reported_as_unadmitted(server):
    """`_authorize_origin` passed a None fingerprint, so authorize_peer
    hit the malformed-identity branch first and told an operator their
    perfectly valid 32-hex host_id was malformed."""
    node = register(server)
    with server.app.lock:
        job, _ = server.app.db.create_job(
            "n8", node["node_id"], "query_network", {}, CONVOY,
            origin_host_id=OTHER)
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 403 and body["peer_reason"] == cp.REASON_UNKNOWN
    assert "malformed" not in body["detail"], (
        "an operator debugging a stuck queue was told their host_id was "
        "malformed when it is fine: %s" % body["detail"])
    # Two correct phrasings: this server admitted NOBODY, so the store
    # file is ABSENT and the refusal says so (and defers); with a
    # present file the record-level "has not been admitted" fires.
    assert ("not been admitted" in body["detail"]
            or "is not admitted" in body["detail"]), body["detail"]


def test_new9_a_hand_edit_to_peers_json_is_honoured_not_clobbered(tmp_path):
    """denylist.json sits right next to it and IS hand-editable by
    design, which actively invites the mistake. Measured: the edit was
    ignored at runtime and then erased by the next write."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(PEER, PEER_FP)
    store.admit(OTHER, OTHER_FP)
    path = os.path.join(directory, cp.PEERS_FILE)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["peers"][PEER]["state"] = "blocked"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f)
    old = time.time() - 5
    os.utime(path, (old, old))

    assert store.authorize_peer(PEER, PEER_FP).reason == cp.REASON_BLOCKED, (
        "a hand edit to peers.json was ignored at runtime")
    store.admit(OTHER, OTHER_FP)        # the next write
    with open(path, encoding="utf-8") as f:
        again = json.load(f)
    assert again["peers"][PEER]["state"] == "blocked", (
        "the next write erased the operator's edit")


def test_new9_the_file_says_what_it_is(tmp_path):
    directory = str(tmp_path / "state")
    cp.PeerStore(directory).admit(PEER, PEER_FP)
    with open(os.path.join(directory, cp.PEERS_FILE), encoding="utf-8") as f:
        data = json.load(f)
    assert "_note" in data and "hand" in data["_note"].lower()


def test_new10_one_fingerprint_cannot_be_pinned_to_two_host_ids(tmp_path):
    """It quietly disables the pin_mismatch detection built on purpose:
    'key X is pinned to host_id Y' can no longer be said."""
    store = cp.PeerStore(str(tmp_path / "state"))
    store.admit(PEER, PEER_FP)
    with pytest.raises(cp.PeerError) as e:
        store.admit(OTHER, PEER_FP)
    assert e.value.reason == "fingerprint_already_pinned"
    assert PEER in e.value.detail
    # forgetting the first frees the key -- a real key move, made explicit
    store.forget(PEER)
    assert store.admit(OTHER, PEER_FP)["state"] == cp.PEER_ADMITTED


def test_minor_f_the_burn_vs_defer_reason_is_stated_correctly():
    """The comment justified burn-vs-defer by 'reversibility', but
    block/forget/observe are all reversible via /peers/admit. The
    behaviour is right; the stated reason was false."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "convoy_hostapp.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    marker = source[source.index("MEMBERSHIP DECISION"):][:1400].lower()
    assert "re-admitting" in marker or "re-admission" in marker, (
        "the real distinction -- a membership decision was taken, and "
        "re-admitting does not resurrect terminalised work -- must be "
        "the one written down")


# =====================================================================
# NEW 11 -- the revocation summary must account for every record it saw
# =====================================================================

def _peer_record(server, key, node, operation="query_network"):
    """One delivery record owned by PEER, straight through the store.

    The submission ROUTE is exercised elsewhere; what these two tests
    need is a POPULATION in specific states, several of which no route
    can produce on demand.
    """
    job, _ = server.app.db.create_job(
        key, node["node_id"], operation, {}, CONVOY,
        origin_host_id=PEER, controller_id="peer:%s:c" % PEER)
    return job["delivery_id"]


def test_new11_every_examined_record_is_accounted_for(server):
    """THE ACCOUNTING INVARIANT. queued/dispatching/running were the only
    states this loop could count, so a record in any OTHER state fell
    out of the summary silently: a windows-latest run reported examined
    250 against buckets summing to 14, and neither the operator's
    response nor the `peer_revoked` audit line said where the remaining
    236 went. `examined` is evidence, and evidence that does not add up
    is worse than none -- it reads as a containment that was measured.
    """
    node = register(server)
    admit(server)
    with server.app.lock:
        db = server.app.db
        for index in range(3):                       # -> refused
            _peer_record(server, "acct-q%d" % index, node)
        for index in range(2):                       # -> left_in_flight
            db.claim_for_dispatch(
                _peer_record(server, "acct-d%d" % index, node))
        for index in range(2):                       # -> left_running
            db.record_node_verdict(
                _peer_record(server, "acct-r%d" % index, node),
                "running", node_job_id="job_0000ab%02d" % index,
                observed_at=100.0)
        # ...and all four TERMINALS, which the sweep could not count.
        db.record_node_verdict(_peer_record(server, "acct-ok", node),
                               "done", node_job_id="job_0000cc01",
                               observed_at=101.0)
        db.record_node_verdict(_peer_record(server, "acct-err", node),
                               "error", node_job_id="job_0000cc02",
                               observed_at=102.0)
        db.mark_indeterminate(_peer_record(server, "acct-ind", node),
                              {"reason": "the node could not be observed"})
        db.mark_refused(_peer_record(server, "acct-ref", node),
                        {"reason": "refused before this sweep ran"})

    code, body = server.call("/peers/block", {"host_id": PEER})
    assert code == 200
    summary = body["revocation"]

    buckets = ("refused", "errors", "left_in_flight", "left_running",
               "already_terminal", "left_queued", "unknown_state")
    accounted = sum(summary[name] for name in buckets)
    assert accounted == summary["examined"], (
        "%d of %d examined records are unaccounted for: %r"
        % (summary["examined"] - accounted, summary["examined"], summary))

    assert summary["examined"] == 11
    assert summary["refused"] == 3
    assert summary["left_in_flight"] == 2
    assert summary["left_running"] == 2
    assert summary["already_terminal"] == 4
    assert summary["unknown_state"] == 0
    # BY STATE, because 4 is a number and this is an answer. Note the
    # two senses of 'refused' sitting side by side and staying apart:
    # three records this sweep refused, one that already was.
    assert summary["untouched_states"] == {
        "succeeded": 1, "failed": 1, "indeterminate": 1, "refused": 1}


def test_new11_the_new_bucket_reaches_the_peer_revoked_audit_line(server):
    """The summary is spread into `peer_revoked` (**summary), and that
    line is the only account a later reader has of what a revocation
    contained. A bucket missing from it is a record missing from it.
    """
    node = register(server)
    admit(server)
    with server.app.lock:
        server.app.db.record_node_verdict(
            _peer_record(server, "acct-audit", node), "done",
            node_job_id="job_0000dd01", observed_at=103.0)

    code, _ = server.call("/peers/block", {"host_id": PEER})
    assert code == 200

    with server.app.lock:
        lines = [e for e in server.app.db.audit_tail(limit=600)
                 if e["event"] == "peer_revoked"]
    assert lines, "no peer_revoked line was written at all"
    detail = lines[-1]["detail"]
    assert detail["examined"] == 1
    assert detail["already_terminal"] == 1, (
        "the settled record is missing from the audit payload: %r" % detail)
    assert detail["untouched_states"] == {"succeeded": 1}


def test_new11_an_observe_only_narrowing_still_adds_up(server):
    """The other caller, and the arm it needs. observe_peer sweeps with
    mutating_only=True, and a queued READ is deliberately left queued --
    a right answer that is NOT 'contained', so it is counted rather than
    skipped out of the arithmetic.
    """
    node = register(server)
    admit(server)
    with server.app.lock:
        _peer_record(server, "acct-read", node, operation="query_network")

    code, body = server.call("/peers/observe", {"host_id": PEER})
    assert code == 200, body
    summary = body["revocation"]

    buckets = ("refused", "errors", "left_in_flight", "left_running",
               "already_terminal", "left_queued", "unknown_state")
    assert sum(summary[name] for name in buckets) == summary["examined"], (
        "an observe-only sweep lost a record out of its summary: %r"
        % (summary,))
    assert summary["examined"] == 1
    assert summary["left_queued"] == 1
    assert summary["refused"] == 0
    assert summary["untouched_states"] == {"queued": 1}
