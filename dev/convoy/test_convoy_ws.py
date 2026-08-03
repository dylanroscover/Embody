"""Deterministic tests for the standalone Convoy WebSocket foundation."""

import concurrent.futures
import socket
import struct
import threading
import time

import pytest

import convoy_ws as ws


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def make_session_pair(handler_a=None, handler_b=None, **options):
    client_sock, server_sock = socket.socketpair()
    client = ws.Session(
        ws.WebSocketConnection(client_sock, "client"), handler_a,
        name="test-client", **options).start()
    server = ws.Session(
        ws.WebSocketConnection(server_sock, "server"), handler_b,
        name="test-server", **options).start()
    return client, server


def close_pair(client, server):
    client.close(timeout_s=0.5)
    server.close(timeout_s=0.5)


def test_rfc6455_accept_vector_and_strict_upgrade_round_trip():
    assert ws.websocket_accept("dGhlIHNhbXBsZSBub25jZQ==") == (
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
    client_sock, server_sock = socket.socketpair()
    captured = {}

    def serve():
        captured["info"] = ws.server_upgrade(
            server_sock, expected_host="host-b:47600", timeout_s=1.0)

    thread = threading.Thread(target=serve)
    thread.start()
    response = ws.client_upgrade(
        client_sock, "host-b:47600", timeout_s=1.0,
        key_bytes=b"0123456789abcdef")
    thread.join(1.0)
    try:
        assert not thread.is_alive()
        assert response["sec-websocket-protocol"] == ws.SUBPROTOCOL
        assert captured["info"].host == "host-b:47600"
        assert captured["info"].path == ws.DEFAULT_PATH
    finally:
        client_sock.close()
        server_sock.close()


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.replace(b"Sec-WebSocket-Version: 13",
                            b"Sec-WebSocket-Version: 12"),
    lambda raw: raw.replace(
        b"Sec-WebSocket-Protocol: embody.convoy.v1",
        b"Sec-WebSocket-Protocol: embody.convoy.v0, embody.convoy.v1"),
    lambda raw: raw.replace(
        b"Sec-WebSocket-Protocol: embody.convoy.v1\r\n",
        b"Sec-WebSocket-Protocol: embody.convoy.v1\r\n"
        b"Sec-WebSocket-Extensions: permessage-deflate\r\n"),
    lambda raw: raw.replace(b"GET /convoy/ws HTTP/1.1",
                            b"POST /convoy/ws HTTP/1.1"),
])
def test_server_refuses_upgrade_downgrade_extension_and_malformed_request(mutation):
    raw, _ = ws.build_client_upgrade(
        "host-b:47600", key_bytes=b"0123456789abcdef")
    with pytest.raises(ws.UpgradeRejected):
        ws.validate_client_upgrade(mutation(raw))


def test_client_refuses_wrong_accept_protocol_and_extensions():
    _, key = ws.build_client_upgrade(
        "host-b:47600", key_bytes=b"0123456789abcdef")
    valid = ws.build_server_upgrade(key)
    variants = [
        valid.replace(b"Sec-WebSocket-Accept: ",
                      b"Sec-WebSocket-Accept: wrong"),
        valid.replace(b"embody.convoy.v1", b"embody.convoy.v0"),
        valid.replace(
            b"Sec-WebSocket-Protocol: embody.convoy.v1\r\n",
            b"Sec-WebSocket-Protocol: embody.convoy.v1\r\n"
            b"Sec-WebSocket-Extensions: permessage-deflate\r\n"),
    ]
    for raw in variants:
        with pytest.raises(ws.UpgradeRejected):
            ws.validate_server_upgrade(raw, key)


def test_http_header_limit_is_enforced_before_unbounded_read():
    left, right = socket.socketpair()
    try:
        right.sendall(b"GET /convoy/ws HTTP/1.1\r\nX: " +
                      b"a" * ws.MAX_HTTP_HEADER_BYTES)
        with pytest.raises(ws.UpgradeRejected, match="16 KiB"):
            ws.server_upgrade(left, timeout_s=1.0)
    finally:
        left.close()
        right.close()


def test_role_correct_masking_and_bidirectional_text_frames():
    client_sock, server_sock = socket.socketpair()
    client = ws.WebSocketConnection(client_sock, "client")
    server = ws.WebSocketConnection(server_sock, "server")
    try:
        client.send_json({"from": "client"}, time.monotonic() + 1.0)
        assert server.receive_json(time.monotonic() + 1.0) == {
            "from": "client"}
        server.send_json({"from": "server"}, time.monotonic() + 1.0)
        assert client.receive_json(time.monotonic() + 1.0) == {
            "from": "server"}
    finally:
        client_sock.close()
        server_sock.close()


