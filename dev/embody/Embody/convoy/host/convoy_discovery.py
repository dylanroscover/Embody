"""Automatic trusted-LAN discovery for Convoy.

This is a small Convoy-specific multicast protocol.  It is deliberately
*not* advertised as mDNS or DNS-SD: those protocols need their complete wire
formats, probing rules, and conflict handling.  Pulling in a zeroconf stack
would also make the separately-installed host runtime larger.  Convoy instead
uses one bounded, canonical JSON datagram on an administratively-scoped IPv4
multicast group.  The semantics are DNS-SD-like (announce a service and its
endpoint), but the bytes are Convoy's own and every announcement is signed.

Security model
--------------

``Enable Convoy`` is the consent gate.  The service creates no socket and
sends no packet while the host has no enabled local Convoy namespace.  A
cryptographically valid peer is admitted by TOFU only when at least one of its
advertised namespaces intersects that enabled set.  The first
``(host_id, SPKI fingerprint)`` binding is durable in ``PeerStore``; a later
announcement with the same host id and another key is reported as a conflict
and never updates the pin.  Existing block/observe decisions are never widened
by discovery.

The UDP socket carries discovery only -- commands still use the mutually
authenticated TLS peer listener.  The advertised TLS address must exactly
equal the UDP source address, preventing an announcement from turning a host
into a connection oracle for a third address.  The receive socket binds a
concrete multicast/interface address (platform dependent), never 0.0.0.0.

The module is pure host Python: stdlib plus the project's centralized
``convoy_hostkeys`` cryptography boundary.  It imports no TouchDesigner module.
All clocks, randomness and sockets used by the state machine are injectable so
foreign-platform and failure behavior is deterministic in tests.
"""

from collections import OrderedDict
from contextlib import nullcontext
import ipaddress
import json
import math
import random
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time

import convoy_hostkeys as hostkeys
import convoy_identity as identity


DISCOVERY_KIND = "convoy.discovery.announce"
DISCOVERY_VERSION = 1
CONVOY_PROTOCOL = "convoy/1"
DISCOVERY_DOMAIN_TAG = b"convoy/1 discovery\x00"

# Administratively scoped IPv4 multicast.  This is not an IANA service
# assignment; it is intentionally configurable at construction time for a
# later product migration.  TTL 1 keeps it on the current LAN segment.
MULTICAST_GROUP = "239.255.67.86"
MULTICAST_PORT = 47601
MULTICAST_TTL = 1

ANNOUNCE_INTERVAL_S = 3.0
ANNOUNCE_JITTER = 0.20
INACTIVE_POLL_S = 1.0
MAX_STEP_SLEEP_S = 1.0
BACKOFF_INITIAL_S = 0.5
BACKOFF_MAX_S = 30.0
BACKOFF_JITTER = 0.25
CANDIDATE_TTL_S = 30.0
MAX_CLOCK_SKEW_S = 120.0

MAX_DATAGRAM_BYTES = 8192
MAX_CERTIFICATE_CHARS = 4096
MAX_CONVOY_IDS = 16
MAX_RECEIVES_PER_STEP = 32
MAX_REPLAY_NONCES = 4096
MAX_REPLAY_STREAMS = 512
MAX_RETIRED_GENERATIONS = 64
MAX_CANDIDATES = 512

_HEX128_RE = re.compile(r"^[0-9a-f]{32}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")
REALM_CANDIDATE = "candidate"
REALM_ESTABLISHED = "established"
REALM_STATES = frozenset((REALM_CANDIDATE, REALM_ESTABLISHED))

_BODY_FIELDS_LEGACY = frozenset((
    "kind", "version", "protocol", "sig_alg", "host_id",
    "fingerprint", "certificate_pem", "endpoint", "convoy_ids",
    "generation", "nonce", "timestamp_ms",
))
_BODY_FIELDS = _BODY_FIELDS_LEGACY | {"realm_states"}
_ENDPOINT_FIELDS = frozenset(("address", "port"))


