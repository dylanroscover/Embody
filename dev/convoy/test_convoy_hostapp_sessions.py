"""Production HostApp integration for persistent, pinned Convoy WSS.

These tests intentionally use two real mutual-TLS LAN listeners.  The lower
level socketpair suite proves frame/session mechanics; this file proves the
HostApp route, pin, trust re-check, duplicate-dial convergence, reconnect, and
WSS-first relay contract together.
"""

import base64
import os
import time
import threading

import pytest

import convoy_hostapp as hostapp
import convoy_hostkeys as hostkeys
import convoy_capabilities as capabilities
import convoy_peerclient as peerclient
import convoy_peerserver as peerserver
import convoy_sessions as sessions
import convoy_ws as ws


CONVOY = "studio"


def _wait_for(predicate, timeout_s=8.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.01)
    return False


def _read_http_response(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        block = sock.recv(4096)
        if not block:
            return bytes(data)
        data.extend(block)
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, separator, value = line.partition(b":")
        if separator and name.lower() == b"content-length":
            length = int(value.strip())
            break
    while len(body) < length:
        block = sock.recv(min(4096, length - len(body)))
        if not block:
            break
        body += block
    return head + b"\r\n\r\n" + body


def _refresh_remote_manifest(mesh):
    previous = mesh.a.session_manager.peer_info(mesh.b.host_id).connection_id
    mesh.b.session_manager.refresh_hello()
    assert _wait_for(lambda: (
        mesh.a.session_manager.peer_info(mesh.b.host_id).state == "connected"
        and mesh.b.session_manager.peer_info(mesh.a.host_id).state
        == "connected"
        and mesh.a.session_manager.peer_info(mesh.b.host_id).connection_id
        == mesh.b.session_manager.peer_info(mesh.a.host_id).connection_id
        and mesh.a.session_manager.peer_info(mesh.b.host_id).connection_id
        != previous), timeout_s=8.0)


class WssMesh:
    def __init__(self, tmp_path):
        self.a = hostapp.HostApp(str(tmp_path / "a"))
        self.b = hostapp.HostApp(str(tmp_path / "b"))
        self.servers = []
        self.threads = []

        code, self.node_a = self.a.register_node({
            "project_root": "/show/a", "comp_path": "/Embody",
            "convoy_id": CONVOY, "runtime_id": "runtime-a",
        })
        assert code == 200
        code, self.node_b = self.b.register_node({
            "project_root": "/show/b", "comp_path": "/Embody",
            "convoy_id": CONVOY, "runtime_id": "runtime-b",
        })
        assert code == 200

        # Bind first so each persisted trust record can contain the exact
        # opposite endpoint before either manager begins dialing.
        server_a, port_a = peerserver.serve_lan(
            self.a, "127.0.0.1", 0)
        server_b, port_b = peerserver.serve_lan(
            self.b, "127.0.0.1", 0)
        self.servers.extend((server_a, server_b))
        self.port_a, self.port_b = port_a, port_b

        with self.a.lock:
            self.a.peers.admit(
                self.b.host_id, self.b.hostkeys.fingerprint,
                cert_pem=self.b.hostkeys.certificate_pem,
                endpoints=[f"127.0.0.1:{port_b}"],
                convoy_ids=[CONVOY])
        with self.b.lock:
            self.b.peers.admit(
                self.a.host_id, self.a.hostkeys.fingerprint,
                cert_pem=self.a.hostkeys.certificate_pem,
                endpoints=[f"127.0.0.1:{port_a}"],
                convoy_ids=[CONVOY])

        for server in self.servers:
            thread = threading.Thread(
                target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.a.set_lan_server(
            server_a, self.threads[0], "127.0.0.1", port_a)
        self.b.set_lan_server(
            server_b, self.threads[1], "127.0.0.1", port_b)
        assert self.a.session_manager is not None
        assert self.b.session_manager is not None
        assert self.a.session_manager.wait_connected(self.b.host_id, 8.0)
        assert self.b.session_manager.wait_connected(self.a.host_id, 8.0)
        stable = {"connection_id": None, "since": 0.0}

        def converged_stably():
            if not self._converged():
                stable["connection_id"] = None
                stable["since"] = 0.0
                return False
            connection_id = self.a.session_manager.peer_info(
                self.b.host_id).connection_id
            if connection_id != stable["connection_id"]:
                stable["connection_id"] = connection_id
                stable["since"] = time.monotonic()
                return False
            return time.monotonic() - stable["since"] >= 0.25

        assert _wait_for(converged_stably)

    def _converged(self):
        left = self.a.session_manager.peer_info(self.b.host_id)
        right = self.b.session_manager.peer_info(self.a.host_id)
        return (left.state == right.state == "connected"
                and left.connection_id == right.connection_id)

    def target_b(self, fingerprint=None):
        return peerclient.PeerTarget(
            self.b.host_id, "127.0.0.1", self.port_b,
            self.b.hostkeys.certificate_pem,
            fingerprint or self.b.hostkeys.fingerprint)

    def relay_body(self, source, target, node, key):
        return {
            "target_host_id": target.host_id,
            "convoy_id": CONVOY,
            "target_node_id": node["node_id"],
            "controller_id": "wss-integration",
            "operation": "convoy_ping",
            "arguments": {},
            "timeout_s": 10.0,
            "idempotency_key": key,
        }

    def close(self):
        self.a.stop_lan_server(timeout_s=2.0)
        self.b.stop_lan_server(timeout_s=2.0)
        self.a.db.close()
        self.b.db.close()


@pytest.fixture
def mesh(tmp_path):
    value = WssMesh(tmp_path)
    yield value
    value.close()


def test_real_mtls_simultaneous_dials_converge_and_relay_both_directions(
        mesh, monkeypatch):
    left = mesh.a.session_manager.peer_info(mesh.b.host_id)
    right = mesh.b.session_manager.peer_info(mesh.a.host_id)
    assert left.connection_id == right.connection_id
    assert {left.connection_role, right.connection_role} == {
        "client", "server"}
    assert mesh.a.session_manager.stats()["connected_peers"] == 1
    assert mesh.b.session_manager.stats()["connected_peers"] == 1

    # An established WSS link is selected before any HTTP compatibility
    # path.  Raising here proves both relays reached the remote durable job
    # handler through the full-duplex session.
    monkeypatch.setattr(
        peerclient, "send_envelope",
        lambda *args, **kwargs: pytest.fail("HTTP fallback was used"))
    code, sent_ab = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "wss-a-to-b"))
    assert code == 200 and sent_ab["ok"] is True, sent_ab
    assert mesh.b.db.get_job(sent_ab["job"]["delivery_id"])[
        "origin_host_id"] == mesh.a.host_id

    code, sent_ba = mesh.b.relay_submit(mesh.relay_body(
        mesh.b, mesh.a, mesh.node_a, "wss-b-to-a"))
    assert code == 200 and sent_ba["ok"] is True, sent_ba
    assert mesh.a.db.get_job(sent_ba["job"]["delivery_id"])[
        "origin_host_id"] == mesh.b.host_id


