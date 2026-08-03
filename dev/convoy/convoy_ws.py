"""Convoy WebSocket session primitives.

This module is deliberately pure stdlib and deliberately does *not* create
TCP or TLS connections.  A caller gives it an already-connected,
already-authenticated TLS socket and the exact host id established by that
authentication.  The layers here provide:

* a strict RFC 6455 HTTP/1.1 Upgrade for the Convoy subprotocol;
* FIN-only text/JSON WebSocket frames with mandatory role-correct masking;
* a host-bound hello exchange which cannot replace the caller's TLS identity;
* a bounded, full-duplex request/response session over one connection.

There is no TouchDesigner import and no access to TD objects.  In particular,
an inbound RPC handler must perform any required main-thread handoff itself.

Convoy v1 intentionally does not negotiate extensions or fragmented frames.
Rejecting those features keeps the LAN transport small, deterministic, and
immune to compression and continuation-frame ambiguity.  A later protocol
version may add them under a different, explicitly negotiated subprotocol.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import queue
import re
import secrets
import select
import socket
import ssl
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple


SUBPROTOCOL = "embody.convoy.v1"
HELLO_PROTOCOL = "convoy-ws/1"
DEFAULT_PATH = "/convoy/ws"
MAX_FRAME_BYTES = 1024 * 1024
# Peer RPC responses are clamped to the SAME budget as the HTTPS peer route
# (convoy_peerserver.MAX_PEER_RESPONSE_BYTES). An over-limit response is
# answered with a bounded response_too_large body rather than tearing down
# the persistent session, so the two transports give the identical outcome
# for the identical relay instead of disagreeing on whichever one is up.
# Kept in sync with peerserver's constant without importing it (peerserver
# imports this module, not the other way around).
MAX_RESPONSE_BYTES = 256 * 1024
MAX_HTTP_HEADER_BYTES = 16 * 1024
MAX_HTTP_LINE_BYTES = 4096
MAX_PENDING_REQUESTS = 128
MAX_INBOUND_QUEUE = 128
DEFAULT_HANDLER_WORKERS = 4

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class ConvoyWebSocketError(Exception):
    """Base class for named WebSocket/session failures."""

    code = "websocket_error"

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail or "")
        super().__init__(
            f"{self.code}: {self.detail}" if self.detail else self.code)


class UpgradeRejected(ConvoyWebSocketError):
    code = "upgrade_rejected"


class ProtocolError(ConvoyWebSocketError):
    code = "protocol_error"


class MessageTooLarge(ProtocolError):
    code = "message_too_large"


class InvalidPayload(ProtocolError):
    code = "invalid_payload"


class WebSocketTimeout(ConvoyWebSocketError):
    code = "timeout"


class ConnectionClosed(ConvoyWebSocketError):
    code = "connection_closed"


class SessionBusy(ConvoyWebSocketError):
    code = "session_busy"


class RemoteError(ConvoyWebSocketError):
    code = "remote_error"

    def __init__(self, remote_code: str, detail: str = "",
                 data: Any = None) -> None:
        self.remote_code = str(remote_code or "remote_error")
        self.data = data
        super().__init__(detail or self.remote_code)


@dataclass(frozen=True)
class Frame:
    opcode: int
    payload: bytes


@dataclass(frozen=True)
class UpgradeInfo:
    path: str
    host: str
    subprotocol: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class PeerHello:
    protocol: str
    host_id: str
    peer_host_id: str
    connection_nonce: str
    generation: int
    namespaces: Tuple[str, ...]
    capability_summary: Mapping[str, Any]
    role: str


@dataclass
class _Pending:
    event: threading.Event
    result: Any = None
    error: Optional[BaseException] = None


def _remaining(deadline: Optional[float], monotonic: Callable[[], float]) -> Optional[float]:
    if deadline is None:
        return None
    return max(0.0, float(deadline) - monotonic())


def _validate_timeout(timeout_s: Optional[float]) -> Optional[float]:
    if timeout_s is None:
        return None
    if isinstance(timeout_s, bool):
        raise ValueError("timeout_s must be a positive number")
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        raise ValueError("timeout_s must be a positive number")
    if not (value > 0.0) or value == float("inf"):
        raise ValueError("timeout_s must be a finite positive number")
    return value


def _deadline(timeout_s: Optional[float], monotonic: Callable[[], float]) -> Optional[float]:
    value = _validate_timeout(timeout_s)
    return None if value is None else monotonic() + value


def _wait_readable(sock: socket.socket, deadline: Optional[float],
                   monotonic: Callable[[], float]) -> None:
    while True:
        # SSLSocket may already hold decrypted application bytes even though
        # the underlying descriptor is no longer readable.
        try:
            if isinstance(sock, ssl.SSLSocket) and sock.pending() > 0:
                return
        except (OSError, ValueError):
            pass
        timeout = _remaining(deadline, monotonic)
        if timeout is not None and timeout <= 0:
            raise WebSocketTimeout("receive deadline expired")
        try:
            readable, _, exceptional = select.select([sock], [], [sock], timeout)
        except (OSError, ValueError) as exc:
            raise ConnectionClosed(str(exc)) from exc
        if exceptional:
            raise ConnectionClosed("socket exception while reading")
        if readable:
            return
        raise WebSocketTimeout("receive deadline expired")


def _wait_writable(sock: socket.socket, deadline: Optional[float],
                   monotonic: Callable[[], float]) -> None:
    while True:
        timeout = _remaining(deadline, monotonic)
        if timeout is not None and timeout <= 0:
            raise WebSocketTimeout("send deadline expired")
        try:
            _, writable, exceptional = select.select([], [sock], [sock], timeout)
        except (OSError, ValueError) as exc:
            raise ConnectionClosed(str(exc)) from exc
        if exceptional:
            raise ConnectionClosed("socket exception while writing")
        if writable:
            return
        raise WebSocketTimeout("send deadline expired")


def _send_all(sock: socket.socket, data: bytes, deadline: Optional[float],
              monotonic: Callable[[], float] = time.monotonic) -> None:
    view = memoryview(data)
    while view:
        _wait_writable(sock, deadline, monotonic)
        try:
            sent = sock.send(view)
        except ssl.SSLWantReadError:
            _wait_readable(sock, deadline, monotonic)
            continue
        except (ssl.SSLWantWriteError, BlockingIOError):
            continue
        except OSError as exc:
            raise ConnectionClosed(str(exc)) from exc
        if sent <= 0:
            raise ConnectionClosed("socket closed while writing")
        view = view[sent:]


def _recv_one(sock: socket.socket, deadline: Optional[float],
              monotonic: Callable[[], float] = time.monotonic) -> bytes:
    while True:
        _wait_readable(sock, deadline, monotonic)
        try:
            data = sock.recv(1)
        except ssl.SSLWantWriteError:
            _wait_writable(sock, deadline, monotonic)
            continue
        except (ssl.SSLWantReadError, BlockingIOError):
            continue
        except OSError as exc:
            raise ConnectionClosed(str(exc)) from exc
        if not data:
            raise ConnectionClosed("socket closed while reading HTTP headers")
        return data


def _read_http_head(sock: socket.socket, deadline: Optional[float],
                    monotonic: Callable[[], float] = time.monotonic) -> bytes:
    # Read one byte at a time so a coalesced first WebSocket frame is never
    # consumed and lost between the upgrade helper and WebSocketConnection.
    data = bytearray()
    marker = b"\r\n\r\n"
    while not data.endswith(marker):
        if len(data) >= MAX_HTTP_HEADER_BYTES:
            raise UpgradeRejected("HTTP upgrade headers exceed 16 KiB")
        data.extend(_recv_one(sock, deadline, monotonic))
    return bytes(data)


def _parse_http_head(raw: bytes) -> Tuple[str, Dict[str, Tuple[str, ...]]]:
    if len(raw) > MAX_HTTP_HEADER_BYTES or not raw.endswith(b"\r\n\r\n"):
        raise UpgradeRejected("invalid or oversized HTTP header block")
    try:
        text = raw.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise UpgradeRejected("HTTP upgrade headers must be ASCII") from exc
    lines = text[:-4].split("\r\n")
    if not lines or not lines[0] or len(lines[0]) > MAX_HTTP_LINE_BYTES:
        raise UpgradeRejected("invalid HTTP start line")
    headers: Dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line or line[:1] in (" ", "\t") or ":" not in line:
            raise UpgradeRejected("malformed or folded HTTP header")
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if not _TOKEN_RE.fullmatch(name):
            raise UpgradeRejected("invalid HTTP header name")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise UpgradeRejected("invalid HTTP header value")
        headers.setdefault(name, []).append(value)
    return lines[0], {name: tuple(values) for name, values in headers.items()}


def _single_header(headers: Mapping[str, Tuple[str, ...]], name: str,
                   required: bool = True) -> Optional[str]:
    values = headers.get(name.lower(), ())
    if not values:
        if required:
            raise UpgradeRejected(f"missing {name} header")
        return None
    if len(values) != 1:
        raise UpgradeRejected(f"duplicate {name} header")
    return values[0]


def _has_token(value: str, token: str) -> bool:
    return token.lower() in {
        part.strip().lower() for part in value.split(",") if part.strip()
    }


def _validate_header_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or any(
            ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{label} must be a non-empty header-safe string")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{label} must not contain newlines")
    return value


def _validate_path(path: str) -> str:
    _validate_header_component(path, "path")
    if not path.startswith("/") or " " in path or "#" in path:
        raise ValueError("path must be an absolute HTTP request target")
    try:
        path.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError("path must be ASCII") from exc
    return path


def websocket_accept(key: str) -> str:
    """Return RFC 6455 Sec-WebSocket-Accept for a validated client key."""
    try:
        decoded = base64.b64decode(key.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise UpgradeRejected("invalid Sec-WebSocket-Key") from exc
    if len(decoded) != 16:
        raise UpgradeRejected("Sec-WebSocket-Key must encode exactly 16 bytes")
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def build_client_upgrade(host: str, path: str = DEFAULT_PATH,
                         key_bytes: Optional[bytes] = None,
                         subprotocol: str = SUBPROTOCOL) -> Tuple[bytes, str]:
    """Build a non-browser Convoy WebSocket request and return request/key."""
    host = _validate_header_component(host, "host")
    path = _validate_path(path)
    subprotocol = _validate_header_component(subprotocol, "subprotocol")
    if not _TOKEN_RE.fullmatch(subprotocol):
        raise ValueError("subprotocol must be one HTTP token")
    key_bytes = secrets.token_bytes(16) if key_bytes is None else key_bytes
    if not isinstance(key_bytes, bytes) or len(key_bytes) != 16:
        raise ValueError("key_bytes must contain exactly 16 bytes")
    key = base64.b64encode(key_bytes).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Protocol: {subprotocol}\r\n"
        "\r\n"
    ).encode("ascii")
    return request, key


def validate_client_upgrade(raw: bytes, expected_path: str = DEFAULT_PATH,
                            expected_host: Optional[str] = None,
                            subprotocol: str = SUBPROTOCOL) -> Tuple[UpgradeInfo, str]:
    """Validate a client request; return public metadata and its WS key."""
    start, headers = _parse_http_head(raw)
    parts = start.split(" ")
    if len(parts) != 3 or parts[0] != "GET" or parts[2] != "HTTP/1.1":
        raise UpgradeRejected("WebSocket upgrade requires GET HTTP/1.1")
    expected_path = _validate_path(expected_path)
    if parts[1] != expected_path:
        raise UpgradeRejected("unexpected WebSocket request path")
    host = _single_header(headers, "host")
    if expected_host is not None and host != expected_host:
        raise UpgradeRejected("unexpected Host header")
    if not _has_token(_single_header(headers, "upgrade"), "websocket"):
        raise UpgradeRejected("Upgrade header does not select websocket")
    if not _has_token(_single_header(headers, "connection"), "upgrade"):
        raise UpgradeRejected("Connection header does not select Upgrade")
    if _single_header(headers, "sec-websocket-version") != "13":
        raise UpgradeRejected("only WebSocket version 13 is supported")
    offered = _single_header(headers, "sec-websocket-protocol")
    # Convoy does not silently pick from an offer list.  Exact matching makes
    # a downgrade or cross-protocol connection fail closed.
    if offered != subprotocol:
        raise UpgradeRejected("exact Convoy subprotocol is required")
    if _single_header(headers, "sec-websocket-extensions", required=False):
        raise UpgradeRejected("WebSocket extensions are not supported")
    if _single_header(headers, "content-length", required=False) not in (None, "0"):
        raise UpgradeRejected("upgrade request must not have a body")
    if _single_header(headers, "transfer-encoding", required=False) is not None:
        raise UpgradeRejected("upgrade request must not use transfer encoding")
    key = _single_header(headers, "sec-websocket-key")
    websocket_accept(key)  # strict key validation
    flattened = {name: values[0] for name, values in headers.items()}
    return UpgradeInfo(parts[1], host, offered, flattened), key


def build_server_upgrade(key: str, subprotocol: str = SUBPROTOCOL) -> bytes:
    accept = websocket_accept(key)
    subprotocol = _validate_header_component(subprotocol, "subprotocol")
    if not _TOKEN_RE.fullmatch(subprotocol):
        raise ValueError("subprotocol must be one HTTP token")
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        f"Sec-WebSocket-Protocol: {subprotocol}\r\n"
        "\r\n"
    ).encode("ascii")


def validate_server_upgrade(raw: bytes, key: str,
                            subprotocol: str = SUBPROTOCOL) -> Mapping[str, str]:
    start, headers = _parse_http_head(raw)
    if start != "HTTP/1.1 101 Switching Protocols":
        raise UpgradeRejected("server did not return HTTP/1.1 101")
    if not _has_token(_single_header(headers, "upgrade"), "websocket"):
        raise UpgradeRejected("Upgrade header does not select websocket")
    if not _has_token(_single_header(headers, "connection"), "upgrade"):
        raise UpgradeRejected("Connection header does not select Upgrade")
    expected_accept = websocket_accept(key)
    supplied_accept = _single_header(headers, "sec-websocket-accept")
    if not secrets.compare_digest(supplied_accept, expected_accept):
        raise UpgradeRejected("Sec-WebSocket-Accept mismatch")
    if _single_header(headers, "sec-websocket-protocol") != subprotocol:
        raise UpgradeRejected("server refused the exact Convoy subprotocol")
    if _single_header(headers, "sec-websocket-extensions", required=False):
        raise UpgradeRejected("server selected an unsupported extension")
    if _single_header(headers, "content-length", required=False) not in (None, "0"):
        raise UpgradeRejected("upgrade response must not have a body")
    if _single_header(headers, "transfer-encoding", required=False) is not None:
        raise UpgradeRejected("upgrade response must not use transfer encoding")
    return {name: values[0] for name, values in headers.items()}


def client_upgrade(sock: socket.socket, host: str,
                   path: str = DEFAULT_PATH, timeout_s: float = 5.0,
                   key_bytes: Optional[bytes] = None,
                   subprotocol: str = SUBPROTOCOL,
                   monotonic: Callable[[], float] = time.monotonic) -> Mapping[str, str]:
    """Perform and validate the client side of an Upgrade cumulatively."""
    deadline = _deadline(timeout_s, monotonic)
    request, key = build_client_upgrade(host, path, key_bytes, subprotocol)
    _send_all(sock, request, deadline, monotonic)
    raw = _read_http_head(sock, deadline, monotonic)
    return validate_server_upgrade(raw, key, subprotocol)


def server_upgrade(sock: socket.socket, expected_path: str = DEFAULT_PATH,
                   expected_host: Optional[str] = None,
                   timeout_s: float = 5.0,
                   subprotocol: str = SUBPROTOCOL,
                   monotonic: Callable[[], float] = time.monotonic) -> UpgradeInfo:
    """Perform and validate the server side of an Upgrade cumulatively."""
    deadline = _deadline(timeout_s, monotonic)
    raw = _read_http_head(sock, deadline, monotonic)
    info, key = validate_client_upgrade(
        raw, expected_path=expected_path, expected_host=expected_host,
        subprotocol=subprotocol)
    _send_all(sock, build_server_upgrade(key, subprotocol), deadline, monotonic)
    return info


def encode_frame(opcode: int, payload: bytes = b"", *, role: str,
                 mask_key: Optional[bytes] = None,
                 max_frame_bytes: int = MAX_FRAME_BYTES) -> bytes:
    """Encode one FIN frame. Client output is always masked; server is not."""
    if role not in ("client", "server"):
        raise ValueError("role must be 'client' or 'server'")
    if (isinstance(max_frame_bytes, bool)
            or not isinstance(max_frame_bytes, int)
            or max_frame_bytes <= 0):
        raise ValueError("max_frame_bytes must be a positive integer")
    max_frame_bytes = min(max_frame_bytes, MAX_FRAME_BYTES)
    if opcode not in (OP_TEXT, OP_CLOSE, OP_PING, OP_PONG):
        raise ProtocolError("unsupported opcode")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    payload = bytes(payload)
    if len(payload) > max_frame_bytes:
        raise MessageTooLarge("frame payload exceeds configured limit")
    if opcode >= 0x8 and len(payload) > 125:
        raise ProtocolError("control frame payload exceeds 125 bytes")
    masked = role == "client"
    if mask_key is not None and not masked:
        raise ValueError("server frames must not specify a mask key")
    if masked:
        mask_key = os.urandom(4) if mask_key is None else mask_key
        if not isinstance(mask_key, bytes) or len(mask_key) != 4:
            raise ValueError("mask_key must contain exactly four bytes")
    first = 0x80 | opcode
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length <= 125:
        header = bytes((first, mask_bit | length))
    elif length <= 0xFFFF:
        header = bytes((first, mask_bit | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, mask_bit | 127)) + struct.pack("!Q", length)
    if not masked:
        return header + payload
    transformed = bytes(
        byte ^ mask_key[index & 3] for index, byte in enumerate(payload))
    return header + mask_key + transformed


def _decode_close_payload(payload: bytes) -> Tuple[Optional[int], str]:
    if not payload:
        return None, ""
    if len(payload) == 1:
        raise ProtocolError("close frame has a one-byte status code")
    code = struct.unpack("!H", payload[:2])[0]
    valid = (
        code in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010,
                 1011, 1012, 1013, 1014)
        or 3000 <= code <= 4999
    )
    if not valid:
        raise ProtocolError("invalid close status code")
    try:
        reason = payload[2:].decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise InvalidPayload("close reason is not valid UTF-8") from exc
    return code, reason


def close_payload(code: int = 1000, reason: str = "") -> bytes:
    if not isinstance(reason, str):
        raise TypeError("close reason must be text")
    payload = struct.pack("!H", int(code)) + reason.encode("utf-8", "strict")
    _decode_close_payload(payload)
    if len(payload) > 125:
        raise ValueError("close reason is too long")
    return payload


def _strict_json_loads(payload: bytes) -> Mapping[str, Any]:
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise InvalidPayload("text frame is not valid UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def unique_object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, parse_constant=reject_constant,
                           object_pairs_hook=unique_object)
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidPayload("text frame does not contain strict JSON") from exc
    if not isinstance(value, dict):
        raise InvalidPayload("WebSocket JSON message must be an object")
    return value


def _strict_json_dumps(message: Mapping[str, Any]) -> bytes:
    if not isinstance(message, Mapping):
        raise InvalidPayload("WebSocket JSON message must be an object")
    try:
        text = json.dumps(message, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidPayload("message is not strict JSON") from exc
    return text.encode("utf-8", "strict")


class WebSocketConnection:
    """Role-aware FIN-only RFC 6455 framing over an existing socket."""

    def __init__(self, sock: socket.socket, role: str, *,
                 max_frame_bytes: int = MAX_FRAME_BYTES,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if role not in ("client", "server"):
            raise ValueError("role must be 'client' or 'server'")
        if not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be a positive integer")
        self.sock = sock
        self.role = role
        self.max_frame_bytes = min(max_frame_bytes, MAX_FRAME_BYTES)
        self.monotonic = monotonic
        self._read_buffer = bytearray()
        self._send_lock = threading.Lock()

    def _parse_buffer(self) -> Optional[Frame]:
        data = self._read_buffer
        if len(data) < 2:
            return None
        first, second = data[0], data[1]
        if not (first & 0x80):
            raise ProtocolError("fragmented frames are not supported")
        if first & 0x70:
            raise ProtocolError("RSV bits require an unsupported extension")
        opcode = first & 0x0F
        if opcode not in (OP_TEXT, OP_CLOSE, OP_PING, OP_PONG):
            raise ProtocolError("unsupported WebSocket opcode")
        masked = bool(second & 0x80)
        expected_masked = self.role == "server"
        if masked != expected_masked:
            expected = "masked" if expected_masked else "unmasked"
            raise ProtocolError(f"inbound frame must be {expected}")
        encoded_length = second & 0x7F
        offset = 2
        if encoded_length == 126:
            if len(data) < offset + 2:
                return None
            length = struct.unpack("!H", data[offset:offset + 2])[0]
            offset += 2
            if length <= 125:
                raise ProtocolError("non-minimal 16-bit frame length")
        elif encoded_length == 127:
            if len(data) < offset + 8:
                return None
            length = struct.unpack("!Q", data[offset:offset + 8])[0]
            offset += 8
            if length <= 0xFFFF or length & (1 << 63):
                raise ProtocolError("invalid or non-minimal 64-bit frame length")
        else:
            length = encoded_length
        if opcode >= 0x8 and length > 125:
            raise ProtocolError("control frame payload exceeds 125 bytes")
        if length > self.max_frame_bytes:
            raise MessageTooLarge("frame payload exceeds configured limit")
        mask_key = None
        if masked:
            if len(data) < offset + 4:
                return None
            mask_key = bytes(data[offset:offset + 4])
            offset += 4
        total = offset + length
        if len(data) < total:
            return None
        payload = bytes(data[offset:total])
        del data[:total]
        if mask_key is not None:
            payload = bytes(
                byte ^ mask_key[index & 3]
                for index, byte in enumerate(payload))
        if opcode == OP_CLOSE:
            _decode_close_payload(payload)
        return Frame(opcode, payload)

    def receive_frame(self, deadline: Optional[float] = None) -> Frame:
        while True:
            frame = self._parse_buffer()
            if frame is not None:
                return frame
            _wait_readable(self.sock, deadline, self.monotonic)
            try:
                chunk = self.sock.recv(65536)
            except ssl.SSLWantWriteError:
                _wait_writable(self.sock, deadline, self.monotonic)
                continue
            except (ssl.SSLWantReadError, BlockingIOError):
                continue
            except OSError as exc:
                raise ConnectionClosed(str(exc)) from exc
            if not chunk:
                raise ConnectionClosed("peer closed the WebSocket")
            self._read_buffer.extend(chunk)
            # recv() can coalesce the tail of one maximum-size frame with the
            # start of the next.  Allow one read quantum beyond the hard frame
            # limit; advertised payload length is checked before allocation.
            if len(self._read_buffer) > self.max_frame_bytes + 65536 + 14:
                raise MessageTooLarge("buffered frame exceeds configured limit")

    def send_frame(self, opcode: int, payload: bytes = b"",
                   deadline: Optional[float] = None) -> None:
        data = encode_frame(opcode, payload, role=self.role,
                            max_frame_bytes=self.max_frame_bytes)
        timeout = _remaining(deadline, self.monotonic)
        if timeout is not None and timeout <= 0:
            raise WebSocketTimeout("send deadline expired")
        acquired = self._send_lock.acquire(
            timeout=timeout if timeout is not None else -1)
        if not acquired:
            raise WebSocketTimeout("send deadline expired waiting for writer")
        try:
            _send_all(self.sock, data, deadline, self.monotonic)
        finally:
            self._send_lock.release()

    def send_json(self, message: Mapping[str, Any],
                  deadline: Optional[float] = None) -> None:
        payload = _strict_json_dumps(message)
        if len(payload) > self.max_frame_bytes:
            raise MessageTooLarge("JSON message exceeds configured limit")
        self.send_frame(OP_TEXT, payload, deadline)

    def receive_json(self, deadline: Optional[float] = None) -> Mapping[str, Any]:
        frame = self.receive_frame(deadline)
        if frame.opcode != OP_TEXT:
            raise ProtocolError("expected a text JSON frame")
        return _strict_json_loads(frame.payload)


_HELLO_FIELDS = frozenset({
    "type", "protocol", "host_id", "peer_host_id", "connection_nonce",
    "generation", "namespaces", "capability_summary", "role",
})


def build_hello(host_id: str, peer_host_id: str, generation: int,
                namespaces: Iterable[str], capability_summary: Mapping[str, Any],
                *, role: str, connection_nonce: Optional[str] = None) -> Dict[str, Any]:
    """Build a hello bound to the caller's authenticated expected peer."""
    nonce = connection_nonce or base64.urlsafe_b64encode(
        secrets.token_bytes(18)).decode("ascii").rstrip("=")
    message = {
        "type": "hello",
        "protocol": HELLO_PROTOCOL,
        "host_id": host_id,
        "peer_host_id": peer_host_id,
        "connection_nonce": nonce,
        "generation": generation,
        "namespaces": list(namespaces),
        "capability_summary": dict(capability_summary),
        "role": role,
    }
    # Use the same strict validator for locally built and remote messages.
    validate_hello(message, local_host_id=peer_host_id,
                   expected_peer_host_id=host_id,
                   local_nonce="not-the-local-connection-nonce",
                   expected_peer_role=role)
    return message


