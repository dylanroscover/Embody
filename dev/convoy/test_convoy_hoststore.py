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

def test_directory_survives_restart_with_ids_and_approvals(tmp_path):
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
    assert restored.lookup(b["node_id"])["td_python_approved"] is True, (
        "an explicit approval must survive a host restart")
    # And the pair mapping still resolves: re-registering is stable.
    assert restored.register("/Work/A", "/Embody", "cv")["node_id"] == \
        a["node_id"]
    db2.close()


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


def test_idempotency_holds_even_if_the_retry_differs(db):
    """The key is the contract. A retry claiming the same key gets the
    ORIGINAL job back rather than silently starting different work."""
    first, _ = db.create_job("k", "n", "query_network", {"parent_path": "/"}, "cv")
    second, created = db.create_job("k", "n", "delete_op", {"op_path": "/x"}, "cv")
    assert created is False
    assert second["delivery_id"] == first["delivery_id"]
    assert second["operation"] == "query_network"


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


def test_indeterminate_is_host_originated_and_first_class(db):
    job, _ = db.create_job("k", "n", "capture_top", {}, "cv")
    updated = db.mark_indeterminate(
        job["delivery_id"], {"detail": "no response within deadline"})
    assert updated["state"] == "indeterminate"
    assert updated["verdict_source"] == "host"
    assert db.get_job(job["delivery_id"])["state"] == "indeterminate"
    assert "indeterminate" in hdb.JOB_STATES


def test_indeterminate_requires_evidence(db):
    """The record is the only proof a consequential op MAY have run
    (16.4), so it must carry what was seen."""
    job, _ = db.create_job("k", "n", "capture_top", {}, "cv")
    with pytest.raises(ValueError):
        db.mark_indeterminate(job["delivery_id"], None)


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
