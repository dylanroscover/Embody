"""Focused HostApp contracts for implicit controller operation claims.

These tests deliberately exercise the durable job boundary instead of only
the pure LeaseRegistry.  A claim is useful only if admission, dispatch,
polling, cancellation, and peer revocation all agree on its lifetime.
"""

from pathlib import Path

import convoy_hostapp as hostapp
import convoy_peers as peers
import pytest


CONVOY = "studio"


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    project = tmp_path / "project"
    project.mkdir()
    forwarded = []

    def forward(port, operation, arguments):
        forwarded.append((port, operation, arguments))
        return {"ok": True, "result": {"operation": operation}}

    app = hostapp.HostApp(
        str(tmp_path / "state"), now=clock, forwarder=forward,
        artifact_cache_path=str(tmp_path / "artifacts"))
    code, node = app.register_node({
        "project_root": project.as_posix(),
        "convoy_id": CONVOY,
        "comp_path": "/Embody",
        "runtime_id": "rt_claims",
        "envoy_port": 9800,
        "envoy_ready": True,
    })
    assert code == 200, node
    try:
        yield app, node, clock, forwarded
    finally:
        app.stop_drain_loop()
        app.db.close()


def create_job(app, node, key, controller_id, operation="set_op_position"):
    arguments = (
        {"parent_path": "/"}
        if operation == "query_network"
        else {"op_path": "/project1/example", "x": 0, "y": 0}
    )
    code, body = app.create_job({
        "idempotency_key": key,
        "node_id": node["node_id"],
        "controller_id": controller_id,
        "operation": operation,
        "arguments": arguments,
    })
    return code, body


def claims(app):
    return sorted(
        app.leases.operation_claims(),
        key=lambda row: row["delivery_id"],
    )


def mark_running(app, job, node_job_id="job_1234abcd"):
    claimed = app.db.claim_for_dispatch(job["delivery_id"])
    assert claimed is not None
    running = app.db.record_node_verdict(
        job["delivery_id"], "running", node_job_id=node_job_id,
        observed_at=app._now(), result={"job_id": node_job_id})
    assert running["state"] == "running"
    return running


def test_claim_serializes_controllers_but_allows_owner_concurrency_and_reads(
        rig):
    app, node, _clock, _forwarded = rig

    code, first = create_job(app, node, "writer-a-1", "controller-a")
    assert code == 200, first
    code, second = create_job(app, node, "writer-a-2", "controller-a")
    assert code == 200, second

    # Reads never participate in writer exclusion, even from another
    # controller, and therefore mint no implicit claim.
    code, read = create_job(
        app, node, "reader-b", "controller-b", operation="query_network")
    assert code == 200, read

    code, blocked = create_job(app, node, "writer-b", "controller-b")
    assert code == 409
    assert blocked["reason"] == "node_leased"
    assert blocked["holder"] == "controller-a"

    rows = claims(app)
    assert {row["delivery_id"] for row in rows} == {
        first["job"]["delivery_id"], second["job"]["delivery_id"]}
    assert {row["controller_id"] for row in rows} == {"controller-a"}


