"""Local controller -> host -> pinned peer -> remote durable job.

This is the first production outbound vertical slice.  It deliberately
uses the real mutual-TLS peer server/client and HostApp's loopback relay
surface; only TouchDesigner/Envoy is replaced by the host-native ping.
"""

import threading
from types import SimpleNamespace

import pytest

import convoy_hostapp as ha
import convoy_capabilities as capabilities
import convoy_peerclient as peerclient
import convoy_peerserver as ps


CONVOY = "studio"


class RelayMesh:
    def __init__(self, tmp_path):
        self.a = ha.HostApp(str(tmp_path / "a"))
        self.b = ha.HostApp(str(tmp_path / "b"))

        # B must trust A before its mutual-TLS listener can accept A.
        with self.b.lock:
            self.b.peers.admit(
                self.a.host_id, self.a.hostkeys.fingerprint,
                cert_pem=self.a.hostkeys.certificate_pem,
                convoy_ids=[CONVOY])

        self.server, self.port = ps.serve_lan(self.b, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.b.set_lan_server(self.server, self.thread,
                              "127.0.0.1", self.port)

        # A's outbound pin and endpoint for B.
        with self.a.lock:
            self.a.peers.admit(
                self.b.host_id, self.b.hostkeys.fingerprint,
                cert_pem=self.b.hostkeys.certificate_pem,
                endpoints=["127.0.0.1:%d" % self.port],
                convoy_ids=[CONVOY])
        code, result = self.b.register_node({
            "project_root": "/show", "comp_path": "/Embody",
            "convoy_id": CONVOY, "runtime_id": "rt_remote"})
        assert code == 200
        self.node = result

    def close(self):
        self.b.stop_lan_server()
        self.a.db.close()
        self.b.db.close()


def test_peer_controller_sanitizer_rejects_scalar_collection_shapes():
    target = SimpleNamespace(host_id="b" * 32)
    row = {
        "controller_id": "controller",
        "selected_node_id": None,
        "leases": "not-a-list",
        "active_jobs": {"not": "a list"},
        "origin_host_ids": "origin-must-not-be-split-into-characters",
    }
    clean = ha.HostApp._sanitize_peer_controller(row, target, CONVOY)
    assert clean["leases"] == []
    assert clean["active_jobs"] == []
    assert clean["origin_host_ids"] == []


@pytest.fixture
def mesh(tmp_path):
    value = RelayMesh(tmp_path)
    yield value
    value.close()


def relay_body(mesh, **overrides):
    body = {
        "target_host_id": mesh.b.host_id,
        "convoy_id": CONVOY,
        "target_node_id": mesh.node["node_id"],
        "controller_id": "bridge-test",
        "operation": "convoy_ping",
        "arguments": {},
        "timeout_s": 30.0,
        "idempotency_key": "relay-ping-1",
    }
    body.update(overrides)
    return body


def test_outbound_relay_persists_dispatches_and_polls(mesh):
    code, accepted = mesh.a.relay_submit(relay_body(mesh))
    assert code == 200 and accepted["ok"] is True
    delivery_id = accepted["job"]["delivery_id"]
    assert accepted["target_host_id"] == mesh.b.host_id

    code, dispatched = mesh.b.dispatch_job(delivery_id)
    assert code == 200
    assert dispatched["job"]["state"] == "succeeded"
    assert dispatched["job"]["result"]["pong"] is True

    code, polled = mesh.a.relay_job({
        "target_host_id": mesh.b.host_id,
        "convoy_id": CONVOY,
        "delivery_id": delivery_id,
    })
    assert code == 200
    assert polled["job"]["state"] == "succeeded"
    assert polled["job"]["result"]["node_id"] == mesh.node["node_id"]


def test_http_manifest_preflight_matches_and_is_refetched_without_advert(
        mesh, monkeypatch):
    manifest_calls = []
    envelope_calls = []
    original_manifest = peerclient.get_peer_manifest
    original_send = peerclient.send_envelope

    def get_manifest(*args, **kwargs):
        manifest_calls.append(1)
        return original_manifest(*args, **kwargs)

    def send(*args, **kwargs):
        envelope_calls.append(1)
        return original_send(*args, **kwargs)

    monkeypatch.setattr(peerclient, "get_peer_manifest", get_manifest)
    monkeypatch.setattr(peerclient, "send_envelope", send)
    for key in ("http-compat-one", "http-compat-two"):
        code, accepted = mesh.a.relay_submit(relay_body(
            mesh, idempotency_key=key))
        assert code == 200 and accepted["ok"] is True, accepted
    # HTTP has no authenticated out-of-band digest advertisement. Re-fetch
    # before each mutation instead of trusting an indefinitely stale cache.
    assert manifest_calls == [1, 1]
    assert envelope_calls == [1, 1]


@pytest.mark.parametrize("change, expected_reason, expected_code", [
    ("absent", "operation_not_exposed", 403),
    ("digest", "incompatible_operation", 409),
    ("protocol", "protocol_mismatch", 409),
])
def test_http_manifest_preflight_refuses_with_zero_envelope_sends(
        mesh, monkeypatch, change, expected_reason, expected_code):
    if change == "absent":
        mesh.b.operations.pop("convoy_ping")
    elif change == "digest":
        mesh.b.operations["convoy_ping"]["schema"] = {
            "different": {"type": "integer"}}
    else:
        original_build = mesh.b.build_remote_manifest

        def future_manifest():
            current = original_build()
            return capabilities.CapabilityManifest(
                "convoy/999", None, current.operations)

        mesh.b.build_remote_manifest = future_manifest

    envelope_calls = []
    monkeypatch.setattr(
        peerclient, "send_envelope",
        lambda *args, **kwargs: envelope_calls.append(1))
    code, refusal = mesh.a.relay_submit(relay_body(
        mesh, idempotency_key=f"http-refuse-{change}"))
    assert code == expected_code
    assert refusal["reason"] == expected_reason
    assert refusal["operation"] == "convoy_ping"
    assert envelope_calls == []


def test_outbound_terminal_ack_is_relayed_and_idempotent(mesh):
    code, accepted = mesh.a.relay_submit(relay_body(
        mesh, idempotency_key="remote-terminal-ack"))
    assert code == 200
    delivery_id = accepted["job"]["delivery_id"]
    code, dispatched = mesh.b.dispatch_job(delivery_id)
    assert code == 200
    assert dispatched["job"]["state"] == "succeeded"

    body = {"target_host_id": mesh.b.host_id, "convoy_id": CONVOY,
            "delivery_id": delivery_id}
    code, first = mesh.a.relay_acknowledge(body)
    assert code == 200 and first["ok"] is True
    assert first["already_acknowledged"] is False
    assert mesh.b.db.get_job(delivery_id)["outcome_acknowledged_at"]

    code, repeated = mesh.a.relay_acknowledge(body)
    assert code == 200 and repeated["ok"] is True
    assert repeated["already_acknowledged"] is True


def test_outbound_relay_refuses_a_namespace_not_in_the_pin(mesh):
    code, result = mesh.a.relay_submit(
        relay_body(mesh, convoy_id="another-show"))
    assert code == 403
    assert result["reason"] == "namespace_not_admitted"


def test_outbound_relay_reuses_the_same_remote_job(mesh):
    first_code, first = mesh.a.relay_submit(relay_body(mesh))
    second_code, second = mesh.a.relay_submit(relay_body(mesh))
    assert first_code == second_code == 200
    assert first["created"] is True
    assert second["created"] is False
    assert first["job"]["delivery_id"] == second["job"]["delivery_id"]


def test_outbound_relay_tries_stale_endpoint_fallback_only_before_send(
        mesh, monkeypatch):
    with mesh.a.lock:
        record = mesh.a.peers.get(mesh.b.host_id)
        mesh.a.peers.admit(
            mesh.b.host_id, mesh.b.hostkeys.fingerprint,
            cert_pem=mesh.b.hostkeys.certificate_pem,
            endpoints=["192.0.2.44:47600", "127.0.0.1:%d" % mesh.port],
            convoy_ids=[CONVOY])
    visited = []

    def send(target, keys, envelope, timeout=None, now=None, pool=None):
        visited.append(target.address)
        if target.address == "192.0.2.44":
            return peerclient.UNREACHABLE
        return {"ok": True, "created": True,
                "job": {"delivery_id": "cj_" + "1" * 32}}

    monkeypatch.setattr(peerclient, "send_envelope", send)
    code, result = mesh.a.relay_submit(relay_body(
        mesh, idempotency_key="endpoint-fallback"))
    assert code == 200 and result["ok"] is True
    assert visited == ["192.0.2.44", "127.0.0.1"]


def test_outbound_relay_never_falls_through_after_ambiguous_send(
        mesh, monkeypatch):
    with mesh.a.lock:
        mesh.a.peers.admit(
            mesh.b.host_id, mesh.b.hostkeys.fingerprint,
            cert_pem=mesh.b.hostkeys.certificate_pem,
            endpoints=["192.0.2.45:47600", "127.0.0.1:%d" % mesh.port],
            convoy_ids=[CONVOY])
    visited = []

    def send(target, keys, envelope, timeout=None, now=None, pool=None):
        visited.append(target.address)
        return None

    monkeypatch.setattr(peerclient, "send_envelope", send)
    code, result = mesh.a.relay_submit(relay_body(
        mesh, idempotency_key="endpoint-ambiguous"))
    assert code == 202
    assert result["reason"] == "delivery_indeterminate"
    assert visited == ["192.0.2.45"]


def test_outbound_relay_conflicting_idempotency_is_409(mesh):
    code, first = mesh.a.relay_submit(relay_body(mesh))
    assert code == 200 and first["ok"] is True
    code, conflict = mesh.a.relay_submit(
        relay_body(mesh, arguments={"different": True}))
    assert code == 409
    assert conflict["reason"] == "idempotency_content_conflict"


def test_outbound_relay_can_cancel_its_remote_queued_job(mesh):
    code, accepted = mesh.a.relay_submit(relay_body(
        mesh, idempotency_key="remote-cancel"))
    assert code == 200
    delivery_id = accepted["job"]["delivery_id"]
    code, cancelled = mesh.a.relay_cancel({
        "target_host_id": mesh.b.host_id,
        "convoy_id": CONVOY,
        "delivery_id": delivery_id,
    })
    assert code == 200
    assert cancelled["cancelled"] is True
    assert cancelled["definitive"] is True
    code, view = mesh.a.relay_job({
        "target_host_id": mesh.b.host_id,
        "convoy_id": CONVOY,
        "delivery_id": delivery_id,
    })
    assert code == 200
    assert view["job"]["state"] == "refused"


def test_controller_listing_federates_target_host_state_without_wake(mesh):
    code, accepted = mesh.a.relay_submit(relay_body(
        mesh, idempotency_key="controller-view"))
    assert code == 200 and accepted["ok"] is True
    code, view = mesh.a.network_controllers(CONVOY)
    assert code == 200
    assert view["wakes_touchdesigner"] is False
    rows = [row for row in view["controllers"]
            if row["host_id"] == mesh.b.host_id]
    assert len(rows) == 1
    assert rows[0]["controller_id"].endswith(":bridge-test")
    assert rows[0]["selected_node_id"] == mesh.node["node_id"]
    assert rows[0]["active_jobs"][0]["delivery_id"] == \
        accepted["job"]["delivery_id"]


def test_idle_selected_controller_heartbeats_over_the_peer_session(mesh):
    body = {
        "target_host_id": mesh.b.host_id,
        "convoy_id": CONVOY,
        "controller_id": "idle-editor",
        "selected_node_id": mesh.node["node_id"],
        "label": "Cursor session",
    }
    code, heartbeat = mesh.a.relay_heartbeat(body)
    assert code == 200 and heartbeat["ok"] is True
    code, view = mesh.a.network_controllers(CONVOY)
    assert code == 200
    rows = [row for row in view["controllers"]
            if row["host_id"] == mesh.b.host_id
            and row["controller_id"].endswith(":idle-editor")]
    assert len(rows) == 1
    assert rows[0]["selected_node_id"] == mesh.node["node_id"]
    assert rows[0]["label"] == "Cursor session"
    assert rows[0]["active_jobs"] == []

    code, cleared = mesh.a.relay_heartbeat(dict(
        body, selected_node_id=None, clear_selected=True))
    assert code == 200 and cleared["ok"] is True
    code, view = mesh.a.network_controllers(CONVOY)
    assert not [row for row in view["controllers"]
                if row["controller_id"].endswith(":idle-editor")]


def test_same_host_sibling_uses_the_durable_job_path(tmp_path):
    app = ha.HostApp(str(tmp_path / "one-host"))
    try:
        code, node = app.register_node({
            "project_root": "/show/local", "comp_path": "/Embody",
            "convoy_id": CONVOY, "runtime_id": "rt_local",
        })
        assert code == 200
        body = {
            "target_host_id": app.host_id,
            "convoy_id": CONVOY,
            "target_node_id": node["node_id"],
            "controller_id": "bridge-local",
            "operation": "convoy_ping",
            "arguments": {},
            "timeout_s": 5.0,
            "idempotency_key": "local-sibling-ping",
        }
        code, accepted = app.relay_submit(body)
        assert code == 200 and accepted["ok"] is True
        assert accepted["local_sibling"] is True
        assert accepted["target_host_id"] == app.host_id
        delivery_id = accepted["job"]["delivery_id"]

        code, dispatched = app.dispatch_job(delivery_id)
        assert code == 200
        assert dispatched["job"]["state"] == "succeeded"
        code, polled = app.relay_job({
            "target_host_id": app.host_id,
            "convoy_id": CONVOY,
            "delivery_id": delivery_id,
        })
        assert code == 200 and polled["local_sibling"] is True
        assert polled["job"]["result"]["pong"] is True

        code, acknowledged = app.relay_acknowledge({
            "target_host_id": app.host_id, "convoy_id": CONVOY,
            "delivery_id": delivery_id,
        })
        assert code == 200 and acknowledged["local_sibling"] is True
        assert acknowledged["already_acknowledged"] is False
    finally:
        app.db.close()


def test_same_host_relay_never_crosses_the_realm_namespace(tmp_path):
    app = ha.HostApp(str(tmp_path / "one-host"))
    try:
        _, node = app.register_node({
            "project_root": "/show/local", "comp_path": "/Embody",
            "convoy_id": CONVOY, "runtime_id": "rt_local",
        })
        body = {
            "target_host_id": app.host_id,
            "convoy_id": "another-show",
            "target_node_id": node["node_id"],
            "controller_id": "bridge-local",
            "operation": "convoy_ping", "arguments": {},
            "timeout_s": 5.0, "idempotency_key": "wrong-realm",
        }
        code, refused = app.relay_submit(body)
        assert code == 404
        assert refused["reason"] == "unknown_node"
    finally:
        app.db.close()


def test_queued_td_delivery_can_be_cancelled_before_wake(tmp_path):
    app = ha.HostApp(str(tmp_path / "cancel"))
    try:
        _, node = app.register_node({
            "project_root": "/show/local", "comp_path": "/Embody",
            "convoy_id": CONVOY, "runtime_id": "rt_local",
        })
        code, accepted = app.relay_submit({
            "target_host_id": app.host_id, "convoy_id": CONVOY,
            "target_node_id": node["node_id"],
            "controller_id": "bridge-local",
            "operation": "query_network", "arguments": {"path": "/"},
            "timeout_s": 5.0, "idempotency_key": "cancel-before-dispatch",
        })
        assert code == 200
        delivery_id = accepted["job"]["delivery_id"]
        code, cancelled = app.cancel_host_job({
            "delivery_id": delivery_id, "convoy_id": CONVOY,
            "origin_host_id": app.host_id,
        })
        assert code == 200
        assert cancelled["cancelled"] is True
        assert cancelled["definitive"] is True
        assert cancelled["job"]["state"] == "refused"
        assert cancelled["job"]["result"]["reason"] == \
            "cancelled_before_dispatch"
    finally:
        app.db.close()
