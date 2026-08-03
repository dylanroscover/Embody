"""Focused contracts for Convoy's node membership and network directory.

These tests intentionally exercise the public projection rather than the
host-private ``/nodes`` registry.  The latter contains routing paths, ports,
and approval state; none of those fields may cross the LAN directory surface.
"""

import threading

import pytest

import convoy_hostapp as ha
import convoy_hostkeys as hostkeys
import convoy_peerclient as peerclient
from test_convoy_hostapp import Server


CONVOY_A = "studio-a"
CONVOY_B = "studio-b"
DISC_A = "nd_" + "1" * 32
DISC_B = "nd_" + "2" * 32
PUBLIC_NODE_KEYS = {
    "node_id", "host_id", "convoy_id", "runtime_id", "node_name",
    "hostname", "toe_name", "embody_version", "touchdesigner_version",
    "ip", "status", "online", "enabled", "perform_mode",
    "wake_active", "sleeping", "remotely_launchable",
    "last_seen_age_s", "controller_count",
}
PRIVATE_NODE_KEYS = {
    "toe_path", "project_root", "comp_path", "envoy_port",
    "td_python_approved", "approval", "metadata", "wake_port",
    "wake_token", "remote_wake", "wake_grace_s",
}


@pytest.fixture
def app(tmp_path):
    instance = ha.HostApp(str(tmp_path / "host"))
    yield instance
    instance.db.close()


@pytest.fixture
def server(tmp_path):
    instance = Server(str(tmp_path / "server"))
    yield instance
    instance.stop()


def _register(app, *, convoy_id=CONVOY_A, discriminator=DISC_A,
              runtime_id="rt_test", envoy_port=9800, root="/shows/demo",
              comp_path="/Embody", metadata=None):
    body = {
        "project_root": root,
        "convoy_id": convoy_id,
        "comp_path": comp_path,
        "runtime_id": runtime_id,
        "node_discriminator": discriminator,
    }
    if envoy_port is not None:
        body["envoy_port"] = envoy_port
    if metadata is not None:
        body["metadata"] = metadata
    code, response = app.register_node(body)
    assert code == 200, response
    return response


def _admit_peer(app, tmp_path, number, convoy_ids=(CONVOY_A,)):
    """Create a real pin/certificate but keep node fetches socket-free."""
    peer_identity = hostkeys.load_or_create(
        str(tmp_path / ("peer-%d" % number)))
    peer_host_id = "%032x" % number
    with app.lock:
        app.peers.admit(
            peer_host_id,
            peer_identity.fingerprint,
            cert_pem=peer_identity.certificate_pem,
            endpoints=["10.20.30.%d:%d" % (number, 7400 + number)],
            convoy_ids=list(convoy_ids),
        )
    return peer_host_id, peer_identity


def _assert_public_only(row):
    assert set(row) == PUBLIC_NODE_KEYS
    assert not (set(row) & PRIVATE_NODE_KEYS)
    assert not any("approv" in key.lower() for key in row)


def test_peer_directory_is_realm_isolated_and_never_leaks_private_metadata(
        app, tmp_path):
    metadata_a = {
        "toe_path": "/shows/demo/private/show-a.toe",
        "toe_name": "show-a.toe",
        "node_name": "render-a",
        "hostname": "render-host",
        "process_id": 4312,
        "embody_version": "6.0.178",
        "touchdesigner_version": "2025.30000",
    }
    node_a = _register(app, metadata=metadata_a)
    code, refused = app.register_node({
        "project_root": "/shows/demo",
        "convoy_id": CONVOY_B,
        "comp_path": "/EmbodyB",
        "runtime_id": "rt_b",
        "node_discriminator": DISC_B,
        "envoy_port": 9801,
        "metadata": {**metadata_a, "toe_path": "/private/show-b.toe",
                     "toe_name": "show-b.toe", "node_name": "render-b"},
    })
    assert code == 409
    assert refused["reason"] == "local_realm_conflict"
    with app.lock:
        app.directory.approve_td_python(node_a["node_id"])
    peer_host_id, peer_identity = _admit_peer(
        app, tmp_path, 1, convoy_ids=(CONVOY_A, CONVOY_B))

    code, payload = app.peer_nodes_view(
        peer_host_id, CONVOY_A, peer_identity.fingerprint)
    assert code == 200
    assert payload["convoy_id"] == CONVOY_A
    assert [row["node_id"] for row in payload["nodes"]] == [node_a["node_id"]]
    row = payload["nodes"][0]
    _assert_public_only(row)
    assert row["node_name"] == "render-a"
    assert row["toe_name"] == "show-a.toe"
    assert metadata_a["toe_path"] not in repr(payload)

    code, payload = app.peer_nodes_view(
        peer_host_id, "not-admitted", peer_identity.fingerprint)
    assert code == 403
    assert payload["reason"] == "namespace_not_admitted"
    assert "nodes" not in payload