def test_terminal_dispatch_releases_only_the_implicit_claim(rig):
    app, node, _clock, forwarded = rig
    node_id = node["node_id"]
    code, explicit = app.acquire_lease({
        "controller_id": "controller-a",
        "node_id": node_id,
        "mode": "exclusive",
        "ttl_s": 600,
    })
    assert code == 200, explicit
    code, created = create_job(app, node, "terminal-a", "controller-a")
    assert code == 200, created
    delivery_id = created["job"]["delivery_id"]
    assert [row["delivery_id"] for row in claims(app)] == [delivery_id]

    code, resolved = app.dispatch_job(delivery_id)
    assert code == 200, resolved
    assert resolved["job"]["state"] == "succeeded"
    assert forwarded and forwarded[0][1] == "set_op_position"
    assert claims(app) == []

    # Completing an operation must never release an explicit user lease
    # held independently by the same controller.
    lease_rows = app.list_leases()[1]["leases"]
    assert lease_rows == [{
        "node_id": node_id,
        "controller_id": "controller-a",
        "mode": "exclusive",
        "expires": 1600.0,
    }]
    code, blocked = create_job(app, node, "terminal-b", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"


def test_terminal_async_poll_releases_the_implicit_claim_immediately(rig):
    app, node, _clock, _forwarded = rig
    code, created = create_job(app, node, "poll-terminal-a", "controller-a")
    assert code == 200, created
    node_job_id = "job_9876abcd"
    mark_running(app, created["job"], node_job_id=node_job_id)
    app.forwarder = lambda _port, _operation, _arguments: {
        "ok": True,
        "result": {
            "id": node_job_id,
            "kind": "run_tests",
            "status": "done",
            "result": {"passed": True},
        },
    }

    code, resolved = app.poll_job(created["job"]["delivery_id"])
    assert code == 200, resolved
    assert resolved["job"]["state"] == "succeeded"
    assert claims(app) == []


def test_cancelling_queued_job_releases_claim_for_next_controller(rig):
    app, node, _clock, _forwarded = rig
    code, created = create_job(app, node, "cancel-a", "controller-a")
    assert code == 200, created
    delivery_id = created["job"]["delivery_id"]

    code, cancelled = app.cancel_host_job({"delivery_id": delivery_id})
    assert code == 200, cancelled
    assert cancelled["cancelled"] is True
    assert cancelled["job"]["state"] == "refused"
    assert claims(app) == []

    code, admitted = create_job(app, node, "cancel-b", "controller-b")
    assert code == 200, admitted


def test_dead_controller_frees_queued_claim_but_old_job_stays_deferred(rig):
    app, node, clock, _forwarded = rig
    code, old = create_job(app, node, "stale-a", "controller-a")
    assert code == 200, old
    old_id = old["job"]["delivery_id"]

    clock.advance(61)
    code, new = create_job(app, node, "fresh-b", "controller-b")
    assert code == 200, new
    assert {row["delivery_id"] for row in claims(app)} == {
        new["job"]["delivery_id"]}

    code, deferred = app.dispatch_job(old_id)
    assert code == 409
    assert deferred["reason"] == "controller_heartbeat_required"
    assert app.db.get_job(old_id)["state"] == "queued"


def test_dead_controller_does_not_free_a_running_writer(rig):
    app, node, clock, _forwarded = rig
    code, created = create_job(app, node, "running-a", "controller-a")
    assert code == 200, created
    mark_running(app, created["job"])

    clock.advance(61)
    code, blocked = create_job(app, node, "running-b", "controller-b")
    assert code == 409
    assert blocked["reason"] == "node_leased"
    assert blocked["holder"] == "controller-a"
    assert claims(app)[0]["delivery_id"] == created["job"]["delivery_id"]


def test_unchanged_peer_poll_is_an_idle_controller_heartbeat(rig):
    app, node, clock, _forwarded = rig
    code, created = create_job(app, node, "poll-a", "controller-a")
    assert code == 200, created
    job = created["job"]

    # Exercise the unchanged-cursor branch: the heartbeat must happen before
    # the fast response, not only when a full job body is returned.
    clock.advance(59)
    code, view = app.peer_job_view(
        app.host_id, job["delivery_id"], since=job["updated"])
    assert code == 200, view
    assert view["changed"] is False

    clock.advance(59)
    assert app.leases.controller_alive("controller-a", clock())
    controller = app.leases.live_controllers(clock())[0]
    assert controller["selected_node_id"] == node["node_id"]

    code, blocked = create_job(app, node, "poll-b", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"
    assert blocked["holder"] == "controller-a"


def test_peer_revocation_refuses_queued_work_and_drops_its_claim(rig):
    app, node, _clock, _forwarded = rig
    peer_host_id = "ab" * 16
    controller_id = peers.namespaced_controller(peer_host_id, "editor")
    job, created = app.db.create_job(
        "peer-revoked", node["node_id"], "set_op_position",
        {"op_path": "/project1/example", "x": 0, "y": 0}, CONVOY,
        origin_host_id=peer_host_id, controller_id=controller_id)
    assert created is True
    with app.lock:
        assert app._ensure_operation_claim_locked(job, "test") is None
    assert claims(app)[0]["controller_id"] == controller_id

    summary = app._revoke_peer_work(peer_host_id, cause="blocked")
    assert summary["refused"] == 1
    assert summary["leases_released"] == 1
    assert app.db.get_job(job["delivery_id"])["state"] == "refused"
    assert claims(app) == []

    code, admitted = create_job(app, node, "after-revoke", "controller-b")
    assert code == 200, admitted


def test_peer_revocation_keeps_running_writer_exclusion(rig):
    app, node, _clock, _forwarded = rig
    peer_host_id = "cd" * 16
    controller_id = peers.namespaced_controller(peer_host_id, "editor")
    job, created = app.db.create_job(
        "peer-running", node["node_id"], "set_op_position",
        {"op_path": "/project1/example", "x": 0, "y": 0}, CONVOY,
        origin_host_id=peer_host_id, controller_id=controller_id)
    assert created is True
    with app.lock:
        assert app._ensure_operation_claim_locked(job, "test") is None
    mark_running(app, job)

    summary = app._revoke_peer_work(peer_host_id, cause="blocked")
    assert summary["left_running"] == 1
    assert app.db.get_job(job["delivery_id"])["state"] == "running"
    assert claims(app)[0]["delivery_id"] == job["delivery_id"]

    code, blocked = create_job(app, node, "during-revoked-run", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"
    assert blocked["holder"] == controller_id


def test_peer_revocation_keeps_in_flight_writer_exclusion(rig):
    app, node, _clock, _forwarded = rig
    peer_host_id = "ef" * 16
    controller_id = peers.namespaced_controller(peer_host_id, "editor")
    job, created = app.db.create_job(
        "peer-dispatching", node["node_id"], "set_op_position",
        {"op_path": "/project1/example", "x": 0, "y": 0}, CONVOY,
        origin_host_id=peer_host_id, controller_id=controller_id)
    assert created is True
    with app.lock:
        assert app._ensure_operation_claim_locked(job, "test") is None
    assert app.db.claim_for_dispatch(job["delivery_id"])[
        "state"] == "dispatching"

    summary = app._revoke_peer_work(peer_host_id, cause="blocked")
    assert summary["left_in_flight"] == 1
    assert app.db.get_job(job["delivery_id"])["state"] == "dispatching"
    assert claims(app)[0]["delivery_id"] == job["delivery_id"]

    code, blocked = create_job(app, node, "during-revoked-flight",
                               "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"


def test_peer_revocation_preserves_claim_when_job_record_is_unreadable(
        rig, monkeypatch):
    app, node, clock, _forwarded = rig
    peer_host_id = "12" * 16
    controller_id = peers.namespaced_controller(peer_host_id, "editor")
    job, created = app.db.create_job(
        "peer-unreadable", node["node_id"], "set_op_position",
        {"op_path": "/project1/example", "x": 0, "y": 0}, CONVOY,
        origin_host_id=peer_host_id, controller_id=controller_id)
    assert created is True
    with app.lock:
        app._note_peer_controller(peer_host_id, controller_id)
        assert app._ensure_operation_claim_locked(job, "test") is None
    mark_running(app, job)
    delivery_id = job["delivery_id"]
    real_scan = app.db.scan_jobs
    real_get = app.db.get_job

    # The revocation scan knows only the unreadable delivery id. Controller
    # attribution survives through the bounded peer-controller map, so a
    # release still occurs and must explicitly preserve this unknown claim.
    monkeypatch.setattr(app.db, "scan_jobs", lambda: ([], [delivery_id]))
    monkeypatch.setattr(
        app.db, "get_job",
        lambda candidate: None if candidate == delivery_id
        else real_get(candidate))
    summary = app._revoke_peer_work(peer_host_id, cause="blocked")
    assert summary["unreadable"] == 1
    assert claims(app)[0]["delivery_id"] == delivery_id

    # Once readable again, durable reconciliation detaches the running claim
    # from the revoked controller and keeps it as the node's writer fence.
    monkeypatch.setattr(app.db, "scan_jobs", real_scan)
    monkeypatch.setattr(app.db, "get_job", real_get)
    code, blocked = create_job(
        app, node, "after-unreadable-revoke", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"
    assert blocked["holder"] == controller_id
    assert not app.leases.controller_alive(controller_id, clock())


def test_peer_revocation_scan_failure_preserves_claims_and_global_fence(
        rig, monkeypatch):
    app, node, _clock, _forwarded = rig
    peer_host_id = "34" * 16
    controller_id = peers.namespaced_controller(peer_host_id, "editor")
    job, created = app.db.create_job(
        "peer-scan-failure", node["node_id"], "set_op_position",
        {"op_path": "/project1/example", "x": 0, "y": 0}, CONVOY,
        origin_host_id=peer_host_id, controller_id=controller_id)
    assert created is True
    with app.lock:
        app._note_peer_controller(peer_host_id, controller_id)
        assert app._ensure_operation_claim_locked(job, "test") is None
    mark_running(app, job)
    delivery_id = job["delivery_id"]
    real_scan = app.db.scan_jobs
    monkeypatch.setattr(
        app.db, "scan_jobs",
        lambda: (_ for _ in ()).throw(OSError("scan unavailable")))

    summary = app._revoke_peer_work(peer_host_id, cause="blocked")
    assert summary["errors"] == 1
    assert {row["delivery_id"] for row in claims(app)} == {delivery_id}
    assert app._operation_job_scan_failed is True

    # A total scan failure has unknown node scope: writer rights fail closed
    # everywhere, while a shared/read-only lease and reads remain available.
    code, observer = app.register_node({
        "project_root": "/Work/observer",
        "convoy_id": CONVOY,
        "comp_path": "/ObserverEmbody",
    })
    assert code == 200, observer
    code, exclusive = app.acquire_lease({
        "controller_id": "controller-b",
        "node_id": observer["node_id"],
        "mode": "exclusive",
    })
    assert code == 503
    assert exclusive["reason"] == "operation_state_unreadable"
    code, shared = app.acquire_lease({
        "controller_id": "controller-b",
        "node_id": observer["node_id"],
        "mode": "shared",
    })
    assert code == 200, shared
    code, read = create_job(
        app, node, "peer-scan-read", "controller-b",
        operation="query_network")
    assert code == 200, read

    monkeypatch.setattr(app.db, "scan_jobs", real_scan)
    code, blocked = create_job(
        app, node, "peer-scan-recovered", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"
    assert blocked["holder"] == controller_id
    assert app._operation_job_scan_failed is False


def test_killswitch_scan_failure_preserves_claims_and_global_fence(
        rig, monkeypatch):
    app, node, _clock, _forwarded = rig
    peer_host_id = "56" * 16
    controller_id = peers.namespaced_controller(peer_host_id, "editor")
    job, created = app.db.create_job(
        "killswitch-scan-failure", node["node_id"], "set_op_position",
        {"op_path": "/project1/example", "x": 0, "y": 0}, CONVOY,
        origin_host_id=peer_host_id, controller_id=controller_id)
    assert created is True
    with app.lock:
        app._note_peer_controller(peer_host_id, controller_id)
        assert app._ensure_operation_claim_locked(job, "test") is None
    mark_running(app, job)
    delivery_id = job["delivery_id"]
    real_scan = app.db.scan_jobs
    monkeypatch.setattr(app.peers, "peers", lambda: [
        {"host_id": peer_host_id}])
    monkeypatch.setattr(
        app.db, "scan_jobs",
        lambda: (_ for _ in ()).throw(OSError("scan unavailable")))

    code, stopped = app.set_lan_killswitch({
        "engaged": True, "reason": "test scan failure"})
    assert code == 200, stopped
    assert {row["delivery_id"] for row in claims(app)} == {delivery_id}
    assert app._operation_job_scan_failed is True

    code, observer = app.register_node({
        "project_root": "/Work/killswitch-observer",
        "convoy_id": CONVOY,
        "comp_path": "/ObserverEmbody",
    })
    assert code == 200, observer
    code, exclusive = app.acquire_lease({
        "controller_id": "controller-b",
        "node_id": observer["node_id"],
        "mode": "exclusive",
    })
    assert code == 503
    assert exclusive["reason"] == "operation_state_unreadable"
    code, shared = app.acquire_lease({
        "controller_id": "controller-b",
        "node_id": observer["node_id"],
        "mode": "shared",
    })
    assert code == 200, shared
    code, read = create_job(
        app, node, "killswitch-scan-read", "controller-b",
        operation="query_network")
    assert code == 200, read

    monkeypatch.setattr(app.db, "scan_jobs", real_scan)
    code, blocked = create_job(
        app, node, "killswitch-scan-recovered", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"
    assert blocked["holder"] == controller_id
    assert app._operation_job_scan_failed is False


def test_controller_heartbeat_cannot_expire_a_running_claim(rig):
    app, node, clock, _forwarded = rig
    code, created = create_job(app, node, "long-running-a", "controller-a")
    assert code == 200, created
    mark_running(app, created["job"])

    # Operation claims use the registry's one-hour hard TTL.  An ordinary
    # controller heartbeat after that point must reconcile the durable
    # running job before opportunistic GC; otherwise the heartbeat itself
    # erases the writer fence.
    clock.advance(3601)
    code, heartbeat = app.heartbeat_controller({
        "controller_id": "controller-a"})
    assert code == 200, heartbeat
    assert claims(app)[0]["delivery_id"] == created["job"]["delivery_id"]

    code, blocked = create_job(app, node, "long-running-b", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"


def test_expired_running_claim_blocks_explicit_lease_acquisition(rig):
    app, node, clock, _forwarded = rig
    code, created = create_job(app, node, "lease-running-a", "controller-a")
    assert code == 200, created
    mark_running(app, created["job"])
    clock.advance(3601)

    code, blocked = app.acquire_lease({
        "controller_id": "controller-b",
        "node_id": node["node_id"],
        "mode": "exclusive",
    })
    assert code == 409
    assert blocked["reason"] == "node_leased"
    assert blocked["holder"] == "controller-a"


def test_passive_lease_listing_cannot_expire_a_running_claim(rig):
    app, node, clock, _forwarded = rig
    code, created = create_job(app, node, "list-running-a", "controller-a")
    assert code == 200, created
    mark_running(app, created["job"])
    clock.advance(3601)

    code, listing = app.list_leases()
    assert code == 200, listing
    assert claims(app)[0]["delivery_id"] == created["job"]["delivery_id"]

    code, blocked = create_job(app, node, "list-running-b", "controller-b")
    assert code == 409 and blocked["reason"] == "node_leased"


def test_unreadable_durable_job_does_not_fail_open_its_claim(
        rig, monkeypatch):
    app, node, _clock, _forwarded = rig
    code, created = create_job(app, node, "unreadable-a", "controller-a")
    assert code == 200, created
    mark_running(app, created["job"])
    delivery_id = created["job"]["delivery_id"]
    assert app.db.job_file_exists(delivery_id)

    # HostStore intentionally returns None for both absent and unreadable.
    # The existence probe is the required distinction: unreadable work may
    # have run and therefore must retain writer exclusion.
    monkeypatch.setattr(app.db, "get_job", lambda _delivery_id: None)
    with app.lock:
        app._reconcile_operation_claims_locked()
    assert claims(app)[0]["delivery_id"] == delivery_id


def test_host_restart_restores_running_writer_claim(tmp_path):
    clock = Clock()
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    first = hostapp.HostApp(
        str(state), now=clock,
        artifact_cache_path=str(tmp_path / "artifacts-first"))
    code, node = first.register_node({
        "project_root": project.as_posix(),
        "convoy_id": CONVOY,
        "comp_path": "/Embody",
        "runtime_id": "rt_restart",
        "envoy_port": 9800,
        "envoy_ready": True,
    })
    assert code == 200, node
    code, created = create_job(first, node, "restart-a", "controller-a")
    assert code == 200, created
    mark_running(first, created["job"])
    first.db.close()

    second = hostapp.HostApp(
        str(state), now=clock,
        artifact_cache_path=str(tmp_path / "artifacts-second"))
    try:
        rows = claims(second)
        assert len(rows) == 1
        assert rows[0]["delivery_id"] == created["job"]["delivery_id"]
        code, blocked = create_job(
            second, node, "restart-b", "controller-b")
        assert code == 409 and blocked["reason"] == "node_leased"
    finally:
        second.db.close()


def test_cold_boot_unreadable_job_blocks_writes_then_recovers(tmp_path):
    clock = Clock()
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    first = hostapp.HostApp(
        str(state), now=clock,
        artifact_cache_path=str(tmp_path / "artifacts-first"))
    code, node = first.register_node({
        "project_root": project.as_posix(),
        "convoy_id": CONVOY,
        "comp_path": "/Embody",
        "runtime_id": "rt_unreadable_restart",
        "envoy_port": 9800,
        "envoy_ready": True,
    })
    assert code == 200, node
    code, created = create_job(
        first, node, "boot-unreadable-a", "controller-a")
    assert code == 200, created
    running = mark_running(first, created["job"])
    delivery_id = running["delivery_id"]
    job_path = Path(first.db.jobs_dir) / f"{delivery_id}.json"
    original = job_path.read_text(encoding="utf-8")
    first.db.close()
    job_path.write_text("{not valid json", encoding="utf-8")

    second = hostapp.HostApp(
        str(state), now=clock,
        artifact_cache_path=str(tmp_path / "artifacts-second"))
    try:
        assert delivery_id in second._unreadable_operation_jobs

        # With no parseable node attribution, every mutation fails closed;
        # an exclusive lease is equally a writer right and must also fail.
        code, exclusive = second.acquire_lease({
            "controller_id": "controller-b",
            "node_id": node["node_id"],
            "mode": "exclusive",
        })
        assert code == 503
        assert exclusive["reason"] == "operation_state_unreadable"

        # Shared leases and reads remain useful for diagnosis. Recovery must
        # later permit this pre-existing reader to coexist with the restored
        # in-flight writer claim; it must not strand the global fence.
        code, shared = second.acquire_lease({
            "controller_id": "controller-b",
            "node_id": node["node_id"],
            "mode": "shared",
        })
        assert code == 200, shared
        code, blocked = create_job(
            second, node, "boot-unreadable-b", "controller-b")
        assert code == 503
        assert blocked["reason"] == "operation_state_unreadable"
        code, read = create_job(
            second, node, "boot-unreadable-read", "controller-b",
            operation="query_network")
        assert code == 200, read

        # Repairing the durable record converts the global fence into the
        # exact restored running claim. A different writer remains blocked
        # until that original operation reaches a terminal verdict.
        job_path.write_text(original, encoding="utf-8")
        code, targeted = create_job(
            second, node, "boot-recovered-b", "controller-b")
        assert code == 409
        assert targeted["reason"] == "node_leased"
        assert targeted["holder"] == "controller-a"
        assert second._unreadable_operation_jobs == set()
        assert claims(second)[0]["delivery_id"] == delivery_id
        assert any(
            row.get("controller_id") == "controller-b"
            and row.get("mode") == "shared"
            for row in second.list_leases()[1]["leases"])

        second.db.record_node_verdict(
            delivery_id, "done", node_job_id=running["node_job_id"],
            observed_at=clock(), result={"passed": True})
        code, released = second.release_lease({
            "controller_id": "controller-b",
            "node_id": node["node_id"],
        })
        assert code == 200 and released["released"] is True
        code, admitted = create_job(
            second, node, "boot-terminal-c", "controller-c")
        assert code == 200, admitted
    finally:
        second.db.close()
