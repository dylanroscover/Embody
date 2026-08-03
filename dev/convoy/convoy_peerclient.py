"""The pinned mutual-TLS client for talking to a PEER host app (Phase 3
slice 3). Stdlib only; X.509 parsing goes through convoy_hostkeys, never
`cryptography` directly (A-44).

TWO PROTOCOLS, TWO TRUST BOUNDARIES, TWO FILES (the plan's prohibition).
convoy_mcpclient is host -> its OWN Envoy over loopback (the node leg);
this is host -> a PEER host app over the LAN (the peer leg). They do not
share a module because they do not share a trust model: the node leg
trusts a local port the node itself registered, the peer leg trusts a
PINNED CERTIFICATE and nothing else.

THERE IS NO PLAINTEXT CODE PATH. This module opens no plaintext-HTTP
connection and no non-TLS socket of any kind -- a source-level test
asserts it (L-04). Every request rides a mutual-TLS-1.3 connection whose
trust anchor is the single pinned peer certificate; a peer that presents
a different key fails the handshake and is reported as pin_mismatch,
never retried against a fallback that does not exist.

THE PIN IS RECOMPUTED, NEVER TRUSTED FROM A NAME. After the handshake
the server's SPKI fingerprint is recomputed LOCALLY from the certificate
it presented and compared to the expected pin. The trust store already
enforces this at the TLS layer; the recompute is the second defense the
plan requires, and it is what turns an impossible-but-catastrophic
"trusted the wrong key" into a named refusal.

RECONCILIATION, NOT BLIND RETRY (A-46 point 2). A connection REFUSED
before any byte was sent means the request did not arrive -> UNREACHABLE,
retry-safe with the SAME idempotency_key. A failure AFTER the request may
have been written means the operation MAY have run -> None, reconcile by
key, never blind-retry. The two are distinguished exactly as
convoy_mcpclient distinguishes them on the node leg.
"""

import base64
from collections import OrderedDict
import hashlib
import http.client
import io
import json
import math
import os
import re
import socket
import ssl
import tempfile
import threading
import time

import convoy_artifact_http as artifact_http
import convoy_artifacts as artifacts_mod
import convoy_hostkeys as hostkeys
import convoy_identity as identity
import convoy_protocol as protocol


# Connect + enqueue is a fast round trip: /peer/envelope PERSISTS a
# durable job and returns immediately (it does not block on the node),
# so the peer leg never needs the node's full budget. This ceilings the
# per-request wait; the envelope's remaining budget shortens it further.
DEFAULT_PEER_TIMEOUT_S = 15.0
# Retained as documentation of the old scheduling preference. It is NOT
# a floor: no transport timeout may exceed the signed remaining budget.
_MIN_PEER_TIMEOUT_S = 2.0

# Every peer response is bounded independently of request size. A
# manifest, node directory or job status is tiny at Convoy's 2-30 host
# scale; 256 KiB leaves ample headroom while preventing a pinned-but-
# faulty peer from making the controller buffer an unbounded response.
MAX_PEER_RESPONSE_BYTES = 256 * 1024
_RESPONSE_READ_CHUNK = 32 * 1024

# One production HostApp owns one pool.  At the supported 30-host ceiling it
# keeps at most one serialized outbound control connection to each sibling;
# artifact byte streams intentionally use separate one-shot connections.
DEFAULT_POOL_MAX_TARGETS = 64
# The peer server closes a socket after 30 seconds of I/O silence.  Retire it
# proactively so the next mutation is never written onto a server-expired
# keep-alive and misclassified as an ambiguous delivery.
DEFAULT_POOL_IDLE_S = 20.0

# Artifact bodies deliberately do not use PeerConnectionPool.  A 512 MiB
# transfer must not head-of-line block a target's small control requests, and
# the peer listener closes every artifact stream after one request.  The
# operation deadline is cumulative across source staging, capability grants,
# every resume attempt, verification, and local materialization.
DEFAULT_ARTIFACT_TIMEOUT_S = 120.0
MAX_ARTIFACT_TIMEOUT_S = 60.0 * 60.0
DEFAULT_ARTIFACT_ATTEMPTS = 3
MAX_ARTIFACT_ATTEMPTS = 5
DEFAULT_ARTIFACT_MAX_BYTES = artifacts_mod.DEFAULT_MAX_ARTIFACT_BYTES
ARTIFACT_STREAM_CHUNK_BYTES = artifact_http.STREAM_CHUNK_BYTES

_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")

# Convoy ids become one opaque URL-safe segment on the peer nodes route.
MAX_CONVOY_ID_BYTES = identity.MAX_CONVOY_ID_BYTES

ROUTE_ENVELOPE = "/peer/envelope"
ROUTE_MANIFEST = "/peer/manifest"
ROUTE_HEALTH = "/peer/health"
ROUTE_JOBS_PREFIX = "/peer/jobs/"
ROUTE_NODES_PREFIX = "/peer/nodes/"
ROUTE_CONTROLLERS_PREFIX = "/peer/controllers/"
ROUTE_CANCEL = "/peer/jobs/cancel"
ROUTE_ACK = "/peer/jobs/ack"
ROUTE_CONTROLLER_HEARTBEAT = "/peer/controllers/heartbeat"


class _Unreachable:
    """The peer's TLS listener REFUSED the connection (or was not
    there): the request never left, so the operation definitely did NOT
    run. Retry-safe with the same idempotency_key."""
    __slots__ = ()

    def __repr__(self):
        return "PEER_UNREACHABLE"


class _PinMismatch:
    """The peer presented a certificate that does not match the pin --
    either it rotated its Convoy identity, or something on the network is
    impersonating it. The connection is refused; the pin is NEVER
    auto-updated (plan 1.2). Carries the operator-facing message and, when
    the offered key could be recovered, its fingerprint."""

    __slots__ = ("host_id", "address", "expected", "offered", "message")

    def __init__(self, host_id, address, expected, offered, message):
        self.host_id = host_id
        self.address = address
        self.expected = expected
        self.offered = offered
        self.message = message

    def __repr__(self):
        return f"PEER_PIN_MISMATCH({self.host_id})"

    def as_dict(self):
        return {"ok": False, "reason": "pin_mismatch", "host_id": self.host_id,
                "address": self.address, "expected": self.expected,
                "offered": self.offered, "detail": self.message}


UNREACHABLE = _Unreachable()


class PeerSocketError(Exception):
    """A dedicated WSS transport socket could not be authenticated."""


class PeerSocketUnavailable(PeerSocketError):
    pass


class PeerSocketPinMismatch(PeerSocketError):
    def __init__(self, mismatch):
        self.mismatch = mismatch
        super().__init__(mismatch.message)


class PeerTarget:
    """Where a peer is and how to pin it. Everything here is LOCAL
    knowledge (from this host's own admission record), never anything the
    peer asserted on the wire."""

    __slots__ = ("host_id", "address", "port", "pinned_cert_pem",
                 "expected_fingerprint")

    def __init__(self, host_id, address, port, pinned_cert_pem,
                 expected_fingerprint):
        self.host_id = host_id
        self.address = address
        self.port = int(port)
        self.pinned_cert_pem = pinned_cert_pem
        # Canonical lowercase, so a case difference in a stored pin can
        # never read as a mismatch.
        self.expected_fingerprint = (expected_fingerprint or "").strip().lower()


class _ArtifactDeadlineExceeded(Exception):
    pass


class _ArtifactProtocolError(Exception):
    pass


class _ArtifactLocalIOError(Exception):
    pass


