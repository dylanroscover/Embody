"""HostApp integration for automatic, leaderless Convoy realm membership."""

import json

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


def audit_events(tmp_path, name="host"):
    path = tmp_path / name / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


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


def test_split_established_realms_refuse_WITHOUT_poisoning_the_realm(
        tmp_path):
    """CHECK, THEN APPLY (field incidents 2026-08-06 and 2026-08-12).

    The old contract asserted that a foreign established registration
    flips this host into a durable CONFLICT and is then refused -- which
    meant one register from a cloned repo permanently wedged the
    receiving daemon before its 409 was computed, with no UI to recover.
    The refusal is unchanged; the poisoning is gone: the local realm
    stays ESTABLISHED, every prior node keeps working, and the foreign
    claim is audited with its source instead of being committed.
    """
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

    # The refusal did NOT touch realm state.
    realm = app.realm.snapshot()
    assert realm["state"] == "established"
    assert realm["convoy_id"] == HIGH
    assert realm["conflict_ids"] == []
    assert app.active_realm_states() == {HIGH: "established"}

    # The foreign claim is attributable after the fact.
    refused_events = [entry for entry in audit_events(tmp_path)
                     if entry.get("event") == "realm_foreign_register_refused"]
    assert len(refused_events) == 1
    detail = refused_events[0]["detail"]
    assert detail["convoy_id"] == OTHER
    assert detail["local_convoy_id"] == HIGH
    assert detail["source"]["via"] == "register"
    assert detail["source"]["project_root"] == "/Work/other"

    # Work in the (untouched) realm continues, trivially.
    code, created = app.create_job({
        "idempotency_key": "preserved-realm-work",
        "node_id": first["node_id"],
        "operation": "convoy_ping",
        "arguments": {},
    })
    assert code == 200 and created["created"] is True
    assert app.network_nodes(OTHER)[0] == 409


# The REAL parsed-announcement shape: provenance rides in an endpoint
# dict, never a top-level address (the first fix read the wrong key and
# recorded an empty address on every production advisory -- the panel
# caught it because the old test fabricated a key the parser never
# emits).
STRANGER = {"host_id": "e" * 32,
            "fingerprint": "cvfp1-stranger-fp",
            "endpoint": {"address": "192.168.88.24", "port": 47600}}


def _announce(app, realm_states, sender=STRANGER):
    app._observe_realm_announcement(dict(sender,
                                         realm_states=dict(realm_states)))


def test_a_strangers_announcement_cannot_move_a_committed_realm(tmp_path):
    """THE 2026-08-12 FIELD INCIDENT: one never-admitted MacBook's UDP
    broadcast latched this daemon into a durable CONFLICT that refused
    every registration. A signature only proves the datagram matches the
    sender's own self-signed cert -- under TOFU any subnet neighbour has
    one -- so a committed realm now moves only on an ADMITTED peer's
    word. The stranger's claim is recorded as an attributable advisory,
    once, and is otherwise powerless."""
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, _first = app.register_node(registration(
        HIGH, binding_state="established"))
    assert code == 200
    _announce(app, {OTHER: "established"})
    realm = app.realm.snapshot()
    assert realm["state"] == "established"
    assert realm["convoy_id"] == HIGH
    assert realm["conflict_ids"] == []

    advisories = [entry for entry in audit_events(tmp_path)
                  if entry.get("event") == "realm_foreign_advisory"]
    assert len(advisories) == 1
    sender = advisories[0]["detail"]["sender"]
    assert sender["host_id"] == STRANGER["host_id"]
    assert sender["fingerprint"] == STRANGER["fingerprint"]
    assert sender["address"] == "192.168.88.24:47600"

    # A 3-second announce cadence must not become an audit firehose:
    # the SAME claim from the SAME sender audits once.
    _announce(app, {OTHER: "established"})
    _announce(app, {OTHER: "established"})
    advisories = [entry for entry in audit_events(tmp_path)
                  if entry.get("event") == "realm_foreign_advisory"]
    assert len(advisories) == 1


def test_an_unbound_host_still_adopts_from_the_lan(tmp_path):
    """Genesis/adoption predate admission by necessity -- gating them on
    admission would orphan every fresh install. Only COMMITTED realms
    are protected from strangers."""
    clock = Clock()
    app = make_app(tmp_path, clock)
    _announce(app, {HIGH: "established"})
    realm = app.realm.snapshot()
    assert realm["state"] == "established"
    assert realm["convoy_id"] == HIGH


class _FullAuthority:
    allowed = True
    may_mutate = True
    reason = None


def _grant_realm_authority(app, host_id, admitted_via="manual",
                           may_mutate=True):
    """Stub OPERATOR-GRADE membership: allowed + may_mutate + a peer
    record whose admission was not self-service TOFU."""
    decision = _FullAuthority()
    decision.may_mutate = may_mutate
    app.peers.authorize_peer = lambda *a, **k: decision
    app.peers.get = lambda _h: {"host_id": host_id,
                                "admitted_via": admitted_via}


