"""Host STORE contracts: durable jobs, idempotency, audit, restart.

Plain JSON files (see convoy_hoststore): measured at 109 KB for 500
projects vs 120 KB for the SQLite it replaced, so the database
bought nothing at the real target scale and cost four review
defects in machinery this does not need.
"""

import json
import os

import pytest

import convoy_hoststore as hdb
import convoy_identity as ci

# A real 128-bit hex host id ("h" is not a hex digit).
VALID_HOST = "ab" * 16


def accepted_timing(at=100.0, remaining=30.0, monotonic_deadline=40.0):
    return {
        "request_deadline_unix": at + remaining,
        "accepted_at_unix": at,
        "accepted_remaining_s": remaining,
        "accepted_expires_unix": at + remaining,
        "accepted_deadline_monotonic": monotonic_deadline,
    }


@pytest.fixture
def db(tmp_path):
    d = hdb.HostStore(str(tmp_path / "state"))
    yield d
    d.close()


# -- store versioning -------------------------------------------------

def test_fresh_store_writes_its_version(tmp_path):
    path = str(tmp_path / "state")
    st = hdb.HostStore(path)
    st.close()
    with open(os.path.join(path, hdb.HOST_FILE), encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] == hdb.STORE_VERSION
    assert ci.is_valid_id(data["host_id"])


def test_reopening_keeps_the_host_id(tmp_path):
    path = str(tmp_path / "state")
    first = hdb.HostStore(path)
    host_id = first.host_id()
    first.close()
    second = hdb.HostStore(path)
    assert second.host_id() == host_id, "host_id is minted ONCE per machine"
    second.close()


def test_newer_store_version_refuses(tmp_path):
    """A downgraded host app must not scribble on newer state."""
    path = str(tmp_path / "state")
    hdb.HostStore(path).close()
    target = os.path.join(path, hdb.HOST_FILE)
    with open(target, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = hdb.STORE_VERSION + 5
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f)

    with pytest.raises(hdb.StoreTooNew) as e:
        hdb.HostStore(path)
    assert e.value.disk_version == hdb.STORE_VERSION + 5
    assert "upgrade" in str(e.value).lower()


def test_corrupt_host_file_refuses_rather_than_reminting(tmp_path):
    """Identity is not guessable: a corrupt host file must NOT silently
    mint a new host_id and orphan every peer relationship."""
    path = str(tmp_path / "state")
    hdb.HostStore(path).close()
    with open(os.path.join(path, hdb.HOST_FILE), "w", encoding="utf-8") as f:
        f.write("{ truncated")
    with pytest.raises(RuntimeError) as e:
        hdb.HostStore(path)
    assert "unreadable" in str(e.value)


def test_state_is_human_readable_json(tmp_path):
    """The whole point of dropping SQLite: you can read and fix it."""
    path = str(tmp_path / "state")
    st = hdb.HostStore(path)
    directory, _ = st.load_directory()
    st.save_node(directory.register("/Work/A", "/Embody", "cv"))
    st.close()
    with open(os.path.join(path, hdb.HOST_FILE), encoding="utf-8") as f:
        text = f.read()
    assert '"nodes"' in text and "/Embody" in text
    json.loads(text)


# -- node persistence -------------------------------------------------

def test_directory_survives_restart_but_legacy_approval_does_not(tmp_path):
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _foreign = db1.load_directory()
    a = directory.register("/Work/A", "/Embody", "cv")
    b = directory.register("/Work/B", "/Embody", "cv")
    directory.approve_td_python(b["node_id"])
    db1.save_node(a)
    db1.save_node(b)
    db1.close()

    db2 = hdb.HostStore(path)
    restored, _foreign = db2.load_directory()
    assert {n["node_id"] for n in restored.nodes()} == \
        {a["node_id"], b["node_id"]}
    assert restored.lookup(a["node_id"])["td_python_approved"] is False
    assert restored.lookup(b["node_id"])["td_python_approved"] is False, (
        "HostStore must not persist code authority; PolicyStore owns it")
    # And the pair mapping still resolves: re-registering is stable.
    assert restored.register("/Work/A", "/Embody", "cv")["node_id"] == \
        a["node_id"]
    db2.close()


def test_node_location_membership_and_metadata_survive_restart(tmp_path):
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _foreign = db1.load_directory()
    node = directory.register(
        "/Work/A", "/Embody", "cv", runtime_id="rt_live",
        envoy_port=9981, node_discriminator="nd_" + "1" * 32)
    directory.set_enabled(node["node_id"], False)
    directory.set_metadata(node["node_id"], {
        "toe_path": "/Work/A/show.toe",
        "toe_name": "show.toe",
        "node_name": "render / show",
        "hostname": "render",
        "process_id": 4312,
        "embody_version": "6.0.178",
        "touchdesigner_version": "2025.30000",
    })
    db1.save_node(directory.lookup(node["node_id"]))
    db1.close()

    with open(os.path.join(path, hdb.HOST_FILE), encoding="utf-8") as f:
        disk = json.load(f)["nodes"][node["node_id"]]
    assert disk["node_discriminator"] == "nd_" + "1" * 32
    assert disk["enabled"] is False
    assert disk["metadata"]["node_name"] == "render / show"
    assert "runtime_id" not in disk
    assert "envoy_port" not in disk

    db2 = hdb.HostStore(path)
    restored, quarantined = db2.load_directory()
    assert quarantined == []
    record = restored.lookup(node["node_id"])
    assert record["node_discriminator"] == "nd_" + "1" * 32
    assert record["enabled"] is False
    assert record["metadata"]["process_id"] == 4312
    assert record["envoy_port"] is None
    assert record["runtime_id"] != "rt_live"
    assert restored.lookup_location(
        "/Work/A", "/Embody",
        node_discriminator="nd_" + "1" * 32)["node_id"] == node["node_id"]
    db2.close()