class _ArtifactDeadline:
    """One monotonic budget shared by every phase of a transfer."""

    __slots__ = ("_end", "_monotonic")

    def __init__(self, timeout_s, monotonic=None):
        if (isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))):
            raise ValueError("timeout_s must be a finite number")
        timeout_s = float(timeout_s)
        if (not math.isfinite(timeout_s) or timeout_s <= 0.0
                or timeout_s > MAX_ARTIFACT_TIMEOUT_S):
            raise ValueError("timeout_s is outside the artifact limit")
        self._monotonic = monotonic or time.monotonic
        self._end = self._monotonic() + timeout_s

    def remaining(self):
        value = self._end - self._monotonic()
        if not math.isfinite(value) or value <= 0.0:
            raise _ArtifactDeadlineExceeded()
        return value

    def socket_timeout(self):
        # A dead peer gets at most the remaining operation authority.  The
        # ordinary 15-second peer ceiling also keeps a single stalled socket
        # from consuming a generous artifact budget before resume can run.
        return min(self.remaining(), DEFAULT_PEER_TIMEOUT_S)


class _PoolEntry:
    __slots__ = ("key", "lock", "conn", "last_used", "handshakes")

    def __init__(self, key):
        self.key = key
        self.lock = threading.Lock()
        self.conn = None
        self.last_used = 0.0
        self.handshakes = 0


class PeerConnectionPool:
    """Bounded persistent pinned-mTLS control connections for one host.

    HTTP/1.1 permits one in-flight request per connection, so each target entry
    is serialized.  Different peers retain independent locks and run in
    parallel.  Any transport ambiguity discards the connection; the existing
    durable idempotency/reconciliation contract remains authoritative.
    """

    def __init__(self, hostkeys_obj, *, max_targets=DEFAULT_POOL_MAX_TARGETS,
                 idle_s=DEFAULT_POOL_IDLE_S, monotonic=None):
        self.hostkeys = hostkeys_obj
        self.max_targets = max(1, int(max_targets))
        self.idle_s = max(0.1, float(idle_s))
        self._monotonic = monotonic or time.monotonic
        self._guard = threading.Lock()
        self._entries = OrderedDict()
        self._handshakes = 0

    @staticmethod
    def _key(target):
        try:
            cert = target.pinned_cert_pem.encode("utf-8", "strict")
        except (AttributeError, UnicodeEncodeError):
            cert = b""
        return (target.host_id, target.address, int(target.port),
                target.expected_fingerprint,
                hashlib.sha256(cert).hexdigest())

    @staticmethod
    def _close_entry(entry):
        conn = entry.conn
        entry.conn = None
        if conn is not None:
            try:
                conn.close()
            except (OSError, http.client.HTTPException):
                pass

    def _entry(self, target):
        key = self._key(target)
        with self._guard:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry, True
            if len(self._entries) >= self.max_targets:
                evicted = False
                for old_key, old in list(self._entries.items()):
                    if old.lock.acquire(blocking=False):
                        try:
                            self._entries.pop(old_key, None)
                            self._close_entry(old)
                            evicted = True
                        finally:
                            old.lock.release()
                        break
                if not evicted:
                    # Every pooled peer is actively in flight.  Preserve the
                    # hard pool bound with a one-shot entry for this request.
                    return _PoolEntry(key), False
            entry = _PoolEntry(key)
            self._entries[key] = entry
            return entry, True

    def request(self, target, method, path, body, timeout):
        # HTTP/1.1 serializes requests per peer.  Waiting for that peer's
        # connection is part of the caller's authority, not an unbounded
        # prelude to a fresh socket timeout.  Start one monotonic deadline
        # before even consulting the pool and pass only what remains to the
        # transport.  Expiry while queued is definitively pre-send, so it is
        # UNREACHABLE (safe for the caller's normal retry contract).
        if (isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))):
            return UNREACHABLE
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0.0:
            return UNREACHABLE
        deadline = self._monotonic() + timeout
        entry, pooled = self._entry(target)
        acquired = False
        try:
            remaining = deadline - self._monotonic()
            if not math.isfinite(remaining) or remaining <= 0.0:
                return UNREACHABLE
            acquired = entry.lock.acquire(timeout=remaining)
            if not acquired:
                return UNREACHABLE
            remaining = deadline - self._monotonic()
            if not math.isfinite(remaining) or remaining <= 0.0:
                return UNREACHABLE
            return self._request_locked(
                entry, target, method, path, body, remaining)
        finally:
            if acquired:
                entry.lock.release()
            if not pooled:
                self._close_entry(entry)

    def _request_locked(self, entry, target, method, path, body, timeout):
        now = self._monotonic()
        if (entry.conn is not None and entry.last_used
                and now - entry.last_used >= self.idle_s):
            self._close_entry(entry)

        if entry.conn is None:
            try:
                context = build_client_ssl_context(
                    self.hostkeys, target.pinned_cert_pem)
            except (ssl.SSLError, OSError, ValueError):
                return UNREACHABLE
            conn = http.client.HTTPSConnection(
                target.address, target.port, timeout=timeout, context=context)
            try:
                conn.connect()
            except ssl.SSLCertVerificationError as exc:
                conn.close()
                return _pin_mismatch(target, offered=None, cause=exc)
            except (ssl.SSLError, ConnectionRefusedError, socket.timeout,
                    TimeoutError, OSError):
                conn.close()
                return UNREACHABLE
            offered_fp = _presented_fingerprint(conn.sock)
            if offered_fp is None:
                conn.close()
                return None
            if offered_fp != target.expected_fingerprint:
                conn.close()
                return _pin_mismatch(
                    target, offered=offered_fp, cause=None)
            entry.conn = conn
            entry.handshakes += 1
            with self._guard:
                self._handshakes += 1

        conn = entry.conn
        try:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(body))
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = _read_bounded_response(response)
            must_close = bool(response.will_close)
        except (OSError, http.client.HTTPException, _ResponseLimitError):
            self._close_entry(entry)
            return None
        finally:
            entry.last_used = self._monotonic()
        if must_close:
            self._close_entry(entry)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._close_entry(entry)
            return None

    def close(self):
        with self._guard:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            with entry.lock:
                self._close_entry(entry)

    def stats(self):
        with self._guard:
            return {"targets": len(self._entries),
                    "handshakes": self._handshakes}