def test_real_mtls_socket_pin_is_recomputed_before_upgrade_bytes(mesh):
    sock = peerclient.open_authenticated_socket(
        mesh.target_b(), mesh.a.hostkeys, timeout=2.0)
    try:
        assert sock.getblocking() is False
        assert hostkeys.fingerprint(hostkeys.public_der_from_certificate(
            sock.getpeercert(binary_form=True))) == mesh.b.hostkeys.fingerprint
    finally:
        sock.close()

    wrong = "cvfp1-" + "0000-" * 7 + "0000"
    with pytest.raises(peerclient.PeerSocketPinMismatch) as caught:
        peerclient.open_authenticated_socket(
            mesh.target_b(fingerprint=wrong), mesh.a.hostkeys, timeout=2.0)
    assert caught.value.mismatch.expected == wrong
    assert caught.value.mismatch.offered == mesh.b.hostkeys.fingerprint


def test_transport_dropout_reconnects_without_replaying_an_rpc(mesh):
    previous = mesh.a.session_manager.peer_info(
        mesh.b.host_id).connection_id
    assert mesh.a.session_manager.disconnect_peer(
        mesh.b.host_id, "test dropout") is True
    assert _wait_for(lambda: (
        mesh.a.session_manager.peer_info(mesh.b.host_id).state == "connected"
        and mesh.b.session_manager.peer_info(mesh.a.host_id).state
        == "connected"
        and mesh.a.session_manager.peer_info(mesh.b.host_id).connection_id
        == mesh.b.session_manager.peer_info(mesh.a.host_id).connection_id
        and mesh.a.session_manager.peer_info(mesh.b.host_id).connection_id
        != previous), timeout_s=8.0)
    result = mesh.a.session_manager.call(
        mesh.b.host_id, CONVOY, peerserver.SESSION_RPC_HEALTH, {},
        timeout_s=2.0)
    assert result["ok"] is True and result["host_id"] == mesh.b.host_id