def test_candidate_binding_survives_restart_without_becoming_authority(
        tmp_path):
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _ = db1.load_directory()
    node = directory.register(
        "/Work/Candidate", "/Embody", "cv_provisional",
        binding_state="candidate")
    db1.save_node(node)
    db1.close()

    with open(os.path.join(path, hdb.HOST_FILE), encoding="utf-8") as f:
        disk = json.load(f)["nodes"][node["node_id"]]
    assert disk["binding_state"] == "candidate"

    db2 = hdb.HostStore(path)
    restored, quarantined = db2.load_directory()
    assert quarantined == []
    record = restored.lookup(node["node_id"])
    assert record["convoy_id"] == "cv_provisional"
    assert record["binding_state"] == "candidate"
    db2.close()


def test_hoststore_rebinds_all_candidates_in_one_write_and_restart_replays_it(
        tmp_path, monkeypatch):
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _ = db1.load_directory()
    first = directory.register(
        "/Work/A", "/Embody", "cv_b", binding_state="candidate",
        runtime_id="rt_a", envoy_port=9981)
    second = directory.register(
        "/Work/B", "/Embody", "cv_c", binding_state="candidate",
        runtime_id="rt_b", envoy_port=9982)
    established = directory.register(
        "/Work/Existing", "/Embody", "cv_existing")
    directory.set_metadata(first["node_id"], {"node_name": "A"})
    directory.set_enabled(second["node_id"], False)
    db1.save_nodes(directory.nodes())

    writes = []
    original_write = db1._write_host

    def counted_write(data=None):
        writes.append(1)
        return original_write(data)

    monkeypatch.setattr(db1, "_write_host", counted_write)
    changed = db1.rebind_candidates(directory, "cv_authoritative")

    assert len(writes) == 1, "all local candidates use one atomic replace"
    assert {row["node_id"] for row in changed} == {
        first["node_id"], second["node_id"]}
    assert directory.lookup(first["node_id"])["runtime_id"] == "rt_a"
    assert directory.lookup(first["node_id"])["envoy_port"] == 9981
    assert directory.lookup(first["node_id"])["metadata"] == {
        "node_name": "A"}
    assert directory.lookup(second["node_id"])["enabled"] is False
    assert established["convoy_id"] == "cv_existing"
    assert established["binding_state"] == "established"
    db1.close()

    db2 = hdb.HostStore(path)
    restored, quarantined = db2.load_directory()
    assert quarantined == []
    for node in (first, second):
        record = restored.lookup(node["node_id"])
        assert record["convoy_id"] == "cv_authoritative"
        assert record["binding_state"] == "established"
    existing = restored.lookup(established["node_id"])
    assert existing["convoy_id"] == "cv_existing"
    assert existing["binding_state"] == "established"
    db2.close()


def test_failed_batch_write_leaves_store_and_directory_candidates_unchanged(
        tmp_path, monkeypatch):
    path = str(tmp_path / "state")
    db = hdb.HostStore(path)
    directory, _ = db.load_directory()
    node = directory.register(
        "/Work/A", "/Embody", "cv_candidate",
        binding_state="candidate")
    db.save_node(node)
    before_state = json.loads(json.dumps(db._state))

    def fail_write(_data=None):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(db, "_write_host", fail_write)
    with pytest.raises(OSError, match="simulated disk failure"):
        db.rebind_candidates(directory, "cv_authoritative")

    assert db._state == before_state
    assert directory.lookup(node["node_id"])["convoy_id"] == "cv_candidate"
    assert directory.lookup(node["node_id"])["binding_state"] == "candidate"
    db.close()


def test_legacy_node_row_replays_with_safe_additive_defaults(tmp_path):
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    host_id = db1.host_id()
    node_id = "cd" * 16
    db1._state["nodes"][node_id] = {
        "project_root": "/Work/Legacy",
        "host_id": host_id,
        "convoy_id": "cv",
        "comp_path": "/Embody",
        "td_python_approved": True,
        "first_seen": 1.0,
        "last_seen": 2.0,
    }
    db1._write_host()
    db1.close()

    db2 = hdb.HostStore(path)
    restored, quarantined = db2.load_directory()
    record = restored.lookup(node_id)
    assert quarantined == []
    assert record["node_discriminator"] == ""
    assert record["binding_state"] == "established"
    assert record["enabled"] is True
    assert record["metadata"] == {}
    assert record["td_python_approved"] is False
    assert record["envoy_port"] is None
    db2.close()


def test_invalid_stored_metadata_drops_decoration_not_node_identity(tmp_path):
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _ = db1.load_directory()
    node = directory.register("/Work/A", "/Embody", "cv")
    db1.save_node(node)
    db1._state["nodes"][node["node_id"]]["metadata"] = {
        "controller_id": "authority-shaped"
    }
    db1._write_host()
    db1.close()

    db2 = hdb.HostStore(path)
    restored, quarantined = db2.load_directory()
    assert quarantined == []
    assert restored.lookup(node["node_id"])["metadata"] == {}
    assert any(row["event"] == "node_metadata_dropped_on_load"
               for row in db2.audit_tail())
    db2.close()


def test_save_node_defensively_refuses_unbounded_or_unknown_metadata(db):
    directory, _ = db.load_directory()
    node = directory.register("/Work/A", "/Embody", "cv")
    forged = dict(node)
    forged["metadata"] = {"authorization": "yes"}
    with pytest.raises(ci.IdentityError) as e:
        db.save_node(forged)
    assert e.value.reason == "malformed_metadata"


