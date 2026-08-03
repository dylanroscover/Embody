"""Host app <-> LAN wiring: submit_envelope's peer path (signer choice,
channel binding), the trust material, /lan/status and /identity, and the
startup gate that keeps a default build off-box.

These are the FAST, socket-free counterparts to test_convoy_peerserver's
end-to-end mTLS tests: they drive submit_envelope and the wiring methods
directly, so a regression in the gate order or the signer choice is
caught without a handshake.
"""

import json
import os
import socket
import threading

import pytest

import convoy_hostapp as ha
import convoy_hostkeys as hk
import convoy_lan as lan_mod
import convoy_peers as peers_mod
import convoy_protocol as protocol
from test_convoy_hostapp import Server        # noqa: F401 -- loopback fixture


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


def _identity(tmp_path, name):
    return hk.load_or_create(str(tmp_path / name))


# -- /identity now carries the public certificate (for pinning) --------

def test_identity_route_carries_the_certificate_pem(server):
    code, body = server.call("/identity")
    assert code == 200
    assert body["certificate_pem"], "a peer must be able to obtain the cert"
    assert "BEGIN CERTIFICATE" in body["certificate_pem"]
    # It IS this host's cert -- its SPKI fingerprint matches the identity.
    assert body["fingerprint"] == server.app.hostkeys.fingerprint


# -- /lan/status: off is distinguishable from broken -------------------

def test_lan_status_reports_disabled_by_default(server):
    code, body = server.call("/lan/status")
    assert code == 200
    assert body["lan_bound"] is False
    assert body["lan_reason"] == "disabled"
    assert body["lan_port"] is None


def test_lan_status_counts_trust_anchors(server):
    # No peers -> no anchors. Admit one WITH a cert -> one anchor.
    other = hk.load_or_create(str(server.app.data_dir) + "-peerx")
    with server.app.lock:
        server.app.peers.admit("a" * 32, other.fingerprint,
                               cert_pem=other.certificate_pem)
    code, body = server.call("/lan/status")
    assert code == 200
    assert body["lan_trust_anchors"] == 1


def test_lan_status_is_loopback_only_not_in_the_peer_table():
    # A structural cross-check: the LAN handler's route constants do not
    # include /lan/status. (The peerserver test sweeps this over the
    # wire; this pins the intent at the source.)
    import convoy_peerserver as ps
    for route in (ps.ROUTE_HEALTH, ps.ROUTE_MANIFEST, ps.ROUTE_ENVELOPE,
                  ps.ROUTE_JOBS_PREFIX):
        assert not route.startswith("/lan/")


# -- lan_trust_material ------------------------------------------------

def test_trust_material_includes_only_peers_with_a_cert(tmp_path):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        withcert = _identity(tmp_path, "withcert")
        nocert = _identity(tmp_path, "nocert")
        with app.lock:
            app.peers.admit("a" * 32, withcert.fingerprint,
                            cert_pem=withcert.certificate_pem)
            # Admitted, but no pinned certificate -> cannot be a TLS anchor.
            app.peers.admit("b" * 32, nocert.fingerprint, cert_pem=None)
        _sig, pems = app.lan_trust_material()
        # The store normalizes trailing whitespace, so compare stripped.
        stripped = [p.strip() for p in pems]
        assert withcert.certificate_pem.strip() in stripped
        assert nocert.certificate_pem.strip() not in stripped
        assert len(pems) == 1
    finally:
        app.db.close()


def test_a_blocked_peer_stays_a_trust_anchor(tmp_path):
    """A blocked peer keeps its pin so it still HANDSHAKES and is then
    refused by authorize_peer -- which is what makes the verifier-spy
    order test meaningful. So the trust store must still hold it."""
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        peer = _identity(tmp_path, "peer")
        with app.lock:
            app.peers.admit("a" * 32, peer.fingerprint,
                            cert_pem=peer.certificate_pem)
            app.peers.block("a" * 32)
        _sig, pems = app.lan_trust_material()
        assert peer.certificate_pem.strip() in [p.strip() for p in pems]
    finally:
        app.db.close()


def test_forget_drops_the_trust_anchor_and_changes_the_signature(tmp_path):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        peer = _identity(tmp_path, "peer")
        with app.lock:
            app.peers.admit("a" * 32, peer.fingerprint,
                            cert_pem=peer.certificate_pem)
        sig1, pems1 = app.lan_trust_material()
        with app.lock:
            app.peers.forget("a" * 32)
        sig2, pems2 = app.lan_trust_material()
        cert = peer.certificate_pem.strip()
        assert cert in [p.strip() for p in pems1]
        assert cert not in [p.strip() for p in pems2]
        assert sig1 != sig2, "forgetting a peer must rebuild the trust store"
    finally:
        app.db.close()


