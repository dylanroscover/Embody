"""Convoy's process-local Perform wake datagram.

The LAN never reaches this endpoint.  A TD node registers a random loopback
UDP port and per-process bearer token with its authenticated local HostApp;
the HostApp sends one small datagram immediately before real work.  The node's
ThreadManager listener validates and queues it, and only TD's main thread may
change effective Perform state.

This module is host-side and stdlib-only.  It deliberately owns only packet
construction/sending; the listener/parser lives in ConvoyExt because released
Embody builds cannot import files from ``dev/convoy``.
"""

from __future__ import annotations

import json
import socket


PROTOCOL = 1
PACKET_MAX = 1024
TOKEN_MIN_CHARS = 32
TOKEN_MAX_CHARS = 128
LEASE_MAX_CHARS = 128
TTL_DEFAULT_S = 120
TTL_MAX_S = 600
_URL_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class WakePacketError(ValueError):
    """A local programming/configuration error; no datagram was sent."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _bounded_text(value, name, maximum, allowed=None):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise WakePacketError("malformed_wake_packet",
                              f"{name} is missing or too long")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise WakePacketError("malformed_wake_packet",
                              f"{name} contains control/non-ASCII text")
    if allowed is not None and any(char not in allowed for char in value):
        raise WakePacketError("malformed_wake_packet",
                              f"{name} contains unsupported characters")
    return value


def build_packet(token, action, lease_id, ttl_s=TTL_DEFAULT_S):
    """Return the canonical compact bytes accepted by ConvoyExt."""
    token = _bounded_text(token, "token", TOKEN_MAX_CHARS, _URL_SAFE)
    if len(token) < TOKEN_MIN_CHARS:
        raise WakePacketError("malformed_wake_packet", "token is too short")
    if action not in ("acquire", "touch", "release"):
        raise WakePacketError("malformed_wake_packet", "unknown wake action")
    lease_id = _bounded_text(lease_id, "lease_id", LEASE_MAX_CHARS)
    if (isinstance(ttl_s, bool) or not isinstance(ttl_s, int)
            or ttl_s < 1 or ttl_s > TTL_MAX_S):
        raise WakePacketError("malformed_wake_packet",
                              "ttl_s is outside the supported range")
    packet = json.dumps({
        "v": PROTOCOL,
        "auth": token,
        "action": action,
        "lease_id": lease_id,
        "ttl_s": ttl_s,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(packet) > PACKET_MAX:
        raise WakePacketError("malformed_wake_packet", "packet is too large")
    return packet


def send(port, token, action, lease_id, ttl_s=TTL_DEFAULT_S,
         socket_factory=socket.socket):
    """Send one bounded loopback datagram; return a total result dictionary.

    UDP is intentional: dispatch may call this while holding HostApp's short
    coordination lock.  There is no response wait and therefore no lock/I/O
    inversion with the registration request the wake is meant to trigger.
    Delivery is retried by the durable job drain; each command is idempotent
    and every acquired lease also has a hard TTL.
    """
    if isinstance(port, bool) or not isinstance(port, int) \
            or port < 1 or port > 65535:
        return {"ok": False, "reason": "malformed_wake_endpoint",
                "detail": "wake port must be an integer 1..65535"}
    try:
        packet = build_packet(token, action, lease_id, ttl_s=ttl_s)
    except WakePacketError as exc:
        return {"ok": False, "reason": exc.reason, "detail": exc.detail}
    sock = None
    try:
        sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.25)
        sock.bind(("127.0.0.1", 0))
        sent = sock.sendto(packet, ("127.0.0.1", port))
        if sent != len(packet):
            return {"ok": False, "reason": "wake_send_incomplete",
                    "detail": "the loopback datagram was not fully sent"}
        return {"ok": True, "bytes": sent, "action": action,
                "lease_id": lease_id}
    except (OSError, TypeError, ValueError) as exc:
        # Never include the packet or token.  Exception class + bounded OS
        # message is actionable without turning logs into credential sinks.
        message = str(exc).replace(str(token), "[redacted]")[:160]
        return {"ok": False, "reason": "wake_unreachable",
                "detail": f"{type(exc).__name__}: {message}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