class DiscoveryError(Exception):
    """A named discovery refusal safe to surface in local diagnostics."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail

    def as_dict(self):
        return {"ok": False, "reason": self.reason, "detail": self.detail}


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _valid_unicast_ipv4(value, field="address"):
    if not isinstance(value, str) or not value or value != value.strip():
        raise DiscoveryError("malformed_endpoint",
                             f"{field} must be an IPv4 literal")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise DiscoveryError("malformed_endpoint",
                             f"{field} is not an IP address")
    if (address.version != 4 or address.is_unspecified or address.is_loopback
            or address.is_link_local or address.is_multicast
            or str(address) == "255.255.255.255"):
        raise DiscoveryError(
            "unsafe_endpoint",
            f"{field} must be a concrete non-loopback IPv4 unicast address")
    return str(address)


def _clean_port(value):
    if (isinstance(value, bool) or not isinstance(value, int)
            or not (1 <= value <= 65535)):
        raise DiscoveryError("malformed_endpoint",
                             "port must be an integer from 1 through 65535")
    return value


def normalize_convoy_ids(values):
    """Return a sorted unique tuple under the canonical namespace policy."""
    if isinstance(values, (str, bytes)) or not isinstance(
            values, (list, tuple, set, frozenset)):
        raise DiscoveryError("malformed_namespaces",
                             "convoy_ids must be a collection of strings")
    if len(values) > MAX_CONVOY_IDS:
        raise DiscoveryError("malformed_namespaces",
                             f"at most {MAX_CONVOY_IDS} convoy_ids are allowed")
    clean = []
    for value in values:
        try:
            namespace = identity.normalize_convoy_id(value)
        except identity.IdentityError as exc:
            raise DiscoveryError("malformed_namespaces", exc.detail)
        if namespace not in clean:
            clean.append(namespace)
    clean.sort()
    return tuple(clean)


def normalize_realm_states(convoy_ids, values=None):
    """Canonical realm classification for an announcement.

    Missing classifications are the rolling-upgrade representation emitted
    before leaderless genesis existed and are treated as established.  A new
    announcement must classify every ID exactly once.
    """
    namespaces = normalize_convoy_ids(convoy_ids)
    if values is None:
        return {namespace: REALM_ESTABLISHED for namespace in namespaces}
    if not isinstance(values, dict) or set(values) != set(namespaces):
        raise DiscoveryError(
            "malformed_realms",
            "realm_states must classify every convoy_id exactly once")
    clean = {}
    for namespace in namespaces:
        state = values.get(namespace)
        if state not in REALM_STATES:
            raise DiscoveryError(
                "malformed_realms",
                "realm state must be candidate or established")
        clean[namespace] = state
    return clean


def _clean_source(source):
    if (not isinstance(source, (tuple, list)) or len(source) < 2
            or isinstance(source[1], bool) or not isinstance(source[1], int)):
        raise DiscoveryError("malformed_source",
                             "UDP source must be an (IPv4, port) pair")
    return _valid_unicast_ipv4(source[0], "UDP source address"), source[1]


def _certificate_public_der(certificate_pem):
    if not isinstance(certificate_pem, str):
        raise DiscoveryError("malformed_certificate",
                             "certificate_pem must be text")
    if (not certificate_pem or len(certificate_pem) > MAX_CERTIFICATE_CHARS
            or "\x00" in certificate_pem):
        raise DiscoveryError("malformed_certificate",
                             "certificate_pem is empty or exceeds its bound")
    try:
        certificate_der = ssl.PEM_cert_to_DER_cert(certificate_pem)
        # PEM_cert_to_DER_cert returns bytes on current CPython and a base64
        # string on some older builds.  Normalize both without importing a
        # second X.509 implementation.
        if isinstance(certificate_der, str):
            import base64
            certificate_der = base64.b64decode(certificate_der, validate=True)
        return hostkeys.public_der_from_certificate(certificate_der)
    except DiscoveryError:
        raise
    except Exception as exc:
        raise DiscoveryError("malformed_certificate",
                             f"certificate could not be parsed ({exc})")


def build_announcement(host_id, hostkeys_obj, listener_address,
                       listener_port, convoy_ids, generation, now=None,
                       nonce=None, realm_states=None):
    """Build one signed, bounded multicast announcement datagram."""
    if not identity.is_valid_id(host_id):
        raise DiscoveryError("malformed_host_id",
                             "host_id must be 32 lowercase hexadecimal chars")
    address = _valid_unicast_ipv4(listener_address)
    port = _clean_port(listener_port)
    namespaces = normalize_convoy_ids(convoy_ids)
    if not namespaces:
        raise DiscoveryError("inactive",
                             "an announcement needs an enabled namespace")
    realms = normalize_realm_states(namespaces, realm_states)
    if not isinstance(generation, str) or not _HEX128_RE.match(generation):
        raise DiscoveryError("malformed_generation",
                             "generation must be 32 lowercase hex chars")
    nonce = secrets.token_hex(16) if nonce is None else nonce
    if not isinstance(nonce, str) or not _HEX128_RE.match(nonce):
        raise DiscoveryError("malformed_nonce",
                             "nonce must be 32 lowercase hex chars")
    now = time.time() if now is None else float(now)
    if not math.isfinite(now) or now < 0:
        raise DiscoveryError("malformed_timestamp", repr(now))
    certificate_pem = getattr(hostkeys_obj, "certificate_pem", None)
    fingerprint = getattr(hostkeys_obj, "fingerprint", None)
    if not hostkeys.is_valid_fingerprint(fingerprint):
        raise DiscoveryError("malformed_identity",
                             "host key has no valid SPKI fingerprint")
    # Prove the certificate and fingerprint agree before publishing.  A
    # degraded identity with no usable certificate may still sign envelopes,
    # but cannot advertise a mutual-TLS listener honestly.
    public_der = _certificate_public_der(certificate_pem)
    if hostkeys.fingerprint(public_der) != fingerprint:
        raise DiscoveryError("identity_mismatch",
                             "host certificate does not match its fingerprint")

    body = {
        "kind": DISCOVERY_KIND,
        "version": DISCOVERY_VERSION,
        "protocol": CONVOY_PROTOCOL,
        "sig_alg": hostkeys.ALG_ED25519_V1,
        "host_id": host_id,
        "fingerprint": fingerprint,
        "certificate_pem": certificate_pem,
        "endpoint": {"address": address, "port": port},
        "convoy_ids": list(namespaces),
        "realm_states": realms,
        "generation": generation,
        "nonce": nonce,
        "timestamp_ms": int(now * 1000),
    }
    body_bytes = _canonical_json(body)
    signature = hostkeys_obj.signer().sign(DISCOVERY_DOMAIN_TAG + body_bytes)
    packet = _canonical_json({"body": body, "signature": signature})
    if len(packet) > MAX_DATAGRAM_BYTES:
        raise DiscoveryError(
            "announcement_too_large",
            f"announcement is {len(packet)} bytes; max is {MAX_DATAGRAM_BYTES}")
    return packet


class ReplayCache:
    """Bounded replay/generation memory for verified announcements.

    Streams are scoped by both host id and fingerprint.  A stranger claiming
    a known host id with another valid key therefore cannot poison the pinned
    peer's generation state; the durable PeerStore pin rejects that stranger
    later.  A generation change retires the prior generation until the replay
    window expires, preventing delayed datagrams from flipping a live stream
    backwards.
    """

    def __init__(self, max_nonces=MAX_REPLAY_NONCES,
                 max_streams=MAX_REPLAY_STREAMS,
                 skew_s=MAX_CLOCK_SKEW_S):
        self.max_nonces = int(max_nonces)
        self.max_streams = int(max_streams)
        self.skew_s = float(skew_s)
        self._nonces = OrderedDict()
        self._streams = OrderedDict()

    def accept(self, host_id, fingerprint, generation, nonce, timestamp_ms,
               now):
        now = float(now)
        self._prune(now)
        packet_time = timestamp_ms / 1000.0
        if abs(packet_time - now) > self.skew_s:
            direction = "future" if packet_time > now else "stale"
            raise DiscoveryError(
                "timestamp_out_of_window",
                f"announcement is {direction}; allowed skew is {self.skew_s}s")

        nonce_key = (host_id, fingerprint, generation, nonce)
        if nonce_key in self._nonces:
            raise DiscoveryError("replay", "announcement nonce was already seen")

        stream_key = (host_id, fingerprint)
        stream = self._streams.get(stream_key)
        if stream is None:
            stream = {"generation": generation, "latest_ms": timestamp_ms,
                      "retired": set(), "expires": now + self.skew_s}
        elif generation in stream["retired"]:
            raise DiscoveryError("retired_generation",
                                 "announcement belongs to an older host run")
        elif generation != stream["generation"]:
            if timestamp_ms < stream["latest_ms"]:
                raise DiscoveryError(
                    "stale_generation",
                    "a new generation cannot move the stream clock backward")
            if len(stream["retired"]) >= MAX_RETIRED_GENERATIONS:
                # A legitimate host restarts occasionally, not 65 times in a
                # two-minute replay window.  Fail the noisy stream closed
                # instead of letting an authenticated sender grow memory or
                # evict old generations until replaying one works again.
                raise DiscoveryError(
                    "generation_churn",
                    "too many host generations occurred in one replay window")
            stream["retired"].add(stream["generation"])
            stream["generation"] = generation
        stream["latest_ms"] = max(stream["latest_ms"], timestamp_ms)
        stream["expires"] = now + self.skew_s
        self._streams[stream_key] = stream
        self._streams.move_to_end(stream_key)

        self._nonces[nonce_key] = now + self.skew_s
        self._nonces.move_to_end(nonce_key)
        while len(self._nonces) > self.max_nonces:
            self._nonces.popitem(last=False)
        while len(self._streams) > self.max_streams:
            self._streams.popitem(last=False)

    def _prune(self, now):
        for key, expiry in list(self._nonces.items()):
            if expiry >= now:
                continue
            del self._nonces[key]
        for key, stream in list(self._streams.items()):
            if stream["expires"] >= now:
                continue
            del self._streams[key]


def parse_announcement(packet, source, now=None, replay_cache=None):
    """Authenticate and return a detached announcement body.

    Validation order is cheap bounds/shape/source/time, then X.509 and
    signature verification.  No field from the datagram is trusted as an
    endpoint or pin before all checks complete.
    """
    source_address, _source_port = _clean_source(source)
    if not isinstance(packet, (bytes, bytearray)):
        raise DiscoveryError("malformed_packet", "datagram must be bytes")
    packet = bytes(packet)
    if not packet or len(packet) > MAX_DATAGRAM_BYTES:
        raise DiscoveryError(
            "packet_too_large",
            f"datagram must be 1..{MAX_DATAGRAM_BYTES} bytes")
    try:
        decoded = json.loads(packet.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DiscoveryError("malformed_packet", f"invalid JSON ({exc})")
    if not isinstance(decoded, dict) or set(decoded) != {"body", "signature"}:
        raise DiscoveryError("malformed_packet",
                             "packet must contain exactly body and signature")
    body = decoded["body"]
    signature = decoded["signature"]
    body_fields = set(body) if isinstance(body, dict) else set()
    if (not isinstance(body, dict)
            or body_fields not in (_BODY_FIELDS, _BODY_FIELDS_LEGACY)):
        raise DiscoveryError("malformed_body",
                             "announcement body has missing or unknown fields")
    if not isinstance(signature, str) or not _SIGNATURE_RE.match(signature):
        raise DiscoveryError("malformed_signature",
                             "signature must be 128 lowercase hex chars")
    if (body["kind"] != DISCOVERY_KIND
            or body["version"] != DISCOVERY_VERSION
            or isinstance(body["version"], bool)
            or body["protocol"] != CONVOY_PROTOCOL
            or body["sig_alg"] != hostkeys.ALG_ED25519_V1):
        raise DiscoveryError("unsupported_protocol",
                             "announcement protocol/version/algorithm differs")
    host_id = body["host_id"]
    if not identity.is_valid_id(host_id):
        raise DiscoveryError("malformed_host_id",
                             "host_id must be 32 lowercase hexadecimal chars")
    fingerprint = body["fingerprint"]
    if not hostkeys.is_valid_fingerprint(fingerprint):
        raise DiscoveryError("malformed_fingerprint",
                             "fingerprint is not canonical cvfp1 SPKI text")
    endpoint = body["endpoint"]
    if not isinstance(endpoint, dict) or set(endpoint) != _ENDPOINT_FIELDS:
        raise DiscoveryError("malformed_endpoint",
                             "endpoint must contain exactly address and port")
    endpoint_address = _valid_unicast_ipv4(endpoint["address"])
    endpoint_port = _clean_port(endpoint["port"])
    if endpoint_address != source_address:
        raise DiscoveryError(
            "source_mismatch",
            f"advertised {endpoint_address}, received from {source_address}")
    namespaces = normalize_convoy_ids(body["convoy_ids"])
    # Builders emit sorted unique IDs.  Requiring that canonical form keeps
    # every implementation's intersection and signed bytes unambiguous.
    if list(namespaces) != body["convoy_ids"] or not namespaces:
        raise DiscoveryError("malformed_namespaces",
                             "convoy_ids must be nonempty, sorted and unique")
    realms = normalize_realm_states(
        namespaces, body.get("realm_states") if "realm_states" in body else None)
    generation = body["generation"]
    nonce = body["nonce"]
    if not isinstance(generation, str) or not _HEX128_RE.match(generation):
        raise DiscoveryError("malformed_generation",
                             "generation must be 32 lowercase hex chars")
    if not isinstance(nonce, str) or not _HEX128_RE.match(nonce):
        raise DiscoveryError("malformed_nonce",
                             "nonce must be 32 lowercase hex chars")
    timestamp_ms = body["timestamp_ms"]
    if (isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0):
        raise DiscoveryError("malformed_timestamp",
                             "timestamp_ms must be a nonnegative integer")
    now = time.time() if now is None else float(now)
    if not math.isfinite(now) or now < 0:
        raise DiscoveryError("malformed_timestamp", repr(now))
    if abs((timestamp_ms / 1000.0) - now) > MAX_CLOCK_SKEW_S:
        raise DiscoveryError("timestamp_out_of_window",
                             f"allowed skew is {MAX_CLOCK_SKEW_S}s")

    public_der = _certificate_public_der(body["certificate_pem"])
    computed_fingerprint = hostkeys.fingerprint(public_der)
    if computed_fingerprint != fingerprint:
        raise DiscoveryError(
            "fingerprint_mismatch",
            "certificate SPKI does not match the advertised fingerprint")
    verifier = hostkeys.verifier_from_public_der(public_der)
    body_bytes = _canonical_json(body)
    if not verifier.verify(DISCOVERY_DOMAIN_TAG + body_bytes, signature):
        raise DiscoveryError("bad_signature",
                             "announcement signature did not verify")
    if replay_cache is not None:
        replay_cache.accept(host_id, fingerprint, generation, nonce,
                            timestamp_ms, now)

    clean = dict(body)
    clean["endpoint"] = {"address": endpoint_address, "port": endpoint_port}
    clean["convoy_ids"] = list(namespaces)
    clean["realm_states"] = realms
    return clean


def _candidate_from(announcement, intersection, status, detail, now):
    """A public/status-safe candidate -- certificate bytes stay private."""
    return {
        "host_id": announcement["host_id"],
        "fingerprint": announcement["fingerprint"],
        "address": announcement["endpoint"]["address"],
        "port": announcement["endpoint"]["port"],
        "convoy_ids": list(announcement["convoy_ids"]),
        "realm_states": dict(announcement["realm_states"]),
        "intersection": list(intersection),
        "generation": announcement["generation"],
        "status": status,
        "detail": detail,
        "last_seen_unix": float(now),
    }


def apply_tofu(peer_store, announcement, intersection, now=None):
    """Apply a verified discovery result to ``PeerStore``.

    The caller must serialize this whole function with every other PeerStore
    operation (PeerStore is intentionally not internally locked).  Passing the
    HostApp's existing RLock as ``DiscoveryCoordinator.admission_lock`` meets
    that contract.
    """
    intersection = normalize_convoy_ids(intersection)
    if not intersection:
        return "namespace_conflict", "no enabled local namespace intersects"
    now = time.time() if now is None else float(now)
    host_id = announcement["host_id"]
    fingerprint = announcement["fingerprint"]

    # An emergency stop cannot be used as a quiet membership-building window.
    # Once lifted, a fresh announcement can perform TOFU visibly.
    killswitch = peer_store.killswitch()
    if killswitch.get("engaged"):
        return "killswitch", "LAN killswitch is engaged; peer was not admitted"

    # The human-owned denylist is authoritative even before a peer has a
    # record.  authorize_peer deliberately checks it first and returns a
    # peer_blocked decision for both explicit entries and fail-closed parse
    # errors.  Other refusals (not-yet-admitted, namespace absent) are exactly
    # what first-use TOFU is here to resolve.
    decision = peer_store.authorize_peer(
        host_id, fingerprint, convoy_id=intersection[0])
    if not decision.allowed and decision.reason == "peer_blocked":
        return "blocked", "denylist or existing block was preserved"

    existing = peer_store.get(host_id)
    if existing is not None and existing.get("fingerprint") != fingerprint:
        return ("pin_mismatch",
                "host_id is already pinned to another SPKI fingerprint")
    if existing is None:
        clash = peer_store.find_by_fingerprint(fingerprint)
        if clash is not None and clash.get("host_id") != host_id:
            return ("fingerprint_conflict",
                    "SPKI fingerprint is already pinned to another host_id")
    elif existing.get("state") == "blocked":
        return "blocked", "existing operator block was preserved"
    elif existing.get("state") == "observe_only":
        return "observe_only", "existing observe-only narrowing was preserved"
    elif existing.get("state") not in ("pending", "admitted"):
        return "refused", "existing peer state was not safe to widen"

    endpoint = (f"{announcement['endpoint']['address']}:"
                f"{announcement['endpoint']['port']}")
    namespaces = set(intersection)
    endpoints = [endpoint]
    if existing is not None:
        namespaces.update(existing.get("convoy_ids") or ())
        # Refresh the discovered address first while preserving manual
        # fallbacks.  PeerStore currently caps endpoints at 16.
        old_endpoints = list(existing.get("endpoints") or ())
        if endpoint not in old_endpoints and len(old_endpoints) >= 16:
            return ("endpoint_conflict",
                    "peer endpoint list is full; existing routes were preserved")
        endpoints.extend(item for item in old_endpoints if item != endpoint)
    desired_namespaces = sorted(namespaces)
    if (existing is not None and existing.get("state") == "admitted"
            and endpoints == list(existing.get("endpoints") or ())
            and desired_namespaces == sorted(existing.get("convoy_ids") or ())
            and (existing.get("cert_pem") or "").strip()
            == announcement["certificate_pem"].strip()):
        # Announcements are heartbeats.  Rewriting peers.json and appending an
        # audit event every three seconds per peer would turn discovery into
        # disk churn; durable state changes only when the pin's routing facts
        # actually move.  Candidate last_seen remains the live heartbeat.
        return "admitted", "existing trusted-LAN pin is current"
    record = peer_store.admit(
        host_id, fingerprint, admitted_via="lan_tofu",
        endpoints=endpoints, convoy_ids=desired_namespaces,
        cert_pem=announcement["certificate_pem"],
        clock_offset_s=(announcement["timestamp_ms"] / 1000.0) - now)
    if record.get("fingerprint") != fingerprint:
        # Defensive assertion at a trust boundary: a callback/store wrapper
        # that did not honor its contract must not be reported as admitted.
        raise DiscoveryError("admission_failed",
                             "peer store returned a different fingerprint")
    return "admitted", "trusted-LAN TOFU admission is active"


class DiscoveryCoordinator:
    """Validated datagram -> namespace decision -> durable TOFU admission."""

    def __init__(self, host_id, peer_store, active_convoy_ids,
                 admission_lock=None, now=None, replay_cache=None,
                 candidate_ttl_s=CANDIDATE_TTL_S,
                 active_realm_states=None, realm_observer=None):
        if not identity.is_valid_id(host_id):
            raise DiscoveryError("malformed_host_id", repr(host_id))
        if not callable(active_convoy_ids):
            raise DiscoveryError("malformed_callback",
                                 "active_convoy_ids must be callable")
        self.host_id = host_id
        self.peer_store = peer_store
        self._active_convoy_ids = active_convoy_ids
        if active_realm_states is not None and not callable(
                active_realm_states):
            raise DiscoveryError("malformed_callback",
                                 "active_realm_states must be callable")
        if realm_observer is not None and not callable(realm_observer):
            raise DiscoveryError("malformed_callback",
                                 "realm_observer must be callable")
        self._active_realm_states = active_realm_states
        self._realm_observer = realm_observer
        self._admission_lock = admission_lock
        self._now = now or time.time
        self.replay_cache = replay_cache or ReplayCache()
        self.candidate_ttl_s = float(candidate_ttl_s)
        self._candidates = OrderedDict()
        self._candidate_lock = threading.Lock()

    def active_namespaces(self):
        return tuple(self.active_realms())

    def active_realms(self):
        if self._active_realm_states is not None:
            values = self._active_realm_states()
            if not isinstance(values, dict):
                raise DiscoveryError(
                    "malformed_realms",
                    "active_realm_states callback must return an object")
            # One callback snapshot supplies both keys and values so a
            # candidate adoption cannot race two independent callbacks and
            # briefly pair the old ID with the new realm state.
            namespaces = normalize_convoy_ids(tuple(values))
            return normalize_realm_states(namespaces, values)
        namespaces = normalize_convoy_ids(self._active_convoy_ids() or ())
        return normalize_realm_states(namespaces)

    def handle_datagram(self, packet, source):
        now = float(self._now())
        # Enable remains the exposure/CPU gate. An entirely unbound host does
        # not own discovery sockets in v1, so malformed datagrams must not be
        # parsed merely because a caller invoked this method directly.
        try:
            if not self.active_realms():
                return {"ok": False, "status": "inactive",
                        "detail": "no enabled local Convoy namespace"}
        except Exception as exc:
            return {"ok": False, "status": "error",
                    "detail": f"active realm lookup failed "
                              f"({type(exc).__name__}: {exc})"}
        try:
            announcement = parse_announcement(
                packet, source, now=now, replay_cache=self.replay_cache)
        except DiscoveryError as exc:
            return {"ok": False, "status": "rejected",
                    "reason": exc.reason, "detail": exc.detail}
        if announcement["host_id"] == self.host_id:
            return {"ok": True, "status": "self", "detail": "ignored self"}

        # The realm observer runs BEFORE admission, so it must be gated on the
        # SAME trust boundary admission uses. Otherwise one unauthenticated
        # datagram from a denylisted sender -- or any sender while the
        # emergency stop is engaged -- can drive this host's realm state
        # machine and push an unbound/candidate host into a permanent CONFLICT
        # (review 2026-08-02). A denylisted or killswitched sender is dropped
        # before the observer sees it.
        try:
            if self.peer_store.killswitch().get("engaged"):
                return {"ok": False, "status": "killswitch",
                        "detail": "LAN killswitch is engaged"}
            convoy_ids = announcement.get("convoy_ids") or ()
            block = self.peer_store.authorize_peer(
                announcement["host_id"], announcement["fingerprint"],
                convoy_id=(convoy_ids[0] if convoy_ids else None))
            if not block.allowed and block.reason == "peer_blocked":
                return {"ok": False, "status": "blocked",
                        "detail": "sender is on the denylist"}
        except Exception as exc:
            # Fail CLOSED: never run the realm observer on a datagram whose
            # trust we could not establish.
            return {"ok": False, "status": "error",
                    "detail": f"trust precheck failed "
                              f"({type(exc).__name__}: {exc})"}

        observer_error = None
        if self._realm_observer is not None:
            try:
                self._realm_observer(dict(announcement))
            except Exception as exc:
                observer_error = (
                    f"realm reconciliation failed ({type(exc).__name__}: "
                    f"{exc})")
        try:
            active_realms = self.active_realms()
        except Exception as exc:
            active_realms = {}
            observer_error = observer_error or (
                f"active realm lookup failed ({type(exc).__name__}: {exc})")
        active = tuple(active_realms)
        if not active:
            status = "error" if observer_error else "inactive"
            return {"ok": False, "status": status,
                    "detail": observer_error or
                    "no enabled local Convoy namespace"}

        intersection = tuple(sorted(
            set(active).intersection(announcement["convoy_ids"])))
        established_intersection = tuple(
            namespace for namespace in intersection
            if active_realms.get(namespace) == REALM_ESTABLISHED
            and announcement["realm_states"].get(namespace)
            == REALM_ESTABLISHED)
        if observer_error:
            status, detail = "error", observer_error
        elif not intersection:
            status, detail = ("namespace_conflict",
                              "peer advertises a different Convoy namespace")
        elif not established_intersection:
            status, detail = (
                "genesis_pending",
                "matching automatic Convoy realm is not established yet")
        else:
            lock = (self._admission_lock if self._admission_lock is not None
                    else nullcontext())
            try:
                with lock:
                    status, detail = apply_tofu(
                        self.peer_store, announcement,
                        established_intersection, now=now)
            except Exception as exc:
                # Persistence/read failures are local diagnostics, never a
                # reason to accept an unpinned in-memory peer.
                status, detail = ("error",
                                  f"peer admission failed ({type(exc).__name__}: "
                                  f"{exc})")
        candidate = _candidate_from(announcement, intersection, status,
                                    detail, now)
        key = (candidate["host_id"], candidate["fingerprint"])
        with self._candidate_lock:
            self._prune_candidates_locked(now)
            self._candidates[key] = candidate
            self._candidates.move_to_end(key)
            while len(self._candidates) > MAX_CANDIDATES:
                self._candidates.popitem(last=False)
        return {"ok": status == "admitted", **candidate}

    def candidates(self):
        now = float(self._now())
        with self._candidate_lock:
            self._prune_candidates_locked(now)
            return [dict(self._candidates[key]) for key in
                    sorted(self._candidates)]

    def _prune_candidates_locked(self, now):
        for key, candidate in list(self._candidates.items()):
            if now - candidate["last_seen_unix"] > self.candidate_ttl_s:
                del self._candidates[key]


class DiscoveryService:
    """Low-overhead multicast lifecycle with injectable deterministic steps.

    ``step()`` is the testable state machine.  ``start()`` merely runs it in a
    tiny daemon thread for the dedicated Convoy host process; no TD object is
    ever touched.  Call ``wake()`` whenever an Enable or listener state changes
    so socket closure/activation is immediate instead of waiting at most one
    second.
    """

    def __init__(self, coordinator, hostkeys_obj, listener_endpoint,
                 local_address, socket_factory=None, wall_clock=None,
                 monotonic=None, random_float=None, nonce_factory=None,
                 generation=None, multicast_group=MULTICAST_GROUP,
                 multicast_port=MULTICAST_PORT, platform_name=None):
        if not isinstance(coordinator, DiscoveryCoordinator):
            raise DiscoveryError("malformed_coordinator",
                                 "coordinator must be DiscoveryCoordinator")
        if not callable(listener_endpoint):
            raise DiscoveryError("malformed_callback",
                                 "listener_endpoint must be callable")
        self.coordinator = coordinator
        self.hostkeys_obj = hostkeys_obj
        self.listener_endpoint = listener_endpoint
        self.local_address = _valid_unicast_ipv4(local_address,
                                                  "local discovery address")
        self.socket_factory = socket_factory or socket.socket
        self.wall_clock = wall_clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.random_float = random_float or random.random
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
        self.generation = generation or secrets.token_hex(16)
        if not _HEX128_RE.match(self.generation):
            raise DiscoveryError("malformed_generation", repr(self.generation))
        self.multicast_group = str(ipaddress.ip_address(multicast_group))
        if not ipaddress.ip_address(self.multicast_group).is_multicast:
            raise DiscoveryError("malformed_multicast_group",
                                 "group must be a multicast IPv4 literal")
        self.multicast_port = _clean_port(multicast_port)
        self.platform_name = platform_name or sys.platform

        self._receiver = None
        self._sender = None
        self._next_announce = 0.0
        self._retry_at = 0.0
        self._failures = 0
        self._last_error = None
        self._last_announcement_unix = None
        self._last_result = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    @property
    def active(self):
        return self._receiver is not None and self._sender is not None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="ConvoyDiscovery", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=2.0):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        self._close_sockets()
        return thread is None or not thread.is_alive()

    def wake(self):
        self._wake.set()

    def replace_identity(self, hostkeys_obj, generation=None):
        """Publish a rotated/reloaded host key on the next loop turn.

        HostApp's identity route is live (it does not require a daemon
        restart), so discovery cannot retain the constructor's key object.
        Validate the public half before swapping; the private signing ability
        is exercised by the next announcement and enters ordinary backoff on
        failure.  A fresh generation makes the change explicit to replay
        caches.  Assignment is intentionally tiny/atomic under CPython; an
        already-running send may finish with the old, still-valid identity,
        after which the immediate wake publishes the new one.
        """
        fingerprint = getattr(hostkeys_obj, "fingerprint", None)
        certificate_pem = getattr(hostkeys_obj, "certificate_pem", None)
        if not hostkeys.is_valid_fingerprint(fingerprint):
            raise DiscoveryError("malformed_identity",
                                 "replacement has no valid fingerprint")
        public_der = _certificate_public_der(certificate_pem)
        if hostkeys.fingerprint(public_der) != fingerprint:
            raise DiscoveryError("identity_mismatch",
                                 "replacement certificate and pin differ")
        generation = generation or secrets.token_hex(16)
        if not isinstance(generation, str) or not _HEX128_RE.match(generation):
            raise DiscoveryError("malformed_generation", repr(generation))
        self.hostkeys_obj = hostkeys_obj
        self.generation = generation
        self._next_announce = 0.0
        self.wake()
        return generation

    def step(self):
        """Run one nonblocking state-machine turn; return next delay."""
        mono = float(self.monotonic())
        try:
            active_realms = self.coordinator.active_realms()
            active_namespaces = tuple(active_realms)
        except Exception as exc:
            self._deactivate()
            self._last_error = f"active namespace lookup failed ({exc})"
            return INACTIVE_POLL_S
        if not active_namespaces:
            self._deactivate()
            self._failures = 0
            self._retry_at = 0.0
            self._last_error = None
            return INACTIVE_POLL_S

        # The emergency stop must silence THIS host on the wire, not only refuse
        # inbound admission: continuing to broadcast our host_id, SPKI
        # fingerprint, certificate, endpoint and realm every few seconds while
        # killswitched defeats the stop (review 2026-08-02). Deactivate
        # discovery entirely until it is lifted; a transient read error fails
        # open so a flaky peer store cannot silently disable normal discovery.
        try:
            if self.coordinator.peer_store.killswitch().get("engaged"):
                self._deactivate()
                self._failures = 0
                self._retry_at = 0.0
                self._last_error = "LAN killswitch engaged"
                return INACTIVE_POLL_S
        except Exception:
            pass

        try:
            endpoint = self.listener_endpoint()
            if (not isinstance(endpoint, (tuple, list)) or len(endpoint) != 2):
                raise DiscoveryError("listener_unavailable",
                                     "listener endpoint is not (address, port)")
            listener_address = _valid_unicast_ipv4(endpoint[0])
            listener_port = _clean_port(endpoint[1])
            if listener_address != self.local_address:
                raise DiscoveryError(
                    "listener_address_mismatch",
                    "TLS listener and discovery interface must be identical")
        except Exception as exc:
            self._deactivate()
            self._last_error = str(exc)
            return INACTIVE_POLL_S

        if not self.active:
            if mono < self._retry_at:
                return min(MAX_STEP_SLEEP_S, self._retry_at - mono)
            try:
                self._open_sockets()
            except Exception as exc:
                self._deactivate()
                self._schedule_retry(mono, exc)
                return min(MAX_STEP_SLEEP_S,
                           max(0.0, self._retry_at - mono))
            self._next_announce = mono

        if mono >= self._next_announce:
            try:
                packet = build_announcement(
                    self.coordinator.host_id, self.hostkeys_obj,
                    listener_address, listener_port, active_namespaces,
                    self.generation, now=self.wall_clock(),
                    nonce=self.nonce_factory(), realm_states=active_realms)
                self._sender.sendto(
                    packet, (self.multicast_group, self.multicast_port))
                self._last_announcement_unix = float(self.wall_clock())
                self._next_announce = mono + self._jittered(
                    ANNOUNCE_INTERVAL_S, ANNOUNCE_JITTER)
            except Exception as exc:
                self._deactivate()
                self._schedule_retry(mono, exc)
                return min(MAX_STEP_SLEEP_S,
                           max(0.0, self._retry_at - mono))

        try:
            for _ in range(MAX_RECEIVES_PER_STEP):
                try:
                    packet, source = self._receiver.recvfrom(
                        MAX_DATAGRAM_BYTES + 1)
                except (BlockingIOError, socket.timeout):
                    break
                self._last_result = self.coordinator.handle_datagram(
                    packet, source)
        except Exception as exc:
            self._deactivate()
            self._schedule_retry(mono, exc)
            return min(MAX_STEP_SLEEP_S, max(0.0, self._retry_at - mono))

        # Reset the exponential sequence only after a complete successful
        # announce/receive turn.  Socket creation can succeed while every send
        # fails (an interface disappearing is the common case); resetting at
        # open would hammer that failure forever at the minimum delay.
        self._failures = 0
        self._retry_at = 0.0
        self._last_error = None
        return min(MAX_STEP_SLEEP_S,
                   max(0.0, self._next_announce - mono))

    def status(self):
        return {
            "active": self.active,
            "generation": self.generation,
            "local_address": self.local_address,
            "multicast_group": self.multicast_group,
            "multicast_port": self.multicast_port,
            "failures": self._failures,
            "retry_at_monotonic": self._retry_at or None,
            "last_error": self._last_error,
            "last_announcement_unix": self._last_announcement_unix,
            "last_result": dict(self._last_result) if self._last_result else None,
            "candidates": self.coordinator.candidates(),
        }

    def _open_sockets(self):
        receiver = self.socket_factory(socket.AF_INET, socket.SOCK_DGRAM,
                                       socket.IPPROTO_UDP)
        sender = None
        try:
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if self.platform_name != "win32" and hasattr(socket, "SO_REUSEPORT"):
                try:
                    receiver.setsockopt(socket.SOL_SOCKET,
                                        socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            # Windows accepts multicast on the concrete interface address;
            # POSIX stacks demultiplex reliably when bound to the concrete
            # group address.  Neither branch binds INADDR_ANY / 0.0.0.0.
            receive_bind = (self.local_address if self.platform_name == "win32"
                            else self.multicast_group)
            receiver.bind((receive_bind, self.multicast_port))
            membership = struct.pack("=4s4s",
                                     socket.inet_aton(self.multicast_group),
                                     socket.inet_aton(self.local_address))
            receiver.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                membership)
            receiver.setblocking(False)

            sender = self.socket_factory(socket.AF_INET, socket.SOCK_DGRAM,
                                         socket.IPPROTO_UDP)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                              MULTICAST_TTL)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                              socket.inet_aton(self.local_address))
            sender.bind((self.local_address, 0))
            self._receiver = receiver
            self._sender = sender
        except Exception:
            try:
                receiver.close()
            except Exception:
                pass
            if sender is not None:
                try:
                    sender.close()
                except Exception:
                    pass
            raise

    def _deactivate(self):
        self._close_sockets()
        self._next_announce = 0.0

    def _close_sockets(self):
        receiver, sender = self._receiver, self._sender
        self._receiver = None
        self._sender = None
        for sock in (receiver, sender):
            if sock is None:
                continue
            try:
                sock.close()
            except Exception:
                pass

    def _schedule_retry(self, mono, exc):
        self._failures += 1
        base = min(BACKOFF_MAX_S,
                   BACKOFF_INITIAL_S * (2 ** min(self._failures - 1, 16)))
        delay = self._jittered(base, BACKOFF_JITTER)
        self._retry_at = mono + delay
        self._last_error = f"{type(exc).__name__}: {exc}"

    def _jittered(self, base, proportion):
        sample = float(self.random_float())
        if not math.isfinite(sample):
            sample = 0.5
        sample = min(1.0, max(0.0, sample))
        return base * (1.0 - proportion + (2.0 * proportion * sample))

    def _run(self):
        while not self._stop.is_set():
            try:
                delay = self.step()
            except Exception as exc:
                # A callback bug must not turn the host app into a crash loop.
                self._deactivate()
                self._last_error = f"discovery loop failed ({exc})"
                delay = INACTIVE_POLL_S
            self._wake.wait(min(MAX_STEP_SLEEP_S, max(0.01, float(delay))))
            self._wake.clear()
        self._close_sockets()