def test_network_aggregation_treats_peer_rows_as_untrusted_descriptions(
        app, tmp_path, monkeypatch):
    peer_host_id, _ = _admit_peer(app, tmp_path, 2)
    malicious = {
        "node_id": "a" * 32,
        "host_id": "f" * 32,
        "convoy_id": "another-convoy",
        "runtime_id": "rt_remote",
        "node_name": "remote node",
        "hostname": "remote-host",
        "toe_name": "remote.toe",
        "embody_version": "6.0.178",
        "touchdesigner_version": "2025.30000",
        "ip": "203.0.113.77",
        "status": "online",
        "online": True,
        "enabled": False,
        "toe_path": "/secret/remote.toe",
        "project_root": "/secret",
        "comp_path": "/Embody",
        "envoy_port": 9911,
        "td_python_approved": True,
        "remotely_launchable": "yes",
        "controller_count": True,
        "last_seen_age_s": -5,
        "approval": {"shell": True},
    }

    def fetch(target, keys, convoy_id, timeout, pool=None):
        return {"ok": True, "host_id": target.host_id,
                "convoy_id": convoy_id, "nodes": [malicious]}

    monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
    code, payload = app.network_nodes(CONVOY_A)
    assert code == 200
    assert len(payload["nodes"]) == 1
    row = payload["nodes"][0]
    _assert_public_only(row)
    assert row["host_id"] == peer_host_id
    assert row["convoy_id"] == CONVOY_A
    assert row["ip"] == "10.20.30.2"
    assert row["enabled"] is True
    assert row["remotely_launchable"] is False
    assert row["controller_count"] == 0
    assert row["last_seen_age_s"] is None
    assert "/secret" not in repr(payload)
    assert "9911" not in repr(payload)


def test_node_directory_coalesces_presence_age_and_controller_count(app):
    registered = _register(app)
    with app.lock:
        code, heartbeat = app.heartbeat_controller({
            "controller_id": "ctl-status",
            "selected_node_id": registered["node_id"],
        })
    assert code == 200, heartbeat

    code, payload = app.network_nodes(CONVOY_A)
    assert code == 200
    row = next(item for item in payload["nodes"]
               if item["node_id"] == registered["node_id"])
    assert row["controller_count"] == 1
    assert isinstance(row["last_seen_age_s"], float)
    assert row["last_seen_age_s"] >= 0.0


def test_peer_node_queries_run_concurrently_instead_of_stacking_timeouts(
        app, tmp_path, monkeypatch):
    peer_ids = [_admit_peer(app, tmp_path, number)[0]
                for number in (3, 4, 5)]
    rendezvous = threading.Barrier(len(peer_ids))
    guard = threading.Lock()
    active = 0
    maximum_active = 0

    def fetch(target, keys, convoy_id, timeout, pool=None):
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            rendezvous.wait(timeout=3)
            return {"ok": True, "host_id": target.host_id,
                    "convoy_id": convoy_id,
                    "nodes": [{"node_id": target.host_id,
                               "node_name": target.host_id,
                               "status": "online", "online": True}]}
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
    code, payload = app.network_nodes(CONVOY_A)
    assert code == 200
    assert maximum_active == len(peer_ids)
    assert {row["host_id"] for row in payload["nodes"]} == set(peer_ids)
    assert {row["status"] for row in payload["peers"]
            if not row.get("local")} == {"online"}


