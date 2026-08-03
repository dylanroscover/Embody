"""The pinned peer client: no plaintext path, pin recomputed and never
auto-updated, nested timeouts, and the reconciliation contract
(UNREACHABLE vs None) that keeps a mutation from double-running.
"""

import ast
import os
import threading
import time

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
                               cert_pem=self.a.hostkeys.certificate_pem,
                               convoy_ids=["studio"])
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


def test_persistent_pool_reuses_one_tls_handshake_for_control_requests(mesh):
    pool = pc.PeerConnectionPool(mesh.a.hostkeys, idle_s=60)
    try:
        first = pc.get_peer_health(
            mesh.target(), mesh.a.hostkeys, pool=pool)
        second = pc.get_peer_manifest(
            mesh.target(), mesh.a.hostkeys, pool=pool)
        assert first["ok"] is True and second["ok"] is True
        assert pool.stats() == {"targets": 1, "handshakes": 1}
    finally:
        pool.close()


def test_persistent_pool_reauthorizes_and_drops_a_blocked_peer(mesh):
    pool = pc.PeerConnectionPool(mesh.a.hostkeys, idle_s=60)
    try:
        assert pc.get_peer_health(
            mesh.target(), mesh.a.hostkeys, pool=pool)["ok"] is True
        with mesh.b.lock:
            mesh.b.peers.block(mesh.a.host_id)
        refused = pc.get_peer_health(
            mesh.target(), mesh.a.hostkeys, pool=pool)
        assert refused["ok"] is False
        assert refused["reason"] == "peer_blocked"
    finally:
        pool.close()


def _contention_target():
    return pc.PeerTarget(
        "1" * 32, "127.0.0.1", 47600, "test-cert", "test-pin")


def test_pool_contention_timeout_expires_before_any_bytes_are_sent():
    pool = pc.PeerConnectionPool(object(), idle_s=60)
    target = _contention_target()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_request(entry, request_target, method, path, body, timeout):
        calls.append(timeout)
        entered.set()
        assert release.wait(timeout=2.0)
        return {"ok": True}

    pool._request_locked = blocked_request
    first_result = []
    first = threading.Thread(target=lambda: first_result.append(
        pool.request(target, "GET", "/first", None, 1.0)))
    try:
        first.start()
        assert entered.wait(timeout=1.0)
        started = time.monotonic()
        second = pool.request(target, "GET", "/second", None, 0.05)
        elapsed = time.monotonic() - started

        assert second is pc.UNREACHABLE
        assert elapsed < 0.5
        assert len(calls) == 1
    finally:
        release.set()
        first.join(timeout=2.0)
        pool.close()
    assert first_result == [{"ok": True}]


def test_pool_contention_passes_only_the_remaining_budget_to_transport():
    pool = pc.PeerConnectionPool(object(), idle_s=60)
    target = _contention_target()
    entered = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    calls = []

    def recorded_request(entry, request_target, method, path, body, timeout):
        calls.append((path, timeout))
        if path == "/first":
            entered.set()
            assert release.wait(timeout=2.0)
        return {"ok": True}

    pool._request_locked = recorded_request
    first = threading.Thread(target=lambda: pool.request(
        target, "GET", "/first", None, 2.0))
    second_result = []

    def request_second():
        second_result.append(pool.request(
            target, "GET", "/second", None, 1.0))
        second_done.set()

    second = threading.Thread(target=request_second)
    try:
        first.start()
        assert entered.wait(timeout=1.0)
        second.start()
        # Keep the second caller queued long enough that passing its original
        # one-second timeout would be observably wrong, with ample CI slack.
        time.sleep(0.15)
        release.set()
        assert second_done.wait(timeout=2.0)
        remaining = dict(calls)["/second"]
        assert 0.0 < remaining < 0.95
        assert second_result == [{"ok": True}]
    finally:
        release.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        pool.close()


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


def test_peer_timeout_never_exceeds_a_nearly_expired_envelope():
    env = protocol.build_envelope(
        "c", "h" * 32, "ctl", "n" * 32, "convoy_ping",
        protocol.HmacSigner("k" * 10), timeout_s=5.0, now=1000.0)
    # 0.5s of signed budget left means at most a 0.5s socket timeout. A
    # convenience floor must never outlive the operation's authority.
    got = pc.peer_timeout_for(env, now=1004.5)
    assert got == pytest.approx(0.5)


def test_an_explicit_timeout_is_still_clamped_to_the_signed_budget(
        mesh, monkeypatch):
    node_id, convoy_id = mesh.node()
    env = mesh.envelope(node_id, convoy_id, timeout_s=5.0)
    seen = []

    def fake_request(target, keys, method, path, body, timeout):
        seen.append(timeout)
        return {"ok": True}

    monkeypatch.setattr(pc, "_request", fake_request)
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env,
                              timeout=999.0, now=env["deadline_unix"] - 0.25)
    assert result["ok"] is True
    assert seen == [pytest.approx(0.25)]


