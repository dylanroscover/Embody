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

def test_job_id_is_host_minted_not_caller_supplied(db):
    job, created = db.create_job("key-1", "node-1", "query_network", {}, "cv")
    assert created is True
    assert job["job_id"].startswith("cj_")
    assert job["state"] == "queued"


def test_idempotent_create_returns_the_same_job(db):
    first, created_1 = db.create_job("same-key", "n", "capture_top", {"a": 1}, "cv")
    second, created_2 = db.create_job("same-key", "n", "capture_top", {"a": 1}, "cv")
    assert created_1 is True and created_2 is False
    assert first["job_id"] == second["job_id"], (
        "a retry must never create a duplicate job")
    assert len(db.jobs()) == 1


def test_idempotency_holds_even_if_the_retry_differs(db):
    """The key is the contract. A retry claiming the same key gets the
    ORIGINAL job back rather than silently starting different work."""
    first, _ = db.create_job("k", "n", "query_network", {"parent_path": "/"}, "cv")
    second, created = db.create_job("k", "n", "delete_op", {"op_path": "/x"}, "cv")
    assert created is False
    assert second["job_id"] == first["job_id"]
    assert second["operation"] == "query_network"


def test_jobs_survive_host_restart(tmp_path):
    """PHASE 1 EXIT CLAUSE: durable jobs survive host restart."""
    path = str(tmp_path / "state")
    db1 = hdb.HostStore(path)
    job, _ = db1.create_job("persist-me", "node-9", "query_network", {}, "cv")
    db1.set_job_state(job["job_id"], "running")
    db1.close()

    db2 = hdb.HostStore(path)
    restored = db2.get_job(job["job_id"])
    assert restored is not None, "an acknowledged job must never be lost"
    assert restored["state"] == "running"
    assert restored["idempotency_key"] == "persist-me"
    # And the idempotency guarantee spans the restart too.
    again, created = db2.create_job("persist-me", "node-9",
                                    "query_network", {}, "cv")
    assert created is False and again["job_id"] == job["job_id"]
    db2.close()


def test_indeterminate_is_a_first_class_state(db):
    job, _ = db.create_job("k", "n", "capture_top", {}, "cv")
    db.set_job_state(job["job_id"], "indeterminate",
                     {"detail": "no response within deadline"})
    assert db.get_job(job["job_id"])["state"] == "indeterminate"
    assert "indeterminate" in hdb.JOB_STATES


def test_unknown_state_refused(db):
    job, _ = db.create_job("k", "n", "x", {}, "cv")
    with pytest.raises(ValueError):
        db.set_job_state(job["job_id"], "probably_fine")


def test_setting_state_on_a_missing_job_raises(db):
    with pytest.raises(KeyError):
        db.set_job_state("cj_nope", "succeeded")


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


def test_job_creation_is_audited(db):
    db.create_job("k", "n", "query_network", {}, "cv")
    assert any(r["event"] == "job_created" for r in db.audit_tail())


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
    assert a["job_id"] != b["job_id"]
    assert b["node_id"] == "node-B"
    assert b["operation"] == "query_network"


def test_idempotency_is_scoped_per_convoy(db):
    """Cross-namespace DoS: one convoy squatting a guessable key must not
    deny it to another (A-25 namespace isolation)."""
    a, _ = db.create_job("nightly", "n", "capture_top", {}, "convoy-A")
    b, created = db.create_job("nightly", "n", "capture_top", {}, "convoy-B")
    assert created is True and a["job_id"] != b["job_id"]


def test_retry_within_the_same_scope_still_dedupes(db):
    first, _ = db.create_job("k", "n", "x", {}, "cv")
    second, created = db.create_job("k", "n", "x", {}, "cv")
    assert created is False and second["job_id"] == first["job_id"]


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