def test_network_directory_uses_persisted_endpoint_fallback(
        app, tmp_path, monkeypatch):
    peer_host_id, peer_identity = _admit_peer(app, tmp_path, 30)
    with app.lock:
        app.peers.admit(
            peer_host_id, peer_identity.fingerprint,
            cert_pem=peer_identity.certificate_pem,
            endpoints=["192.0.2.30:47600", "192.0.2.31:47600"],
            convoy_ids=[CONVOY_A])
    visited = []

    def fetch(target, keys, convoy_id, timeout, pool=None):
        visited.append(target.address)
        if target.address == "192.0.2.30":
            return peerclient.UNREACHABLE
        return {"ok": True, "host_id": target.host_id,
                "convoy_id": convoy_id,
                "nodes": [{"node_id": "e" * 32,
                           "node_name": "fallback node",
                           "status": "online", "online": True}]}

    monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
    code, payload = app.network_nodes(CONVOY_A)
    assert code == 200
    assert visited == ["192.0.2.30", "192.0.2.31"]
    assert payload["nodes"][0]["ip"] == "192.0.2.31"


def test_cached_peer_nodes_are_retained_but_marked_offline_or_error(
        app, tmp_path, monkeypatch):
    peer_host_id, peer_identity = _admit_peer(app, tmp_path, 6)
    # This test asserts the PROJECTION semantics of consecutive refreshes
    # (offline marking, pin-mismatch reasons, cached-name retention), so
    # every call must actually refresh.  Advance the directory cache clock
    # past the TTL on every read; the TTL/burst-dedupe behavior itself is
    # proven by test_convoy_scale.py.
    ticks = {"value": 0.0}

    def cache_clock():
        ticks["value"] += ha.NETWORK_NODE_CACHE_TTL_S + 1.0
        return ticks["value"]

    app._network_nodes_cache_clock = cache_clock
    mode = {"value": "online"}

    def fetch(target, keys, convoy_id, timeout, pool=None):
        if mode["value"] == "offline":
            return peerclient.UNREACHABLE
        if mode["value"] == "error":
            return peerclient._PinMismatch(
                target.host_id, target.address,
                target.expected_fingerprint, "cvfp1-offered",
                "the pinned identity changed")
        return {"ok": True, "host_id": target.host_id,
                "convoy_id": convoy_id,
                "nodes": [{"node_id": "b" * 32,
                           "node_name": "cached remote",
                           "status": "online", "online": True}]}

    monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
    code, first = app.network_nodes(CONVOY_A)
    assert code == 200
    assert first["nodes"][0]["status"] == "online"
    assert first["nodes"][0]["online"] is True

    mode["value"] = "offline"
    code, offline = app.network_nodes(CONVOY_A)
    assert code == 200
    assert offline["nodes"][0]["node_name"] == "cached remote"
    assert offline["nodes"][0]["status"] == "offline"
    assert offline["nodes"][0]["online"] is False
    assert next(row for row in offline["peers"]
                if row["host_id"] == peer_host_id)["reason"] == \
        "peer_unreachable"

    mode["value"] = "error"
    code, errored = app.network_nodes(CONVOY_A)
    assert code == 200
    assert errored["nodes"][0]["status"] == "error"
    assert errored["nodes"][0]["online"] is False
    assert next(row for row in errored["peers"]
                if row["host_id"] == peer_host_id)["reason"] == \
        "pin_mismatch"
    assert peer_identity.fingerprint not in repr(errored)