def build_client_ssl_context(hostkeys_obj, pinned_cert_pem):
    """A mutual-TLS-1.3 client context that trusts EXACTLY the pinned peer
    certificate -- no system CAs, no hostname checking (pinning replaces
    PKI names)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    # Pinning, not PKI: the cert carries a fingerprint as its name purely
    # for humans, and that name must never be trusted (convoy_hostkeys
    # _issue_certificate). Identity is the recomputed SPKI, below.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=pinned_cert_pem)
    # OUR certificate, for the server's mutual-auth requirement.
    context.load_cert_chain(hostkeys_obj.cert_path, hostkeys_obj.key_path)
    return context


def open_authenticated_socket(target, hostkeys_obj,
                              timeout=DEFAULT_PEER_TIMEOUT_S):
    """Open one raw, nonblocking, mutually authenticated TLS socket.

    This is the WSS dial seam.  It sends no HTTP/WebSocket bytes: callers may
    safely try another endpoint after ``PeerSocketUnavailable``.  Once this
    returns, both TLS trust validation and the independent SPKI recomputation
    have matched the locally pinned peer identity.
    """
    if (isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or float(timeout) <= 0):
        raise ValueError("timeout must be a finite positive number")
    timeout = float(timeout)
    try:
        context = build_client_ssl_context(
            hostkeys_obj, target.pinned_cert_pem)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise PeerSocketUnavailable(str(exc)) from exc

    raw = None
    tls = None
    try:
        raw = socket.create_connection(
            (target.address, int(target.port)), timeout=timeout)
        raw.settimeout(timeout)
        try:
            tls = context.wrap_socket(raw, server_hostname=None)
        except ssl.SSLCertVerificationError as exc:
            mismatch = _pin_mismatch(target, offered=None, cause=exc)
            raise PeerSocketPinMismatch(mismatch) from exc
        except (ssl.SSLError, socket.timeout, TimeoutError, OSError) as exc:
            raise PeerSocketUnavailable(str(exc)) from exc
        raw = None  # ownership moved to SSLSocket
        offered = _presented_fingerprint(tls)
        if offered is None:
            raise PeerSocketUnavailable(
                "peer certificate was unavailable after TLS handshake")
        if offered != target.expected_fingerprint:
            mismatch = _pin_mismatch(target, offered=offered, cause=None)
            raise PeerSocketPinMismatch(mismatch)
        # convoy_ws owns all subsequent readiness/deadline handling.
        tls.setblocking(False)
        return tls
    except (PeerSocketError, ValueError):
        if tls is not None:
            _close_connection(tls)
        if raw is not None:
            _close_connection(raw)
        raise
    except (ConnectionRefusedError, socket.timeout, TimeoutError,
            OSError) as exc:
        if tls is not None:
            _close_connection(tls)
        if raw is not None:
            _close_connection(raw)
        raise PeerSocketUnavailable(str(exc)) from exc


def peer_timeout_for(envelope, ceiling=DEFAULT_PEER_TIMEOUT_S, now=None):
    """The per-request timeout for sending `envelope`, NESTED under its
    deadline rather than stacked on top of it (A-46 point 3): never
    longer than the envelope's remaining budget, and ceilinged so a
    generous deadline does not become a generous socket timeout. Returns
    0.0 for an already-expired envelope, which the caller short-circuits
    without a network round trip."""
    remaining = protocol.remaining_budget(envelope, now=now)
    if remaining <= 0.0:
        return 0.0
    try:
        ceiling = float(ceiling)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(ceiling) or ceiling <= 0.0:
        return 0.0
    return min(remaining, ceiling)


def send_envelope(target, hostkeys_obj, envelope, timeout=None, now=None,
                  pool=None):
    """POST a signed envelope to a peer's /peer/envelope. Returns the
    peer's structured JSON answer (a dict), UNREACHABLE (did not arrive),
    a _PinMismatch, or None (transport failed after the request may have
    been written -> reconcile, never blind-retry). Never raises."""
    timeout = peer_timeout_for(
        envelope,
        ceiling=DEFAULT_PEER_TIMEOUT_S if timeout is None else timeout,
        now=now)
    if timeout == 0.0:
        # Locally expired (or unusable timeout): the server would refuse
        # deadline_exceeded anyway; skip the round trip. Most importantly,
        # an explicit caller timeout cannot bypass the signed budget.
        return {"ok": False, "reason": "deadline_exceeded",
                "detail": "the envelope expired before it was sent"}
    # The wire contract is the SAME as the loopback /envelope route --
    # {"envelope": <envelope>} -- so the peer leg and the local leg feed
    # the one submit_envelope authority rather than two.
    body = json.dumps({"envelope": envelope}).encode("utf-8")
    return _call_request(target, hostkeys_obj, "POST", ROUTE_ENVELOPE, body,
                         timeout, pool)


def get_peer_job(target, hostkeys_obj, delivery_id, since=None,
                 timeout=DEFAULT_PEER_TIMEOUT_S, pool=None):
    """GET a delivery record's status from the peer that owns it, with an
    optional `since` cursor (A-46 point 1). Same outcome contract as
    send_envelope."""
    path = ROUTE_JOBS_PREFIX + _quote_segment(delivery_id)
    if since is not None:
        path += f"?since={float(since)}"
    return _call_request(
        target, hostkeys_obj, "GET", path, None, timeout, pool)


def get_peer_manifest(target, hostkeys_obj, timeout=DEFAULT_PEER_TIMEOUT_S,
                      pool=None):
    """GET a peer's remote-exposed operation manifest (refuse-before-send
    for a controller)."""
    return _call_request(target, hostkeys_obj, "GET", ROUTE_MANIFEST, None,
                         timeout, pool)


def get_peer_health(target, hostkeys_obj, timeout=DEFAULT_PEER_TIMEOUT_S,
                    pool=None):
    """GET a peer's liveness + host_id over the pinned channel."""
    return _call_request(
        target, hostkeys_obj, "GET", ROUTE_HEALTH, None, timeout, pool)


def get_peer_nodes(target, hostkeys_obj, convoy_id,
                   timeout=DEFAULT_PEER_TIMEOUT_S, pool=None):
    """GET the peer-safe node directory for one admitted Convoy.

    The namespace is encoded as one canonical base64url path segment;
    neither slashes nor query text can escape into the route. The server
    independently decodes, bounds and authorizes the same namespace.
    """
    segment = _encode_convoy_segment(convoy_id)
    if segment is None:
        return {"ok": False, "reason": "malformed",
                "detail": "convoy_id must be non-empty bounded text"}
    return _call_request(target, hostkeys_obj, "GET",
                         ROUTE_NODES_PREFIX + segment, None, timeout, pool)


def get_peer_controllers(target, hostkeys_obj, convoy_id,
                         timeout=DEFAULT_PEER_TIMEOUT_S, pool=None):
    """GET the peer's non-waking controller/lease view for one Convoy."""
    segment = _encode_convoy_segment(convoy_id)
    if segment is None:
        return {"ok": False, "reason": "malformed",
                "detail": "convoy_id must be non-empty bounded text"}
    return _call_request(
        target, hostkeys_obj, "GET", ROUTE_CONTROLLERS_PREFIX + segment,
        None, timeout, pool)


def cancel_peer_job(target, hostkeys_obj, convoy_id, delivery_id,
                    timeout=DEFAULT_PEER_TIMEOUT_S, pool=None):
    """Ask the owning peer to cancel one delivery created by this host.

    The target independently binds the TLS-authenticated origin, namespace,
    and durable job. A failed response never proves cancellation; callers
    reconcile through ``get_peer_job`` using the unchanged delivery id.
    """
    segment = _encode_convoy_segment(convoy_id)
    clean_id = _quote_segment(delivery_id)
    if segment is None or not clean_id or clean_id != str(delivery_id or ""):
        return {"ok": False, "reason": "malformed",
                "detail": "convoy_id/delivery_id must be canonical"}
    body = json.dumps({"convoy_id": convoy_id,
                       "delivery_id": clean_id},
                      allow_nan=False).encode("utf-8")
    return _call_request(target, hostkeys_obj, "POST", ROUTE_CANCEL, body,
                         timeout, pool)


def acknowledge_peer_job(target, hostkeys_obj, convoy_id, delivery_id,
                         timeout=DEFAULT_PEER_TIMEOUT_S, pool=None):
    """Explicitly acknowledge one observed terminal peer outcome.

    This is never called as a side effect of polling: acknowledgement is a
    separate durable action so a lost response cannot silently release the
    target's retained terminal evidence or result-artifact protection.
    """
    segment = _encode_convoy_segment(convoy_id)
    clean_id = _quote_segment(delivery_id)
    if segment is None or not clean_id or clean_id != str(delivery_id or ""):
        return {"ok": False, "reason": "malformed",
                "detail": "convoy_id/delivery_id must be canonical"}
    body = json.dumps({"convoy_id": convoy_id,
                       "delivery_id": clean_id},
                      allow_nan=False).encode("utf-8")
    return _call_request(target, hostkeys_obj, "POST", ROUTE_ACK, body,
                         timeout, pool)