# -- durable jobs -----------------------------------------------------

def test_delivery_id_is_host_minted_not_caller_supplied(db):
    job, created = db.create_job("key-1", "node-1", "query_network", {}, "cv")
    assert created is True
    assert job["delivery_id"].startswith("cj_")
    assert job["state"] == "queued"
    # The two records of A-15: the host's delivery id exists now; the
    # node's execution id does not until a node runs the work.
    assert job["node_job_id"] is None
    assert job["verdict_source"] is None


def test_idempotent_create_returns_the_same_job(db):
    first, created_1 = db.create_job("same-key", "n", "capture_top", {"a": 1}, "cv")
    second, created_2 = db.create_job("same-key", "n", "capture_top", {"a": 1}, "cv")
    assert created_1 is True and created_2 is False
    assert first["delivery_id"] == second["delivery_id"], (
        "a retry must never create a duplicate job")
    assert len(db.jobs()) == 1


def test_idempotency_key_reuse_with_different_work_is_a_conflict(db):
    """A key cannot silently substitute the original command for new work."""
    first, _ = db.create_job("k", "n", "query_network", {"parent_path": "/"}, "cv")
    with pytest.raises(hdb.IdempotencyContentConflict) as e:
        db.create_job("k", "n", "delete_op", {"op_path": "/x"}, "cv")
    assert e.value.reason == "idempotency_content_conflict"
    assert e.value.delivery_id == first["delivery_id"]
    assert e.value.existing_digest != e.value.requested_digest
    assert len(db.jobs()) == 1


def test_idempotency_digest_is_canonical_for_argument_key_order(db):
    first, _ = db.create_job(
        "k", "n", "query_network", {"a": 1, "nested": {"y": 2, "x": 3}},
        "cv")
    retry, created = db.create_job(
        "k", "n", "query_network", {"nested": {"x": 3, "y": 2}, "a": 1},
        "cv")
    assert created is False
    assert retry["delivery_id"] == first["delivery_id"]


def test_conflicting_upgrade_retry_does_not_strand_an_empty_marker(db):
    """A local origin-scoped retry may inherit the pre-origin marker.

    If that first retry conflicts, its freshly claimed four-part marker
    must not be left empty: otherwise the following correct retry skips
    the legacy lookup and mints duplicate acknowledged work.
    """
    first, _ = db.create_job("k", "n", "query_network", {"a": 1}, "cv")
    with pytest.raises(hdb.IdempotencyContentConflict):
        db.create_job("k", "n", "delete_op", {"a": 2}, "cv",
                      origin_host_id=db.host_id())
    retry, created = db.create_job(
        "k", "n", "query_network", {"a": 1}, "cv",
        origin_host_id=db.host_id())
    assert created is False
    assert retry["delivery_id"] == first["delivery_id"]
    assert len(db.jobs()) == 1


@pytest.mark.parametrize("change", ["runtime", "controller", "arguments"])
def test_idempotency_digest_binds_execution_preconditions_and_caller(db,
                                                                     change):
    base = dict(expected_runtime_id="rt-1", origin_host_id=VALID_HOST,
                controller_id="ctl-1")
    first, _ = db.create_job("k", "n", "query_network", {"a": 1}, "cv",
                             **base)
    changed = dict(base)
    arguments = {"a": 1}
    if change == "runtime":
        changed["expected_runtime_id"] = "rt-2"
    elif change == "controller":
        changed["controller_id"] = "ctl-2"
    else:
        arguments = {"a": 2}
    with pytest.raises(hdb.IdempotencyContentConflict) as e:
        db.create_job("k", "n", "query_network", arguments, "cv", **changed)
    assert e.value.delivery_id == first["delivery_id"]


def test_jobs_survive_host_restart(tmp_path):
    """PHASE 1 EXIT CLAUSE: durable jobs survive host restart."""
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    job, _ = db1.create_job("persist-me", "node-9", "query_network", {}, "cv")
    db1.record_node_verdict(job["delivery_id"], "running",
                            node_job_id="job_ab12cd34", observed_at=1000.0)
    db1.close()

    db2 = hdb.HostStore(path)
    restored = db2.get_job(job["delivery_id"])
    assert restored is not None, "an acknowledged job must never be lost"
    assert restored["state"] == "running"
    assert restored["node_job_id"] == "job_ab12cd34"
    assert restored["verdict_source"] == "node_poll"
    assert restored["idempotency_key"] == "persist-me"
    # And the idempotency guarantee spans the restart too.
    again, created = db2.create_job("persist-me", "node-9",
                                    "query_network", {}, "cv")
    assert created is False and again["delivery_id"] == job["delivery_id"]
    db2.close()


def test_accepted_expiry_is_durable_and_refused_after_restart(tmp_path):
    path = str(tmp_path / "state")
    clock = {"wall": 100.0, "mono": 10.0}
    db1 = hdb.HostStore(path, now=lambda: clock["wall"],
                        monotonic=lambda: clock["mono"])
    job, _ = db1.create_job(
        "expires", "node-9", "query_network", {}, "cv",
        origin_host_id=VALID_HOST,
        accepted_timing=accepted_timing(monotonic_deadline=40.0))
    stored = db1.get_job(job["delivery_id"])
    assert stored["accepted_expires_unix"] == 130.0
    assert "accepted_deadline_monotonic" not in stored, (
        "a process-local monotonic epoch must never be persisted")
    db1.close()

    clock.update(wall=131.0, mono=2.0)
    db2 = hdb.HostStore(path, now=lambda: clock["wall"],
                        monotonic=lambda: clock["mono"])
    assert db2.claim_for_dispatch(job["delivery_id"]) is None
    refused = db2.get_job(job["delivery_id"])
    assert refused["state"] == "refused"
    assert refused["result"]["reason"] == "deadline_exceeded"
    assert any(row["event"] == "job_expired_before_dispatch"
               for row in db2.audit_tail())
    db2.close()


