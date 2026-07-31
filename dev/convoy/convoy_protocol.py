"""Convoy/1 request envelope -- production module (Phase 1).

Pure stdlib, no TD, no network. The tracer (dev/convoy_tracer) proved
the SHAPE end to end across a real LAN; this is its production form, and
it corrects the tracer's field model to match A-21 exactly.

THE A-21 MODEL (who signs what). A HOST signs -- it owns the signing key
-- on behalf of a CONTROLLER, targeting a NODE:

  origin_host_id / source_host_id -- the HOST that originated / last
    relayed the envelope. The signature is verified against the pinned
    fingerprint for origin_host_id (Phase 3); a hop is "a host app
    re-emitting the envelope", and v1 enforces origin == source.
  controller_id -- which controller (a driving session) originated the
    request. Signed, so a relay cannot rewrite who asked.
  target_node_id -- which node (project-on-a-machine) it is for.

The canonical signed subset is A-21's, verbatim: convoy_id, request_id,
idempotency_key, controller_id, origin_host_id, target_node_id,
operation, hash(arguments), deadline -- plus source_host_id, hop_limit,
expected_runtime_id, and the sig_alg tag, which are also decision fields
and so must be signed too.

Signing is PLUGGABLE (A-21's Phase 3 target). Phase 1 ships HMAC over a
pre-shared key: this is GROUP / MEMBERSHIP authentication and message
integrity within a trusted LAN (A-8), NOT per-node identity -- any PSK
holder can set any origin_host_id and still sign, so a signed
origin_host_id is authentic only against outsiders, not against a
malicious member. Phase 3 swaps in a per-host keypair pinned by
fingerprint, which is what makes origin_host_id genuinely authoritative;
the Signer interface is the seam and no caller changes.

job_id is NOT in the request (A-22): it is minted target-side at
persist-before-acknowledge and returned in the ack. Deadlines are
ABSOLUTE unix times computed once at the origin, so a multi-hop path
cannot silently multiply a per-hop timeout.
"""

import hashlib
import hmac
import json
import time
import uuid

PROTOCOL = "convoy/1"

# Signature algorithm tags. The tag is INSIDE the signed payload, so a
# verifier keyed for one scheme cannot be fooled into accepting another.
ALG_HMAC_SHA256 = "hmac-sha256"

# Fields covered by the signature, in this exact order. A-21's canonical
# subset plus the other fields that decide what runs where. Explicit
# tuple, never sorted(): a field cannot silently leave the covered set by
# being renamed.
_SIGNED_FIELDS = (
    "protocol",
    "convoy_id",
    "request_id",
    "idempotency_key",
    "controller_id",
    "origin_host_id",
    "source_host_id",
    "target_node_id",
    "operation",
    "arguments_sha256",
    "expected_runtime_id",
    "deadline_unix",
    "hop_limit",
    "sig_alg",
)


# ---------------------------------------------------------------------
# Signers -- the pluggable seam. Phase 3 adds a keypair signer here.
# ---------------------------------------------------------------------

class Signer:
    """Signs and verifies the canonical payload. alg tags the scheme."""

    alg = None

    def sign(self, payload_bytes):
        raise NotImplementedError

    def verify(self, payload_bytes, signature_hex):
        raise NotImplementedError


class HmacSigner(Signer):
    """Pre-shared-key HMAC-SHA256: GROUP authentication + integrity on a
    trusted LAN (A-8), NOT per-node identity. Phase 3 replaces this with
    per-host keys pinned by fingerprint, without touching callers."""

    alg = ALG_HMAC_SHA256

    def __init__(self, psk):
        if not psk:
            raise ValueError("HmacSigner requires a pre-shared key")
        self._key = psk.encode("utf-8") if isinstance(psk, str) else psk

    def sign(self, payload_bytes):
        return hmac.new(self._key, payload_bytes, hashlib.sha256).hexdigest()

    def verify(self, payload_bytes, signature_hex):
        expected = self.sign(payload_bytes)
        # constant-time; also rejects non-ASCII/None rather than leaking
        # via a fast path (the auth-crash class an earlier panel found).
        try:
            provided = (signature_hex or "").encode("ascii")
        except (AttributeError, UnicodeEncodeError):
            return False
        return hmac.compare_digest(expected.encode("ascii"), provided)


# ---------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------

