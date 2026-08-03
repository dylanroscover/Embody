"""The LAN peer listener for the embody-convoy host app (Phase 3 slice
3). Stdlib only (ssl is stdlib); X.509 parsing goes through
convoy_hostkeys, never `cryptography` directly (A-44, enforced by the
import-isolation test).

THE SECOND LISTENER, AND WHY IT IS A SEPARATE MODULE AND A SEPARATE
HANDLER CLASS. The loopback listener (convoy_hostapp.make_handler)
authenticates with the per-install IPC token and exposes the FULL local
route table -- /psk hands out the group signing key, /peers* grants
admission, /shutdown stops the daemon. None of that may ever be reachable
off-box. The plan's defense is structural, not a path filter: the LAN
handler below is a DIFFERENT CLASS with its OWN, SHORT route table, so a
route added to the loopback if-chain cannot become LAN-reachable by
accident. A parameterized test walks EVERY loopback route and asserts 404
here (L-03; /psk named).

THE PEER IS IDENTIFIED BY THE CERTIFICATE IT PRESENTED, NEVER BY ANYTHING
IT SAID. Mutual TLS 1.3, trust anchored on the pinned self-signed peer
certificates (the pin store IS the trust store). After the handshake the
peer's SPKI fingerprint is RECOMPUTED LOCALLY from the DER it presented
and that -- not any name in the cert, not any field in the body -- is
who the peer is. `authorize_peer` then runs BEFORE a single envelope
signature is verified (a blocked peer is refused while still holding a
valid key), and channel binding refuses an envelope whose signed
source disagrees with the authenticated peer.

NOTHING BINDS OFF-BOX WITHOUT lan.json (convoy_lan): a default build has
no LAN socket at all. The host app calls serve_lan only when
config.should_bind.
"""

import base64
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import convoy_hostkeys as hostkeys
import convoy_identity as identity
import convoy_peers as peers_mod
import convoy_artifact_http as artifact_http
import convoy_ws as convoy_ws


# -- caps (L-16 host exhaustion, S2 audit flooding) -------------------

# Same body cap as the loopback listener. A peer envelope is small; a
# pathological one is refused at the door, before the JSON parser.
MAX_PEER_BODY_BYTES = 1 * 1024 * 1024

# Keep peer replies bounded too. A target may have a faulty extension or
# an unexpectedly large node/result object; neither should turn one
# authenticated request into an unbounded socket write. Kept in sync
# with convoy_peerclient.MAX_PEER_RESPONSE_BYTES without importing the
# client into the listener's trust boundary.
MAX_PEER_RESPONSE_BYTES = 256 * 1024

# Namespace ids ride as one canonical base64url segment.
MAX_CONVOY_ID_BYTES = identity.MAX_CONVOY_ID_BYTES
MAX_PEER_NODES = 256
MAX_PEER_CONTROLLERS = 512

# Concurrent peer connections.  A 30-host direct mesh needs 29 inbound
# persistent control channels before accounting for short artifact transfers
# and reconnect overlap.  Sixty-four covers that design target while retaining
# a hard thread/socket exhaustion bound.
# Overflow closes the raw socket at once, audited (rate-limited).
DEFAULT_MAX_CONNECTIONS = 64

# One address cannot consume the entire global pool with slow handshakes
# or idle request bodies. A SINGLE well-behaved peer, though, legitimately
# holds several concurrent slots from its own IP: one long-lived /peer/session
# WSS upgrade, one persistent pooled control connection, and up to
# DEFAULT_MAX_TRANSFERS one-shot artifact streams (1 + 1 + 4 = 6). The cap
# must clear that sum or a peer starves its own transfers; it is derived from
# the transfer count so the two constants can never drift apart.
DEFAULT_MAX_CONNECTIONS_PER_IP = artifact_http.DEFAULT_MAX_TRANSFERS + 2

# The TLS handshake runs in the WORKER thread (not the accept loop), so a
# slow or stalled handshake cannot starve accepts -- but it still needs a
# ceiling of its own, or a peer that connects and never speaks holds a
# slot forever.
DEFAULT_HANDSHAKE_TIMEOUT_S = 10.0

# Once the handshake is up, a slow-loris request body is bounded by this.
DEFAULT_IO_TIMEOUT_S = 30.0

# The LAN route prefixes. Kept as constants so the loopback-route-leakage
# test can assert the WHOLE set and nothing outside it answers.
ROUTE_HEALTH = "/peer/health"
ROUTE_MANIFEST = "/peer/manifest"
ROUTE_ENVELOPE = "/peer/envelope"
ROUTE_JOBS_PREFIX = "/peer/jobs/"
ROUTE_NODES_PREFIX = "/peer/nodes/"
ROUTE_CONTROLLERS_PREFIX = "/peer/controllers/"
ROUTE_CANCEL = "/peer/jobs/cancel"
ROUTE_ACK = "/peer/jobs/ack"
ROUTE_CONTROLLER_HEARTBEAT = "/peer/controllers/heartbeat"
ROUTE_ARTIFACTS_PREFIX = artifact_http.PEER_ROUTE_PREFIX
ROUTE_SESSION = "/peer/session"