@pytest.mark.parametrize("size", [0, 1, 125, 126, 65535, 65536,
                                   ws.MAX_FRAME_BYTES])
def test_frame_length_boundaries_round_trip_without_fragmentation(size):
    receiver_sock, sender_sock = socket.socketpair()
    receiver = ws.WebSocketConnection(receiver_sock, "server")
    payload = bytes((index % 251 for index in range(size)))
    try:
        encoded = ws.encode_frame(
            ws.OP_TEXT, payload, role="client", mask_key=b"mask")
        receiver._read_buffer.extend(encoded)
        assert receiver.receive_frame(time.monotonic() + 1.0).payload == payload
    finally:
        receiver_sock.close()
        sender_sock.close()


def test_encoder_hard_caps_frames_at_one_mib_even_if_caller_asks_for_more():
    with pytest.raises(ws.MessageTooLarge):
        ws.encode_frame(
            ws.OP_TEXT, b"x" * (ws.MAX_FRAME_BYTES + 1), role="server",
            max_frame_bytes=ws.MAX_FRAME_BYTES * 2)
    with pytest.raises(ws.ProtocolError, match="control"):
        ws.encode_frame(ws.OP_PING, b"x" * 126, role="server")


@pytest.mark.parametrize("receiver_role,sender_role", [
    ("server", "server"),  # client -> server was not masked
    ("client", "client"),  # server -> client was masked
])
def test_masking_direction_is_enforced(receiver_role, sender_role):
    receiver_sock, sender_sock = socket.socketpair()
    receiver = ws.WebSocketConnection(receiver_sock, receiver_role)
    try:
        sender_sock.sendall(ws.encode_frame(
            ws.OP_TEXT, b"{}", role=sender_role,
            mask_key=b"mask" if sender_role == "client" else None))
        with pytest.raises(ws.ProtocolError, match="must be"):
            receiver.receive_frame(time.monotonic() + 1.0)
    finally:
        receiver_sock.close()
        sender_sock.close()


def test_fragmented_reserved_binary_and_nonminimal_frames_are_refused():
    bad_frames = [
        b"\x01\x80" + b"mask",                 # FIN clear
        b"\x82\x80" + b"mask",                 # binary opcode
        b"\xc1\x80" + b"mask",                 # RSV1 set
        b"\x81\xfe\x00\x01" + b"mask" + b"x",  # non-minimal len
    ]
    for raw in bad_frames:
        receiver_sock, sender_sock = socket.socketpair()
        receiver = ws.WebSocketConnection(receiver_sock, "server")
        try:
            sender_sock.sendall(raw)
            with pytest.raises(ws.ProtocolError):
                receiver.receive_frame(time.monotonic() + 1.0)
        finally:
            receiver_sock.close()
            sender_sock.close()


def test_advertised_oversized_frame_is_refused_without_receiving_payload():
    receiver_sock, sender_sock = socket.socketpair()
    receiver = ws.WebSocketConnection(receiver_sock, "server")
    try:
        header = (b"\x81\xff" +
                  struct.pack("!Q", ws.MAX_FRAME_BYTES + 1))
        sender_sock.sendall(header)
        with pytest.raises(ws.MessageTooLarge):
            receiver.receive_frame(time.monotonic() + 1.0)
    finally:
        receiver_sock.close()
        sender_sock.close()


@pytest.mark.parametrize("payload", [
    b"\xff",
    b'{"duplicate":1,"duplicate":2}',
    b'{"not_finite":NaN}',
    b"[]",
])
def test_text_payload_must_be_strict_utf8_json_object(payload):
    receiver_sock, sender_sock = socket.socketpair()
    receiver = ws.WebSocketConnection(receiver_sock, "server")
    try:
        sender_sock.sendall(ws.encode_frame(
            ws.OP_TEXT, payload, role="client", mask_key=b"mask"))
        with pytest.raises(ws.InvalidPayload):
            receiver.receive_json(time.monotonic() + 1.0)
    finally:
        receiver_sock.close()
        sender_sock.close()