def _valid_identifier(value: Any, label: str, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ProtocolError(f"hello {label} must be non-empty text")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ProtocolError(f"hello {label} contains control characters")
    return value


def validate_hello(message: Mapping[str, Any], *, local_host_id: str,
                   expected_peer_host_id: str, local_nonce: str,
                   expected_peer_role: str) -> PeerHello:
    """Validate hello against identity established outside this module.

    ``expected_peer_host_id`` must come from the authenticated TLS/pinning
    layer.  A peer-provided host id is never treated as an identity claim on
    its own.  ``peer_host_id`` must bind back to this exact local host.
    """
    if not isinstance(message, Mapping) or set(message) != _HELLO_FIELDS:
        raise ProtocolError("hello has missing or unexpected fields")
    if message.get("type") != "hello":
        raise ProtocolError("first WebSocket message must be hello")
    if message.get("protocol") != HELLO_PROTOCOL:
        raise ProtocolError("hello protocol version mismatch")
    local_host_id = _valid_identifier(local_host_id, "local_host_id")
    expected_peer_host_id = _valid_identifier(
        expected_peer_host_id, "expected_peer_host_id")
    if local_host_id == expected_peer_host_id:
        raise ProtocolError("self-host WebSocket sessions are not supported")
    host_id = _valid_identifier(message.get("host_id"), "host_id")
    peer_host_id = _valid_identifier(message.get("peer_host_id"), "peer_host_id")
    if host_id != expected_peer_host_id:
        raise ProtocolError("hello host_id does not match authenticated peer")
    if peer_host_id != local_host_id:
        raise ProtocolError("hello is not bound to this local host")
    role = message.get("role")
    if role not in ("client", "server") or role != expected_peer_role:
        raise ProtocolError("hello role mismatch")
    nonce = message.get("connection_nonce")
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise ProtocolError("hello connection_nonce is not a strong token")
    if not isinstance(local_nonce, str) or not _NONCE_RE.fullmatch(local_nonce):
        raise ProtocolError("local connection nonce is not a strong token")
    if secrets.compare_digest(nonce, local_nonce):
        raise ProtocolError("reflected hello nonce")
    generation = message.get("generation")
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0 or generation > 0x7FFFFFFFFFFFFFFF):
        raise ProtocolError("hello generation must be an unsigned 63-bit integer")
    namespaces = message.get("namespaces")
    if not isinstance(namespaces, list) or len(namespaces) > 128:
        raise ProtocolError("hello namespaces must be a bounded array")
    normalized = []
    seen = set()
    for namespace in namespaces:
        namespace = _valid_identifier(namespace, "namespace", 128)
        if namespace in seen:
            raise ProtocolError("hello namespaces must be unique")
        seen.add(namespace)
        normalized.append(namespace)
    summary = message.get("capability_summary")
    if not isinstance(summary, dict) or len(summary) > 256:
        raise ProtocolError("hello capability_summary must be a bounded object")
    # This also rejects non-JSON values and non-finite numbers supplied by an
    # in-process caller rather than a decoded wire message.
    _strict_json_dumps(summary)
    return PeerHello(
        HELLO_PROTOCOL, host_id, peer_host_id, nonce, generation,
        tuple(normalized), dict(summary), role)