SESSION_RPC_HEALTH = "peer.health"
SESSION_RPC_MANIFEST = "peer.manifest"
SESSION_RPC_ENVELOPE = "peer.envelope"
SESSION_RPC_JOB = "peer.job"
SESSION_RPC_NODES = "peer.nodes"
SESSION_RPC_CONTROLLERS = "peer.controllers"
SESSION_RPC_CANCEL = "peer.cancel"
SESSION_RPC_ACK = "jobs.ack"
SESSION_RPC_CONTROLLER_HEARTBEAT = "controller.heartbeat"
SESSION_RPC_METHODS = frozenset({
    SESSION_RPC_HEALTH, SESSION_RPC_MANIFEST, SESSION_RPC_ENVELOPE,
    SESSION_RPC_JOB, SESSION_RPC_NODES, SESSION_RPC_CONTROLLERS,
    SESSION_RPC_CANCEL, SESSION_RPC_ACK, SESSION_RPC_CONTROLLER_HEARTBEAT,
})


class LanBindError(Exception):
    """The LAN listener could not bind. `reason` is a stable code; the
    daemon logs it and keeps serving loopback rather than dying."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# -- rate-limited audit for unauthenticated noise ----------------------

class _TokenBucket:
    """A token bucket so a port scanner cannot fill audit.jsonl (S2/S13).

    Handshake failures and connection-overflow closes are audited THROUGH
    this: when the bucket is empty the event is COUNTED but not written,
    and the count rides out on the next event that IS written -- so the
    trail records "and N more suppressed" instead of either lying by
    omission or letting a scanner author 254 lines a sweep. now() is
    injected so the pacing is testable without real time.
    """

    def __init__(self, capacity, refill_per_s, now):
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._refill = float(refill_per_s)
        self._now = now
        self._last = now()
        self._suppressed = 0
        self._lock = threading.Lock()

    def take(self):
        """(allowed, suppressed_since_last_allowed). allowed False means
        do not write; the caller still performs the non-audit action."""
        with self._lock:
            now = self._now()
            elapsed = max(0.0, now - self._last)
            self._last = now
            self._tokens = min(self._capacity,
                               self._tokens + elapsed * self._refill)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                carried = self._suppressed
                self._suppressed = 0
                return True, carried
            self._suppressed += 1
            return False, 0


# -- the server SSL context (rebuilt on peer change) -------------------

def build_server_ssl_context(cert_path, key_path, peer_pems):
    """A mutual-TLS-1.3 server context whose trust store is the pinned
    peer certificates -- no CA, no PKI names.

    peer_pems is the PEM text of every peer this host will let connect
    (see HostApp.lan_trust_material -- every peer with a stored
    certificate, INCLUDING blocked ones: a blocked peer keeps its pin so
    it still HANDSHAKES and is then refused by authorize_peer, which is
    what makes 'refused while holding a valid key' true and the
    verifier-spy order test meaningful). An EMPTY trust store is legal
    and fail-closed: CERT_REQUIRED with no anchors means every client
    certificate fails verification, so the socket stays bound and
    reachable but admits nobody until a peer is on file.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    # MUTUAL: require a client certificate. Without this the server would
    # accept any client and lean entirely on the envelope signature --
    # exactly the identity-by-assertion the pin exists to replace.
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert_path, key_path)
    # ONE ANCHOR AT A TIME, never a concatenated cadata blob. A single
    # unparseable stored cert_pem (a corrupt record, a future join route
    # that stored a bad cert) would make load_verify_locations reject the
    # WHOLE blob -- and because that build runs inside the accept path's
    # handshake try/except, EVERY peer handshake would then fail: a total
    # off-box outage from one bad record, mislabelled as a per-peer
    # handshake refusal. Loading each anchor on its own is fail-closed PER
    # PEER: a peer with a broken cert simply cannot connect (its anchor
    # was skipped), every other peer still can. Each pinned self-signed
    # leaf becomes a trust anchor; ca=False/key_cert_sign=False
    # (convoy_hostkeys._issue_certificate) stops a pinned leaf from
    # issuing certs for other identities.
    for pem in peer_pems:
        try:
            context.load_verify_locations(cadata=pem)
        except (ssl.SSLError, ValueError, TypeError):
            # A malformed anchor is dropped, not fatal. The affected peer
            # is unreachable (correct -- its cert is unusable) and the
            # rest of the mesh is unaffected.
            continue
    return context


class _ContextProvider:
    """Hands the accept path the CURRENT server context, rebuilding it
    only when the admitted peer set changes.

    Admission takes effect without a restart: an admit/block/forget/
    re-pin changes lan_trust_material's signature, and the next
    connection is wrapped with a freshly built context. Rebuilding is
    cheap relative to a handshake and only happens on a membership
    change, never per connection on a stable mesh.
    """

    def __init__(self, app):
        self._app = app
        self._sig = None
        self._ctx = None
        self._lock = threading.Lock()

    def context(self):
        # UNDER THE APP LOCK: lan_trust_material reads the peer store, and
        # the store is serialized by the host app's single lock -- reading
        # it from this accept-path thread while a loopback admit/block
        # holds the lock would be a data race on peers.json. The app lock
        # is released before the (CPU-bound) context build, so a slow
        # rebuild never stalls a concurrent loopback request.
        with self._app.lock:
            signature, pems = self._app.lan_trust_material()
        with self._lock:
            if signature != self._sig or self._ctx is None:
                self._ctx = build_server_ssl_context(
                    self._app.hostkeys.cert_path,
                    self._app.hostkeys.key_path,
                    pems)
                self._sig = signature
            return self._ctx


# -- the listener ------------------------------------------------------

class PeerHTTPSServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that binds a NAMED interface (never the
    wildcard) and wraps each accepted connection in TLS INSIDE THE WORKER
    THREAD, so a slow handshake cannot stall the accept loop.
    """

    # A shutdown must not block on a peer holding a connection open; a
    # security listener wants to go down FIRST and fast (A-46 point 4).
    daemon_threads = True
    # socketserver's tiny default backlog can shed a healthy 29-peer reconnect
    # wave before the bounded connection gate even sees it.
    request_queue_size = 64
    # NEVER reuse the address: the plan requires a taken port to REFUSE,
    # and SO_REUSEADDR on Windows would additionally let another process
    # hijack the port. Default HTTPServer sets this True; override it.
    allow_reuse_address = False

    def __init__(self, server_address, handler_class, app, context_provider,
                 audit_bucket, audit_sink,
                 max_connections=DEFAULT_MAX_CONNECTIONS,
                 max_connections_per_ip=DEFAULT_MAX_CONNECTIONS_PER_IP,
                 handshake_timeout=DEFAULT_HANDSHAKE_TIMEOUT_S,
                 io_timeout=DEFAULT_IO_TIMEOUT_S):
        self.app = app
        self._context_provider = context_provider
        self._audit_bucket = audit_bucket
        self._audit_sink = audit_sink
        self._configured_max_connections = max(1, int(max_connections))
        self._sem = threading.BoundedSemaphore(
            self._configured_max_connections)
        self._max_connections_per_ip = max(
            1, min(int(max_connections_per_ip),
                   self._configured_max_connections))
        self._source_counts = {}
        self._source_counts_lock = threading.Lock()
        self._handshake_timeout = handshake_timeout
        self._io_timeout = io_timeout
        # bind_and_activate happens in super().__init__; a bind failure
        # (port taken) raises OSError, which serve_lan turns into a named
        # LanBindError so the daemon logs it and keeps loopback alive.
        super().__init__(server_address, handler_class)

    def get_request(self):
        # RAW accept only -- the TLS wrap is deferred to the worker thread
        # in finish_request. Returning the raw socket here keeps the
        # accept loop free during a handshake.
        return self.socket.accept()

    def process_request(self, request, client_address):
        """Claim capacity BEFORE ThreadingMixIn creates a worker.

        The old finish_request gate bounded active handlers but still
        created one thread per overflow connection. A scanner could thus
        churn thousands of threads that immediately failed the semaphore.
        This accept-thread gate closes overflow sockets without spawning.
        """
        source = _source_ip(client_address)
        if not self._claim_connection(source):
            self._audit_throttled(
                "peer_connection_overflow",
                {"source": source,
                 "max_connections": self._sem_limit,
                 "max_connections_per_ip": self._max_connections_per_ip})
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            # Thread construction itself failed. No worker exists to run
            # process_request_thread's finally block, so release here.
            self._release_connection(source)
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        source = _source_ip(client_address)
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_connection(source)

    def _claim_connection(self, source):
        if not self._sem.acquire(blocking=False):
            return False
        with self._source_counts_lock:
            current = self._source_counts.get(source, 0)
            if current >= self._max_connections_per_ip:
                self._sem.release()
                return False
            self._source_counts[source] = current + 1
        return True

    def _release_connection(self, source):
        with self._source_counts_lock:
            current = self._source_counts.get(source, 0)
            if current <= 1:
                self._source_counts.pop(source, None)
            else:
                self._source_counts[source] = current - 1
        self._sem.release()

    @property
    def _sem_limit(self):
        # threading.Semaphore deliberately exposes no public initial
        # value. The configured limit is retained explicitly for audit.
        return self._configured_max_connections

    def finish_request(self, request, client_address):
        # Runs in the bounded per-connection worker thread.
        request.settimeout(self._handshake_timeout)
        try:
            context = self._context_provider.context()
            tls = context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            # A scanner, an unpinned or revoked-anchor certificate, a
            # plaintext client. The connection never became a peer;
            # audit it RATE-LIMITED and drop it. Never re-raise -- an
            # unhandled handshake error would call handle_error and
            # print a traceback per scan packet.
            self._audit_throttled(
                "peer_handshake_refused",
                {"source": _source_ip(client_address),
                 "detail": _handshake_detail(e)})
            _quiet_close(request)
            return
        try:
            tls.settimeout(self._io_timeout)
            # Construct the handler on the TLS socket; it reads/writes
            # over the encrypted channel. Closing is handled by
            # shutdown_request on the raw fd (idempotent).
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            _quiet_close(tls)

    def shutdown_request(self, request):
        # We may have already closed the TLS socket over this fd in
        # finish_request; swallow the resulting OSError. socket.close is
        # idempotent, so a double close is harmless.
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        _quiet_close(request)

    def handle_error(self, request, client_address):
        # The base implementation prints a traceback to stderr. Under a
        # supervisor that is noise per malformed connection; the routes
        # already answer every expected error, so anything here is
        # unexpected -- audit it once, rate-limited, and move on.
        self._audit_throttled(
            "peer_handler_error",
            {"source": _source_ip(client_address)})

    def _audit_throttled(self, event, detail):
        allowed, suppressed = self._audit_bucket.take()
        if not allowed:
            return
        if suppressed:
            detail = dict(detail, suppressed_since_last=suppressed)
        try:
            self._audit_sink(event, detail)
        except Exception:
            pass        # the trail is evidence, never a control path


def _source_ip(client_address):
    try:
        return str(client_address[0])
    except (IndexError, TypeError):
        return "?"


def _handshake_detail(error):
    # The exception class + a short reason, never the raw message: an
    # SSLError message can carry peer-controlled bytes, and this rides
    # into a JSON audit line.
    reason = getattr(error, "reason", None)
    if reason:
        return f"{type(error).__name__}:{reason}"
    return type(error).__name__


def _quiet_close(sock):
    try:
        sock.close()
    except OSError:
        pass


def _rebuild_upgrade_request(handler):
    """Rebuild parsed headers without losing duplicates or accepting folds."""
    request_line = getattr(handler, "raw_requestline", b"")
    if (not isinstance(request_line, bytes)
            or not request_line.endswith(b"\r\n")):
        raise ValueError("invalid HTTP request line")
    chunks = [request_line]
    raw_items = getattr(handler.headers, "raw_items", None)
    if not callable(raw_items):
        raise ValueError("raw HTTP headers unavailable")
    for name, value in raw_items():
        if (not isinstance(name, str) or not isinstance(value, str)
                or "\r" in name or "\n" in name
                or "\r" in value or "\n" in value):
            # Includes obsolete folded lines accepted by email.parser but
            # forbidden by convoy_ws's strict Upgrade grammar.
            raise ValueError("folded or malformed HTTP upgrade header")
        chunks.append(
            name.encode("ascii", "strict") + b": "
            + value.encode("ascii", "strict") + b"\r\n")
    chunks.append(b"\r\n")
    raw = b"".join(chunks)
    if len(raw) > convoy_ws.MAX_HTTP_HEADER_BYTES:
        raise ValueError("HTTP upgrade headers exceed limit")
    return raw


# -- the handler: the LAN route table, and NOTHING ELSE ----------------

class PeerRequestHandler(BaseHTTPRequestHandler):
    """The SHORT route table. Every path not named here is 404 -- the
    structural half of keeping loopback routes off-box."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- identity ------------------------------------------------------

    @property
    def _app(self):
        return self.server.app

    def _peer_identity(self):
        """(host_id, spki_der, fingerprint) for the certificate this
        connection presented, or None after sending a refusal AND closing
        the connection.

        THE CERT->IDENTITY mapping only -- NOT admission. The handshake
        proved the cert chains to a pinned anchor; this recomputes the
        SPKI fingerprint LOCALLY (through hostkeys, which owns the X.509
        dependency) and maps it to the peer RECORD, whatever its state.
        Whether that peer may be HEARD right now (block, denylist,
        killswitch, observe-only) is decided per request by _authorize,
        so a peer blocked mid-connection is refused on the NEXT request
        rather than riding a cached decision.

        Cached per connection because the certificate cannot change on a
        live connection. A resolution FAILURE (no cert, malformed cert,
        no matching record) is terminal for the connection -- the cert
        will not change -- so the refusal also sets close_connection, or a
        second keep-alive request would return the cached None and get NO
        response, hanging the client until the I/O timeout.
        """
        cached = getattr(self, "_identity_cache", ...)
        if cached is not ...:
            return cached
        result = self._resolve_identity()
        self._identity_cache = result
        return result

    def _resolve_identity(self):
        cert_der = self.connection.getpeercert(binary_form=True)
        if not cert_der:
            # CERT_REQUIRED means this cannot happen after a successful
            # handshake -- but fail closed if it somehow does.
            self.close_connection = True
            self._send(403, {"ok": False, "reason": "no_client_certificate"})
            return None
        try:
            spki = hostkeys.public_der_from_certificate(cert_der)
        except hostkeys.HostKeyError as e:
            self.close_connection = True
            self._send(403, {"ok": False, "reason": "malformed_peer_cert",
                             "detail": e.reason})
            return None
        fingerprint = hostkeys.fingerprint(spki)
        app = self._app
        with app.lock:
            record = app.peers.find_by_fingerprint(fingerprint)
        if record is None:
            # The cert passed the trust store but no admission record
            # matches its fingerprint -- a forget() between handshake and
            # now, or an anchor that outlived its record. Terminal.
            self.close_connection = True
            self._send(403, {"ok": False, "reason": "peer_unknown"})
            return None
        return record["host_id"], spki, fingerprint

    def _authorize(self, host_id, fingerprint, convoy_id=None):
        """THE admission gate, ON EVERY REQUEST (the plan's two-point
        authorize: at connection accept AND per request, so a block /
        denylist entry / killswitch that lands mid-connection is honored
        without waiting for the peer to reconnect). Returns the
        PeerDecision; the caller refuses when it is not allowed.

        This is the fix for the read routes bypassing authorization: WITH
        it, a blocked/pending/denylisted peer -- which still holds a valid
        pinned cert and so still completes the handshake -- is refused on
        /peer/health, /peer/manifest and /peer/jobs exactly as it is on
        /peer/envelope, and the A-32 killswitch contains reads too.
        touch_seen fires here, once per connection and only for a peer
        that is actually allowed (a refused peer must not be able to drive
        a peers.json rewrite per connection).
        """
        app = self._app
        with app.lock:
            decision = app.peers.authorize_peer(
                host_id, fingerprint, convoy_id=convoy_id)
            if decision.allowed and not getattr(self, "_touched", False):
                app.peers.touch_seen(host_id)
                self._touched = True
        return decision

    def _refuse_peer(self, decision, discard_body=False):
        # Every peer-authorization refusal is 403: "this host will not
        # hear you", not "you sent something malformed".
        # Refused identities may not squat on a persistent control slot.
        self.close_connection = True
        self._send(403, {"ok": False, "reason": decision.reason,
                         "detail": decision.detail})
        if discard_body:
            # The response is already flushed. Drain (never parse) a
            # bounded declared body before closing: on Windows, closing a
            # socket with unread POST bytes commonly emits an RST that
            # discards the just-written refusal, turning an affirmative
            # target decision into an ambiguous client-side None.
            self._discard_body()

    # -- HTTP ----------------------------------------------------------

    def _send(self, code, payload, extra_headers=None):
        try:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            code = 500
            body = b'{"ok": false, "reason": "internal_error"}'
        if len(body) > MAX_PEER_RESPONSE_BYTES:
            code = 500
            body = (b'{"ok":false,"reason":"response_too_large",'
                    b'"detail":"peer response exceeded the wire limit"}')
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                if name.lower() not in ("content-type", "content-length"):
                    self.send_header(name, str(value))
            self.send_header(
                "Connection", "close" if self.close_connection
                else "keep-alive")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            # The peer hung up mid-response. Nothing to do; the worker
            # thread ends and the slot frees.
            pass

    def _send_artifact_stream(self, code, lease, headers):
        """Stream verified artifact bytes over the pinned TLS channel."""
        self.close_connection = True
        try:
            self.send_response(code)
            for name, value in headers.items():
                self.send_header(name, str(value))
            self.send_header("Connection", "close")
            self.end_headers()
            for block in lease:
                self.wfile.write(block)
            self.wfile.flush()
        except OSError:
            pass
        finally:
            lease.close()

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_PEER_BODY_BYTES:
            return None
        try:
            raw = self.rfile.read(length)
        except OSError:
            return None
        try:
            # Reject JSON's NaN/Infinity tokens exactly as the loopback
            # listener does: they are the fail-open vector for every
            # numeric guard downstream (a NaN deadline never expires).
            return json.loads(raw.decode("utf-8"),
                              parse_constant=_reject_json_constant)
        except ValueError:
            return None

    def _discard_body(self):
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        if remaining <= 0 or remaining > MAX_PEER_BODY_BYTES:
            return
        try:
            while remaining:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            return

    def do_GET(self):
        identity = self._peer_identity()
        if identity is None:
            return              # a refusal was already sent
        host_id, spki, fingerprint = identity
        decision = self._authorize(host_id, fingerprint)
        if not decision.allowed:
            # Blocked / pending / denylisted / killswitch -> refused on
            # EVERY read too (X0 containment), not only on /peer/envelope.
            self._refuse_peer(decision)
            return
        try:
            if self.path == ROUTE_SESSION:
                self._handle_session_upgrade(host_id, spki, fingerprint)
                return
            artifact_route = artifact_http.parse_route(
                self.path, ROUTE_ARTIFACTS_PREFIX)
            if artifact_route is not None:
                if artifact_route[0] != "download":
                    self._send(405, {"ok": False,
                                     "reason": "method_not_allowed"})
                else:
                    self._handle_artifact_download(
                        host_id, fingerprint, artifact_route)
                return
            if self.path == ROUTE_HEALTH:
                # The peer analogue of loopback /health: liveness +
                # host_id, no secrets. A caller confirms it reached the
                # host it pinned. Requires the mutual-TLS identity like
                # every peer route (it is NOT the unauthenticated route
                # /health is on the loopback side).
                self._send(200, {"ok": True, "protocol": "convoy-peer/1",
                                 "host_id": self._app.host_id})
                return
            if self.path == ROUTE_MANIFEST:
                code, payload = self._app.get_peer_manifest(host_id)
                self._send(code, payload)
                return
            if self.path.startswith(ROUTE_NODES_PREFIX):
                self._handle_peer_nodes(host_id, fingerprint)
                return
            if self.path.startswith(ROUTE_CONTROLLERS_PREFIX):
                self._handle_peer_controllers(host_id, fingerprint)
                return
            if self.path.startswith(ROUTE_JOBS_PREFIX):
                self._handle_peer_job(host_id)
                return
            self._send(404, {"ok": False, "reason": "not_found"})
        except artifact_http.ArtifactHTTPError as e:
            self._send(e.status, e.payload(), e.headers)
        except Exception as e:
            # Last resort: every expected error is a named 4xx above. This
            # exists so an unforeseen bug cannot kill the worker thread
            # with no response (the dead-thread class the loopback panel
            # found on the auth path).
            self._send(500, {"ok": False, "reason": "internal_error",
                             "detail": type(e).__name__})

    def _handle_session_upgrade(self, host_id, spki, fingerprint):
        """Strictly upgrade one authenticated HTTP connection to Convoy WSS.

        BaseHTTPRequestHandler has already consumed the request headers, so
        reconstruct their original multiplicity with ``raw_items`` and run
        the exact convoy_ws validator before emitting 101.  The handler then
        remains alive until the selected/duplicate session closes; returning
        earlier would let socketserver close a socket now owned by Session.
        """
        try:
            manager = self._app.prepare_peer_session(
                host_id, fingerprint, spki)
            raw = _rebuild_upgrade_request(self)
            _info, key = convoy_ws.validate_client_upgrade(
                raw, expected_path=ROUTE_SESSION)
            response = convoy_ws.build_server_upgrade(key)
        except (ValueError, UnicodeError, convoy_ws.ConvoyWebSocketError) as exc:
            self.close_connection = True
            self._send(400, {
                "ok": False,
                "reason": "websocket_upgrade_rejected",
                "detail": getattr(exc, "code", type(exc).__name__),
            })
            return
        except Exception as exc:
            self.close_connection = True
            self._send(503, {
                "ok": False,
                "reason": "websocket_session_unavailable",
                "detail": type(exc).__name__,
            })
            return

        self.close_connection = True
        try:
            self.connection.sendall(response)
            accepted = manager.accept_authenticated_websocket(
                convoy_ws.WebSocketConnection(self.connection, "server"),
                host_id,
                timeout_s=min(5.0, self.server._io_timeout),
                authentication_context=(fingerprint, bytes(spki)))
            accepted.wait_closed()
        except Exception as exc:
            # 101 may already be on the wire, so an HTTP error is no longer a
            # legal response.  Close and leave one bounded diagnostic.
            try:
                self._app._audit_best_effort(
                    "peer_websocket_closed", {
                        "peer_host_id": host_id,
                        "error": f"{type(exc).__name__}: {exc}"[:512],
                    })
            except Exception:
                pass

    def do_POST(self):
        identity = self._peer_identity()
        if identity is None:
            return
        host_id, spki, fingerprint = identity
        # The accept-time authorize (blocked/pending/denylist/killswitch)
        # BEFORE the body is even read -- a refused peer gets no parser
        # surface. submit_envelope re-authorizes as the per-request check
        # (the plan's second point); the two together are the two-point
        # authorization, and an observe-only peer passes here and is
        # refused at the mutation gate inside submit_envelope.
        decision = self._authorize(host_id, fingerprint)
        if not decision.allowed:
            self._refuse_peer(decision, discard_body=True)
            return
        try:
            artifact_route = artifact_http.parse_route(
                self.path, ROUTE_ARTIFACTS_PREFIX)
        except artifact_http.ArtifactHTTPError as e:
            # A bad route (query string, bad base64 namespace, extra path
            # segment) leaves the declared POST body unread. DRAIN it so the
            # error response is delivered cleanly and the keep-alive
            # connection stays usable. Closing without draining RSTs the
            # socket on Windows and discards the response we just wrote
            # (review 2026-08-02); the malformed-body path below must close
            # only because ITS lengths are unparseable.
            self._discard_body()
            self._send(e.status, e.payload(), e.headers)
            return
        if artifact_route is not None and artifact_route[0] == "upload":
            # A raw byte stream is deliberately one request per connection;
            # undeclared trailing bytes must never become a pipelined request.
            self.close_connection = True
            self._handle_artifact_upload(
                host_id, fingerprint, artifact_route)
            return
        if artifact_route is not None:
            # The namespace is carried by the path, so reject a foreign
            # Convoy before offering even the small JSON capability parser.
            namespace_decision = self._authorize(
                host_id, fingerprint, convoy_id=artifact_route[1])
            if not namespace_decision.allowed:
                self._refuse_peer(namespace_decision, discard_body=True)
                return
        body = self._read_body()
        if not isinstance(body, dict):
            # Invalid/missing lengths may leave unread bytes.  Close rather
            # than attempting to parse them as the next keep-alive request.
            self.close_connection = True
            self._send(400, {"ok": False, "reason": "malformed"})
            return
        try:
            if (artifact_route is not None
                    and artifact_route[0] == "capability"):
                self._handle_artifact_capability(
                    host_id, fingerprint, artifact_route, body)
                return
            if artifact_route is not None:
                self._send(405, {"ok": False,
                                 "reason": "method_not_allowed"})
                return
            if self.path == ROUTE_ENVELOPE:
                # The PEER ORIGIN, established LOCALLY from the cert -- host
                # id from the pinned record, fingerprint recomputed from
                # the presented DER, and the SPKI so submit_envelope can
                # build the Ed25519 verifier (THE LISTENER CHOOSES THE
                # SIGNER: a peer envelope is verified against the peer's
                # pinned key, never the group PSK). Nothing here comes from
                # the request body.
                origin = {"host_id": host_id, "fingerprint": fingerprint,
                          "public_der": spki}
                with self._app.lock:
                    code, payload = self._app.submit_envelope(body, origin)
                self._send(code, payload)
                return
            if self.path == ROUTE_CANCEL:
                self._handle_peer_cancel(host_id, fingerprint, body)
                return
            if self.path == ROUTE_ACK:
                self._handle_peer_ack(host_id, fingerprint, body)
                return
            if self.path == ROUTE_CONTROLLER_HEARTBEAT:
                self._handle_controller_heartbeat(
                    host_id, fingerprint, body)
                return
            self._send(404, {"ok": False, "reason": "not_found"})
        except Exception as e:
            self._send(500, {"ok": False, "reason": "internal_error",
                             "detail": type(e).__name__})

    def _handle_artifact_upload(self, host_id, fingerprint, route):
        _action, convoy_id, _artifact_id = route
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed or not decision.may_mutate:
            self._refuse_peer(decision)
            return
        try:
            metadata = artifact_http.upload_metadata(
                self.headers, self._app.artifacts.max_artifact_bytes)
        except artifact_http.ArtifactHTTPError as e:
            self._send(e.status, e.payload(), e.headers)
            return
        if not self._app.begin_artifact_transfer():
            self._send(429, {"ok": False,
                             "reason": "artifact_transfer_busy"})
            return
        reader = artifact_http.LimitedReader(
            self.rfile, metadata["expected_size"])
        try:
            code, payload = self._app.artifact_upload(
                convoy_id, reader, metadata, peer_host_id=host_id,
                peer_fingerprint=fingerprint)
        except (OSError, ConnectionError):
            return
        finally:
            self._app.end_artifact_transfer()
        self._send(code, payload)

    def _handle_artifact_capability(self, host_id, fingerprint, route, body):
        _action, convoy_id, artifact_id = route
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            self._refuse_peer(decision)
            return
        code, payload = self._app.artifact_peer_grant(
            convoy_id, artifact_id, body, peer_host_id=host_id,
            peer_fingerprint=fingerprint)
        self._send(code, payload)

    def _handle_artifact_download(self, host_id, fingerprint, route):
        _action, convoy_id, artifact_id = route
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            self._refuse_peer(decision)
            return
        try:
            node_id = artifact_http.bounded_header(
                self.headers, artifact_http.HEADER_NODE_ID)
            controller_id = artifact_http.bounded_header(
                self.headers, artifact_http.HEADER_CONTROLLER_ID)
            token = artifact_http.capability_from_headers(self.headers)
            range_header = artifact_http.range_from_headers(self.headers)
        except artifact_http.ArtifactHTTPError as e:
            self._send(e.status, e.payload(), e.headers)
            return
        if not self._app.begin_artifact_transfer():
            self._send(429, {"ok": False,
                             "reason": "artifact_transfer_busy"})
            return
        try:
            code, payload, lease, headers = (
                self._app.artifact_open_peer_download(
                    convoy_id, artifact_id, token, node_id, controller_id,
                    range_header, peer_host_id=host_id,
                    peer_fingerprint=fingerprint))
            if lease is None:
                self._send(code, payload, headers)
            else:
                self._send_artifact_stream(code, lease, headers)
        finally:
            self._app.end_artifact_transfer()

    def _handle_peer_job(self, host_id):
        # /peer/jobs/<delivery_id>[?since=<cursor>]. Per-peer authorized:
        # a peer may read ITS OWN delivery records and no others (Gap 2).
        remainder = self.path[len(ROUTE_JOBS_PREFIX):]
        delivery_id, _, query = remainder.partition("?")
        since = None
        if query:
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "since":
                    try:
                        since = float(value)
                    except (TypeError, ValueError):
                        since = None
        code, payload = self._app.peer_job_view(host_id, delivery_id, since)
        self._send(code, payload)

    def _handle_peer_nodes(self, host_id, fingerprint):
        """Serve one namespace-bound, peer-safe node directory.

        HostApp contract: ``peer_nodes_view(host_id, convoy_id,
        authenticated_fingerprint)`` returns ``(http_status, json_object)``
        and owns any app locking it needs. The fingerprint is recomputed
        from THIS TLS connection, never re-read from the mutable peer
        record; this lets HostApp close a concurrent re-pin/revocation
        race. The handler also performs a namespace authorization before
        invoking that method.
        """
        segment = self.path[len(ROUTE_NODES_PREFIX):]
        convoy_id = _decode_convoy_segment(segment)
        if convoy_id is None:
            self._send(400, {"ok": False, "reason": "malformed",
                             "detail": "invalid Convoy namespace segment"})
            return
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            self._refuse_peer(decision)
            return
        code, payload = self._app.peer_nodes_view(
            host_id, convoy_id, fingerprint)
        if code == 200 and not _valid_nodes_payload(payload, convoy_id):
            self._send(500, {"ok": False,
                             "reason": "invalid_peer_nodes_view"})
            return
        self._send(code, payload)

    def _handle_peer_controllers(self, host_id, fingerprint):
        """Serve a namespace-bound controller view without touching TD."""
        segment = self.path[len(ROUTE_CONTROLLERS_PREFIX):]
        convoy_id = _decode_convoy_segment(segment)
        if convoy_id is None:
            self._send(400, {"ok": False, "reason": "malformed",
                             "detail": "invalid Convoy namespace segment"})
            return
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            self._refuse_peer(decision)
            return
        code, payload = self._app.peer_controllers_view(
            host_id, convoy_id, fingerprint)
        if code == 200 and not _valid_controllers_payload(
                payload, convoy_id):
            self._send(500, {"ok": False,
                             "reason": "invalid_peer_controllers_view"})
            return
        self._send(code, payload)

    def _handle_peer_cancel(self, host_id, fingerprint, body):
        """Cancel only work owned by this authenticated peer/namespace."""
        convoy_id = body.get("convoy_id")
        delivery_id = body.get("delivery_id")
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
        except identity.IdentityError:
            self._send(400, {"ok": False, "reason": "malformed",
                             "detail": "invalid Convoy namespace"})
            return
        if (not isinstance(delivery_id, str) or not delivery_id
                or len(delivery_id) > 128
                or any(not (ch.isalnum() or ch in "_-")
                       for ch in delivery_id)):
            self._send(400, {"ok": False, "reason": "malformed",
                             "detail": "invalid delivery id"})
            return
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            self._refuse_peer(decision)
            return
        code, payload = self._app.peer_cancel_job(
            host_id, convoy_id, delivery_id, fingerprint)
        self._send(code, payload)

    def _handle_peer_ack(self, host_id, fingerprint, body):
        """Acknowledge terminal evidence owned by this peer/namespace."""
        convoy_id = body.get("convoy_id")
        delivery_id = body.get("delivery_id")
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
        except identity.IdentityError:
            self._send(400, {"ok": False, "reason": "malformed",
                             "detail": "invalid Convoy namespace"})
            return
        if (not isinstance(delivery_id, str) or not delivery_id
                or len(delivery_id) > 128
                or any(not (ch.isalnum() or ch in "_-")
                       for ch in delivery_id)):
            self._send(400, {"ok": False, "reason": "malformed",
                             "detail": "invalid delivery id"})
            return
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            self._refuse_peer(decision)
            return
        code, payload = self._app.peer_acknowledge_job(
            host_id, convoy_id, delivery_id, fingerprint)
        self._send(code, payload)

    def _handle_controller_heartbeat(self, host_id, fingerprint, body):
        """Project one authenticated peer controller onto its target host."""
        convoy_id = body.get("convoy_id")
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
        except identity.IdentityError:
            self._send(400, {"ok": False, "reason": "malformed",
                             "detail": "invalid Convoy namespace"})
            return
        decision = self._authorize(host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            self._refuse_peer(decision)
            return
        code, payload = self._app.peer_heartbeat_controller(
            host_id, convoy_id, body, fingerprint)
        self._send(code, payload)


def _reject_json_constant(name):
    raise ValueError(f"{name} is not permitted in a request body")


def _decode_convoy_segment(segment):
    if (not isinstance(segment, str) or not segment or "/" in segment
            or "?" in segment or "#" in segment or "=" in segment):
        return None
    # Reject before padding/allocation. Canonical base64url for the
    # bounded UTF-8 identifier is at most ceil(bytes * 4 / 3)
    # characters without '=' padding.
    if len(segment) > ((MAX_CONVOY_ID_BYTES * 4 + 2) // 3):
        return None
    try:
        encoded = segment.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_",
                               validate=True)
        text = raw.decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError):
        return None
    if (not raw or len(raw) > MAX_CONVOY_ID_BYTES
            or any(byte < 0x20 or byte == 0x7f for byte in raw)):
        return None
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return text if canonical == segment and text == text.strip() else None


def _valid_nodes_payload(payload, convoy_id):
    """Structural namespace fence around the HostApp-provided view."""
    if (not isinstance(payload, dict) or payload.get("ok") is not True
            or payload.get("convoy_id") != convoy_id):
        return False
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > MAX_PEER_NODES:
        return False
    for node in nodes:
        if not isinstance(node, dict):
            return False
        # Rows may omit the repeated namespace to save bytes, but if they
        # name one it must be the exact authorized namespace.
        if ("convoy_id" in node
                and node.get("convoy_id") != convoy_id):
            return False
    return True


def _valid_controllers_payload(payload, convoy_id):
    if (not isinstance(payload, dict) or payload.get("ok") is not True
            or payload.get("convoy_id") != convoy_id):
        return False
    rows = payload.get("controllers")
    if not isinstance(rows, list) or len(rows) > MAX_PEER_CONTROLLERS:
        return False
    for row in rows:
        if (not isinstance(row, dict)
                or not isinstance(row.get("controller_id"), str)
                or not row.get("controller_id")):
            return False
        leases = row.get("leases", [])
        jobs = row.get("active_jobs", [])
        if (not isinstance(leases, list) or not isinstance(jobs, list)
                or len(leases) > MAX_PEER_NODES
                or len(jobs) > MAX_PEER_NODES):
            return False
    return True