# -- submit_envelope, the peer path, socket-free -----------------------

class TwoHosts:
    def __init__(self, tmp_path):
        self.a = ha.HostApp(str(tmp_path / "a"))
        self.b = ha.HostApp(str(tmp_path / "b"))
        with self.b.lock:
            self.b.peers.admit(self.a.host_id, self.a.hostkeys.fingerprint,
                               cert_pem=self.a.hostkeys.certificate_pem,
                               convoy_ids=["studio"])

    def origin(self, host_id=None, fingerprint=None, public_der=...):
        return {
            "host_id": host_id or self.a.host_id,
            "fingerprint": fingerprint or self.a.hostkeys.fingerprint,
            "public_der": (self.a.hostkeys.public_der
                           if public_der is ... else public_der),
        }

    def node(self, convoy_id="studio"):
        code, body = self.b.register_node({
            "project_root": "/W/p", "comp_path": "/E",
            "convoy_id": convoy_id, "runtime_id": "rt-1"})
        assert code == 200
        return body["node_id"], convoy_id

    def env(self, node_id, convoy_id, signer=None, origin_host_id=None,
            source_host_id=None, operation="convoy_ping"):
        return {"envelope": protocol.build_envelope(
            convoy_id, origin_host_id or self.a.host_id, "ctl", node_id,
            operation, signer or self.a.hostkeys.signer(),
            source_host_id=source_host_id, timeout_s=60.0)}

    def submit(self, body, origin):
        with self.b.lock:
            return self.b.submit_envelope(body, origin)

    def close(self):
        self.a.db.close()
        self.b.db.close()


@pytest.fixture
def two(tmp_path):
    t = TwoHosts(tmp_path)
    yield t
    t.close()


def test_peer_path_verifies_with_the_ed25519_key(two):
    node_id, convoy_id = two.node()
    code, body = two.submit(two.env(node_id, convoy_id), two.origin())
    assert code == 200 and body["created"] is True
    assert body["job"]["origin_host_id"] == two.a.host_id


def test_peer_path_refuses_a_psk_signed_envelope(two):
    node_id, convoy_id = two.node()
    body = two.env(node_id, convoy_id, signer=protocol.HmacSigner("k" * 20))
    code, resp = two.submit(body, two.origin())
    assert resp["reason"] == "algorithm_mismatch"


def test_peer_path_channel_binding_refuses_a_third_origin(two):
    node_id, convoy_id = two.node()
    body = two.env(node_id, convoy_id, origin_host_id="c" * 32)
    code, resp = two.submit(body, two.origin())
    assert code == 403 and resp["reason"] == "source_mismatch"


def test_peer_path_refuses_a_missing_public_key(two):
    node_id, convoy_id = two.node()
    code, resp = two.submit(two.env(node_id, convoy_id),
                            two.origin(public_der=None))
    assert resp["reason"] == "peer_key_unusable"


def test_loopback_path_is_unchanged_and_uses_the_psk(two):
    """The positive control: the SAME operation over the loopback origin
    still works with the group PSK -- the signer choice is by LISTENER,
    and loopback is untouched."""
    node_id, convoy_id = two.node()
    psk = two.b.db.ensure_convoy_psk(convoy_id)
    env = {"envelope": protocol.build_envelope(
        convoy_id, two.b.host_id, "ctl-local", node_id, "convoy_ping",
        protocol.HmacSigner(psk), timeout_s=60.0)}
    with two.b.lock:
        code, resp = two.b.submit_envelope(env, ha.LOOPBACK_ORIGIN)
    assert code == 200 and resp["created"] is True


def test_the_remote_manifest_excludes_the_worker_only_operations(two):
    # run_tests and save_project are LOCAL-only (remote_exposed=False,
    # finding 544): present in the local manifest, filtered from the remote
    # one. convoy_ping and the rest remain on both.
    remote = two.b.build_remote_manifest().to_dict()["operations"]
    local = two.b.build_manifest().to_dict()["operations"]
    assert "run_tests" in local and "save_project" in local
    assert "run_tests" not in remote and "save_project" not in remote
    assert "convoy_ping" in remote
    assert set(remote) == set(local) - {"run_tests", "save_project"}


# -- enabled-node-driven LAN lifecycle ---------------------------------

def _enable_node(app):
    code, node = app.register_node({
        "project_root": "/Work/p", "convoy_id": "studio",
        "comp_path": "/Embody", "runtime_id": "rt1"})
    assert code == 200, node
    return node

