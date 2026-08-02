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

import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import convoy_hostkeys as hostkeys
import convoy_peers as peers_mod


# -- caps (L-16 host exhaustion, S2 audit flooding) -------------------

# Same body cap as the loopback listener. A peer envelope is small; a
# pathological one is refused at the door, before the JSON parser.
MAX_PEER_BODY_BYTES = 1 * 1024 * 1024

# Concurrent peer connections. A 2-30 machine LAN never needs more, and
# an unbounded count is exactly the thread-exhaustion vector L-16 names.
# Overflow closes the raw socket at once, audited (rate-limited).
DEFAULT_MAX_CONNECTIONS = 16

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
    # NEVER reuse the address: the plan requires a taken port to REFUSE,
    # and SO_REUSEADDR on Windows would additionally let another process
    # hijack the port. Default HTTPServer sets this True; override it.
    allow_reuse_address = False

    def __init__(self, server_address, handler_class, app, context_provider,
                 audit_bucket, audit_sink,
                 max_connections=DEFAULT_MAX_CONNECTIONS,
                 handshake_timeout=DEFAULT_HANDSHAKE_TIMEOUT_S,
                 io_timeout=DEFAULT_IO_TIMEOUT_S):
        self.app = app
        self._context_provider = context_provider
        self._audit_bucket = audit_bucket
        self._audit_sink = audit_sink
        self._sem = threading.BoundedSemaphore(max_connections)
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

    def finish_request(self, request, client_address):
        # Runs in the per-connection worker thread (ThreadingMixIn).
        if not self._sem.acquire(blocking=False):
            self._audit_throttled(
                "peer_connection_overflow",
                {"source": _source_ip(client_address)})
            _quiet_close(request)
            return
        try:
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
        finally:
            self._sem.release()

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

    def _authorize(self, host_id, fingerprint):
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
            decision = app.peers.authorize_peer(host_id, fingerprint)
            if decision.allowed and not getattr(self, "_touched", False):
                app.peers.touch_seen(host_id)
                self._touched = True
        return decision

    def _refuse_peer(self, decision):
        # Every peer-authorization refusal is 403: "this host will not
        # hear you", not "you sent something malformed".
        self._send(403, {"ok": False, "reason": decision.reason,
                         "detail": decision.detail})

    # -- HTTP ----------------------------------------------------------

    def _send(self, code, payload):
        try:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
        except ValueError:
            code = 500
            body = b'{"ok": false, "reason": "internal_error"}'
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The peer hung up mid-response. Nothing to do; the worker
            # thread ends and the slot frees.
            pass

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

    def do_GET(self):
        identity = self._peer_identity()
        if identity is None:
            return              # a refusal was already sent
        host_id, _spki, fingerprint = identity
        decision = self._authorize(host_id, fingerprint)
        if not decision.allowed:
            # Blocked / pending / denylisted / killswitch -> refused on
            # EVERY read too (X0 containment), not only on /peer/envelope.
            self._refuse_peer(decision)
            return
        try:
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
            if self.path.startswith(ROUTE_JOBS_PREFIX):
                self._handle_peer_job(host_id)
                return
            self._send(404, {"ok": False, "reason": "not_found"})
        except Exception as e:
            # Last resort: every expected error is a named 4xx above. This
            # exists so an unforeseen bug cannot kill the worker thread
            # with no response (the dead-thread class the loopback panel
            # found on the auth path).
            self._send(500, {"ok": False, "reason": "internal_error",
                             "detail": type(e).__name__})

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
            self._refuse_peer(decision)
            return
        body = self._read_body()
        if not isinstance(body, dict):
            self._send(400, {"ok": False, "reason": "malformed"})
            return
        try:
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
            self._send(404, {"ok": False, "reason": "not_found"})
        except Exception as e:
            self._send(500, {"ok": False, "reason": "internal_error",
                             "detail": type(e).__name__})

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


def _reject_json_constant(name):
    raise ValueError(f"{name} is not permitted in a request body")


# -- serving -----------------------------------------------------------

def serve_lan(app, address, port, audit_sink=None, now=None,
              max_connections=DEFAULT_MAX_CONNECTIONS,
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