def test_wss_job_ack_is_explicit_and_owner_bound(mesh):
    code, accepted = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "wss-explicit-ack"))
    assert code == 200 and accepted["ok"] is True
    delivery_id = accepted["job"]["delivery_id"]
    code, dispatched = mesh.b.dispatch_job(delivery_id)
    assert code == 200 and dispatched["job"]["state"] == "succeeded"
    assert mesh.b.db.get_job(delivery_id)["outcome_acknowledged_at"] is None

    result = mesh.a.session_manager.call(
        mesh.b.host_id, CONVOY, peerserver.SESSION_RPC_ACK,
        {"delivery_id": delivery_id}, timeout_s=2.0)
    assert result["ok"] is True
    assert result["already_acknowledged"] is False
    assert result["wakes_touchdesigner"] is False
    assert mesh.b.db.get_job(delivery_id)["outcome_acknowledged_at"] \
        == result["acknowledged_at"]


def test_each_wss_call_reauthorizes_the_live_pin(mesh, tmp_path):
    stranger = hostkeys.load_or_create(str(tmp_path / "repinned-key"))
    with mesh.b.lock:
        mesh.b.peers.admit(
            mesh.a.host_id, stranger.fingerprint,
            cert_pem=stranger.certificate_pem,
            endpoints=[f"127.0.0.1:{mesh.port_a}"],
            convoy_ids=[CONVOY])
    # Deliberately do not reconcile the manager.  The pre-existing socket is
    # still cryptographically A, and the HostApp callback must compare that
    # authenticated context with the *current* trust record per call.
    with pytest.raises(ws.RemoteError) as caught:
        mesh.a.session_manager.call(
            mesh.b.host_id, CONVOY, peerserver.SESSION_RPC_HEALTH, {},
            timeout_s=2.0)
    assert caught.value.remote_code == "pin_mismatch"


@pytest.mark.parametrize("bad_header", [
    # Duplicate singleton header: preserving multiplicity matters.
    "Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Key: {key}\r\n",
    # Obsolete folding accepted by permissive email parsers is forbidden.
    "Sec-WebSocket-Key: {key}\r\n\tcontinued\r\n",
])
def test_peer_session_route_rejects_ambiguous_upgrade_headers(
        mesh, bad_header):
    sock = peerclient.open_authenticated_socket(
        mesh.target_b(), mesh.a.hostkeys, timeout=2.0)
    try:
        sock.settimeout(2.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /peer/session HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{mesh.port_b}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            + bad_header.format(key=key)
            + "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = _read_http_response(sock)
        assert response.startswith(b"HTTP/1.1 400 "), response
        assert b"websocket_upgrade_rejected" in response
    finally:
        sock.close()


def test_selected_wss_presend_failure_falls_back_to_http(mesh, monkeypatch):
    """A PROVABLY pre-send WSS failure (PeerUnavailable / NamespaceNotAuthorized
    / MessageTooLarge are all raised BEFORE any byte reaches the socket) must
    NOT be reported as delivery_indeterminate: nothing left the process, so the
    HTTPS compatibility path is safe to run and is not a possible double-execute
    (review 2026-08-02)."""
    calls = []
    code, warmed = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "wss-presend-warm"))
    assert code == 200 and warmed["ok"] is True

    def presend(*args, **kwargs):
        raise sessions.PeerUnavailable("no usable session")

    monkeypatch.setattr(mesh.a.session_manager, "call", presend)
    monkeypatch.setattr(
        peerclient, "send_envelope",
        lambda *args, **kwargs: (calls.append(1) or {
            "ok": True, "created": True,
            "job": {"delivery_id": "cj_" + "3" * 32}}))
    code, result = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "wss-presend"))
    assert calls == [1], "a provably pre-send failure must fall back to HTTP"
    assert code == 200 and result["ok"] is True