def test_an_observe_only_peer_cannot_move_a_committed_realm(tmp_path):
    """`allowed` alone passes an observe-only peer -- one the operator
    deliberately stripped of every mutation. The panel latched a
    CONFLICT through exactly that gap; realm authority requires
    may_mutate."""
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, _first = app.register_node(registration(
        HIGH, binding_state="established"))
    assert code == 200
    _grant_realm_authority(app, STRANGER["host_id"], may_mutate=False)
    _announce(app, {OTHER: "established"})
    realm = app.realm.snapshot()
    assert realm["state"] == "established"
    assert realm["convoy_id"] == HIGH


def test_a_tofu_admitted_neighbour_cannot_move_a_committed_realm(
        tmp_path):
    """TOFU admission is self-service: any LAN host that echoes this
    host's broadcast convoy_id gets auto-admitted (admitted_via
    'lan_tofu'). The panel wedged a committed realm in two datagrams
    through that door; realm-moving authority requires an
    operator-grade admission."""
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, _first = app.register_node(registration(
        HIGH, binding_state="established"))
    assert code == 200
    _grant_realm_authority(app, STRANGER["host_id"],
                           admitted_via="lan_tofu")
    _announce(app, {OTHER: "established"})
    realm = app.realm.snapshot()
    assert realm["state"] == "established"
    assert realm["convoy_id"] == HIGH
    advisories = [entry for entry in audit_events(tmp_path)
                  if entry.get("event") == "realm_foreign_advisory"]
    assert len(advisories) == 1, 'downgraded to an advisory, attributed'


def test_an_admitted_peers_announcement_still_latches_the_conflict(
        tmp_path):
    """Two genuinely established meshes meeting IS the case the conflict
    state exists for -- when the evidence carries admission-level
    authority, it still latches, and now with provenance."""
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, _first = app.register_node(registration(
        HIGH, binding_state="established"))
    assert code == 200

    _grant_realm_authority(app, STRANGER["host_id"])
    _announce(app, {OTHER: "established"})
    realm = app.realm.snapshot()
    assert realm["state"] == "conflict"
    assert realm["convoy_id"] == HIGH
    assert realm["conflict_ids"] == sorted([HIGH, OTHER])

    conflicts = [entry for entry in audit_events(tmp_path)
                 if entry.get("event") == "realm_conflict"]
    assert conflicts
    source = conflicts[-1]["detail"]["source"]
    assert source["via"] == "announcement"
    assert source["host_id"] == STRANGER["host_id"]


def test_the_denylist_route_blocks_a_stranger_with_no_peer_record(
        tmp_path):
    """The documented recovery ('block the sender, then reset') was a
    404 for exactly the sender class that causes conflicts -- an
    un-admitted host has no peer record for /peers/block to set. The
    loopback denylist route appends to denylist.json directly."""
    clock = Clock()
    app = make_app(tmp_path, clock)
    code, out = app.denylist_identity({
        "host_id": STRANGER["host_id"],
        "reason": "foreign realm broadcast"})
    assert code == 200 and out["ok"] is True
    assert STRANGER["host_id"] in out["denylist"]["host_ids"]
    blocked, detail = app.peers.denylist.blocks(
        STRANGER["host_id"], STRANGER["fingerprint"])
    assert blocked, detail
    denylisted = [entry for entry in audit_events(tmp_path)
                  if entry.get("event") == "peer_denylisted"]
    assert len(denylisted) == 1

    # A malformed value is REFUSED, not written: one bad entry in
    # denylist.json flips the whole file fail-closed and blocks every
    # peer on this host, which would turn the recovery tool into an
    # outage.
    code, refusal = app.denylist_identity({
        "fingerprint": "not-a-fingerprint"})
    assert code == 400
    assert refusal["reason"] == "denylist_entry_malformed"
    blocked, _detail = app.peers.denylist.blocks(
        "f" * 32, "cvfp1-unrelated")
    assert not blocked, 'the denylist must still work after the refusal'


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



def test_the_default_settle_window_is_randomized_within_bounds(
        tmp_path, monkeypatch):
    """ADR-003's randomized listen window: the production default draws
    from [8, 20] s. Zero coverage let the original fixed-8.0 ship."""
    captured = {}
    real_store = hostapp.realm_mod.RealmStore

    def spy(path, **kw):
        captured.update(kw)
        return real_store(path, **kw)

    monkeypatch.setattr(hostapp.realm_mod, "RealmStore", spy)
    monkeypatch.setattr(hostapp.secrets, "randbelow", lambda n: n - 1)
    hostapp.HostApp(str(tmp_path / "hi"))
    high = captured["settle_delay_s"]
    monkeypatch.setattr(hostapp.secrets, "randbelow", lambda n: 0)
    hostapp.HostApp(str(tmp_path / "lo"))
    low = captured["settle_delay_s"]
    base = hostapp.realm_mod.DEFAULT_SETTLE_DELAY_S
    jitter = hostapp.realm_mod.DEFAULT_SETTLE_JITTER_S
    assert low == base
    assert base < high <= base + jitter