def test_disable_hides_node_and_fences_work_queued_before_disable(
        app, tmp_path):
    registered = _register(app)
    peer_host_id, peer_identity = _admit_peer(app, tmp_path, 7)
    code, created = app.create_job({
        "idempotency_key": "queued-before-disable",
        "node_id": registered["node_id"],
        "operation": "convoy_ping",
        "arguments": {},
    })
    assert code == 200
    delivery_id = created["job"]["delivery_id"]
    code, created_for_drain = app.create_job({
        "idempotency_key": "drained-after-disable",
        "node_id": registered["node_id"],
        "operation": "convoy_ping",
        "arguments": {},
    })
    assert code == 200
    drain_delivery_id = created_for_drain["job"]["delivery_id"]

    with app.lock:
        code, stopped = app.unregister_node({
            "node_id": registered["node_id"],
            "runtime_id": registered["runtime_id"],
            "reason": "disabled",
        })
    assert code == 200 and stopped["enabled"] is False

    code, directory = app.network_nodes(CONVOY_A)
    assert code == 200
    assert registered["node_id"] not in {
        row["node_id"] for row in directory["nodes"]}
    code, peer_directory = app.peer_nodes_view(
        peer_host_id, CONVOY_A, peer_identity.fingerprint)
    assert code == 200
    assert registered["node_id"] not in {
        row["node_id"] for row in peer_directory["nodes"]}

    code, refused = app.create_job({
        "idempotency_key": "submitted-after-disable",
        "node_id": registered["node_id"],
        "operation": "convoy_ping",
        "arguments": {},
    })
    assert code == 409 and refused["reason"] == "node_disabled"

    code, refused = app.dispatch_job(delivery_id)
    assert code == 409
    assert refused["reason"] == "node_disabled"
    assert refused["job"]["state"] == "refused"

    summary = app.drain_once()
    assert summary["refused"] == 1
    assert summary["errors"] == 0
    drained = app.db.get_job(drain_delivery_id)
    assert drained["state"] == "refused"
    assert drained["result"]["reason"] == "node_disabled"


def test_shutdown_keeps_membership_enabled_and_lists_node_offline(
        app, tmp_path):
    registered = _register(app)
    peer_host_id, peer_identity = _admit_peer(app, tmp_path, 8)
    with app.lock:
        code, stopped = app.unregister_node({
            "node_id": registered["node_id"],
            "runtime_id": registered["runtime_id"],
            "reason": "shutdown",
        })
    assert code == 200
    assert stopped["enabled"] is True
    assert stopped["envoy_port"] is None

    code, directory = app.network_nodes(CONVOY_A)
    assert code == 200
    row = next(row for row in directory["nodes"]
               if row["node_id"] == registered["node_id"])
    assert row["enabled"] is True
    assert row["online"] is False
    assert row["status"] == "offline"
    code, peer_directory = app.peer_nodes_view(
        peer_host_id, CONVOY_A, peer_identity.fingerprint)
    assert code == 200
    peer_row = next(row for row in peer_directory["nodes"]
                    if row["node_id"] == registered["node_id"])
    assert peer_row["enabled"] is True
    assert peer_row["online"] is False
    assert peer_row["status"] == "offline"


def test_registration_routes_by_saved_toe_discriminator_and_stores_metadata(app):
    metadata = {
        "toe_path": "/shows/demo/show.toe",
        "toe_name": "show.toe",
        "node_name": "render-01 / show",
        "hostname": "render-01",
        "process_id": 8675,
        "embody_version": "6.0.178",
        "touchdesigner_version": "2025.30000",
    }
    first = _register(app, metadata=metadata)
    second = _register(
        app, discriminator=DISC_B, runtime_id="rt_second",
        envoy_port=9801, metadata={**metadata, "toe_path":
                                   "/shows/demo/other.toe"})
    assert first["node_id"] != second["node_id"]
    with app.lock:
        first_record = app.directory.lookup(first["node_id"])
        second_record = app.directory.lookup(second["node_id"])
    assert first_record["node_discriminator"] == DISC_A
    assert second_record["node_discriminator"] == DISC_B
    assert first_record["metadata"] == metadata

    # Metadata is descriptive only: authority-shaped additions are rejected.
    code, refused = app.register_node({
        "project_root": "/shows/other",
        "convoy_id": CONVOY_A,
        "comp_path": "/Embody",
        "runtime_id": "rt_bad",
        "node_discriminator": "nd_" + "3" * 32,
        "metadata": {"node_name": "looks harmless",
                     "td_python_approved": True},
    })
    assert code == 400 and refused["reason"] == "malformed_metadata"