def test_no_lan_json_means_no_socket(tmp_path):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        assert ha.start_lan_if_configured(app, log=lambda m: None) is False
        assert app.lan_server is None
        assert app.lan_reason == "no_enabled_nodes"
    finally:
        app.db.close()


def test_enabled_but_no_certificate_refuses_to_bind(tmp_path, monkeypatch):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        _enable_node(app)
        # Simulate an identity with no TLS certificate (envelopes still
        # sign; TLS cannot serve).
        monkeypatch.setattr(app.hostkeys, "certificate_pem", None)
        _write_lan(app.data_dir, {"enabled": True})
        assert ha.start_lan_if_configured(app, log=lambda m: None) is False
        assert app.lan_server is None
        assert app.lan_reason == "no_certificate"
    finally:
        app.db.close()


def test_a_malformed_lan_json_refuses_and_keeps_loopback(tmp_path):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        _enable_node(app)
        with open(os.path.join(app.data_dir, lan_mod.LAN_FILE), "w") as f:
            f.write("{not json")
        assert ha.start_lan_if_configured(app, log=lambda m: None) is False
        assert app.lan_reason == "lan_config_malformed"
    finally:
        app.db.close()


def test_start_binds_and_stop_is_idempotent(tmp_path, monkeypatch):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        _enable_node(app)
        port = _free_port()
        _write_lan(app.data_dir, {"enabled": True, "port": port})
        # Force a loopback bind for the test (a real interface would
        # expose the socket; resolve_bind refuses loopback by design, so
        # override it just here).
        monkeypatch.setattr(lan_mod, "resolve_bind",
                            lambda config, **k: "127.0.0.1")
        assert ha.start_lan_if_configured(app, log=lambda m: None) is True
        assert app.lan_server is not None
        assert app.lan_port == port
        code, body = 200, app.lan_status()[1]
        assert body["lan_bound"] is True
        assert body["lan_address"] == "127.0.0.1"
        # Stop is idempotent and actually frees the port.
        app.stop_lan_server()
        app.stop_lan_server()
        assert app.lan_server is None
    finally:
        app.db.close()


def test_absent_lan_json_uses_automatic_membership_defaults(
        tmp_path, monkeypatch):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        _enable_node(app)
        port = _free_port()
        monkeypatch.setattr(
            lan_mod, "load_config",
            lambda _data: lan_mod.LanConfig(
                enabled=False, port=port, bind="auto", present=False))
        monkeypatch.setattr(lan_mod, "resolve_bind",
                            lambda config, **k: "127.0.0.1")
        assert ha.start_lan_if_configured(app, log=lambda m: None) is True
        assert app.lan_port == port
    finally:
        app.stop_lan_server()
        app.db.close()


def test_desired_lan_endpoint_honors_admin_disable(tmp_path):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        _enable_node(app)
        _write_lan(app.data_dir, {"enabled": False})
        with pytest.raises(lan_mod.LanConfigError) as refusal:
            ha.desired_lan_endpoint(app)
        assert refusal.value.reason == "admin_disabled"
    finally:
        app.db.close()


def test_lan_reconciler_rebinds_after_routed_address_changes(
        tmp_path, monkeypatch):
    app = ha.HostApp(str(tmp_path / "host"))
    try:
        _enable_node(app)
        app.lan_server = object()
        app.discovery_service = object()
        app.lan_address = "192.0.2.10"
        app.lan_port = 47600
        events = []

        monkeypatch.setattr(
            ha, "desired_lan_endpoint",
            lambda _app: ("192.0.2.11", 47600))

        def stop():
            events.append(("stop", app.lan_address, app.lan_port))
            app.lan_server = None
            app.discovery_service = None

        def start(_app, log=None):
            events.append(("start", "192.0.2.11", 47600))
            app.lan_server = object()
            app.discovery_service = object()
            app.lan_address = "192.0.2.11"
            app.lan_port = 47600
            return True

        monkeypatch.setattr(app, "stop_lan_server", stop)
        monkeypatch.setattr(ha, "start_lan_if_configured", start)

        app._reconcile_lan_once()

        assert events == [
            ("stop", "192.0.2.10", 47600),
            ("start", "192.0.2.11", 47600),
        ]
        assert app.lan_address == "192.0.2.11"
    finally:
        app.db.close()


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _write_lan(data_dir, obj):
    with open(os.path.join(data_dir, lan_mod.LAN_FILE), "w",
              encoding="utf-8") as f:
        f.write(json.dumps(obj))