def exchange_hello(ws: WebSocketConnection, local_hello: Mapping[str, Any],
                   *, expected_peer_host_id: str,
                   timeout_s: float = 5.0,
                   monotonic: Callable[[], float] = time.monotonic) -> PeerHello:
    """Exchange role-ordered hellos and bind the remote one to TLS identity."""
    deadline = _deadline(timeout_s, monotonic)
    if ws.role not in ("client", "server"):
        raise ValueError("invalid WebSocket role")
    local = validate_hello(
        local_hello,
        local_host_id=local_hello.get("peer_host_id"),
        expected_peer_host_id=local_hello.get("host_id"),
        local_nonce="not-the-local-connection-nonce",
        expected_peer_role=ws.role)
    if local.peer_host_id != expected_peer_host_id:
        raise ProtocolError(
            "local hello peer binding differs from authenticated peer")
    opposite = "server" if ws.role == "client" else "client"
    if ws.role == "client":
        ws.send_json(local_hello, deadline)
        remote_message = ws.receive_json(deadline)
    else:
        remote_message = ws.receive_json(deadline)
        # Validate before sending our own identity/capability information.
        remote = validate_hello(
            remote_message, local_host_id=local.host_id,
            expected_peer_host_id=expected_peer_host_id,
            local_nonce=local.connection_nonce,
            expected_peer_role=opposite)
        ws.send_json(local_hello, deadline)
        return remote
    return validate_hello(
        remote_message, local_host_id=local.host_id,
        expected_peer_host_id=expected_peer_host_id,
        local_nonce=local.connection_nonce,
        expected_peer_role=opposite)