def test_live_runtime_cannot_silently_take_over_a_stable_node(
        app, monkeypatch):
    first = _register(app, runtime_id="rt_first", envoy_port=9800)
    monkeypatch.setattr(ha, "_loopback_port_open", lambda port: True)
    code, refused = app.register_node({
        "project_root": "/shows/demo",
        "convoy_id": CONVOY_A,
        "comp_path": "/Embody",
        "runtime_id": "rt_competing",
        "envoy_port": 9801,
        "node_discriminator": DISC_A,
        "metadata": {"node_name": "competing runtime"},
    })
    assert code == 409
    assert refused["reason"] == "node_runtime_conflict"
    with app.lock:
        incumbent = app.directory.lookup(first["node_id"])
    assert incumbent["runtime_id"] == "rt_first"
    assert incumbent["envoy_port"] == 9800
    assert incumbent["metadata"] == {}


@pytest.mark.parametrize("incumbent_port,claimant_port", [
    (None, None),
    (9800, 9800),
])
def test_live_process_identity_fences_same_or_missing_envoy_ports(
        app, monkeypatch, incumbent_port, claimant_port):
    """Port equality/absence is not proof that the incumbent TD exited.

    A second live process can claim the same configured port even though it
    failed to bind it, and two pre-Envoy runtimes both report no port.  The
    process id supplied in registration metadata is the cross-platform
    ownership proof for those cases.
    """
    first = _register(
        app, runtime_id="rt_incumbent", envoy_port=incumbent_port,
        metadata={"node_name": "incumbent", "process_id": 41001})
    monkeypatch.setattr(
        ha.platform_mod, "pid_is_alive", lambda pid, **kwargs: pid == 41001)

    body = {
        "project_root": "/shows/demo",
        "convoy_id": CONVOY_A,
        "comp_path": "/Embody",
        "runtime_id": "rt_claimant",
        "node_discriminator": DISC_A,
        "metadata": {"node_name": "claimant", "process_id": 41002},
    }
    if claimant_port is not None:
        body["envoy_port"] = claimant_port
    code, refused = app.register_node(body)
    assert code == 409
    assert refused["reason"] == "node_runtime_conflict"
    with app.lock:
        incumbent = app.directory.lookup(first["node_id"])
    assert incumbent["runtime_id"] == "rt_incumbent"
    assert incumbent["metadata"]["node_name"] == "incumbent"


def test_persisted_legacy_offline_node_is_reclaimable_after_host_restart(
        tmp_path):
    """Upgrade compatibility must not strand a durable legacy node forever.

    Old rows have no process metadata and replay without a live endpoint.  A
    replayed row is durable membership/identity, not proof that its former TD
    runtime is still alive, so the first current registration must reclaim the
    same node id.
    """
    data_dir = str(tmp_path / "legacy-replay")
    before = ha.HostApp(data_dir)
    try:
        legacy = _register(
            before, runtime_id="rt_legacy", envoy_port=None, metadata=None)
        with before.lock:
            code, stopped = before.unregister_node({
                "node_id": legacy["node_id"],
                "runtime_id": legacy["runtime_id"],
                "reason": "shutdown",
            })
        assert code == 200 and stopped["enabled"] is True
    finally:
        before.db.close()

    after = ha.HostApp(data_dir)
    try:
        code, current = after.register_node({
            "project_root": "/shows/demo",
            "convoy_id": CONVOY_A,
            "comp_path": "/Embody",
            "runtime_id": "rt_current",
            "node_discriminator": DISC_A,
            "metadata": {"node_name": "current", "process_id": 51002},
        })
        assert code == 200, current
        assert current["node_id"] == legacy["node_id"]
        assert current["runtime_id"] == "rt_current"
    finally:
        after.db.close()