def test_live_monotonic_deadline_survives_wall_clock_rollback(tmp_path):
    clock = {"wall": 100.0, "mono": 10.0}
    store = hdb.HostStore(str(tmp_path / "state"),
                          now=lambda: clock["wall"],
                          monotonic=lambda: clock["mono"])
    job, _ = store.create_job(
        "k", "n", "query_network", {}, "cv",
        origin_host_id=VALID_HOST,
        accepted_timing=accepted_timing(monotonic_deadline=40.0))
    # Wall time still claims almost the entire budget remains, but the
    # target-local monotonic clock proves the accepted duration elapsed.
    clock.update(wall=100.5, mono=40.1)
    status = store.job_timing(job["delivery_id"])
    assert status["expired"] is True
    assert status["reason"] == "deadline_exceeded"
    assert store.claim_for_dispatch(job["delivery_id"]) is None
    assert store.get_job(job["delivery_id"])["state"] == "refused"
    store.close()


def test_material_clock_rollback_after_restart_fails_closed(tmp_path):
    path = str(tmp_path / "state")
    clock = {"wall": 100.0, "mono": 10.0}
    store = hdb.HostStore(path, now=lambda: clock["wall"],
                          monotonic=lambda: clock["mono"])
    job, _ = store.create_job(
        "k", "n", "query_network", {}, "cv",
        origin_host_id=VALID_HOST,
        accepted_timing=accepted_timing(monotonic_deadline=40.0))
    store.close()

    clock.update(wall=90.0, mono=1.0)
    restored = hdb.HostStore(path, now=lambda: clock["wall"],
                             monotonic=lambda: clock["mono"])
    status = restored.job_timing(job["delivery_id"])
    assert status["expired"] is True
    assert status["reason"] == "clock_rollback"
    restored.close()


def test_idempotent_retry_may_refresh_envelope_timing_but_not_work(db):
    first, _ = db.create_job(
        "k", "n", "query_network", {}, "cv",
        origin_host_id=VALID_HOST,
        accepted_timing=accepted_timing(at=100.0, remaining=10.0,
                                        monotonic_deadline=20.0))
    retry, created = db.create_job(
        "k", "n", "query_network", {}, "cv",
        origin_host_id=VALID_HOST,
        accepted_timing=accepted_timing(at=105.0, remaining=30.0,
                                        monotonic_deadline=40.0))
    assert created is False
    assert retry["delivery_id"] == first["delivery_id"]
    assert retry["accepted_expires_unix"] == 110.0, (
        "a retry must not refresh an already accepted delivery budget")


@pytest.mark.parametrize("field,bad", [
    ("accepted_remaining_s", float("inf")),
    ("accepted_expires_unix", 99.0),
    ("request_deadline_unix", 120.0),
])
def test_malformed_accepted_timing_is_refused_before_ack(db, field, bad):
    timing = accepted_timing()
    timing[field] = bad
    with pytest.raises(ValueError):
        db.create_job("k", "n", "query_network", {}, "cv",
                      accepted_timing=timing)
    assert db.jobs() == []


def test_indeterminate_is_host_originated_and_first_class(db):
    job, _ = db.create_job("k", "n", "capture_top", {}, "cv")
    updated = db.mark_indeterminate(
        job["delivery_id"], {"detail": "no response within deadline"})
    assert updated["state"] == "indeterminate"
    assert updated["verdict_source"] == "host"
    assert db.get_job(job["delivery_id"])["state"] == "indeterminate"
    assert "indeterminate" in hdb.JOB_STATES


def test_reap_notifies_cleanup_only_after_job_file_is_deleted(tmp_path):
    clock = {"t": 100.0}
    store = hdb.HostStore(str(tmp_path / "state"), now=lambda: clock["t"])
    job, _ = store.create_job("cleanup", "n", "query_network", {}, "cv")
    store.mark_refused(job["delivery_id"], {"reason": "test"})
    clock["t"] = 200.0
    observed = []

    def cleanup(reaped):
        assert store.get_job(reaped["delivery_id"]) is None
        observed.append(reaped["delivery_id"])

    result = store.reap(retention_s=10.0, on_reap=cleanup)
    assert result["jobs"] == 1
    assert observed == [job["delivery_id"]]
    store.close()


def test_indeterminate_requires_evidence(db):
    """The record is the only proof a consequential op MAY have run
    (16.4), so it must carry what was seen."""
    job, _ = db.create_job("k", "n", "capture_top", {}, "cv")
    with pytest.raises(ValueError):
        db.mark_indeterminate(job["delivery_id"], None)


def test_indeterminate_evidence_is_not_reaped_before_acknowledgement(tmp_path):
    clock = {"t": 100.0}
    store = hdb.HostStore(str(tmp_path / "state"),
                          now=lambda: clock["t"])
    job, _ = store.create_job("k", "n", "capture_top", {}, "cv")
    terminal = store.mark_indeterminate(
        job["delivery_id"], {"detail": "response was lost"})
    assert terminal["terminal_at"] == 100.0
    assert terminal["outcome_acknowledged_at"] is None

    clock["t"] = 1000.0
    assert store.reap(retention_s=10.0)["jobs"] == 0
    assert store.get_job(job["delivery_id"])["state"] == "indeterminate"

    acknowledged = store.acknowledge_outcome(job["delivery_id"])
    assert acknowledged["outcome_acknowledged_at"] == 1000.0
    clock["t"] = 1011.0
    assert store.reap(retention_s=10.0)["jobs"] == 1
    assert store.get_job(job["delivery_id"]) is None
    store.close()