def test_selected_wss_ambiguous_failure_is_never_replayed_over_http(
        mesh, monkeypatch):
    """A post-send / ambiguous WSS failure (a generic PairSessionError, where
    bytes MAY already be on the wire) must NEVER be replayed over HTTP -- that
    would risk a double-execute -- so it is reported delivery_indeterminate and
    the compatibility path is suppressed."""
    calls = []
    # Warm the digest-keyed manifest cache so the injected ambiguity occurs on
    # the envelope call, not on its preceding read-only preflight.
    code, warmed = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "wss-ambig-warm"))
    assert code == 200 and warmed["ok"] is True

    def ambiguous(*args, **kwargs):
        raise sessions.PairSessionError("connection dropped after send")

    monkeypatch.setattr(mesh.a.session_manager, "call", ambiguous)
    monkeypatch.setattr(
        peerclient, "send_envelope",
        lambda *args, **kwargs: calls.append(1))
    code, result = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "wss-ambig"))
    assert code == 202
    assert result["reason"] == "delivery_indeterminate"
    assert calls == []


def test_established_inbound_wss_routes_without_a_dial_endpoint(
        mesh, monkeypatch):
    # Discovery endpoints are compatibility/dial data, not authority over an
    # already authenticated full-duplex link.  Simulate a peer observed only
    # through its inbound session and keep the existing connection alive.
    with mesh.a.lock:
        mesh.a.peers.admit(
            mesh.b.host_id, mesh.b.hostkeys.fingerprint,
            cert_pem=mesh.b.hostkeys.certificate_pem,
            endpoints=[], convoy_ids=[CONVOY])
        record = mesh.a.peers.get(mesh.b.host_id)
        assert mesh.a._peer_targets_from_record(record)[0] == ()

    monkeypatch.setattr(
        peerclient, "send_envelope",
        lambda *args, **kwargs: pytest.fail("HTTP fallback was used"))
    code, accepted = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "wss-inbound-only"))
    assert code == 200 and accepted["ok"] is True

    code, nodes = mesh.a.network_nodes(CONVOY)
    assert code == 200
    assert any(row["node_id"] == mesh.node_b["node_id"]
               for row in nodes["nodes"])
    peer = next(row for row in nodes["peers"]
                if row["host_id"] == mesh.b.host_id)
    assert peer["status"] == "online"

    code, controllers = mesh.a.network_controllers(CONVOY)
    assert code == 200
    assert any(row["host_id"] == mesh.b.host_id
               for row in controllers["controllers"])


def test_wss_manifest_preflight_matches_caches_and_projects_status(
        mesh, monkeypatch):
    calls = []
    original = mesh.b.get_peer_manifest

    def counted(host_id):
        calls.append(host_id)
        return original(host_id)

    mesh.b.get_peer_manifest = counted
    monkeypatch.setattr(
        peerclient, "get_peer_manifest",
        lambda *args, **kwargs: pytest.fail("HTTP manifest fallback was used"))
    monkeypatch.setattr(
        peerclient, "send_envelope",
        lambda *args, **kwargs: pytest.fail("HTTP envelope fallback was used"))

    for key in ("compat-cache-one", "compat-cache-two"):
        code, accepted = mesh.a.relay_submit(mesh.relay_body(
            mesh.a, mesh.b, mesh.node_b, key))
        assert code == 200 and accepted["ok"] is True, accepted
    assert calls == [mesh.a.host_id]
    assert len(mesh.a._peer_manifest_cache) == 1

    code, status = mesh.a.network_nodes(CONVOY)
    assert code == 200
    peer = next(row for row in status["peers"]
                if row["host_id"] == mesh.b.host_id)
    node = next(row for row in status["nodes"]
                if row["host_id"] == mesh.b.host_id)
    assert peer["compatibility"] == "compatible"
    assert node["compatibility"] == "compatible"


@pytest.mark.parametrize("change, reason", [
    ("absent", "operation_not_exposed"),
    ("digest", "incompatible_operation"),
    ("protocol", "protocol_mismatch"),
])
def test_wss_manifest_preflight_refuses_before_envelope(
        mesh, monkeypatch, change, reason):
    if change == "absent":
        mesh.b.operations.pop("convoy_ping")
    elif change == "digest":
        mesh.b.operations["convoy_ping"]["schema"] = {
            "new_required_field": {"type": "string"}}
    else:
        original_build = mesh.b.build_remote_manifest

        def newer_protocol():
            current = original_build()
            return capabilities.CapabilityManifest(
                "convoy/999", None, current.operations)

        mesh.b.build_remote_manifest = newer_protocol
        profile = mesh.b._session_hello_profile()
        summary = dict(profile.capability_summary)
        summary["envelope_protocol"] = "convoy/999"
        mesh.b.session_manager._hello_provider = lambda: (
            sessions.HelloProfile(profile.namespaces, summary))
    _refresh_remote_manifest(mesh)

    envelope_calls = []
    original_submit = mesh.b.submit_envelope

    def submitted(*args, **kwargs):
        envelope_calls.append(1)
        return original_submit(*args, **kwargs)

    mesh.b.submit_envelope = submitted
    monkeypatch.setattr(
        peerclient, "send_envelope",
        lambda *args, **kwargs: envelope_calls.append(1))
    code, refusal = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, f"compat-refuse-{change}"))
    assert code in (403, 409)
    assert refusal["reason"] == reason
    assert refusal["operation"] == "convoy_ping"
    assert envelope_calls == []
    if change == "protocol":
        code, status = mesh.a.network_nodes(CONVOY)
        assert code == 200
        peer_status = next(row for row in status["peers"]
                           if row["host_id"] == mesh.b.host_id)
        node_status = next(row for row in status["nodes"]
                           if row["host_id"] == mesh.b.host_id)
        assert peer_status["compatibility"] == "incompatible"
        assert node_status["compatibility"] == "incompatible"


def test_wss_manifest_cache_invalidates_on_capability_and_pin_change(
        mesh, tmp_path):
    code, accepted = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "compat-before-change"))
    assert code == 200 and accepted["ok"] is True
    old_keys = tuple(mesh.a._peer_manifest_cache)
    assert len(old_keys) == 1

    mesh.b.operations["convoy_ping"]["schema"] = {
        "changed": {"type": "boolean"}}
    _refresh_remote_manifest(mesh)
    code, refusal = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "compat-after-change"))
    assert code == 409 and refusal["reason"] == "incompatible_operation"
    new_keys = tuple(mesh.a._peer_manifest_cache)
    assert len(new_keys) == 1 and new_keys != old_keys
    code, status = mesh.a.network_nodes(CONVOY)
    assert code == 200
    peer_status = next(row for row in status["peers"]
                       if row["host_id"] == mesh.b.host_id)
    node_status = next(row for row in status["nodes"]
                       if row["host_id"] == mesh.b.host_id)
    assert peer_status["compatibility"] == "limited"
    assert node_status["compatibility"] == "limited"

    stranger = hostkeys.load_or_create(str(tmp_path / "replacement-peer"))
    code, admitted = mesh.a.admit_peer({
        "host_id": mesh.b.host_id,
        "fingerprint": stranger.fingerprint,
        "cert_pem": stranger.certificate_pem,
        "endpoints": [f"127.0.0.1:{mesh.port_b}"],
        "convoy_ids": [CONVOY],
    })
    assert code == 200, admitted
    assert tuple(mesh.a._peer_manifest_cache) == ()
    code, refusal = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "compat-after-repin"))
    assert code in (409, 503)
    assert refusal["reason"] in ("pin_mismatch", "peer_unreachable")


def test_new_pin_cannot_launder_the_old_wss_before_reconciliation(
        mesh, tmp_path):
    code, accepted = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "pin-race-warm"))
    assert code == 200 and accepted["ok"] is True
    assert mesh.a._peer_manifest_cache

    replacement = hostkeys.load_or_create(str(tmp_path / "race-pin"))
    # Model the exact public-route race: the trust write has landed under the
    # HostApp lock, but reconcile_peer_sessions has not yet closed old WSS.
    with mesh.a.lock:
        mesh.a.peers.admit(
            mesh.b.host_id, replacement.fingerprint,
            cert_pem=replacement.certificate_pem,
            endpoints=[f"127.0.0.1:{mesh.port_b}"],
            convoy_ids=[CONVOY])

    manifest_calls = []
    envelope_calls = []
    mesh.b.get_peer_manifest = lambda *_: manifest_calls.append(1)
    mesh.b.submit_envelope = lambda *_: envelope_calls.append(1)
    code, refusal = mesh.a.relay_submit(mesh.relay_body(
        mesh.a, mesh.b, mesh.node_b, "pin-race-refuse"))
    assert code in (409, 503)
    assert refusal["reason"] in ("pin_mismatch", "peer_unreachable")
    assert manifest_calls == [] and envelope_calls == []
    assert tuple(mesh.a._peer_manifest_cache) == ()