def canonical_arguments_digest(arguments):
    """SHA-256 over canonicalized arguments. Sorted keys + compact
    separators so two structurally identical argument dicts always digest
    identically regardless of construction order."""
    blob = json.dumps(arguments or {}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _signing_payload(envelope):
    subset = {k: envelope.get(k) for k in _SIGNED_FIELDS}
    return json.dumps(subset, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


# ---------------------------------------------------------------------
# Build / verify
# ---------------------------------------------------------------------

class EnvelopeRejected(Exception):
    """Structured refusal. `reason` is a stable machine-readable code."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def build_envelope(convoy_id, origin_host_id, controller_id, target_node_id,
                   operation, signer, arguments=None, timeout_s=30.0,
                   expected_runtime_id=None, idempotency_key=None,
                   hop_limit=2, source_host_id=None, now=None):
    """Construct a signed request envelope (A-21 field model)."""
    now = time.time() if now is None else now
    envelope = {
        "protocol": PROTOCOL,
        "convoy_id": convoy_id,
        "request_id": str(uuid.uuid4()),
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "controller_id": controller_id,
        "origin_host_id": origin_host_id,
        "source_host_id": source_host_id or origin_host_id,
        "target_node_id": target_node_id,
        "operation": operation,
        "arguments": arguments or {},
        "arguments_sha256": canonical_arguments_digest(arguments),
        "expected_runtime_id": expected_runtime_id,
        "deadline_unix": round(now + timeout_s, 3),
        "hop_limit": hop_limit,
        "sig_alg": signer.alg,
    }
    envelope["signature"] = signer.sign(_signing_payload(envelope))
    return envelope


# Operation classes for which A-22 makes expected_runtime_id MANDATORY: a
# request that could act on stale state must name the run it addressed.
# The operation registry (not built in Phase 1) will own the membership
# test; the constant lives here so the verifier can enforce it now.
RUNTIME_ID_REQUIRED_OPS = frozenset()   # populated when the registry lands


def verify_envelope(envelope, signer, convoy_id, my_node_id,
                    my_runtime_id=None, now=None,
                    runtime_required=False):
    """Validate an inbound envelope. Raises EnvelopeRejected, or returns.

    Order is deliberate: cheap structural checks, then authentication,
    and only then anything that acts on the contents. EVERY refusal is a
    named EnvelopeRejected -- a malformed signed field must never escape
    as a raw TypeError/ValueError (that would be an unnamed failure the
    audit trail cannot classify).

    runtime_required: the caller (eventually the operation registry)
    passes True for restart / read-modify-write / exclusive-batch /
    wake-lease operations, for which A-22 makes expected_runtime_id
    mandatory. When True, a missing expected_runtime_id is REFUSED, not
    waved through.
    """
    now = time.time() if now is None else now

    if not isinstance(envelope, dict):
        raise EnvelopeRejected("malformed", "envelope is not an object")
    if envelope.get("protocol") != PROTOCOL:
        raise EnvelopeRejected(
            "protocol_mismatch",
            f"expected {PROTOCOL}, got {envelope.get('protocol')!r}")

    for field in ("convoy_id", "request_id", "controller_id",
                  "origin_host_id", "source_host_id", "target_node_id",
                  "operation"):
        if not envelope.get(field):
            raise EnvelopeRejected("malformed", f"missing {field}")

    if envelope["convoy_id"] != convoy_id:
        raise EnvelopeRejected("namespace_mismatch",
                               f"not a member of {envelope['convoy_id']!r}")

    # The signer must match the algorithm the envelope was signed with,
    # or a v1 HMAC envelope could be checked by a v3 verifier (or vice
    # versa) and fail confusingly as "bad signature".
    if envelope.get("sig_alg") != signer.alg:
        raise EnvelopeRejected(
            "algorithm_mismatch",
            f"envelope signed with {envelope.get('sig_alg')!r}, "
            f"verifier is {signer.alg!r}")

    # Authenticate BEFORE trusting any other field, including the digest.
    if not signer.verify(_signing_payload(envelope),
                         envelope.get("signature")):
        raise EnvelopeRejected("bad_signature",
                               "signature does not match the signed subset")

    # The signature covers the DIGEST; confirm the body still matches it.
    actual = canonical_arguments_digest(envelope.get("arguments"))
    if not hmac.compare_digest(actual,
                               envelope.get("arguments_sha256") or ""):
        raise EnvelopeRejected("arguments_tampered",
                               "arguments do not match the signed digest")

    if envelope["target_node_id"] != my_node_id:
        raise EnvelopeRejected(
            "wrong_target",
            f"addressed to {envelope['target_node_id']!r}, "
            f"I am {my_node_id!r}")

    expected_runtime = envelope.get("expected_runtime_id")
    if runtime_required and not expected_runtime:
        raise EnvelopeRejected(
            "runtime_id_required",
            f"{envelope['operation']!r} may act on stale state and MUST "
            f"carry expected_runtime_id (A-22)")
    if expected_runtime:
        # If the target does not know its own runtime we CANNOT confirm
        # the precondition -- refuse rather than wave it through, which
        # was the fail-open the panel flagged.
        if not my_runtime_id:
            raise EnvelopeRejected(
                "runtime_unverifiable",
                "expected_runtime_id was set but this target has no "
                "runtime to check it against")
        if expected_runtime != my_runtime_id:
            raise EnvelopeRejected(
                "runtime_changed",
                f"addressed runtime {expected_runtime!r}, "
                f"this is {my_runtime_id!r}")

    try:
        deadline = float(envelope.get("deadline_unix"))
    except (TypeError, ValueError):
        # A signed-but-nonsense deadline is a named refusal, never a raw
        # ValueError escaping the verifier.
        raise EnvelopeRejected("malformed",
                               "deadline_unix is not a number")
    if deadline <= now:
        raise EnvelopeRejected("deadline_exceeded",
                               "the request expired before it was executed")

    # v1 enforces origin == source: exactly one hop, no brokers yet.
    if envelope["origin_host_id"] != envelope["source_host_id"]:
        raise EnvelopeRejected(
            "hop_limit_exceeded",
            "v1 permits no relaying host between origin and target")


def remaining_budget(envelope, now=None):
    """Seconds left before the absolute deadline (never negative, never
    raises on a malformed deadline)."""
    now = time.time() if now is None else now
    try:
        return max(0.0, float(envelope.get("deadline_unix")) - now)
    except (TypeError, ValueError):
        return 0.0