def test_nonterminal_outcome_cannot_be_acknowledged(db):
    job, _ = db.create_job("k", "n", "capture_top", {}, "cv")
    with pytest.raises(ValueError):
        db.acknowledge_outcome(job["delivery_id"])


def test_a_node_correction_invalidates_the_prior_acknowledgement(tmp_path):
    clock = {"t": 100.0}
    store = hdb.HostStore(str(tmp_path / "state"),
                          now=lambda: clock["t"])
    job, _ = store.create_job("k", "n", "capture_top", {}, "cv")
    store.mark_indeterminate(job["delivery_id"], {"detail": "lost"})
    store.acknowledge_outcome(job["delivery_id"])

    clock["t"] = 200.0
    corrected = store.record_node_verdict(
        job["delivery_id"], "error", node_job_id="job_deadbeef",
        observed_at=200.0, result={"detail": "node later answered"})
    assert corrected["state"] == "failed"
    assert corrected["terminal_at"] == 200.0
    assert corrected["outcome_acknowledged_at"] is None
    store.close()


def test_host_cannot_originate_an_execution_verdict(db):
    """A-15: the node owns the verdict. There is NO host method that
    writes succeeded/failed without node provenance -- the only path is
    record_node_verdict, which demands node_job_id + observed_at."""
    job, _ = db.create_job("k", "n", "capture_top", {}, "cv")
    with pytest.raises(ValueError):
        db.record_node_verdict(job["delivery_id"], "done",
                               node_job_id="", observed_at=1000.0)
    with pytest.raises(ValueError):
        db.record_node_verdict(job["delivery_id"], "done",
                               node_job_id="job_x", observed_at=None)
    # And the state never changed off queued.
    assert db.get_job(job["delivery_id"])["state"] == "queued"


def test_node_verdict_maps_shipped_statuses(db):
    for node_status, expected in (("running", "running"),
                                  ("done", "succeeded"),
                                  ("error", "failed")):
        job, _ = db.create_job(f"k-{node_status}", "n", "x", {}, "cv")
        updated = db.record_node_verdict(
            job["delivery_id"], node_status,
            node_job_id="job_deadbeef", observed_at=1234.5,
            result={"summary": node_status})
        assert updated["state"] == expected
        assert updated["node_job_id"] == "job_deadbeef"
        assert updated["observed_at"] == 1234.5


def test_unknown_node_status_refused(db):
    job, _ = db.create_job("k", "n", "x", {}, "cv")
    with pytest.raises(ValueError):
        db.record_node_verdict(job["delivery_id"], "probably_fine",
                               node_job_id="job_x", observed_at=1.0)


def test_verdict_on_a_missing_job_raises(db):
    with pytest.raises(KeyError):
        db.record_node_verdict("cj_nope", "done",
                               node_job_id="job_x", observed_at=1.0)
    with pytest.raises(KeyError):
        db.mark_indeterminate("cj_nope", {"detail": "gone"})


def test_missing_idempotency_key_refused(db):
    with pytest.raises(ValueError):
        db.create_job("", "n", "x", {}, "cv")


# -- audit (A-40) -----------------------------------------------------

def test_audit_records_go_to_the_host_store(db):
    db.audit("test", "something_happened", {"k": "v"})
    events = [row["event"] for row in db.audit_tail()]
    assert "something_happened" in events
    assert "host_id_minted" in events, "identity minting is audited"


def test_audit_is_append_only_and_survives_a_torn_line(tmp_path):
    """Appending cannot corrupt earlier records -- the reason the audit
    is a JSONL file rather than part of the state blob."""
    path = str(tmp_path / "state")
    st = hdb.HostStore(path)
    st.audit("a", "first", {})
    st.audit("a", "second", {})
    st.close()
    with open(os.path.join(path, hdb.AUDIT_FILE), "a", encoding="utf-8") as f:
        f.write('{"ts": 1, "actor": "a", "event": "torn"')   # no newline/brace

    st2 = hdb.HostStore(path)
    events = [r["event"] for r in st2.audit_tail()]
    assert "first" in events and "second" in events, (
        "a torn final line must not lose earlier records")
    assert "torn" not in events
    st2.close()


def test_job_creation_is_audited_with_the_join_key(db):
    """audit.jsonl is the one append-only artifact that survives a store
    restore, so the create line must carry idempotency_key -- otherwise a
    recovered audit cannot be joined back to a controller's request."""
    db.create_job("join-key", "n", "query_network", {}, "cv")
    created = [r for r in db.audit_tail() if r["event"] == "job_created"]
    assert created
    detail = created[-1]["detail"]
    assert detail["idempotency_key"] == "join-key"
    assert detail["delivery_id"].startswith("cj_")


# =====================================================================
# Panel regressions (2026-07-31) -- each reproduces a PROVEN defect.
# =====================================================================

def test_idempotency_is_scoped_per_node_not_global(db):
    """PROVEN BUG: one global key space meant a submission for node B
    came back bound to node A -- B's work was never created, and B would
    poll a job that answers with A's result."""
    a, created_a = db.create_job("deploy", "node-A", "capture_top", {}, "cv")
    b, created_b = db.create_job("deploy", "node-B", "query_network", {}, "cv")
    assert created_a is True
    assert created_b is True, "node B's job must actually be created"
    assert a["delivery_id"] != b["delivery_id"]
    assert b["node_id"] == "node-B"
    assert b["operation"] == "query_network"