def test_authenticated_hello_exchange_is_bidirectional_and_exactly_bound():
    client_sock, server_sock = socket.socketpair()
    client_ws = ws.WebSocketConnection(client_sock, "client")
    server_ws = ws.WebSocketConnection(server_sock, "server")
    client_hello = ws.build_hello(
        "host-a", "host-b", 4, ["project-one"], {"digest": "aaa"},
        role="client", connection_nonce="A" * 24)
    server_hello = ws.build_hello(
        "host-b", "host-a", 7, ["project-one", "project-two"],
        {"digest": "bbb"}, role="server", connection_nonce="B" * 24)
    captured = {}

    def serve():
        captured["remote"] = ws.exchange_hello(
            server_ws, server_hello, expected_peer_host_id="host-a",
            timeout_s=1.0)

    thread = threading.Thread(target=serve)
    thread.start()
    remote = ws.exchange_hello(
        client_ws, client_hello, expected_peer_host_id="host-b",
        timeout_s=1.0)
    thread.join(1.0)
    try:
        assert not thread.is_alive()
        assert remote.host_id == "host-b"
        assert remote.generation == 7
        assert captured["remote"].host_id == "host-a"
        assert captured["remote"].namespaces == ("project-one",)
    finally:
        client_sock.close()
        server_sock.close()


def test_hello_reflection_identity_binding_and_protocol_downgrade_fail_closed():
    local_nonce = "A" * 24
    valid_remote = ws.build_hello(
        "host-b", "host-a", 1, ["project"], {}, role="server",
        connection_nonce="B" * 24)

    reflected = dict(valid_remote, connection_nonce=local_nonce)
    with pytest.raises(ws.ProtocolError, match="reflected"):
        ws.validate_hello(
            reflected, local_host_id="host-a",
            expected_peer_host_id="host-b", local_nonce=local_nonce,
            expected_peer_role="server")

    wrong_identity = dict(valid_remote, host_id="host-c")
    with pytest.raises(ws.ProtocolError, match="authenticated peer"):
        ws.validate_hello(
            wrong_identity, local_host_id="host-a",
            expected_peer_host_id="host-b", local_nonce=local_nonce,
            expected_peer_role="server")

    wrong_binding = dict(valid_remote, peer_host_id="host-c")
    with pytest.raises(ws.ProtocolError, match="local host"):
        ws.validate_hello(
            wrong_binding, local_host_id="host-a",
            expected_peer_host_id="host-b", local_nonce=local_nonce,
            expected_peer_role="server")

    downgraded = dict(valid_remote, protocol="convoy-ws/0")
    with pytest.raises(ws.ProtocolError, match="version"):
        ws.validate_hello(
            downgraded, local_host_id="host-a",
            expected_peer_host_id="host-b", local_nonce=local_nonce,
            expected_peer_role="server")


def test_establish_helpers_cover_upgrade_hello_and_rpc():
    client_sock, server_sock = socket.socketpair()
    captured = {}

    def serve():
        captured["session"], captured["hello"] = ws.establish_server(
            server_sock, local_host_id="host-b",
            expected_peer_host_id="host-a", generation=2,
            namespaces=["project"], capability_summary={"b": 2},
            expected_host="host-b:47600",
            handler=lambda method, params: [method, params], timeout_s=2.0,
            ping_interval_s=None, idle_timeout_s=None)

    thread = threading.Thread(target=serve)
    thread.start()
    client, remote = ws.establish_client(
        client_sock, host_header="host-b:47600", local_host_id="host-a",
        expected_peer_host_id="host-b", generation=1,
        namespaces=["project"], capability_summary={"a": 1},
        handler=lambda method, params: None, timeout_s=2.0,
        ping_interval_s=None, idle_timeout_s=None)
    thread.join(2.0)
    try:
        assert not thread.is_alive()
        assert remote.host_id == "host-b"
        assert captured["hello"].host_id == "host-a"
        assert client.call("echo", {"value": 3}, timeout_s=1.0) == [
            "echo", {"value": 3}]
    finally:
        client.close(timeout_s=0.5)
        if "session" in captured:
            captured["session"].close(timeout_s=0.5)


def test_full_duplex_calls_can_cross_on_one_session():
    client, server = make_session_pair(
        handler_a=lambda method, params: {"at": "a", "params": params},
        handler_b=lambda method, params: {"at": "b", "params": params},
        ping_interval_s=None, idle_timeout_s=None)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            to_server = pool.submit(client.call, "work", 1, 1.0)
            to_client = pool.submit(server.call, "work", 2, 1.0)
            assert to_server.result() == {"at": "b", "params": 1}
            assert to_client.result() == {"at": "a", "params": 2}
    finally:
        close_pair(client, server)