@pytest.mark.parametrize("query", [
    "?convoy_id=",
    "?convoy_id=studio-a&convoy_id=studio-b",
    "?unknown=value",
    "?convoy_id=studio-a&unknown=value",
])
def test_network_nodes_http_route_rejects_ambiguous_or_unknown_query(
        server, query):
    code, payload = server.call("/network/nodes" + query, method="GET")
    assert code == 400
    assert payload["reason"] == "malformed"


def test_network_nodes_http_route_accepts_one_namespace_and_requires_token(
        server):
    code, payload = server.call(
        "/network/nodes?convoy_id=studio-a", method="GET")
    assert code == 200
    assert payload["convoy_id"] == "studio-a"

    code, payload = server.call(
        "/network/nodes?convoy_id=studio-a", token=None, method="GET")
    assert code == 401
    assert payload["reason"] == "unauthenticated"


def test_missed_heartbeat_ages_a_hard_killed_node_offline(tmp_path):
    clock = [1000.0]
    instance = ha.HostApp(str(tmp_path / "heartbeat"), now=lambda: clock[0])
    try:
        node = _register(instance, metadata={
            "node_name": "render-a", "process_id": 12345})
        code, current = instance.network_nodes(CONVOY_A)
        assert code == 200
        row = next(r for r in current["nodes"]
                   if r["node_id"] == node["node_id"])
        assert row["status"] == "online"

        clock[0] += ha.NODE_HEARTBEAT_GRACE_S + 0.001
        code, current = instance.network_nodes(CONVOY_A)
        assert code == 200
        row = next(r for r in current["nodes"]
                   if r["node_id"] == node["node_id"])
        assert row["status"] == "offline"
        assert row["online"] is False
    finally:
        instance.db.close()


def test_a_register_landing_during_a_flight_is_reflected_in_that_read(
        app, tmp_path, monkeypatch):
    """A membership mutation that lands DURING a directory refresh supersedes
    that flight's projection, so the read recomputes rather than return a
    pre-register directory.

    This is read-your-own-write for ConvoyExt, which registers and then reads
    the directory in the same worker turn. The generation fence is applied to
    the RETURNED result, not only to the cache write.
    """
    _admit_peer(app, tmp_path, 3)
    registered = {}
    fetches = []

    def fetch(target, keys, convoy_id, timeout, pool=None):
        fetches.append(target.host_id)
        if not registered:
            # A /register lands mid-flight: it bumps the directory generation
            # and clears the cache under the app lock while this fanout runs.
            registered.update(
                _register(app, discriminator=DISC_B, runtime_id="rt_mid"))
        return {"ok": True, "host_id": target.host_id,
                "convoy_id": convoy_id, "nodes": []}

    monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
    code, payload = app.network_nodes(CONVOY_A)

    assert code == 200
    assert registered, "the mid-flight register never ran"
    node_ids = {row["node_id"] for row in payload["nodes"]}
    assert registered["node_id"] in node_ids, (
        "the directory returned to the caller predated its own register")
    assert payload.get("stale") is not True
    # The superseded first projection forced exactly one recompute.
    assert len(fetches) == 2


def test_a_stable_flight_is_not_marked_superseded_or_recomputed(
        app, tmp_path, monkeypatch):
    """The fence must not fire when nothing mutates: a plain refresh with no
    membership change returns authoritative (never stale) after ONE fanout."""
    _admit_peer(app, tmp_path, 4)
    fetches = []

    def fetch(target, keys, convoy_id, timeout, pool=None):
        fetches.append(target.host_id)
        return {"ok": True, "host_id": target.host_id,
                "convoy_id": convoy_id, "nodes": []}

    monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
    code, payload = app.network_nodes(CONVOY_A)
    assert code == 200
    assert "stale" not in payload
    assert len(fetches) == 1