def test_idempotency_is_scoped_per_convoy(db):
    """Cross-namespace DoS: one convoy squatting a guessable key must not
    deny it to another (A-25 namespace isolation)."""
    a, _ = db.create_job("nightly", "n", "capture_top", {}, "convoy-A")
    b, created = db.create_job("nightly", "n", "capture_top", {}, "convoy-B")
    assert created is True and a["delivery_id"] != b["delivery_id"]


def test_retry_within_the_same_scope_still_dedupes(db):
    first, _ = db.create_job("k", "n", "x", {}, "cv")
    second, created = db.create_job("k", "n", "x", {}, "cv")
    assert created is False and second["delivery_id"] == first["delivery_id"]


def test_convoy_id_is_required_for_scoping(db):
    with pytest.raises(ValueError):
        db.create_job("k", "n", "x", {}, "")


def test_load_directory_skips_rows_from_another_host(tmp_path):
    """PROVEN BUG: replaying stored rows against the CURRENT host_id meant
    a COPIED state directory reproduced the same node_ids on a second
    machine -- destroying A-12 clone-uniqueness."""
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _ = db1.load_directory()
    mine = directory.register("/Work/Mine", "/Embody", "cv")
    db1.save_node(mine)
    foreign_record = dict(mine)
    foreign_record["node_id"] = ci.mint_id()
    foreign_record["host_id"] = "f" * 32
    foreign_record["project_root"] = "/Work/Theirs"
    db1.save_node(foreign_record)
    db1.close()

    db2 = hdb.HostStore(path)
    restored, foreign = db2.load_directory()
    assert [n["node_id"] for n in restored.nodes()] == [mine["node_id"]]
    assert len(foreign) == 1
    assert foreign[0]["host_id"] == "f" * 32
    assert any(r["event"] == "nodes_quarantined_on_load"
               for r in db2.audit_tail())
    db2.close()


def test_a_foreign_row_cannot_carry_an_approval_onto_another_node(tmp_path):
    """The dangerous half: a skipped row must not hand its TD-Python
    grant to a different node_id. Fail-OPEN on a security control."""
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _ = db1.load_directory()
    mine = directory.register("/Work/A", "/Embody", "cv")
    db1.save_node(mine)
    db1.save_node({**mine, "node_id": ci.mint_id(), "host_id": "f" * 32,
                   "project_root": "/Work/Theirs", "td_python_approved": True})
    db1.close()

    db2 = hdb.HostStore(path)
    restored, _foreign = db2.load_directory()
    assert all(n["td_python_approved"] is False for n in restored.nodes()), (
        "an approval from a foreign host row must never land on my node")
    db2.close()


def test_one_bad_row_does_not_crash_the_daemon_at_boot(tmp_path):
    """A supervised daemon that dies on bad data crash-loops forever --
    and the supervisor spike proved it restarts every 60s."""
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    directory, _ = db1.load_directory()
    good = directory.register("/Work/A", "/Embody", "cv")
    db1.save_node(good)
    # A row the SCHEMA accepts but the identity policy refuses: convoy_id
    # is NOT NULL yet empty. (The duplicate-key case the panel raised is
    # now structurally impossible -- UNIQUE(anchor, host_id, comp_path)
    # blocks it at write time -- so this is the reachable variant.)
    db1.save_node({**good, "node_id": ci.mint_id(), "project_root": "/Work/B", "convoy_id": ""})
    db1.close()

    db2 = hdb.HostStore(path)
    restored, foreign = db2.load_directory()   # must NOT raise
    assert len(restored.nodes()) == 1
    assert len(foreign) == 1
    assert "load_error" in foreign[0]
    db2.close()


# =====================================================================
# Per-key idempotency markers (2026-07-31 A-15 resolution)
#
# Replaces the shared _by_key.json blob whose single unreadable read
# annihilated EVERY prior mapping and re-minted a duplicate per key
# (proven live). These pin the anti-fragility properties of the marker
# design: one-key blast radius, fail-closed reads, crash-window heal.
# =====================================================================

def test_one_unreadable_marker_cannot_annihilate_other_keys(db):
    """The headline fix. Corrupting ONE key's marker must not touch any
    other key, and the corrupted key must FAIL CLOSED (raise) rather than
    silently re-minting a duplicate -- the exact inversion of the old
    shared-index annihilation."""
    a, _ = db.create_job("key-A", "n", "capture_top", {}, "cv")
    b, _ = db.create_job("key-B", "n", "query_network", {}, "cv")

    marker_a = db._idem_path("cv", "n", "key-A")
    with open(marker_a, "w", encoding="utf-8") as f:
        f.write("{ corrupt, not json")     # non-empty + unparseable

    # key-B's marker is independent -> its retry still dedupes.
    b_again, created_b = db.create_job("key-B", "n", "query_network", {}, "cv")
    assert created_b is False
    assert b_again["delivery_id"] == b["delivery_id"]

    # key-A fails closed rather than duplicating.
    with pytest.raises(OSError):
        db.create_job("key-A", "n", "capture_top", {}, "cv")

    a_jobs = [j for j in db.jobs() if j["idempotency_key"] == "key-A"]
    assert len(a_jobs) == 1 and a_jobs[0]["delivery_id"] == a["delivery_id"]


def test_empty_marker_from_a_crash_heals_without_duplicate(db):
    """A crash AFTER the O_EXCL gate but BEFORE the job write leaves an
    empty marker. The next accept heals it and a retry then dedupes to
    that one job -- never a second."""
    marker = db._idem_path("cv", "n", "crash-key")
    open(marker, "w", encoding="utf-8").close()     # gated, not yet filled

    job, created = db.create_job("crash-key", "n", "capture_top", {}, "cv")
    assert created is True and job["delivery_id"].startswith("cj_")

    again, created2 = db.create_job("crash-key", "n", "capture_top", {}, "cv")
    assert created2 is False and again["delivery_id"] == job["delivery_id"]
    assert len([j for j in db.jobs()
                if j["idempotency_key"] == "crash-key"]) == 1