def heartbeat_peer_controller(
        target, hostkeys_obj, convoy_id, controller_id, selected_node_id=None,
        *, clear_selected=False, label="", timeout=DEFAULT_PEER_TIMEOUT_S,
        pool=None):
    """Refresh one controller at the host that owns its selected node."""
    segment = _encode_convoy_segment(convoy_id)
    if (segment is None or not isinstance(controller_id, str)
            or not controller_id or len(controller_id) > 256):
        return {"ok": False, "reason": "malformed",
                "detail": "convoy_id/controller_id must be canonical"}
    body_obj = {"convoy_id": convoy_id, "controller_id": controller_id,
                "clear_selected": bool(clear_selected)}
    if selected_node_id is not None:
        body_obj["selected_node_id"] = selected_node_id
    if label:
        body_obj["label"] = label
    try:
        body = json.dumps(body_obj, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return {"ok": False, "reason": "malformed"}
    return _call_request(
        target, hostkeys_obj, "POST", ROUTE_CONTROLLER_HEARTBEAT, body,
        timeout, pool)


# -- authenticated artifact transfer ---------------------------------

def _artifact_refusal(reason, detail=""):
    result = {"ok": False, "reason": reason}
    if detail:
        result["detail"] = detail
    return result


def _deadline_refusal():
    return _artifact_refusal(
        "deadline_exceeded", "artifact transfer deadline was exhausted")


def _artifact_transport_failure(deadline, otherwise=None):
    try:
        deadline.remaining()
    except _ArtifactDeadlineExceeded:
        return _deadline_refusal()
    return otherwise


def _artifact_attempt_count(value):
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 1 or value > MAX_ARTIFACT_ATTEMPTS):
        raise ValueError("max_attempts is outside the artifact limit")
    return value


def _artifact_max_bytes(value):
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0 or value > DEFAULT_ARTIFACT_MAX_BYTES):
        raise ValueError("max_bytes is outside the artifact limit")
    return value


def _artifact_text(value, name, *, optional=False, filename=False):
    if value is None and optional:
        return ""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty text")
    try:
        encoded = value.encode("utf-8", "strict")
        # Python's stdlib HTTP client serializes request header values as
        # latin-1. Refuse rather than silently changing a security scope.
        value.encode("latin-1", "strict")
    except (UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{name} is not an HTTP header value") from exc
    limit = 255 if filename else artifact_http.MAX_HEADER_TEXT_BYTES
    if (len(encoded) > limit
            or any(byte < 32 or byte == 127 for byte in encoded)):
        raise ValueError(f"{name} is outside the artifact limit")
    if filename and (value in (".", "..") or "/" in value
                     or "\\" in value or ":" in value):
        raise ValueError("filename_hint must be one plain basename")
    return value


def _artifact_namespace(convoy_id):
    try:
        segment = artifact_http.encode_convoy_segment(convoy_id)
        normalized = artifact_http.decode_convoy_segment(segment)
    except artifact_http.ArtifactHTTPError as exc:
        raise ValueError(exc.detail or exc.reason) from exc
    return normalized, segment


def _artifact_media_type(value):
    value = _artifact_text(value or "application/octet-stream", "mime_type")
    if "/" not in value or any(char.isspace() for char in value):
        raise ValueError("mime_type must be a media type")
    return value.lower()


def _artifact_reference_fields(reference, convoy_id, max_bytes):
    if not isinstance(reference, dict):
        raise ValueError("artifact reference must be an object")
    artifact_id = reference.get("artifact_id")
    digest = reference.get("sha256")
    size = reference.get("size")
    if (not isinstance(artifact_id, str)
            or not _ARTIFACT_ID_RE.fullmatch(artifact_id)):
        raise ValueError("artifact reference has a malformed artifact_id")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ValueError("artifact reference has a malformed SHA-256")
    if artifact_id != "art_" + digest:
        raise ValueError("artifact_id does not match artifact SHA-256")
    if (isinstance(size, bool) or not isinstance(size, int)
            or size < 0 or size > max_bytes):
        raise ValueError("artifact size is outside the transfer limit")
    if reference.get("convoy_id") != convoy_id:
        raise ValueError("artifact reference belongs to another Convoy")
    mime_type = _artifact_media_type(reference.get("mime_type"))
    filename = _artifact_text(
        reference.get("filename_hint"), "filename_hint", optional=True,
        filename=True)
    owner = {}
    for name in ("job_id", "controller_id", "host_id", "node_id"):
        if name in reference:
            owner[name] = _artifact_text(reference[name], name)
    return {
        "artifact_id": artifact_id, "sha256": digest, "size": size,
        "mime_type": mime_type, "filename_hint": filename or None,
        "owner": owner,
    }


def _controller_id_for_download(controller_id, owner):
    """Accept either the caller-local id or its returned peer namespace.

    HostApp applies ``peer:<authenticated-host>:`` at the trust boundary.
    Artifact references correctly expose that persisted attribution.  A bridge
    naturally forwarding the reference's controller field must not cause the
    prefix to be applied twice, so strip it only when the reference's exact
    owner host and controller prove which namespace it is.
    """
    owner_host = owner.get("host_id")
    owner_controller = owner.get("controller_id")
    if (controller_id == owner_controller and isinstance(owner_host, str)):
        prefix = "peer:" + owner_host.strip().lower() + ":"
        if controller_id.startswith(prefix) and len(controller_id) > len(prefix):
            return controller_id[len(prefix):]
    return controller_id


def _artifact_path(segment, artifact_id=None, capability=False):
    path = artifact_http.PEER_ROUTE_PREFIX + segment
    if artifact_id is not None:
        path += "/" + artifact_id
    if capability:
        path += "/capability"
    return path


def _close_connection(conn):
    if conn is not None:
        try:
            conn.close()
        except (OSError, http.client.HTTPException):
            pass


def _artifact_connect(target, hostkeys_obj, deadline):
    """Open and independently verify one dedicated artifact mTLS socket."""
    try:
        timeout = deadline.socket_timeout()
    except _ArtifactDeadlineExceeded:
        return None, _deadline_refusal()
    try:
        context = build_client_ssl_context(
            hostkeys_obj, target.pinned_cert_pem)
    except (ssl.SSLError, OSError, ValueError):
        return None, UNREACHABLE
    conn = http.client.HTTPSConnection(
        target.address, target.port, timeout=timeout, context=context)
    try:
        conn.connect()
    except ssl.SSLCertVerificationError as exc:
        _close_connection(conn)
        return None, _pin_mismatch(target, offered=None, cause=exc)
    except (ssl.SSLError, ConnectionRefusedError, socket.timeout,
            TimeoutError, OSError):
        _close_connection(conn)
        try:
            deadline.remaining()
        except _ArtifactDeadlineExceeded:
            return None, _deadline_refusal()
        return None, UNREACHABLE
    offered = _presented_fingerprint(conn.sock)
    if offered is None:
        _close_connection(conn)
        return None, None
    if offered != target.expected_fingerprint:
        _close_connection(conn)
        return None, _pin_mismatch(target, offered=offered, cause=None)
    return conn, None


def _set_artifact_socket_timeout(conn, deadline):
    timeout = deadline.socket_timeout()
    conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)


def _response_header(response, name, *, required=False):
    values = [value for key, value in response.getheaders()
              if key.lower() == name.lower()]
    if len(values) > 1:
        raise _ArtifactProtocolError("duplicate " + name)
    if not values:
        if required:
            raise _ArtifactProtocolError("missing " + name)
        return None
    return values[0]