class Session:
    """Bounded full-duplex multiplexed RPC over one WebSocket connection.

    ``handler`` is called as ``handler(method, params)`` on a bounded worker
    pool.  Calls made by either peer use independent correlation ids, so both
    sides may issue requests concurrently and responses may complete in any
    order.  All waiting within :meth:`call` consumes one cumulative monotonic
    deadline: pending-slot backpressure, writer contention, network send, and
    response wait do not each receive a fresh timeout.
    """

    def __init__(self, ws: WebSocketConnection,
                 handler: Optional[Callable[[str, Any], Any]] = None, *,
                 max_pending: int = MAX_PENDING_REQUESTS,
                 max_inbound_queue: int = MAX_INBOUND_QUEUE,
                 handler_workers: int = DEFAULT_HANDLER_WORKERS,
                 ping_interval_s: Optional[float] = 15.0,
                 idle_timeout_s: Optional[float] = 45.0,
                 max_response_bytes: int = MAX_RESPONSE_BYTES,
                 monotonic: Callable[[], float] = time.monotonic,
                 name: Optional[str] = None) -> None:
        for value, label in (
                (max_pending, "max_pending"),
                (max_inbound_queue, "max_inbound_queue"),
                (handler_workers, "handler_workers"),
                (max_response_bytes, "max_response_bytes")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        if ping_interval_s is not None:
            ping_interval_s = _validate_timeout(ping_interval_s)
        if idle_timeout_s is not None:
            idle_timeout_s = _validate_timeout(idle_timeout_s)
        if (ping_interval_s is not None and idle_timeout_s is not None
                and idle_timeout_s <= ping_interval_s):
            raise ValueError("idle_timeout_s must exceed ping_interval_s")
        self.ws = ws
        self.handler = handler
        self.max_pending = max_pending
        self.max_inbound_queue = max_inbound_queue
        self.handler_workers = handler_workers
        self.max_response_bytes = max_response_bytes
        self.ping_interval_s = ping_interval_s
        self.idle_timeout_s = idle_timeout_s
        self.monotonic = monotonic
        self.name = name or f"convoy-ws-{ws.role}-{id(self):x}"

        self._state_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._pending_slots = threading.BoundedSemaphore(max_pending)
        self._inbound: queue.Queue = queue.Queue(max_inbound_queue)
        self._active_inbound = set()
        self._closed = threading.Event()
        self._closing = threading.Event()
        self._started = False
        self._reader: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        now = self.monotonic()
        self._last_rx = now
        self._last_ping = now
        self._ping_payload: Optional[bytes] = None
        self._terminal_error: Optional[BaseException] = None
        self._stats_lock = threading.Lock()
        self._stats = {
            "requests_sent": 0,
            "requests_received": 0,
            "responses_sent": 0,
            "responses_received": 0,
            "late_responses": 0,
            "busy_responses": 0,
            "pings_sent": 0,
            "pings_received": 0,
            "pongs_received": 0,
        }

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def terminal_error(self) -> Optional[BaseException]:
        return self._terminal_error

    def stats(self) -> Mapping[str, int]:
        with self._stats_lock:
            result = dict(self._stats)
        with self._pending_lock:
            result["pending"] = len(self._pending)
            result["inbound_active"] = len(self._active_inbound)
        result["inbound_queued"] = self._inbound.qsize()
        return result

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self._stats[key] += 1

    def start(self) -> "Session":
        with self._state_lock:
            if self._started:
                return self
            if self._closed.is_set():
                raise ConnectionClosed("session is already closed")
            self._started = True
            for index in range(self.handler_workers):
                worker = threading.Thread(
                    target=self._worker_loop,
                    name=f"{self.name}-handler-{index}", daemon=True)
                self._workers.append(worker)
                worker.start()
            self._reader = threading.Thread(
                target=self._reader_loop, name=f"{self.name}-reader",
                daemon=True)
            self._reader.start()
        return self

    def call(self, method: str, params: Any = None,
             timeout_s: float = 30.0) -> Any:
        if not isinstance(method, str) or not method or len(method) > 256:
            raise ValueError("method must be non-empty text up to 256 characters")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in method):
            raise ValueError("method contains control characters")
        deadline = _deadline(timeout_s, self.monotonic)
        with self._state_lock:
            if not self._started:
                raise ConnectionClosed("session has not been started")
            if self._closed.is_set() or self._closing.is_set():
                raise ConnectionClosed("session is closed")
        remaining = _remaining(deadline, self.monotonic)
        if remaining is None or remaining <= 0 or not self._pending_slots.acquire(
                timeout=remaining):
            raise SessionBusy("pending request limit reached before deadline")
        request_id = uuid.uuid4().hex
        waiter = _Pending(threading.Event())
        with self._pending_lock:
            if self._closed.is_set():
                self._pending_slots.release()
                raise ConnectionClosed("session closed before request registration")
            self._pending[request_id] = waiter
        message = {
            "type": "request", "id": request_id,
            "method": method, "params": params,
        }
        try:
            self.ws.send_json(message, deadline)
            self._bump("requests_sent")
        except BaseException as exc:
            self._remove_pending(request_id, waiter)
            # A send deadline can expire after part of a frame reached the
            # socket.  The only safe recovery is to discard the stream rather
            # than append a future frame to an indeterminate payload.
            if isinstance(exc, (WebSocketTimeout, ConnectionClosed)):
                self._abort(exc)
            raise
        remaining = _remaining(deadline, self.monotonic)
        signalled = bool(remaining and waiter.event.wait(remaining))
        if not signalled:
            removed = self._remove_pending(request_id, waiter)
            if removed:
                raise WebSocketTimeout("RPC response deadline expired")
            # The reader won the boundary race and set the event under the
            # pending lock before removing the entry.
            waiter.event.wait(0)
        if waiter.error is not None:
            raise waiter.error
        return waiter.result

    def _remove_pending(self, request_id: str, waiter: _Pending) -> bool:
        with self._pending_lock:
            if self._pending.get(request_id) is not waiter:
                return False
            del self._pending[request_id]
            self._pending_slots.release()
            return True

    def _reader_deadline(self) -> float:
        now = self.monotonic()
        candidates = [now + 0.25]
        if self.ping_interval_s is not None:
            candidates.append(self._last_ping + self.ping_interval_s)
        if self.idle_timeout_s is not None:
            candidates.append(self._last_rx + self.idle_timeout_s)
        # If a deadline is already due, still allow a short select quantum so
        # a queued pong is consumed before declaring an idle connection dead.
        return max(now + 0.001, min(candidates))

    def _reader_loop(self) -> None:
        try:
            while not self._closing.is_set():
                try:
                    frame = self.ws.receive_frame(self._reader_deadline())
                except WebSocketTimeout:
                    self._keepalive_tick()
                    continue
                self._last_rx = self.monotonic()
                if frame.opcode == OP_PING:
                    self._bump("pings_received")
                    self.ws.send_frame(OP_PONG, frame.payload,
                                       self.monotonic() + 1.0)
                    continue
                if frame.opcode == OP_PONG:
                    self._bump("pongs_received")
                    if self._ping_payload is not None and secrets.compare_digest(
                            frame.payload, self._ping_payload):
                        self._ping_payload = None
                    continue
                if frame.opcode == OP_CLOSE:
                    if not self._closing.is_set():
                        try:
                            self.ws.send_frame(OP_CLOSE, frame.payload,
                                               self.monotonic() + 1.0)
                        except ConvoyWebSocketError:
                            pass
                    self._closing.set()
                    break
                if frame.opcode != OP_TEXT:
                    raise ProtocolError("only text JSON RPC messages are supported")
                self._handle_message(_strict_json_loads(frame.payload))
        except BaseException as exc:
            if not self._closing.is_set():
                self._terminal_error = exc
                close_code = 1009 if isinstance(exc, MessageTooLarge) else 1002
                if isinstance(exc, InvalidPayload):
                    close_code = 1007
                elif isinstance(exc, WebSocketTimeout):
                    close_code = 1001
                try:
                    self.ws.send_frame(
                        OP_CLOSE, close_payload(close_code, exc.code),
                        self.monotonic() + 0.25)
                except BaseException:
                    pass
        finally:
            self._abort(self._terminal_error or ConnectionClosed("peer closed session"))

    def _keepalive_tick(self) -> None:
        now = self.monotonic()
        if self.idle_timeout_s is not None and now - self._last_rx >= self.idle_timeout_s:
            raise WebSocketTimeout("peer idle timeout expired")
        if (self.ping_interval_s is not None
                and now - self._last_ping >= self.ping_interval_s):
            payload = secrets.token_bytes(8)
            self.ws.send_frame(OP_PING, payload, now + 1.0)
            self._last_ping = now
            self._ping_payload = payload
            self._bump("pings_sent")

    def _handle_message(self, message: Mapping[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "response":
            self._handle_response(message)
        elif message_type == "request":
            self._handle_request(message)
        else:
            raise ProtocolError("unknown RPC message type")

    @staticmethod
    def _message_id(message: Mapping[str, Any]) -> str:
        request_id = message.get("id")
        if not isinstance(request_id, str) or not _MESSAGE_ID_RE.fullmatch(request_id):
            raise ProtocolError("invalid RPC correlation id")
        return request_id

    def _handle_response(self, message: Mapping[str, Any]) -> None:
        if set(message) not in (
                {"type", "id", "ok", "result"},
                {"type", "id", "ok", "error"}):
            raise ProtocolError("malformed RPC response")
        request_id = self._message_id(message)
        ok = message.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolError("RPC response ok must be boolean")
        if ok and "result" not in message:
            raise ProtocolError("successful RPC response lacks result")
        if not ok and "error" not in message:
            raise ProtocolError("failed RPC response lacks error")
        remote_error = None
        if not ok:
            error = message.get("error")
            if not isinstance(error, dict):
                raise ProtocolError("RPC error must be an object")
            code = error.get("code")
            detail = error.get("message", "")
            if not isinstance(code, str) or not code or not isinstance(detail, str):
                raise ProtocolError("malformed RPC error")
            remote_error = RemoteError(code, detail, error.get("data"))
        with self._pending_lock:
            waiter = self._pending.pop(request_id, None)
            if waiter is None:
                self._bump("late_responses")
                return
            if ok:
                waiter.result = message.get("result")
            else:
                waiter.error = remote_error
            waiter.event.set()
            self._pending_slots.release()
        self._bump("responses_received")

    def _handle_request(self, message: Mapping[str, Any]) -> None:
        if set(message) != {"type", "id", "method", "params"}:
            raise ProtocolError("malformed RPC request")
        request_id = self._message_id(message)
        method = message.get("method")
        if (not isinstance(method, str) or not method or len(method) > 256
                or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in method)):
            raise ProtocolError("invalid RPC method")
        with self._pending_lock:
            if request_id in self._pending:
                raise ProtocolError("reflected outbound RPC request id")
            if request_id in self._active_inbound:
                raise ProtocolError("duplicate active RPC request id")
            self._active_inbound.add(request_id)
        try:
            self._inbound.put_nowait((request_id, method, message.get("params")))
        except queue.Full:
            with self._pending_lock:
                self._active_inbound.discard(request_id)
            self._bump("busy_responses")
            self._send_response(
                request_id, ok=False,
                error={"code": "server_busy",
                       "message": "inbound request queue is full"})
            return
        self._bump("requests_received")

    def _worker_loop(self) -> None:
        while True:
            item = self._inbound.get()
            try:
                if item is None:
                    return
                request_id, method, params = item
                try:
                    if self.handler is None:
                        raise RemoteError("method_not_found", "no RPC handler configured")
                    result = self.handler(method, params)
                    _strict_json_dumps({"result": result})
                    self._send_response(request_id, ok=True, result=result)
                except RemoteError as exc:
                    self._send_response(
                        request_id, ok=False,
                        error={"code": exc.remote_code,
                               "message": exc.detail, "data": exc.data})
                except BaseException as exc:
                    self._send_response(
                        request_id, ok=False,
                        error={"code": "handler_error", "message": str(exc)})
                finally:
                    with self._pending_lock:
                        self._active_inbound.discard(request_id)
            finally:
                self._inbound.task_done()

    def _send_response(self, request_id: str, *, ok: bool,
                       result: Any = None,
                       error: Optional[Mapping[str, Any]] = None) -> None:
        if self._closed.is_set() or self._closing.is_set():
            return
        message = {"type": "response", "id": request_id, "ok": ok}
        if ok:
            message["result"] = result
        else:
            message["error"] = dict(error or {
                "code": "handler_error", "message": "request failed"})
        # An over-limit response must not tear down the persistent session:
        # the HTTPS peer route answers an over-limit relay response with a
        # bounded response_too_large body, and the two transports must give
        # the SAME outcome for the SAME relay.  Clamp here rather than let
        # send_json raise MessageTooLarge and _abort() the connection.
        try:
            oversized = len(_strict_json_dumps(message)) > self.max_response_bytes
        except (TypeError, ValueError, ConvoyWebSocketError):
            # Not serializable here either -- fall through and let send_json
            # raise, so the existing abort path handles it exactly as before.
            oversized = False
        if oversized:
            message = {
                "type": "response", "id": request_id, "ok": False,
                "error": {
                    "code": "response_too_large",
                    "message": "peer response exceeded the wire limit"},
            }
        try:
            self.ws.send_json(message, self.monotonic() + 5.0)
            self._bump("responses_sent")
        except BaseException as exc:
            self._terminal_error = exc
            self._abort(exc)

    def close(self, timeout_s: float = 1.0, reason: str = "") -> None:
        timeout_s = _validate_timeout(timeout_s)
        deadline = self.monotonic() + timeout_s
        first = not self._closing.is_set()
        self._closing.set()
        if first and self._started and not self._closed.is_set():
            try:
                self.ws.send_frame(OP_CLOSE, close_payload(1000, reason), deadline)
            except (ConvoyWebSocketError, TypeError, ValueError):
                pass
        remaining = _remaining(deadline, self.monotonic)
        if self._reader is not threading.current_thread() and remaining:
            self._closed.wait(remaining)
        self._abort(ConnectionClosed("session closed locally"))
        threads = [self._reader] + list(self._workers)
        for thread in threads:
            if thread is None or thread is threading.current_thread():
                continue
            remaining = _remaining(deadline, self.monotonic)
            if remaining and remaining > 0:
                thread.join(remaining)

    stop = close

    def _abort(self, error: BaseException) -> None:
        with self._state_lock:
            if self._closed.is_set():
                return
            self._terminal_error = self._terminal_error or error
            self._closing.set()
            self._closed.set()
            try:
                self.ws.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.ws.sock.close()
            except OSError:
                pass
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
                self._active_inbound.clear()
                for waiter in pending:
                    waiter.error = self._terminal_error
                    waiter.event.set()
                    self._pending_slots.release()
            # Wake every worker.  If the bounded queue is full, drain queued
            # work: shutdown must not wait for requests that can no longer be
            # answered on the closed connection.
            while True:
                try:
                    self._inbound.get_nowait()
                    self._inbound.task_done()
                except queue.Empty:
                    break
            for _ in self._workers:
                try:
                    self._inbound.put_nowait(None)
                except queue.Full:
                    break


def establish_client(sock: socket.socket, *, host_header: str,
                     local_host_id: str, expected_peer_host_id: str,
                     generation: int, namespaces: Iterable[str],
                     capability_summary: Mapping[str, Any],
                     handler: Optional[Callable[[str, Any], Any]] = None,
                     path: str = DEFAULT_PATH,
                     timeout_s: float = 5.0, **session_options: Any) -> Tuple[Session, PeerHello]:
    """Upgrade, authenticate hello, and start a client-role Session."""
    deadline = _deadline(timeout_s, time.monotonic)
    try:
        remaining = _remaining(deadline, time.monotonic)
        if not remaining:
            raise WebSocketTimeout("establishment deadline expired")
        client_upgrade(sock, host_header, path, timeout_s=remaining)
        ws = WebSocketConnection(sock, "client")
        local = build_hello(
            local_host_id, expected_peer_host_id, generation, namespaces,
            capability_summary, role="client")
        remaining = _remaining(deadline, time.monotonic)
        if not remaining:
            raise WebSocketTimeout("establishment deadline expired")
        remote = exchange_hello(
            ws, local, expected_peer_host_id=expected_peer_host_id,
            timeout_s=remaining)
        return Session(ws, handler, **session_options).start(), remote
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise


def establish_server(sock: socket.socket, *, local_host_id: str,
                     expected_peer_host_id: str, generation: int,
                     namespaces: Iterable[str],
                     capability_summary: Mapping[str, Any],
                     handler: Optional[Callable[[str, Any], Any]] = None,
                     expected_path: str = DEFAULT_PATH,
                     expected_host: Optional[str] = None,
                     timeout_s: float = 5.0, **session_options: Any) -> Tuple[Session, PeerHello]:
    """Upgrade, authenticate hello, and start a server-role Session."""
    deadline = _deadline(timeout_s, time.monotonic)
    try:
        remaining = _remaining(deadline, time.monotonic)
        if not remaining:
            raise WebSocketTimeout("establishment deadline expired")
        server_upgrade(sock, expected_path, expected_host,
                       timeout_s=remaining)
        ws = WebSocketConnection(sock, "server")
        local = build_hello(
            local_host_id, expected_peer_host_id, generation, namespaces,
            capability_summary, role="server")
        remaining = _remaining(deadline, time.monotonic)
        if not remaining:
            raise WebSocketTimeout("establishment deadline expired")
        remote = exchange_hello(
            ws, local, expected_peer_host_id=expected_peer_host_id,
            timeout_s=remaining)
        return Session(ws, handler, **session_options).start(), remote
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise
