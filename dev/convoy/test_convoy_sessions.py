"""Focused tests for persistent Convoy host-pair session management."""

import concurrent.futures
import socket
import threading
import time
from collections import Counter

import pytest

import convoy_sessions as pairs
import convoy_ws as ws


CV = "cv_test"


def wait_until(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class SocketpairFabric:
    """In-memory authenticated-socket adapter used below the manager API."""

    def __init__(self):
        self.lock = threading.Lock()
        self.routes = {}
        self.attempts = Counter()
        self.accept_threads = []
        self.accept_errors = []
        self.dial_barrier = None

    @staticmethod
    def key(endpoint):
        return endpoint.address, endpoint.port, endpoint.server_name

    def register(self, endpoint, manager):
        with self.lock:
            self.routes[self.key(endpoint)] = manager

    def unregister(self, endpoint):
        with self.lock:
            self.routes.pop(self.key(endpoint), None)

    def dialer_for(self, local_host_id):
        def dial(peer_host_id, endpoint, timeout_s):
            with self.lock:
                target = self.routes.get(self.key(endpoint))
                self.attempts[(local_host_id, peer_host_id, endpoint.address)] += 1
                barrier = self.dial_barrier
            if target is None:
                raise OSError("endpoint unavailable")
            if barrier is not None:
                barrier.wait(timeout_s)
            client, server = socket.socketpair()

            def accept():
                try:
                    target.accept_authenticated_socket(
                        server, local_host_id, timeout_s=timeout_s)
                except BaseException as exc:
                    # Duplicate convergence and deliberate revocation close
                    # handshakes; retain errors for diagnostics, not failure.
                    with self.lock:
                        self.accept_errors.append(exc)
                    try:
                        server.close()
                    except OSError:
                        pass

            thread = threading.Thread(target=accept, daemon=True)
            with self.lock:
                self.accept_threads.append(thread)
            thread.start()
            return client

        return dial

    def join(self, timeout=1.0):
        deadline = time.monotonic() + timeout
        with self.lock:
            threads = list(self.accept_threads)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)


def endpoint(name, port=47600):
    return pairs.PeerEndpoint(name, port, server_name=name)


def make_manager(fabric, host_id, *, namespaces=(CV,), capabilities=None,
                 max_peers=64, dial_workers=1, max_events=64,
                 handler=None):
    profile = pairs.HelloProfile(
        namespaces,
        capabilities or {
            "host_app_version": "test-1",
            "protocol": "1.0",
            "host": host_id,
        })
    if handler is None:
        def handler(peer_host_id, convoy_id, method, payload, auth_context):
            return {
                "served_by": host_id,
                "source": peer_host_id,
                "convoy_id": convoy_id,
                "method": method,
                "payload": payload,
            }
    return pairs.HostPairSessionManager(
        host_id, fabric.dialer_for(host_id), lambda: profile, handler,
        reconnect_policy=pairs.ReconnectPolicy(
            initial_delay_s=0.01, maximum_delay_s=0.05,
            jitter_ratio=0.0, connect_timeout_s=1.0),
        max_peers=max_peers, dial_workers=dial_workers,
        max_events=max_events, supervisor_interval_s=0.005,
        session_options={
            "handler_workers": 1,
            "max_pending": 16,
            "max_inbound_queue": 16,
            "ping_interval_s": None,
            "idle_timeout_s": None,
        },
        name=f"test-pairs-{host_id}")


def configure_pair(left, left_endpoint, right, right_endpoint,
                   *, right_dials=True):
    left.configure_peer(right.local_host_id, [right_endpoint])
    right.configure_peer(
        left.local_host_id, [left_endpoint] if right_dials else [])


def wait_converged(left, right, timeout=4.0):
    def converged():
        try:
            a = left.peer_info(right.local_host_id)
            b = right.peer_info(left.local_host_id)
            return (a.state == b.state == "connected"
                    and a.connection_id == b.connection_id)
        except pairs.PairSessionError:
            return False

    assert wait_until(converged, timeout), (
        left.snapshot(), right.snapshot(), left.poll_events(100),
        right.poll_events(100))
    return (left.peer_info(right.local_host_id),
            right.peer_info(left.local_host_id))


def stop_all(managers, fabric, timeout=0.5):
    for manager in managers:
        manager.stop(timeout_s=timeout)
    fabric.join(timeout=1.0)


def test_authenticated_hello_namespace_intersection_and_full_duplex_rpc():
    fabric = SocketpairFabric()
    a_ep, b_ep = endpoint("a"), endpoint("b")
    a = make_manager(
        fabric, "host-a", namespaces=(CV, "cv_a"),
        capabilities={"host_app_version": "a-1", "tools": ["query"]})
    b = make_manager(
        fabric, "host-b", namespaces=(CV, "cv_b"),
        capabilities={"host_app_version": "b-1", "tools": ["git"]})
    fabric.register(a_ep, a)
    fabric.register(b_ep, b)
    configure_pair(a, a_ep, b, b_ep, right_dials=False)
    b.start()
    a.start()
    try:
        a_info, b_info = wait_converged(a, b)
        assert a_info.authorized_namespaces == (CV,)
        assert b_info.authorized_namespaces == (CV,)
        assert a_info.remote_protocol == ws.HELLO_PROTOCOL
        assert a_info.remote_capability_summary["host_app_version"] == "b-1"
        assert b_info.remote_capability_summary["host_app_version"] == "a-1"
        assert a_info.remote_generation is not None
        assert a.connected_with_authentication_context("host-b", None)
        assert not a.connected_with_authentication_context(
            "host-b", ("different-pin", b"different-spki"))

        left_result = a.call(
            "host-b", CV, "query_network", {"path": "/"}, timeout_s=1.0)
        right_result = b.call(
            "host-a", CV, "git_status", {"short": True}, timeout_s=1.0)
        assert left_result == {
            "served_by": "host-b", "source": "host-a",
            "convoy_id": CV, "method": "query_network",
            "payload": {"path": "/"},
        }
        assert right_result["served_by"] == "host-a"
        assert right_result["source"] == "host-b"
        with pytest.raises(pairs.NamespaceNotAuthorized):
            a.call("host-b", "cv_a", "query", {}, timeout_s=0.2)
    finally:
        stop_all([a, b], fabric)


def test_already_upgraded_server_connection_integration_path():
    fabric = SocketpairFabric()
    a_ep, b_ep = endpoint("a"), endpoint("b")
    a = make_manager(fabric, "host-a")
    b = make_manager(fabric, "host-b")

    class UpgradeRoute:
        def accept_authenticated_socket(
                self, sock, peer_host_id, timeout_s):
            ws.server_upgrade(
                sock, expected_path=b.websocket_path,
                timeout_s=timeout_s)
            accepted = b.accept_authenticated_websocket(
                ws.WebSocketConnection(sock, "server"),
                peer_host_id, timeout_s=timeout_s)
            # This is what a BaseHTTPRequestHandler hijack route must do so
            # socketserver does not close the upgraded socket on return.
            accepted.wait_closed()
            return accepted

    fabric.register(a_ep, a)
    fabric.register(b_ep, UpgradeRoute())
    configure_pair(a, a_ep, b, b_ep, right_dials=False)
    b.start()
    a.start()
    try:
        wait_converged(a, b)
        assert a.call("host-b", CV, "ping", 7, timeout_s=1.0)[
            "payload"] == 7
    finally:
        stop_all([a, b], fabric)


def test_refresh_hello_reconnects_with_new_namespaces_and_generation():
    fabric = SocketpairFabric()
    a_ep, b_ep = endpoint("a"), endpoint("b")
    profiles = {
        "host-a": pairs.HelloProfile((CV,), {"revision": 1}),
        "host-b": pairs.HelloProfile((CV,), {"revision": 1}),
    }

    def manager(host_id, dial_workers=1):
        def handler(peer, convoy_id, method, payload, auth_context):
            return host_id, convoy_id, payload

        return pairs.HostPairSessionManager(
            host_id, fabric.dialer_for(host_id),
            lambda: profiles[host_id], handler,
            reconnect_policy=pairs.ReconnectPolicy(
                initial_delay_s=0.01, maximum_delay_s=0.05,
                jitter_ratio=0.0, connect_timeout_s=1.0),
            max_peers=1, dial_workers=dial_workers,
            supervisor_interval_s=0.005,
            session_options={
                "handler_workers": 1, "ping_interval_s": None,
                "idle_timeout_s": None,
            })

    a, b = manager("host-a"), manager("host-b")
    fabric.register(a_ep, a)
    fabric.register(b_ep, b)
    configure_pair(a, a_ep, b, b_ep, right_dials=False)
    b.start()
    a.start()
    try:
        old_a, old_b = wait_converged(a, b)
        profiles["host-a"] = pairs.HelloProfile(
            ("cv_new",), {"revision": 2})
        profiles["host-b"] = pairs.HelloProfile(
            ("cv_new",), {"revision": 2})
        # Refresh both sides concurrently so neither publishes stale hello
        # data as the settled replacement connection.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda manager_value: manager_value.refresh_hello(),
                          (a, b)))
        new_a, new_b = wait_converged(a, b)
        assert new_a.connection_id != old_a.connection_id
        assert new_b.connection_id != old_b.connection_id
        assert new_a.authorized_namespaces == ("cv_new",)
        assert new_a.remote_generation > old_a.remote_generation
        assert new_a.remote_capability_summary["revision"] == 2
        with pytest.raises(pairs.NamespaceNotAuthorized):
            a.call("host-b", CV, "old", None, timeout_s=0.2)
        assert a.call(
            "host-b", "cv_new", "new", 9, timeout_s=1.0) == [
                "host-b", "cv_new", 9]
    finally:
        stop_all([a, b], fabric)


def test_simultaneous_duplicate_dials_converge_to_host_id_canonical_socket():
    fabric = SocketpairFabric()
    a_ep, b_ep = endpoint("a"), endpoint("b")
    a = make_manager(fabric, "host-a")
    b = make_manager(fabric, "host-b")
    fabric.register(a_ep, a)
    fabric.register(b_ep, b)
    configure_pair(a, a_ep, b, b_ep, right_dials=True)
    fabric.dial_barrier = threading.Barrier(2)
    a.start()
    b.start()
    try:
        wait_converged(a, b)
        # Both sockets can be momentarily selected before the second hello
        # finishes and the deterministic rank displaces the non-canonical
        # one.  Wait for the actual canonical fixed point, not that transient
        # shared connection id.
        assert wait_until(lambda: (
            a.peer_info("host-b").state == "connected"
            and b.peer_info("host-a").state == "connected"
            and a.peer_info("host-b").connection_id
            == b.peer_info("host-a").connection_id
            and a.peer_info("host-b").connection_role == "client"
            and b.peer_info("host-a").connection_role == "server"))
        a_info = a.peer_info("host-b")
        b_info = b.peer_info("host-a")
        assert fabric.attempts[("host-a", "host-b", "b")] >= 1
        assert fabric.attempts[("host-b", "host-a", "a")] >= 1
        # The lexically lower host is the canonical client on both sides.
        assert a_info.connection_role == "client"
        assert b_info.connection_role == "server"
        assert a_info.connection_id == b_info.connection_id
        assert a.stats()["connected_peers"] == 1
        assert b.stats()["connected_peers"] == 1
        assert a.call("host-b", CV, "ping", 1, timeout_s=1.0)[
            "served_by"] == "host-b"
    finally:
        fabric.dial_barrier = None
        stop_all([a, b], fabric)


def test_dropout_uses_backoff_then_reconnects_without_retrying_an_rpc():
    fabric = SocketpairFabric()
    a_ep, b_ep = endpoint("a"), endpoint("b")
    calls = []

    def b_handler(peer, convoy_id, method, payload, auth_context):
        calls.append((peer, method, payload))
        return "ok"

    a = make_manager(fabric, "host-a")
    b = make_manager(fabric, "host-b", handler=b_handler)
    fabric.register(a_ep, a)
    fabric.register(b_ep, b)
    configure_pair(a, a_ep, b, b_ep, right_dials=False)
    b.start()
    a.start()
    try:
        old_id = wait_converged(a, b)[0].connection_id
        fabric.unregister(b_ep)
        assert a.disconnect_peer("host-b", "test dropout")
        assert wait_until(
            lambda: a.peer_info("host-b").failure_count > 0, 2.0)
        with pytest.raises(pairs.PeerUnavailable):
            a.call("host-b", CV, "mutation", {"n": 1}, timeout_s=0.03)
        assert calls == []

        fabric.register(b_ep, b)
        a_info, b_info = wait_converged(a, b)
        assert a_info.connection_id != old_id
        assert a_info.connection_id == b_info.connection_id
        assert a.call(
            "host-b", CV, "mutation", {"n": 2}, timeout_s=1.0) == "ok"
        assert calls == [("host-a", "mutation", {"n": 2})]
    finally:
        stop_all([a, b], fabric)


def test_endpoint_change_closes_old_link_and_redials_new_address():
    fabric = SocketpairFabric()
    a_ep = endpoint("a")
    b_old = endpoint("b-old")
    b_new = endpoint("b-new", 47601)
    a = make_manager(fabric, "host-a")
    b = make_manager(fabric, "host-b")
    fabric.register(a_ep, a)
    fabric.register(b_old, b)
    configure_pair(a, a_ep, b, b_old, right_dials=False)
    b.start()
    a.start()
    try:
        old_id = wait_converged(a, b)[0].connection_id
        fabric.unregister(b_old)
        fabric.register(b_new, b)
        a.configure_peer("host-b", [b_new])
        a_info, b_info = wait_converged(a, b)
        assert a_info.connection_id != old_id
        assert a_info.active_endpoint == b_new
        assert b_info.active_endpoint is None
        assert fabric.attempts[("host-a", "host-b", "b-new")] >= 1
    finally:
        stop_all([a, b], fabric)


def test_revocation_fails_closed_until_explicit_restore():
    fabric = SocketpairFabric()
    a_ep, b_ep = endpoint("a"), endpoint("b")
    a = make_manager(fabric, "host-a")
    b = make_manager(fabric, "host-b")
    fabric.register(a_ep, a)
    fabric.register(b_ep, b)
    configure_pair(a, a_ep, b, b_ep, right_dials=False)
    b.start()
    a.start()
    try:
        wait_converged(a, b)
        a.revoke_peer("host-b")
        assert a.peer_info("host-b").state == "revoked"
        with pytest.raises(pairs.PeerRevoked):
            a.configure_peer("host-b", [b_ep])
        with pytest.raises(pairs.PeerRevoked):
            a.call("host-b", CV, "ping", None, timeout_s=0.05)
        assert wait_until(
            lambda: b.peer_info("host-a").state != "connected", 1.0)

        a.restore_peer("host-b", [b_ep])
        wait_converged(a, b)
        assert a.call("host-b", CV, "ping", None, timeout_s=1.0)[
            "served_by"] == "host-b"
    finally:
        stop_all([a, b], fabric)


def test_unknown_authenticated_peer_is_closed_before_upgrade_or_hello():
    fabric = SocketpairFabric()
    manager = make_manager(fabric, "host-a", max_peers=1)
    manager.start()
    client, server = socket.socketpair()
    client.settimeout(0.5)
    try:
        with pytest.raises(pairs.PeerUnknown):
            manager.accept_authenticated_socket(
                server, "host-unknown", timeout_s=0.2)
        assert client.recv(1) == b""
    finally:
        client.close()
        manager.stop(timeout_s=0.2)


@pytest.mark.parametrize("pair_count", [10, 20, 30])
def test_scale_full_duplex_10_20_30_host_pairs(pair_count):
    fabric = SocketpairFabric()
    hub_endpoint = endpoint("hub")
    hub = make_manager(
        fabric, "host-hub", max_peers=pair_count,
        dial_workers=min(8, pair_count))
    fabric.register(hub_endpoint, hub)
    peers = []
    for index in range(pair_count):
        host_id = f"host-{index:02d}"
        peer_endpoint = endpoint(f"peer-{index:02d}", 47700 + index)
        peer = make_manager(fabric, host_id, max_peers=1)
        fabric.register(peer_endpoint, peer)
        hub.configure_peer(host_id, [peer_endpoint])
        # Inbound-only on the spoke avoids manufacturing duplicate sockets;
        # duplicate simultaneous dial is covered independently above.
        peer.configure_peer("host-hub", [])
        peers.append(peer)
    for peer in peers:
        peer.start()
    hub.start()
    try:
        assert wait_until(
            lambda: hub.stats()["connected_peers"] == pair_count, 8.0)
        for peer in peers:
            assert peer.wait_connected("host-hub", 1.0)
            assert (peer.peer_info("host-hub").connection_id
                    == hub.peer_info(peer.local_host_id).connection_id)

        calls = []
        for peer in peers:
            calls.append((hub, peer.local_host_id, peer.local_host_id))
            calls.append((peer, "host-hub", "host-hub"))

        def invoke(item):
            manager, target, expected = item
            result = manager.call(
                target, CV, "echo", manager.local_host_id, timeout_s=2.0)
            return result["served_by"], expected

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(invoke, calls))
        assert all(actual == expected for actual, expected in results)
        assert hub.stats()["connected_peers"] == pair_count
    finally:
        stop_all([hub] + peers, fabric)


def test_manager_capacity_event_backpressure_and_bounded_shutdown():
    fabric = SocketpairFabric()
    release_dial = threading.Event()
    dial_started = threading.Event()

    def blocking_dial(peer_host_id, endpoint_value, timeout_s):
        dial_started.set()
        release_dial.wait(5.0)  # Deliberately violates the dialer contract.
        raise OSError("released")

    manager = pairs.HostPairSessionManager(
        "host-a", blocking_dial,
        lambda: pairs.HelloProfile((CV,), {"version": 1}),
        reconnect_policy=pairs.ReconnectPolicy(
            initial_delay_s=0.01, maximum_delay_s=0.02,
            jitter_ratio=0.0, connect_timeout_s=0.05),
        max_peers=1, dial_workers=1, max_events=2,
        supervisor_interval_s=0.005,
        session_options={"handler_workers": 1,
                         "ping_interval_s": None,
                         "idle_timeout_s": None})
    manager.configure_peer("host-b", [endpoint("blocked")])
    with pytest.raises(pairs.PeerCapacityReached):
        manager.configure_peer("host-c", [endpoint("other")])
    manager.start()
    try:
        assert dial_started.wait(1.0)
        # Fill/rotate the bounded event ring without growing it.
        manager.configure_peer("host-b", [endpoint("blocked-2")])
        manager.configure_peer("host-b", [endpoint("blocked-3")])
        assert manager.stats()["events_queued"] <= 2
        assert manager.stats()["events_dropped"] > 0

        started = time.monotonic()
        manager.stop(timeout_s=0.05)
        elapsed = time.monotonic() - started
        assert elapsed < 0.25
        assert manager.is_stopped
    finally:
        release_dial.set()
        manager.stop(timeout_s=0.05)