def test_marker_pointing_at_a_deleted_job_heals(db):
    """If the delivery record a marker names is gone (manual cleanup,
    retention), a retry mints a fresh one rather than refusing forever."""
    job, _ = db.create_job("k", "n", "x", {}, "cv")
    os.remove(db._job_path(job["delivery_id"]))
    healed, created = db.create_job("k", "n", "x", {}, "cv")
    assert created is True
    assert healed["delivery_id"] != job["delivery_id"]


def test_marker_for_missing_job_still_rejects_different_request(db):
    """The atomic marker is binding evidence even if its job is absent."""
    job, _ = db.create_job("k", "n", "query_network", {"a": 1}, "cv")
    os.remove(db._job_path(job["delivery_id"]))
    with pytest.raises(hdb.IdempotencyContentConflict) as e:
        db.create_job("k", "n", "delete_op", {"a": 2}, "cv")
    assert e.value.delivery_id == job["delivery_id"]


def test_idempotency_markers_are_not_listed_as_jobs(db):
    db.create_job("k1", "n", "x", {}, "cv")
    db.create_job("k2", "n", "y", {}, "cv")
    jobs = db.jobs()
    assert len(jobs) == 2
    assert all(j["delivery_id"].startswith("cj_") for j in jobs)
    markers = [f for f in os.listdir(os.path.join(db.dir, "jobs"))
               if f.startswith("idem_")]
    assert len(markers) == 2, "one marker per key, invisible to jobs()"


# =====================================================================
# The RUNNING mirror (async node jobs, Phase 4 polling slice)
#
# A node job the host handed off is mirrored 'running' with the node's
# provenance. That record is NODE-OWNED: the host's own recovery
# machinery (the load sweep, the drain reaper) resolves CLAIMS, and a
# running mirror is not a claim. These pin that boundary -- a future
# "sweep everything non-terminal" edit would destroy every in-flight
# node job on a host restart.
# =====================================================================

def test_a_running_mirror_survives_a_host_restart_untouched(tmp_path):
    """A1: the crash-recovery contract for async node jobs. The node owns
    the execution; the host's mirror must come back byte-identical so the
    poller can resume it."""
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    job, _ = db1.create_job("k", "n", "run_tests", {}, "cv")
    did = job["delivery_id"]
    db1.claim_for_dispatch(did)
    started = db1.record_node_verdict(did, "running", node_job_id="job_1a2b3c4d",
                                      observed_at=1000.0,
                                      result={"status": "running"})
    assert started["state"] == "running"
    db1.close()

    db2 = hdb.HostStore(path)              # the host restarts
    revived = db2.get_job(did)
    assert revived["state"] == "running", "the sweep burned a node job"
    assert revived["node_job_id"] == "job_1a2b3c4d"
    assert revived["verdict_source"] == "node_poll"
    assert revived["observed_at"] == 1000.0
    db2.close()


def test_the_load_sweep_touches_only_dispatching(tmp_path):
    """A2: one job per state through a restart. Only the host's own
    transient CLAIM is resolved; every other state is left exactly as the
    authority that wrote it left it."""
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    ids = {}
    for key, state in (("q", "queued"), ("d", "dispatching"),
                       ("r", "running"), ("s", "succeeded"),
                       ("f", "failed"), ("i", "indeterminate")):
        job, _ = db1.create_job(key, "n", "run_tests", {}, "cv")
        ids[state] = job["delivery_id"]
    db1.claim_for_dispatch(ids["dispatching"])
    db1.claim_for_dispatch(ids["running"])
    db1.record_node_verdict(ids["running"], "running",
                            node_job_id="job_00000001", observed_at=1.0)
    db1.record_sync_result(ids["succeeded"], True, observed_at=1.0)
    db1.record_sync_result(ids["failed"], False, observed_at=1.0)
    db1.mark_indeterminate(ids["indeterminate"], {"reason": "test"})
    db1.close()

    db2 = hdb.HostStore(path)
    after = {state: db2.get_job(did)["state"] for state, did in ids.items()}
    assert after == {"queued": "queued",
                     "dispatching": "indeterminate",   # the claim, resolved
                     "running": "running",
                     "succeeded": "succeeded",
                     "failed": "failed",
                     "indeterminate": "indeterminate"}
    db2.close()


def test_committed_lifecycle_ledger_can_reconcile_boot_indeterminate(tmp_path):
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    job, _ = db1.create_job(
        "restart-recovery", "node-1", "convoy_restart_node",
        {"timeout_s": 5}, "cv", expected_runtime_id="runtime-1")
    delivery_id = job["delivery_id"]
    db1.claim_for_dispatch(delivery_id)
    db1.close()

    db2 = hdb.HostStore(path)
    assert db2.get_job(delivery_id)["state"] == "indeterminate"
    result = {
        "ok": True, "code": "ok", "detail": "operation completed",
        "capability": "host.td-lifecycle/v1",
        "node_id": "node-1", "operation_id": delivery_id,
        "runtime_id": "runtime-new",
    }
    recovered = db2.reconcile_lifecycle_result(
        delivery_id, True, 1234.0, result=result)
    assert recovered["state"] == "succeeded"
    assert recovered["verdict_source"] == "host_operation_recovery"
    assert recovered["result"] == result
    db2.close()