# -- serving -----------------------------------------------------------

def serve_lan(app, address, port, audit_sink=None, now=None,
              max_connections=DEFAULT_MAX_CONNECTIONS,
              max_connections_per_ip=DEFAULT_MAX_CONNECTIONS_PER_IP,
              handshake_timeout=DEFAULT_HANDSHAKE_TIMEOUT_S,
              io_timeout=DEFAULT_IO_TIMEOUT_S,
              audit_capacity=20, audit_refill_per_s=0.5):
    """Bind the LAN listener on a NAMED address and return (server, port).

    A taken port raises LanBindError (reason 'lan_port_in_use') rather
    than falling back to a random one -- a peer names the port out of
    band, so a random one is unreachable by design. The caller runs
    server.serve_forever() on its own thread and stops it FIRST at
    shutdown (A-46 point 4).
    """
    now = now or app._now
    sink = audit_sink or (lambda event, detail:
                          app.db.audit("peerserver", event, detail))
    bucket = _TokenBucket(audit_capacity, audit_refill_per_s, now)
    provider = _ContextProvider(app)
    try:
        server = PeerHTTPSServer(
            (address, int(port)), PeerRequestHandler, app, provider,
            bucket, sink, max_connections=max_connections,
            max_connections_per_ip=max_connections_per_ip,
            handshake_timeout=handshake_timeout, io_timeout=io_timeout)
    except OSError as e:
        # EADDRINUSE / EADDRNOTAVAIL / an unbindable named interface.
        raise LanBindError(
            "lan_port_in_use" if _is_addr_in_use(e) else "lan_bind_failed",
            f"could not bind the LAN listener on {address}:{port} "
            f"({type(e).__name__}: {getattr(e, 'strerror', None) or e}). "
            f"The daemon keeps serving loopback; free the port or set a "
            f"different one in lan.json, then restart.")
    actual_port = server.server_address[1]
    return server, actual_port


def _is_addr_in_use(error):
    import errno
    return getattr(error, "errno", None) in (
        errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", None))
