"""The pinned peer client: no plaintext path, pin recomputed and never
auto-updated, nested timeouts, and the reconciliation contract
(UNREACHABLE vs None) that keeps a mutation from double-running.
"""

import ast
import os
import threading

import pytest

import convoy_hostapp as ha
import convoy_hostkeys as hk
import convoy_peerclient as pc
import convoy_peerserver as ps
import convoy_protocol as protocol


HERE = os.path.dirname(os.path.abspath(__file__))


# -- L-04: THERE IS NO PLAINTEXT CODE PATH -----------------------------

def test_the_client_has_no_plaintext_opener():
    """A source-level guard (S7-style: its absence is invisible at
    runtime). The module must never construct a non-TLS connection or
    name an http:// URL -- the peer leg has no fallback to fall back TO."""
    with open(os.path.join(HERE, "convoy_peerclient.py"),
              "r", encoding="utf-8") as f:
        source = f.read()
    assert "http://" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # http.client.HTTPConnection (the plaintext one) must never be
        # referenced; only HTTPSConnection is allowed.
        if isinstance(node, ast.Attribute):
            assert node.attr != "HTTPConnection", (
                "the peer client must not use a plaintext HTTPConnection")
        if isinstance(node, ast.Name):
            assert node.id != "HTTPConnection"
    # urllib (the loopback clients' opener) has no place here either.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("urllib"), (
                    "the peer client must not import urllib")
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("urllib")


# -- a live mesh, reusing the server harness ---------------------------

class Mesh:
    def __init__(self, tmp_path):
        self.a = ha.HostApp(str(tmp_path / "a"))
        self.b = ha.HostApp(str(tmp_path / "b"))
        with self.b.lock:
            self.b.peers.admit(self.a.host_id, self.a.hostkeys.fingerprint,
                               cert_pem=self.a.hostkeys.certificate_pem)
        self.server, self.port = ps.serve_lan(self.b, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.b.set_lan_server(self.server, self.thread, "127.0.0.1", self.port)

    def target(self, **over):
        kw = dict(host_id=self.b.host_id, address="127.0.0.1", port=self.port,
                  pinned_cert_pem=self.b.hostkeys.certificate_pem,
                  expected_fingerprint=self.b.hostkeys.fingerprint)
        kw.update(over)
        return pc.PeerTarget(**kw)

    def node(self, convoy_id="studio"):
        with self.b.lock:
            code, body = self.b.register_node({
                "project_root": "/W/p", "comp_path": "/E",
                "convoy_id": convoy_id, "runtime_id": "rt-1"})
        assert code == 200
        return body["node_id"], convoy_id

    def envelope(self, node_id, convoy_id, **over):
        return protocol.build_envelope(
            convoy_id, self.a.host_id, "ctl", node_id,
            over.get("operation", "convoy_ping"), self.a.hostkeys.signer(),
            arguments=over.get("arguments", {}),
            timeout_s=over.get("timeout_s", 60.0),
            idempotency_key=over.get("idempotency_key"))

    def stop(self):
        self.b.stop_lan_server()
        self.a.db.close()
        self.b.db.close()


@pytest.fixture
def mesh(tmp_path):
    m = Mesh(tmp_path)
    yield m
    m.stop()


# -- the pinned round trip ---------------------------------------------

def test_a_pinned_round_trip_reaches_the_peer(mesh):
    body = pc.get_peer_health(mesh.target(), mesh.a.hostkeys)
    assert body["ok"] is True and body["host_id"] == mesh.b.host_id


def test_send_envelope_over_the_pinned_channel(mesh):
    node_id, convoy_id = mesh.node()
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys,
                              mesh.envelope(node_id, convoy_id))
    assert result["ok"] is True and result["created"] is True


# -- L-05: the pin is never auto-updated -------------------------------

def test_a_wrong_expected_fingerprint_is_a_pin_mismatch(mesh):
    """Trust store holds B's real cert (handshake succeeds), but the
    EXPECTED pin is wrong: the post-handshake recompute catches it."""
    wrong = "cvfp1-" + "0000-" * 7 + "0000"
    target = mesh.target(expected_fingerprint=wrong)
    result = pc.get_peer_health(target, mesh.a.hostkeys)
    assert isinstance(result, pc._PinMismatch)
    # The offered key is reported, and the pin the client held is
    # UNCHANGED -- the client never writes it back.
    assert result.offered == mesh.b.hostkeys.fingerprint
    assert result.expected == wrong


def test_a_cert_from_a_stranger_fails_the_pin_at_tls(mesh, tmp_path):
    """Trust store holds a THIRD host's cert, so B's real cert does not
    validate at the TLS layer -> pin_mismatch before any byte is sent."""
    stranger = hk.load_or_create(str(tmp_path / "stranger"))
    target = mesh.target(pinned_cert_pem=stranger.certificate_pem,
                         expected_fingerprint=stranger.fingerprint)
    result = pc.get_peer_health(target, mesh.a.hostkeys)
    assert isinstance(result, pc._PinMismatch)


def test_pin_mismatch_carries_the_operator_message(mesh):
    target = mesh.target(expected_fingerprint="cvfp1-" + "0000-" * 7 + "0000")
    result = pc.get_peer_health(target, mesh.a.hostkeys)
    assert isinstance(result, pc._PinMismatch)
    assert "will not connect until you decide" in result.message
    assert mesh.b.host_id in result.message


# -- UNREACHABLE vs None: the reconciliation contract ------------------

def test_a_refused_connection_is_unreachable(mesh):
    """No listener on the port -> connection refused -> did NOT arrive ->
    UNREACHABLE, retry-safe with the same idempotency_key."""
    # Point at a port nothing listens on (the server's port + a large
    # offset is almost certainly closed; a refusal is what we assert).
    dead = mesh.target(port=1)      # port 1 is not our listener
    node_id, convoy_id = mesh.node()
    result = pc.send_envelope(dead, mesh.a.hostkeys,
                              mesh.envelope(node_id, convoy_id))
    assert result is pc.UNREACHABLE


def test_an_expired_envelope_is_not_even_sent(mesh):
    node_id, convoy_id = mesh.node()
    env = mesh.envelope(node_id, convoy_id, timeout_s=60.0)
    # Force expiry by evaluating the timeout far in the future.
    result = pc.send_envelope(
        mesh.target(), mesh.a.hostkeys, env,
        now=env["deadline_unix"] + 10.0)
    assert result["reason"] == "deadline_exceeded"


# -- nested timeouts ---------------------------------------------------

def test_peer_timeout_is_nested_under_the_deadline():
    env = protocol.build_envelope(
        "c", "h" * 32, "ctl", "n" * 32, "convoy_ping",
        protocol.HmacSigner("k" * 10), timeout_s=5.0, now=1000.0)
    # 5s of budget, ceiling is higher -> the budget wins (nested, not
    # stacked): the socket wait never exceeds the envelope's own life.
    got = pc.peer_timeout_for(env, now=1000.0)
    assert got == pytest.approx(5.0, abs=0.01)


def test_peer_timeout_is_ceilinged():
    env = protocol.build_envelope(
        "c", "h" * 32, "ctl", "n" * 32, "convoy_ping",
        protocol.HmacSigner("k" * 10), timeout_s=3600.0, now=1000.0)
    got = pc.peer_timeout_for(env, ceiling=15.0, now=1000.0)
    assert got == 15.0


def test_peer_timeout_is_zero_when_expired():
    env = protocol.build_envelope(
        "c", "h" * 32, "ctl", "n" * 32, "convoy_ping",
        protocol.HmacSigner("k" * 10), timeout_s=5.0, now=1000.0)
    assert pc.peer_timeout_for(env, now=2000.0) == 0.0


def test_peer_timeout_has_a_floor_for_a_nearly_expired_envelope():
    env = protocol.build_envelope(
        "c", "h" * 32, "ctl", "n" * 32, "convoy_ping",
        protocol.HmacSigner("k" * 10), timeout_s=5.0, now=1000.0)
    # 0.5s of budget left -> floored so a valid envelope still gets a
    # real attempt, never a doomed 0.5s socket timeout.
    got = pc.peer_timeout_for(env, now=1004.5)
    assert got == pc._MIN_PEER_TIMEOUT_S