# -- namespace-bound peer node directory ------------------------------

def test_get_peer_nodes_returns_the_real_namespace_filtered_view(mesh):
    node_id, _ = mesh.node("studio")
    body = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, "studio")
    assert body["ok"] is True
    assert body["convoy_id"] == "studio"
    assert [row["node_id"] for row in body["nodes"]] == [node_id]
    assert all(row["convoy_id"] == "studio" for row in body["nodes"])


def test_get_peer_nodes_uses_one_encoded_namespace_segment(mesh):
    calls = []

    def view(host_id, convoy_id, authenticated_fingerprint):
        calls.append((host_id, convoy_id, authenticated_fingerprint))
        return 200, {"ok": True, "convoy_id": convoy_id, "nodes": []}

    mesh.b.peer_nodes_view = view
    result = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, "studio")
    assert result == {"ok": True, "convoy_id": "studio", "nodes": []}
    assert calls == [(mesh.a.host_id, "studio",
                      mesh.a.hostkeys.fingerprint)]


def test_get_peer_nodes_is_refused_outside_the_admitted_namespace(mesh):
    mesh.b.peer_nodes_view = lambda *_: pytest.fail(
        "unauthorized namespace reached the HostApp view")
    result = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, "other")
    assert result["reason"] == "namespace_not_admitted"


@pytest.mark.parametrize("bad", [None, "", " studio ", "x" * 129,
                                  "舞" * 43, "bad\x00id"])
def test_get_peer_nodes_rejects_bad_ids_before_network(mesh, bad,
                                                       monkeypatch):
    monkeypatch.setattr(pc, "_request", lambda *a, **k: pytest.fail(
        "invalid namespace opened a network connection"))
    result = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, bad)
    assert result["reason"] == "malformed"


def test_convoy_id_limit_is_measured_in_utf8_bytes():
    exact = ("舞" * 42) + "ab"       # 126 + 2 = 128 UTF-8 bytes
    assert len(exact) < pc.MAX_CONVOY_ID_BYTES
    segment = pc._encode_convoy_segment(exact)
    assert segment is not None
    assert ps._decode_convoy_segment(segment) == exact
    assert pc._encode_convoy_segment(exact + "c") is None


# -- bounded response reader ------------------------------------------

class _FakeResponse:
    def __init__(self, chunks, content_length=None):
        self.chunks = list(chunks)
        self.content_length = content_length
        self.read_calls = 0

    def getheader(self, name):
        return self.content_length if name == "Content-Length" else None

    def read(self, amount):
        self.read_calls += 1
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) <= amount:
            return chunk
        self.chunks.insert(0, chunk[amount:])
        return chunk[:amount]


def test_declared_oversize_response_is_rejected_without_reading():
    response = _FakeResponse([], str(pc.MAX_PEER_RESPONSE_BYTES + 1))
    with pytest.raises(pc._ResponseLimitError):
        pc._read_bounded_response(response)
    assert response.read_calls == 0


def test_chunked_or_lengthless_response_is_still_bounded():
    response = _FakeResponse(
        [b"x" * pc.MAX_PEER_RESPONSE_BYTES, b"y"], None)
    with pytest.raises(pc._ResponseLimitError):
        pc._read_bounded_response(response)


def test_declared_response_length_must_match_the_bytes_received():
    response = _FakeResponse([b"{}"], "3")
    with pytest.raises(pc._ResponseLimitError):
        pc._read_bounded_response(response)


def test_oversize_post_response_preserves_may_have_run_semantics(monkeypatch):
    response = _FakeResponse([], str(pc.MAX_PEER_RESPONSE_BYTES + 1))

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.sock = object()
            self.requested = False
            self.closed = False

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            self.requested = True

        def getresponse(self):
            return response

        def close(self):
            self.closed = True

    made = []

    def connection(*args, **kwargs):
        item = FakeConnection(*args, **kwargs)
        made.append(item)
        return item

    expected = "cvfp1-" + "0000-" * 7 + "0000"
    target = pc.PeerTarget("b" * 32, "127.0.0.1", 1, "pem", expected)
    monkeypatch.setattr(pc, "build_client_ssl_context", lambda *a: object())
    monkeypatch.setattr(pc.http.client, "HTTPSConnection", connection)
    monkeypatch.setattr(pc, "_presented_fingerprint", lambda sock: expected)
    result = pc._request(target, object(), "POST", pc.ROUTE_ENVELOPE,
                         b"{}", 1.0)
    assert result is None, "after POST, oversize is ambiguous, not unreachable"
    assert made[0].requested is True and made[0].closed is True
    assert response.read_calls == 0
