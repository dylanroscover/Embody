"""HostApp integration for automatic, leaderless Convoy realm membership."""

import convoy_hostapp as hostapp


HIGH = "cv_" + "f" * 16
LOW = "cv_" + "0" * 16
OTHER = "cv_" + "8" * 16


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def make_app(tmp_path, clock, name="host", settle=2.0):
    return hostapp.HostApp(
        str(tmp_path / name), now=clock,
        realm_settle_delay_s=settle)


def registration(convoy_id, *, root="/Work/show", comp="/Embody",
                 runtime="rt_test", binding_state="candidate"):
    return {
        "project_root": root,
        "comp_path": comp,
        "convoy_id": convoy_id,
        "binding_state": binding_state,
        "runtime_id": runtime,
    }


def test_candidate_cannot_execute_or_issue_psk_until_settled(
        tmp_path):
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, node = app.register_node(registration(HIGH))
    assert code == 200
    assert node["convoy_id"] == HIGH
    assert node["realm_state"] == "candidate"
    assert app.active_realm_states() == {HIGH: "candidate"}

    code, refused = app.create_job({
        "idempotency_key": "before-establishment",
        "node_id": node["node_id"],
        "operation": "convoy_ping",
        "arguments": {},
    })
    assert code == 409
    assert refused["reason"] == "realm_not_established"
    code, refused = app.issue_convoy_psk({"convoy_id": HIGH})
    assert code == 409
    assert refused["reason"] == "realm_not_established"

    clock.advance(2.1)
    assert app._tick_realm() is True
    record = app.directory.lookup(node["node_id"])
    assert record["convoy_id"] == HIGH
    assert record["binding_state"] == "established"
    assert app.active_realm_states() == {HIGH: "established"}
    assert app.issue_convoy_psk({"convoy_id": HIGH})[0] == 200
    assert app.create_job({
        "idempotency_key": "after-establishment",
        "node_id": node["node_id"],
        "operation": "convoy_ping",
        "arguments": {},
    })[0] == 200


def test_local_fresh_candidates_converge_on_lowest_without_new_node_ids(
        tmp_path):
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, first = app.register_node(registration(
        HIGH, root="/Work/a", runtime="rt_a"))
    assert code == 200 and first["realm_state"] == "candidate"
    code, second = app.register_node(registration(
        LOW, root="/Work/b", runtime="rt_b"))
    assert code == 200
    assert second["convoy_id"] == LOW
    assert second["realm_state"] == "candidate"
    records = app.directory.nodes()
    assert {record["convoy_id"] for record in records} == {LOW}
    assert {record["binding_state"] for record in records} == {"candidate"}

    # The first TD process has not written the host's answer yet. Its stale
    # candidate heartbeat must receive the authority without reminting node ID.
    code, healed = app.register_node(registration(
        HIGH, root="/Work/a", runtime="rt_a"))
    assert code == 200
    assert healed["node_id"] == first["node_id"]
    assert healed["convoy_id"] == LOW


def test_established_discovery_immediately_beats_local_candidate(tmp_path):
    clock = Clock()
    app = make_app(tmp_path, clock)
    _, node = app.register_node(registration(HIGH))
    app._observe_realm_announcement({
        "realm_states": {LOW: "established"}})
    realm = app.realm.snapshot()
    assert realm["state"] == "established"
    assert realm["convoy_id"] == LOW
    record = app.directory.lookup(node["node_id"])
    assert record["convoy_id"] == LOW
    assert record["binding_state"] == "established"

    code, healed = app.register_node(registration(HIGH))
    assert code == 200
    assert healed["node_id"] == node["node_id"]
    assert healed["convoy_id"] == LOW
    assert healed["realm_state"] == "established"


def test_split_established_realms_fail_closed_but_preserve_existing_work(
        tmp_path):
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, first = app.register_node(registration(
        HIGH, binding_state="established"))
    assert code == 200
    code, refusal = app.register_node(registration(
        OTHER, root="/Work/other", runtime="rt_other",
        binding_state="established"))
    assert code == 409
    assert refusal["reason"] == "local_realm_conflict"
    realm = app.realm.snapshot()
    assert realm["state"] == "conflict"
    assert realm["convoy_id"] == HIGH
    assert realm["conflict_ids"] == sorted([HIGH, OTHER])
    assert app.active_realm_states() == {HIGH: "established"}

    # ADR-003 keeps authenticated work in the preserved realm operational.
    code, created = app.create_job({
        "idempotency_key": "preserved-realm-work",
        "node_id": first["node_id"],
        "operation": "convoy_ping",
        "arguments": {},
    })
    assert code == 200 and created["created"] is True
    assert app.network_nodes(OTHER)[0] == 409


def test_candidate_deadline_and_binding_survive_host_restart(tmp_path):
    clock = Clock()
    first = make_app(tmp_path, clock, settle=5.0)
    _, node = first.register_node(registration(HIGH))
    first.db.close()

    clock.advance(2.0)
    second = make_app(tmp_path, clock, settle=5.0)
    assert second.realm.snapshot()["state"] == "candidate"
    assert second.directory.lookup(node["node_id"])["binding_state"] \
        == "candidate"
    clock.advance(3.1)
    assert second._tick_realm() is True
    assert second.realm.snapshot()["state"] == "established"
    second.db.close()

    third = make_app(tmp_path, clock, settle=5.0)
    assert third.realm.snapshot()["state"] == "established"
    assert third.directory.lookup(node["node_id"])["binding_state"] \
        == "established"


def test_legacy_registration_without_state_is_established(tmp_path):
    clock = Clock()
    app = make_app(tmp_path, clock)
    body = registration(HIGH)
    del body["binding_state"]
    code, result = app.register_node(body)
    assert code == 200
    assert result["realm_state"] == "established"
    assert app.realm.snapshot()["state"] == "established"