def _read_artifact_json_response(conn, response, deadline):
    """Read one small JSON response under the operation's absolute budget."""
    declared_text = _response_header(response, "Content-Length")
    declared = None
    if declared_text is not None:
        try:
            declared = int(declared_text)
        except (TypeError, ValueError) as exc:
            raise _ArtifactProtocolError("invalid JSON Content-Length") from exc
        if declared < 0 or declared > MAX_PEER_RESPONSE_BYTES:
            raise _ArtifactProtocolError("artifact JSON response is too large")
    output = bytearray()
    while True:
        allowance = MAX_PEER_RESPONSE_BYTES + 1 - len(output)
        if allowance <= 0:
            raise _ArtifactProtocolError("artifact JSON response is too large")
        _set_artifact_socket_timeout(conn, deadline)
        block = response.read(min(_RESPONSE_READ_CHUNK, allowance))
        if not block:
            break
        output.extend(block)
        if len(output) > MAX_PEER_RESPONSE_BYTES:
            raise _ArtifactProtocolError("artifact JSON response is too large")
    if declared is not None and len(output) != declared:
        raise _ArtifactProtocolError("artifact JSON response was truncated")
    try:
        payload = json.loads(bytes(output).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _ArtifactProtocolError("artifact response is not JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise _ArtifactProtocolError("artifact response is malformed")
    if 200 <= response.status < 300:
        if payload["ok"] is not True:
            raise _ArtifactProtocolError("successful response refused itself")
    elif payload["ok"] is not False:
        raise _ArtifactProtocolError("error response claimed success")
    return payload


def _artifact_json_request(target, hostkeys_obj, method, path, body,
                           deadline):
    conn, failure = _artifact_connect(target, hostkeys_obj, deadline)
    if conn is None:
        return failure
    try:
        _set_artifact_socket_timeout(conn, deadline)
        headers = {"Accept": "application/json", "Connection": "close"}
        if body is not None:
            headers.update({"Content-Type": "application/json",
                            "Content-Length": str(len(body))})
        conn.request(method, path, body=body, headers=headers)
        _set_artifact_socket_timeout(conn, deadline)
        response = conn.getresponse()
        return _read_artifact_json_response(conn, response, deadline)
    except _ArtifactDeadlineExceeded:
        return _deadline_refusal()
    except (OSError, http.client.HTTPException, _ArtifactProtocolError):
        # The request may have reached the peer. Capability issuance is safe
        # to repeat, but the caller must make that decision within its bounded
        # attempt loop rather than mistaking this for UNREACHABLE.
        return _artifact_transport_failure(deadline)
    finally:
        _close_connection(conn)


def _request_peer_artifact_capability(
        target, hostkeys_obj, segment, artifact_id, node_id, controller_id,
        deadline, ttl_s=None, job_id=None):
    payload = {"node_id": node_id, "controller_id": controller_id}
    if job_id:
        payload["job_id"] = job_id
    if ttl_s is not None:
        if (isinstance(ttl_s, bool)
                or not isinstance(ttl_s, (int, float))
                or not math.isfinite(float(ttl_s)) or float(ttl_s) <= 0.0):
            return _artifact_refusal(
                "artifact_invalid", "ttl_s must be a positive finite number")
        payload["ttl_s"] = float(ttl_s)
    try:
        body = json.dumps(
            payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return _artifact_refusal("artifact_invalid", "invalid capability scope")
    return _artifact_json_request(
        target, hostkeys_obj, "POST",
        _artifact_path(segment, artifact_id, capability=True), body, deadline)


def request_peer_artifact_capability(
        target, hostkeys_obj, convoy_id, artifact_id, node_id, controller_id,
        *, job_id=None, timeout_s=DEFAULT_ARTIFACT_TIMEOUT_S, ttl_s=None,
        monotonic=None):
    """Request a one-shot peer/node/controller-bound download capability.

    Returns the peer JSON response, ``UNREACHABLE``, ``_PinMismatch``, or
    ``None`` using the same transport outcome contract as control requests.
    The token is never placed in a URL and callers should keep it in memory.
    """
    try:
        _normalized, segment = _artifact_namespace(convoy_id)
        if (not isinstance(artifact_id, str)
                or not _ARTIFACT_ID_RE.fullmatch(artifact_id)):
            raise ValueError("malformed artifact_id")
        node_id = _artifact_text(node_id, "node_id")
        controller_id = _artifact_text(controller_id, "controller_id")
        job_id = _artifact_text(job_id, "job_id", optional=True) or None
        deadline = _ArtifactDeadline(timeout_s, monotonic=monotonic)
    except ValueError as exc:
        return _artifact_refusal("artifact_invalid", str(exc))
    return _request_peer_artifact_capability(
        target, hostkeys_obj, segment, artifact_id, node_id, controller_id,
        deadline, ttl_s=ttl_s, job_id=job_id)


def _artifact_error_response(conn, response, deadline):
    try:
        return _read_artifact_json_response(conn, response, deadline)
    except _ArtifactDeadlineExceeded:
        return _deadline_refusal()
    except (OSError, http.client.HTTPException, _ArtifactProtocolError):
        return None


def _download_artifact_range(
        target, hostkeys_obj, path, partial_path, offset, metadata, node_id,
        controller_id, capability, deadline):
    """Append exactly one authenticated suffix to ``partial_path``."""
    conn, failure = _artifact_connect(target, hostkeys_obj, deadline)
    if conn is None:
        return failure
    expected = metadata["size"] - offset
    headers = {
        "Accept": "application/octet-stream",
        "Connection": "close",
        artifact_http.HEADER_NODE_ID: node_id,
        artifact_http.HEADER_CONTROLLER_ID: controller_id,
        artifact_http.HEADER_CAPABILITY: capability,
    }
    if offset:
        headers["Range"] = "bytes=%d-" % offset
    try:
        _set_artifact_socket_timeout(conn, deadline)
        conn.request("GET", path, headers=headers)
        _set_artifact_socket_timeout(conn, deadline)
        response = conn.getresponse()
        wanted_status = 206 if offset else 200
        if response.status != wanted_status:
            return _artifact_error_response(conn, response, deadline)

        transfer_encoding = _response_header(response, "Transfer-Encoding")
        if transfer_encoding:
            raise _ArtifactProtocolError(
                "artifact streams require fixed Content-Length")
        length_text = _response_header(
            response, "Content-Length", required=True)
        try:
            response_size = int(length_text)
        except (TypeError, ValueError) as exc:
            raise _ArtifactProtocolError(
                "invalid artifact Content-Length") from exc
        if response_size != expected:
            raise _ArtifactProtocolError(
                "artifact response length does not match the reference")
        if _response_header(
                response, artifact_http.HEADER_SHA256, required=True
                ) != metadata["sha256"]:
            raise _ArtifactProtocolError(
                "artifact response hash does not match the reference")
        if _response_header(
                response, "X-Convoy-Artifact-ID", required=True
                ) != metadata["artifact_id"]:
            raise _ArtifactProtocolError(
                "artifact response id does not match the reference")
        content_range = _response_header(response, "Content-Range")
        if offset:
            wanted_range = "bytes %d-%d/%d" % (
                offset, metadata["size"] - 1, metadata["size"])
            if content_range != wanted_range:
                raise _ArtifactProtocolError(
                    "artifact resume range does not match the reference")
        elif content_range is not None:
            raise _ArtifactProtocolError(
                "full artifact response unexpectedly included a range")

        output = None
        try:
            output = open(partial_path, "r+b", buffering=0)
            output.seek(0, os.SEEK_END)
            if output.tell() != offset:
                output.close()
                output = None
                raise _ArtifactLocalIOError()
        except OSError as exc:
            if output is not None:
                output.close()
            raise _ArtifactLocalIOError() from exc
        try:
            received = 0
            while received < expected:
                _set_artifact_socket_timeout(conn, deadline)
                try:
                    block = response.read(min(
                        ARTIFACT_STREAM_CHUNK_BYTES, expected - received))
                except (OSError, http.client.HTTPException) as exc:
                    raise ConnectionError() from exc
                if not block:
                    raise ConnectionError()
                view = memoryview(block)
                while view:
                    deadline.remaining()
                    try:
                        written = output.write(view)
                    except OSError as exc:
                        raise _ArtifactLocalIOError() from exc
                    if not isinstance(written, int) or written <= 0:
                        raise _ArtifactLocalIOError()
                    view = view[written:]
                received += len(block)
            try:
                os.fsync(output.fileno())
            except OSError as exc:
                raise _ArtifactLocalIOError() from exc
        finally:
            output.close()
        return {"ok": True, "received": expected}
    except _ArtifactDeadlineExceeded:
        return _deadline_refusal()
    except _ArtifactProtocolError as exc:
        return _artifact_refusal("artifact_corrupt", str(exc))
    except _ArtifactLocalIOError:
        return _artifact_refusal(
            "artifact_local_io", "local temporary storage refused the transfer")
    except (ConnectionError, OSError, http.client.HTTPException):
        # A fresh one-shot capability plus a Range beginning at the actual
        # partial-file size is the only retry path. No upload-style blind
        # replay and no reuse of a consumed bearer token occurs here.
        return _artifact_transport_failure(deadline)
    finally:
        _close_connection(conn)


def _make_artifact_temp(temp_dir):
    if temp_dir is not None:
        try:
            temp_dir = os.path.abspath(os.fspath(temp_dir))
        except (TypeError, ValueError, OSError) as exc:
            raise ValueError("temp_dir must be a directory") from exc
        if not os.path.isdir(temp_dir) or os.path.islink(temp_dir):
            raise ValueError("temp_dir must be a non-symlink directory")
    work = partial = ""
    try:
        work = tempfile.mkdtemp(
            prefix=".convoy-peer-artifact-", dir=temp_dir)
        try:
            os.chmod(work, 0o700)
        except OSError:
            if os.name != "nt":
                raise
        partial = os.path.join(work, "transfer.part")
        with open(partial, "xb"):
            pass
        try:
            os.chmod(partial, 0o600)
        except OSError:
            if os.name != "nt":
                raise
        return work, partial
    except OSError as exc:
        _cleanup_artifact_temp(work, partial)
        raise _ArtifactLocalIOError() from exc


def _cleanup_artifact_temp(work, partial):
    # Exact, private paths only. Never recurse into caller-provided storage.
    if partial:
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    if work:
        try:
            os.rmdir(work)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _hash_artifact_partial(path, expected_size, deadline):
    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as source:
            while True:
                deadline.remaining()
                block = source.read(ARTIFACT_STREAM_CHUNK_BYTES)
                if not block:
                    break
                total += len(block)
                if total > expected_size:
                    raise _ArtifactProtocolError(
                        "artifact response exceeded its declared size")
                digest.update(block)
    except OSError as exc:
        raise _ArtifactLocalIOError() from exc
    return total, digest.hexdigest()


class _DeadlineReader:
    """Keep local materialization inside the same network transfer budget."""

    __slots__ = ("_stream", "_deadline")

    def __init__(self, stream, deadline):
        self._stream = stream
        self._deadline = deadline

    def read(self, size=-1):
        self._deadline.remaining()
        result = self._stream.read(size)
        self._deadline.remaining()
        return result


def download_peer_artifact(
        target, hostkeys_obj, local_store, convoy_id, node_id, controller_id,
        reference, *, timeout_s=DEFAULT_ARTIFACT_TIMEOUT_S,
        max_attempts=DEFAULT_ARTIFACT_ATTEMPTS, max_bytes=None,
        temp_dir=None, monotonic=None, protection_id=None,
        protection_kind=None):
    """Fetch, resume, verify, and atomically cache one peer artifact.

    A fresh one-shot capability is minted for every Range attempt. Partial
    bytes exist only in a private temporary directory and are removed on every
    return path. The complete size and SHA-256 are checked before calling the
    local ``ArtifactStore.put_stream`` atomic materializer.

    Success is ``{"ok": True, "artifact": <local reference>, ...}``.
    Transport failures use the normal peer-client sentinels. Structured peer,
    validation, deadline, corruption, quota, and local-I/O failures are dicts.
    """
    work = partial = ""
    try:
        normalized, segment = _artifact_namespace(convoy_id)
        node_id = _artifact_text(node_id, "node_id")
        controller_id = _artifact_text(controller_id, "controller_id")
        if not isinstance(local_store, artifacts_mod.ArtifactStore):
            raise ValueError("local_store must be an ArtifactStore")
        attempts_limit = _artifact_attempt_count(max_attempts)
        configured_max = getattr(local_store, "max_artifact_bytes", None)
        if (isinstance(configured_max, bool)
                or not isinstance(configured_max, int) or configured_max < 0):
            raise ValueError("local store has an invalid artifact limit")
        configured_max = min(configured_max, DEFAULT_ARTIFACT_MAX_BYTES)
        if max_bytes is None:
            max_bytes = configured_max
        else:
            max_bytes = min(_artifact_max_bytes(max_bytes), configured_max)
        metadata = _artifact_reference_fields(reference, normalized, max_bytes)
        # Callers may pass their original controller id or forward the exact
        # peer-namespaced attribution returned in the artifact reference.
        controller_id = _controller_id_for_download(
            controller_id, metadata["owner"])
        deadline = _ArtifactDeadline(timeout_s, monotonic=monotonic)
        work, partial = _make_artifact_temp(temp_dir)

        last_failure = UNREACHABLE
        attempts = 0
        resumed = False
        complete = False
        while attempts < attempts_limit:
            attempts += 1
            try:
                deadline.remaining()
            except _ArtifactDeadlineExceeded:
                return _deadline_refusal()
            try:
                offset = os.path.getsize(partial)
            except OSError:
                return _artifact_refusal(
                    "artifact_local_io", "local partial file became unavailable")
            if offset > metadata["size"]:
                return _artifact_refusal(
                    "artifact_corrupt", "partial artifact exceeded expected size")
            if offset == metadata["size"] and complete:
                break
            grant = _request_peer_artifact_capability(
                target, hostkeys_obj, segment, metadata["artifact_id"],
                node_id, controller_id, deadline,
                job_id=metadata["owner"].get("job_id"))
            if isinstance(grant, _PinMismatch):
                return grant
            if grant is UNREACHABLE or grant is None:
                last_failure = grant
                continue
            if not isinstance(grant, dict) or grant.get("ok") is not True:
                return grant
            capability_record = grant.get("capability")
            capability = (capability_record.get("token")
                          if isinstance(capability_record, dict) else None)
            if (not isinstance(capability, str)
                    or not _CAPABILITY_RE.fullmatch(capability)
                    or capability_record.get("artifact_id")
                    != metadata["artifact_id"]
                    or capability_record.get("convoy_id") != normalized):
                return _artifact_refusal(
                    "artifact_corrupt", "peer returned an invalid capability")

            outcome = _download_artifact_range(
                target, hostkeys_obj,
                _artifact_path(segment, metadata["artifact_id"]), partial,
                offset, metadata, node_id, controller_id, capability, deadline)
            if isinstance(outcome, _PinMismatch):
                return outcome
            if outcome is UNREACHABLE or outcome is None:
                last_failure = outcome
                try:
                    resumed = resumed or os.path.getsize(partial) > 0
                except OSError:
                    return _artifact_refusal(
                        "artifact_local_io", "local partial file became unavailable")
                continue
            if not isinstance(outcome, dict) or outcome.get("ok") is not True:
                return outcome
            resumed = resumed or offset > 0
            try:
                complete = os.path.getsize(partial) == metadata["size"]
            except OSError:
                return _artifact_refusal(
                    "artifact_local_io", "local partial file became unavailable")
            if complete:
                break

        try:
            size = os.path.getsize(partial)
        except OSError:
            return _artifact_refusal(
                "artifact_local_io", "local partial file became unavailable")
        if size != metadata["size"]:
            return last_failure
        try:
            actual_size, actual_digest = _hash_artifact_partial(
                partial, metadata["size"], deadline)
        except _ArtifactDeadlineExceeded:
            return _deadline_refusal()
        except _ArtifactProtocolError as exc:
            return _artifact_refusal("artifact_corrupt", str(exc))
        except _ArtifactLocalIOError:
            return _artifact_refusal(
                "artifact_local_io", "local temporary artifact could not be read")
        if (actual_size != metadata["size"]
                or actual_digest != metadata["sha256"]):
            return _artifact_refusal(
                "artifact_corrupt", "downloaded artifact failed size/hash verification")

        try:
            with open(partial, "rb") as source:
                local_reference = local_store.put_stream(
                    normalized, _DeadlineReader(source, deadline),
                    expected_size=metadata["size"],
                    expected_sha256=metadata["sha256"],
                    mime_type=metadata["mime_type"],
                    filename_hint=metadata["filename_hint"],
                    owner=metadata["owner"],
                    protection_id=protection_id,
                    protection_kind=protection_kind)
        except _ArtifactDeadlineExceeded:
            return _deadline_refusal()
        except artifacts_mod.ArtifactError as exc:
            return _artifact_refusal(exc.reason, exc.detail)
        except OSError:
            return _artifact_refusal(
                "artifact_local_io", "local artifact cache refused materialization")
        return {
            "ok": True, "artifact": local_reference,
            "transfer": {"attempts": attempts, "resumed": resumed,
                         "bytes": metadata["size"]},
        }
    except ValueError as exc:
        return _artifact_refusal("artifact_invalid", str(exc))
    except _ArtifactLocalIOError:
        return _artifact_refusal(
            "artifact_local_io", "local temporary storage is unavailable")
    finally:
        _cleanup_artifact_temp(work, partial)


def _stage_upload_source(stream, partial, metadata, deadline):
    if isinstance(stream, (bytes, bytearray, memoryview)):
        stream = io.BytesIO(bytes(stream))
    if not hasattr(stream, "read"):
        raise ValueError("stream must provide read()")
    digest = hashlib.sha256()
    total = 0
    try:
        with open(partial, "wb", buffering=0) as output:
            while total < metadata["size"]:
                deadline.remaining()
                amount = min(
                    ARTIFACT_STREAM_CHUNK_BYTES, metadata["size"] - total)
                block = stream.read(amount)
                deadline.remaining()
                if not isinstance(block, (bytes, bytearray, memoryview)):
                    raise ValueError("stream.read() must return bytes")
                block = bytes(block)
                if len(block) > amount:
                    raise ValueError("stream exceeded requested read bound")
                if not block:
                    raise ValueError("stream ended before expected_size")
                view = memoryview(block)
                while view:
                    deadline.remaining()
                    written = output.write(view)
                    if not isinstance(written, int) or written <= 0:
                        raise _ArtifactLocalIOError()
                    view = view[written:]
                digest.update(block)
                total += len(block)
            extra = stream.read(1)
            deadline.remaining()
            if not isinstance(extra, (bytes, bytearray, memoryview)):
                raise ValueError("stream.read() must return bytes")
            if extra:
                raise ValueError("stream exceeds expected_size")
            os.fsync(output.fileno())
    except OSError as exc:
        raise _ArtifactLocalIOError() from exc
    if digest.hexdigest() != metadata["sha256"]:
        raise _ArtifactProtocolError("upload source failed SHA-256 verification")


def _upload_artifact_once(
        target, hostkeys_obj, path, staged_path, metadata, node_id,
        controller_id, deadline):
    """Upload a fully verified staged file; every retry reopens at byte 0."""
    conn, failure = _artifact_connect(target, hostkeys_obj, deadline)
    if conn is None:
        return failure
    headers = {
        "Accept": "application/json",
        "Connection": "close",
        "Content-Type": metadata["mime_type"],
        "Content-Length": str(metadata["size"]),
        artifact_http.HEADER_SHA256: metadata["sha256"],
        artifact_http.HEADER_NODE_ID: node_id,
        artifact_http.HEADER_CONTROLLER_ID: controller_id,
    }
    if metadata.get("filename_hint"):
        headers[artifact_http.HEADER_FILENAME] = metadata["filename_hint"]
    try:
        _set_artifact_socket_timeout(conn, deadline)
        conn.putrequest("POST", path)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders()
        with open(staged_path, "rb") as source:
            sent = 0
            while sent < metadata["size"]:
                deadline.remaining()
                block = source.read(min(
                    ARTIFACT_STREAM_CHUNK_BYTES, metadata["size"] - sent))
                if not block:
                    raise _ArtifactLocalIOError()
                _set_artifact_socket_timeout(conn, deadline)
                conn.send(block)
                sent += len(block)
        _set_artifact_socket_timeout(conn, deadline)
        response = conn.getresponse()
        return _read_artifact_json_response(conn, response, deadline)
    except _ArtifactDeadlineExceeded:
        return _deadline_refusal()
    except _ArtifactLocalIOError:
        return _artifact_refusal(
            "artifact_local_io", "staged upload became unavailable")
    except (OSError, http.client.HTTPException, _ArtifactProtocolError):
        # The endpoint is content-addressed and verifies every duplicate body.
        # The bounded caller may replay, but only by reopening this verified
        # staged file at byte zero. Convoy has no upload Range/resume contract.
        return _artifact_transport_failure(deadline)
    finally:
        _close_connection(conn)


def upload_peer_artifact(
        target, hostkeys_obj, convoy_id, node_id, controller_id, stream, *,
        expected_size, expected_sha256,
        mime_type="application/octet-stream", filename_hint=None,
        timeout_s=DEFAULT_ARTIFACT_TIMEOUT_S, max_attempts=2,
        max_bytes=DEFAULT_ARTIFACT_MAX_BYTES, temp_dir=None, monotonic=None):
    """Verify locally, then upload an artifact over pinned mutual TLS.

    The source is first staged and hashed under the cumulative deadline.
    Retries are whole, content-addressed uploads from byte zero; an interrupted
    upload is never mislabeled or attempted as a Range resume. Temporary bytes
    are deleted on success and every failure path.
    """
    work = partial = ""
    try:
        normalized, segment = _artifact_namespace(convoy_id)
        node_id = _artifact_text(node_id, "node_id")
        controller_id = _artifact_text(controller_id, "controller_id")
        attempts_limit = _artifact_attempt_count(max_attempts)
        max_bytes = _artifact_max_bytes(max_bytes)
        if (isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0 or expected_size > max_bytes):
            raise ValueError("expected_size is outside the transfer limit")
        if (not isinstance(expected_sha256, str)
                or not _SHA256_RE.fullmatch(expected_sha256)):
            raise ValueError("expected_sha256 must be lowercase SHA-256")
        metadata = {
            "artifact_id": "art_" + expected_sha256,
            "sha256": expected_sha256,
            "size": expected_size,
            "mime_type": _artifact_media_type(mime_type),
            "filename_hint": _artifact_text(
                filename_hint, "filename_hint", optional=True, filename=True)
                or None,
        }
        deadline = _ArtifactDeadline(timeout_s, monotonic=monotonic)
        work, partial = _make_artifact_temp(temp_dir)
        try:
            _stage_upload_source(stream, partial, metadata, deadline)
        except _ArtifactDeadlineExceeded:
            return _deadline_refusal()
        except _ArtifactProtocolError as exc:
            return _artifact_refusal("artifact_corrupt", str(exc))
        except _ArtifactLocalIOError:
            return _artifact_refusal(
                "artifact_local_io", "upload staging storage refused the source")

        last_failure = UNREACHABLE
        ambiguous = False
        for attempt in range(1, attempts_limit + 1):
            outcome = _upload_artifact_once(
                target, hostkeys_obj, _artifact_path(segment), partial,
                metadata, node_id, controller_id, deadline)
            if isinstance(outcome, _PinMismatch):
                return outcome
            if outcome is UNREACHABLE or outcome is None:
                ambiguous = ambiguous or outcome is None
                last_failure = outcome
                continue
            if not isinstance(outcome, dict) or outcome.get("ok") is not True:
                return outcome
            try:
                returned = _artifact_reference_fields(
                    outcome.get("artifact"), normalized, max_bytes)
            except ValueError as exc:
                return _artifact_refusal("artifact_corrupt", str(exc))
            if (returned["artifact_id"] != metadata["artifact_id"]
                    or returned["sha256"] != metadata["sha256"]
                    or returned["size"] != metadata["size"]):
                return _artifact_refusal(
                    "artifact_corrupt", "peer acknowledged different content")
            result = dict(outcome)
            result["transfer"] = {
                "attempts": attempt, "resumed": False,
                "retry_mode": "full_from_zero", "bytes": metadata["size"],
            }
            return result
        # Once any request may have been sent, never downgrade the final
        # answer to retry-safe UNREACHABLE merely because a later connect was
        # refused.
        return None if ambiguous else last_failure
    except ValueError as exc:
        return _artifact_refusal("artifact_invalid", str(exc))
    except _ArtifactLocalIOError:
        return _artifact_refusal(
            "artifact_local_io", "local upload staging is unavailable")
    finally:
        _cleanup_artifact_temp(work, partial)


def _call_request(target, hostkeys_obj, method, path, body, timeout, pool):
    # Preserve the six-argument seam used by existing tests and embedders when
    # no production pool is supplied.
    if pool is None:
        return _request(target, hostkeys_obj, method, path, body, timeout)
    return _request(
        target, hostkeys_obj, method, path, body, timeout, pool=pool)


def _quote_segment(value):
    # A delivery id is cj_<hex>, but treat it as untrusted: strip anything
    # that could alter the path. The server validates too.
    text = str(value or "")
    return "".join(ch for ch in text if ch.isalnum() or ch in "_-")


def _encode_convoy_segment(value):
    if (not isinstance(value, str) or not value
            or value != value.strip()):
        return None
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if (len(raw) > MAX_CONVOY_ID_BYTES
            or any(byte < 0x20 or byte == 0x7f for byte in raw)):
        return None
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _ResponseLimitError(Exception):
    pass


def _read_bounded_response(response, limit=MAX_PEER_RESPONSE_BYTES):
    """Read at most ``limit`` bytes, honoring and verifying length.

    A missing Content-Length is legal (for example a future chunked
    response), but remains bounded through incremental reads. A declared
    oversize body is rejected without reading it at all.
    """
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            declared = int(declared)
        except (TypeError, ValueError):
            raise _ResponseLimitError("invalid content length")
        if declared < 0 or declared > limit:
            raise _ResponseLimitError("response exceeds limit")
    out = bytearray()
    while True:
        allowance = limit + 1 - len(out)
        if allowance <= 0:
            raise _ResponseLimitError("response exceeds limit")
        chunk = response.read(min(_RESPONSE_READ_CHUNK, allowance))
        if not chunk:
            break
        out.extend(chunk)
        if len(out) > limit:
            raise _ResponseLimitError("response exceeds limit")
    if declared is not None and len(out) != declared:
        raise _ResponseLimitError("response length mismatch")
    return bytes(out)


def _request(target, hostkeys_obj, method, path, body, timeout, pool=None):
    if pool is not None:
        if not isinstance(pool, PeerConnectionPool):
            return UNREACHABLE
        return pool.request(target, method, path, body, timeout)
    try:
        context = build_client_ssl_context(hostkeys_obj,
                                            target.pinned_cert_pem)
    except (ssl.SSLError, OSError, ValueError) as e:
        # A malformed pinned PEM or an unreadable local cert: this host
        # cannot even form the connection. Not "did not arrive" in the
        # network sense, but the request definitely never left -> treat
        # as unreachable (retry-safe once the local material is fixed).
        return UNREACHABLE
    conn = http.client.HTTPSConnection(
        target.address, target.port, timeout=timeout, context=context)
    try:
        try:
            conn.connect()
        except ssl.SSLCertVerificationError as e:
            # The pin did not validate at the TLS layer: the presented
            # cert does not chain to the one we pinned. Never
            # auto-updated -- refuse and tell the operator.
            return _pin_mismatch(target, offered=None, cause=e)
        except ssl.SSLError as e:
            # Any other handshake failure (a protocol/version problem, a
            # truncated handshake). The request never left; treat as a
            # refused-before-send unreachable so it is retry-safe.
            return UNREACHABLE
        except (ConnectionRefusedError, socket.timeout, TimeoutError):
            return UNREACHABLE
        except OSError as e:
            # Connect-time OS errors never delivered a request.
            return UNREACHABLE

        # POST-HANDSHAKE PIN RECOMPUTE (the plan's second defense). The
        # trust store already enforced the pin; this catches the
        # impossible-but-catastrophic case where a cert passed trust yet
        # its SPKI is not what we pinned, and it is what would identify a
        # peer even if the trust store were ever misconfigured.
        offered_fp = _presented_fingerprint(conn.sock)
        if offered_fp is None:
            return None         # no cert after a "successful" handshake
        if offered_fp != target.expected_fingerprint:
            return _pin_mismatch(target, offered=offered_fp, cause=None)

        # The pin held. From here a failure may mean the request WAS
        # written, so it resolves to None (reconcile), never UNREACHABLE.
        try:
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(body))
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = _read_bounded_response(response)
        except (OSError, http.client.HTTPException, _ResponseLimitError):
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # A peer that answered with unparseable bytes told us nothing
            # usable; the request may have run -> reconcile.
            return None
    finally:
        try:
            conn.close()
        except (OSError, http.client.HTTPException):
            pass


def _presented_fingerprint(sock):
    """The SPKI fingerprint of the certificate the peer presented, or
    None. Recomputed through hostkeys so the X.509 dependency stays in
    the one module chartered to hold it."""
    try:
        cert_der = sock.getpeercert(binary_form=True)
    except (ssl.SSLError, OSError, ValueError):
        return None
    if not cert_der:
        return None
    try:
        spki = hostkeys.public_der_from_certificate(cert_der)
        return hostkeys.fingerprint(spki)
    except hostkeys.HostKeyError:
        return None


def _pin_mismatch(target, offered, cause):
    offered_line = (f"it presented key {offered}"
                    if offered else "it presented a certificate that does "
                    "not match the pin")
    message = (
        f"Refusing to connect to {target.host_id} at "
        f"{target.address}:{target.port}: {offered_line}; this host pinned "
        f"{target.expected_fingerprint}. Either that peer rotated its "
        f"Convoy identity (re-admit after comparing fingerprints out of "
        f"band) or something on this network is impersonating it. Convoy "
        f"will not connect until you decide.")
    return _PinMismatch(target.host_id, f"{target.address}:{target.port}",
                        target.expected_fingerprint, offered, message)