def test_concurrent_responses_complete_out_of_order_by_correlation_id():
    entered = [threading.Event() for _ in range(4)]
    release = [threading.Event() for _ in range(4)]
    completed = [threading.Event() for _ in range(4)]
    handler_order = []
    order_lock = threading.Lock()

    def handler(method, index):
        entered[index].set()
        assert release[index].wait(1.0)
        with order_lock:
            handler_order.append(index)
        completed[index].set()
        return index * 10

    client, server = make_session_pair(
        handler_b=handler, handler_workers=4,
        ping_interval_s=None, idle_timeout_s=None)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(client.call, "ordered", i, 2.0)
                       for i in range(4)]
            assert all(event.wait(1.0) for event in entered)
            for index in reversed(range(4)):
                release[index].set()
                assert completed[index].wait(1.0)
            assert [future.result() for future in futures] == [0, 10, 20, 30]
        assert handler_order == [3, 2, 1, 0]
    finally:
        close_pair(client, server)


def test_sixty_concurrent_bidirectional_calls_model_thirty_peer_load():
    client, server = make_session_pair(
        handler_a=lambda method, value: {"side": "a", "value": value},
        handler_b=lambda method, value: {"side": "b", "value": value},
        max_pending=64, max_inbound_queue=64, handler_workers=12,
        ping_interval_s=None, idle_timeout_s=None)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=60) as pool:
            futures = []
            for value in range(30):
                futures.append(("b", value, pool.submit(
                    client.call, "load", value, 5.0)))
                futures.append(("a", value, pool.submit(
                    server.call, "load", value, 5.0)))
            for side, value, future in futures:
                assert future.result() == {"side": side, "value": value}
        assert client.stats()["responses_received"] == 30
        assert server.stats()["responses_received"] == 30
        assert client.stats()["pending"] == 0
        assert server.stats()["pending"] == 0
    finally:
        close_pair(client, server)


def test_pending_limit_applies_backpressure_with_one_cumulative_deadline():
    entered = threading.Event()
    release = threading.Event()

    def handler(method, params):
        entered.set()
        assert release.wait(1.0)
        return "done"

    client, server = make_session_pair(
        handler_b=handler, max_pending=1,
        ping_interval_s=None, idle_timeout_s=None)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.call, "slow", None, 2.0)
            assert entered.wait(1.0)
            started = time.monotonic()
            with pytest.raises(ws.SessionBusy):
                client.call("blocked", None, timeout_s=0.06)
            elapsed = time.monotonic() - started
            assert 0.045 <= elapsed < 0.25
            release.set()
            assert first.result() == "done"
    finally:
        release.set()
        close_pair(client, server)


def test_bounded_inbound_queue_returns_server_busy_without_closing_session():
    entered = threading.Event()
    release = threading.Event()

    def handler(method, value):
        if value == 1:
            entered.set()
            assert release.wait(2.0)
        return value

    client, server = make_session_pair(
        handler_b=handler, max_inbound_queue=1, handler_workers=1,
        ping_interval_s=None, idle_timeout_s=None)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.call, "queue", 1, 3.0)
            assert entered.wait(1.0)
            second = pool.submit(client.call, "queue", 2, 3.0)
            assert wait_until(lambda: server.stats()["inbound_queued"] == 1)
            with pytest.raises(ws.RemoteError) as refused:
                client.call("queue", 3, timeout_s=1.0)
            assert refused.value.remote_code == "server_busy"
            assert not client.is_closed
            release.set()
            assert first.result() == 1
            assert second.result() == 2
    finally:
        release.set()
        close_pair(client, server)


def test_timeout_discards_late_response_and_session_remains_usable():
    entered = threading.Event()
    release = threading.Event()

    def handler(method, value):
        if method == "slow":
            entered.set()
            assert release.wait(1.0)
        return value

    client, server = make_session_pair(
        handler_b=handler, handler_workers=2,
        ping_interval_s=None, idle_timeout_s=None)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.call, "slow", 10, 0.08)
            assert entered.wait(1.0)
            with pytest.raises(ws.WebSocketTimeout):
                future.result()
        release.set()
        assert wait_until(lambda: client.stats()["late_responses"] == 1)
        assert client.call("fast", 20, timeout_s=1.0) == 20
    finally:
        release.set()
        close_pair(client, server)


def test_ping_is_echoed_as_pong_and_keepalive_tracks_it():
    client, server = make_session_pair(
        ping_interval_s=0.04, idle_timeout_s=0.4)
    try:
        client.ws.send_frame(
            ws.OP_PING, b"deterministic", time.monotonic() + 1.0)
        assert wait_until(lambda: server.stats()["pings_received"] >= 1)
        assert wait_until(lambda: client.stats()["pongs_received"] >= 1)
        assert wait_until(
            lambda: client.stats()["pings_sent"] >= 1
            and server.stats()["pings_sent"] >= 1)
        assert not client.is_closed
        assert not server.is_closed
    finally:
        close_pair(client, server)


