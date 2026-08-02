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

import errno
import http.client
import json
import socket
import ssl

import convoy_hostkeys as hostkeys
import convoy_protocol as protocol


# Connect + enqueue is a fast round trip: /peer/envelope PERSISTS a
# durable job and returns immediately (it does not block on the node),
# so the peer leg never needs the node's full budget. This ceilings the
# per-request wait; the envelope's remaining budget shortens it further.
DEFAULT_PEER_TIMEOUT_S = 15.0
# A floor so a nearly-expired-but-still-valid envelope gets a real
# attempt rather than a 0-second timeout that fails locally.
_MIN_PEER_TIMEOUT_S = 2.0

ROUTE_ENVELOPE = "/peer/envelope"
ROUTE_MANIFEST = "/peer/manifest"
ROUTE_HEALTH = "/peer/health"
ROUTE_JOBS_PREFIX = "/peer/jobs/"


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
    return max(_MIN_PEER_TIMEOUT_S, min(remaining, ceiling))


def send_envelope(target, hostkeys_obj, envelope, timeout=None, now=None):
    """POST a signed envelope to a peer's /peer/envelope. Returns the
    peer's structured JSON answer (a dict), UNREACHABLE (did not arrive),
    a _PinMismatch, or None (transport failed after the request may have
    been written -> reconcile, never blind-retry). Never raises."""
    if timeout is None:
        timeout = peer_timeout_for(envelope, now=now)
        if timeout == 0.0:
            # Locally expired: the server would refuse deadline_exceeded
            # anyway; skip the round trip and say so honestly.
            return {"ok": False, "reason": "deadline_exceeded",
                    "detail": "the envelope expired before it was sent"}
    # The wire contract is the SAME as the loopback /envelope route --
    # {"envelope": <envelope>} -- so the peer leg and the local leg feed
    # the one submit_envelope authority rather than two.
    body = json.dumps({"envelope": envelope}).encode("utf-8")
    return _request(target, hostkeys_obj, "POST", ROUTE_ENVELOPE, body,
                    timeout)


def get_peer_job(target, hostkeys_obj, delivery_id, since=None,
                 timeout=DEFAULT_PEER_TIMEOUT_S):
    """GET a delivery record's status from the peer that owns it, with an
    optional `since` cursor (A-46 point 1). Same outcome contract as
    send_envelope."""
    path = ROUTE_JOBS_PREFIX + _quote_segment(delivery_id)
    if since is not None:
        path += f"?since={float(since)}"
    return _request(target, hostkeys_obj, "GET", path, None, timeout)


def get_peer_manifest(target, hostkeys_obj, timeout=DEFAULT_PEER_TIMEOUT_S):
    """GET a peer's remote-exposed operation manifest (refuse-before-send
    for a controller)."""
    return _request(target, hostkeys_obj, "GET", ROUTE_MANIFEST, None,
                    timeout)


def get_peer_health(target, hostkeys_obj, timeout=DEFAULT_PEER_TIMEOUT_S):
    """GET a peer's liveness + host_id over the pinned channel."""
    return _request(target, hostkeys_obj, "GET", ROUTE_HEALTH, None, timeout)


def _quote_segment(value):
    # A delivery id is cj_<hex>, but treat it as untrusted: strip anything
    # that could alter the path. The server validates too.
    text = str(value or "")
    return "".join(ch for ch in text if ch.isalnum() or ch in "_-")


def _request(target, hostkeys_obj, method, path, body, timeout):
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
            raw = response.read()
        except (OSError, http.client.HTTPException):
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