def test_lifecycle_reconciliation_refuses_unrelated_or_unbound_evidence(
        tmp_path):
    db = hdb.HostStore(str(tmp_path / "state"))
    job, _ = db.create_job("shell", "node-1", "convoy_shell", {}, "cv")
    delivery_id = job["delivery_id"]
    db.claim_for_dispatch(delivery_id)
    db.mark_indeterminate(
        delivery_id, {"reason": "host_exited_mid_dispatch"})
    result = {
        "ok": True, "code": "ok", "detail": "operation completed",
        "capability": "host.td-lifecycle/v1",
        "node_id": "node-1", "operation_id": delivery_id,
    }
    with pytest.raises(ValueError, match="only reviewed lifecycle"):
        db.reconcile_lifecycle_result(
            delivery_id, True, 1234.0, result=result)

    lifecycle, _ = db.create_job(
        "restart", "node-1", "convoy_restart_node", {}, "cv",
        expected_runtime_id="runtime-1")
    lifecycle_id = lifecycle["delivery_id"]
    db.claim_for_dispatch(lifecycle_id)
    db.mark_indeterminate(
        lifecycle_id, {"reason": "host_exited_mid_dispatch"})
    with pytest.raises(ValueError, match="not bound"):
        db.reconcile_lifecycle_result(
            lifecycle_id, True, 1234.0, result=result)
    assert db.get_job(lifecycle_id)["state"] == "indeterminate"
    db.close()


def test_a_terminal_job_never_regresses_to_running(tmp_path):
    """A3: a poll answer that arrives AFTER the job terminalised must not
    drag it back. record_node_verdict('running') on a terminal record is
    ignored and audited -- the late response is debris, not news."""
    db = hdb.HostStore(str(tmp_path / "state"))
    job, _ = db.create_job("k", "n", "run_tests", {}, "cv")
    did = job["delivery_id"]
    db.record_node_verdict(did, "done", node_job_id="job_deadbeef",
                           observed_at=10.0, result={"summary": "ok"})
    unchanged = db.record_node_verdict(did, "running",
                                       node_job_id="job_deadbeef",
                                       observed_at=20.0,
                                       result={"status": "running"})
    assert unchanged["state"] == "succeeded"
    assert unchanged["result"] == {"summary": "ok"}
    assert unchanged["observed_at"] == 10.0
    assert any(r["event"] == "verdict_regression_ignored"
               for r in db.audit_tail(limit=20))
    # a terminal answer still lands (done -> failed is a real correction
    # the node authored, not a regression)
    assert db.record_node_verdict(did, "error", node_job_id="job_deadbeef",
                                  observed_at=30.0)["state"] == "failed"
    db.close()


def test_state_counts_matches_a_full_jobs_scan(db):
    """A4: status() reports from state_counts, so it must agree with the
    scan it replaces -- exactly, including states with no jobs at all."""
    assert db.state_counts() == {}
    for n in range(3):
        db.create_job(f"q{n}", "n", "query_network", {}, "cv")
    claimed, _ = db.create_job("c", "n", "query_network", {}, "cv")
    db.claim_for_dispatch(claimed["delivery_id"])
    running, _ = db.create_job("r", "n", "run_tests", {}, "cv")
    db.record_node_verdict(running["delivery_id"], "running",
                           node_job_id="job_0000000a", observed_at=1.0)
    done, _ = db.create_job("d", "n", "query_network", {}, "cv")
    db.record_sync_result(done["delivery_id"], True, observed_at=1.0)

    counts = db.state_counts()
    scanned = {}
    for job in db.jobs():
        scanned[job["state"]] = scanned.get(job["state"], 0) + 1
    assert counts == scanned
    assert counts == {"queued": 3, "dispatching": 1, "running": 1,
                      "succeeded": 1}


def test_reap_removes_an_unreadable_record_past_retention(tmp_path):
    """An unreadable delivery record used to be immortal: reap skipped it
    by definition (`get_job -> None`), and every node-cleanup sweep spares
    a row while any record is unreadable -- so ONE truncated file (a crash
    mid-write) froze all node cleanup on the host, for ever, invisibly."""
    store = hdb.HostStore(str(tmp_path / "state"))
    try:
        bad = os.path.join(store.jobs_dir, "cj_truncated.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ truncated")
        assert store.get_job("cj_truncated") is None
        _jobs, unreadable = store.scan_jobs()
        assert unreadable == ["cj_truncated"]

        # Inside the retention window it stays: it may still be a record
        # something is mid-write on.
        store.reap(24 * 3600.0)
        assert os.path.exists(bad)

        # Past it, the file is debris by any reading and must go, or the
        # sweeps it blocks never run again.
        old = os.path.getmtime(bad) - (48 * 3600.0)
        os.utime(bad, (old, old))
        result = store.reap(24 * 3600.0)
        assert result["jobs"] >= 1
        assert not os.path.exists(bad)
        assert store.scan_jobs()[1] == []
    finally:
        store.close()


def test_delete_node_writes_disk_before_committing_memory(tmp_path, monkeypatch):
    """A failed host.json write must leave memory and disk AGREEING.

    The old body popped the row from memory first, so a raised write left
    the row absent in memory, present on disk, and the retry -- finding
    nothing to pop -- wrote nothing and reported success. The next daemon
    start replayed the ghost.
    """
    store = hdb.HostStore(str(tmp_path / "state"))
    try:
        directory = ci.NodeDirectory(store.host_id())
        record = directory.register("/Work/x", "/Embody", "cv")
        store.save_node(record)
        node_id = record["node_id"]
        assert node_id in store._state["nodes"]

        boom = OSError(32, "sharing violation")

        def _fail(*a, **k):
            raise boom

        monkeypatch.setattr(store, "_write_host", _fail)
        with pytest.raises(OSError):
            store.delete_node(node_id)
        assert node_id in store._state["nodes"], \
            "a failed durable write must not have committed in memory"
    finally:
        store.close()