def test_idle_peer_that_never_pongs_is_closed():
    client_sock, silent_sock = socket.socketpair()
    client = ws.Session(
        ws.WebSocketConnection(client_sock, "client"),
        ping_interval_s=0.02, idle_timeout_s=0.08,
        name="idle-client").start()
    try:
        assert client._closed.wait(1.0)
        assert isinstance(client.terminal_error, ws.WebSocketTimeout)
        assert client.stats()["pings_sent"] >= 1
    finally:
        client.close(timeout_s=0.2)
        silent_sock.close()


def test_reflected_outbound_request_id_is_protocol_error(monkeypatch):
    class FixedId:
        hex = "fixed-correlation-id"

    monkeypatch.setattr(ws.uuid, "uuid4", lambda: FixedId())
    client_sock, peer_sock = socket.socketpair()
    client = ws.Session(
        ws.WebSocketConnection(client_sock, "client"),
        ping_interval_s=None, idle_timeout_s=None,
        name="reflection-client").start()
    peer = ws.WebSocketConnection(peer_sock, "server")

    def reflect():
        request = peer.receive_json(time.monotonic() + 1.0)
        peer.send_json(request, time.monotonic() + 1.0)

    thread = threading.Thread(target=reflect)
    thread.start()
    try:
        with pytest.raises(ws.ProtocolError, match="reflected"):
            client.call("reflect", None, timeout_s=1.0)
        assert client.is_closed
    finally:
        thread.join(1.0)
        client.close(timeout_s=0.2)
        peer_sock.close()


def test_graceful_shutdown_releases_pending_calls_and_all_threads():
    entered = threading.Event()
    release = threading.Event()

    def handler(method, value):
        entered.set()
        release.wait(1.0)
        return value

    client, server = make_session_pair(
        handler_b=handler, handler_workers=2,
        ping_interval_s=None, idle_timeout_s=None)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(client.call, "slow", 1, 5.0)
    try:
        assert entered.wait(1.0)
        client.close(timeout_s=0.5, reason="test complete")
        with pytest.raises(ws.ConnectionClosed):
            future.result(timeout=1.0)
        release.set()
        assert server._closed.wait(1.0)
        server.close(timeout_s=0.5)
        assert client.stats()["pending"] == 0
        assert server.stats()["pending"] == 0
        assert client.stats()["inbound_active"] == 0
        assert server.stats()["inbound_active"] == 0
        assert not client._reader.is_alive()
        assert not server._reader.is_alive()
        assert all(not thread.is_alive() for thread in client._workers)
        assert all(not thread.is_alive() for thread in server._workers)
    finally:
        release.set()
        client.close(timeout_s=0.2)
        server.close(timeout_s=0.2)
        executor.shutdown(wait=True)


def test_oversized_response_is_clamped_not_aborted():
    # Conformance with the HTTPS peer route: an over-limit handler result is
    # answered with a bounded response_too_large refusal (the SAME outcome the
    # HTTP route gives via MAX_PEER_RESPONSE_BYTES) rather than tearing down the
    # persistent session, so the two transports agree on the same relay.
    big = {"blob": "x" * (ws.MAX_RESPONSE_BYTES + 4096)}

    def handler(method, value):
        return big if method == "huge" else {"echo": value}

    client, server = make_session_pair(
        handler_b=handler, ping_interval_s=None, idle_timeout_s=None)
    try:
        with pytest.raises(ws.RemoteError) as excinfo:
            client.call("huge", None, timeout_s=2.0)
        assert excinfo.value.remote_code == "response_too_large"
        # The session was NOT aborted by the over-limit response: a normal
        # call still round-trips over the same connection.
        assert not client.is_closed
        assert not server.is_closed
        assert client.call("small", {"v": 7}, timeout_s=2.0) == {
            "echo": {"v": 7}}
        # The peer received the request and answered it (nothing torn down
        # pre-send), unlike the abort path which left requests_received at 0.
        assert server.stats()["requests_received"] >= 2
    finally:
        close_pair(client, server)


def test_response_just_under_budget_still_round_trips():
    # The clamp only fires ABOVE the budget; a large-but-legal response is
    # delivered intact, so the two transports share one request/response
    # byte budget rather than the WSS path being stricter or looser.
    # Account for the {"type":"response","id":...,"ok":true,"result":...}
    # wrapper so the encoded message lands just under the ceiling.
    payload = "y" * (ws.MAX_RESPONSE_BYTES - 4096)
    result = {"blob": payload}

    client, server = make_session_pair(
        handler_b=lambda method, value: result,
        ping_interval_s=None, idle_timeout_s=None)
    try:
        assert client.call("large", None, timeout_s=2.0) == result
    finally:
        close_pair(client, server)
