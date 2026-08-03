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
operation, hash(arguments), created time, duration, and deadline -- plus
source_host_id, hop_limit, expected_runtime_id, and the sig_alg tag,
which are also decision fields and so must be signed too.

Signing is PLUGGABLE (A-21's Phase 3 target). Phase 1 ships HMAC over a
pre-shared key: this is GROUP / MEMBERSHIP authentication and message
integrity within a trusted LAN (A-8), NOT per-node identity -- any PSK
holder can set any origin_host_id and still sign, so a signed
origin_host_id is authentic only against outsiders, not against a
malicious member. Phase 3 swaps in a per-host keypair pinned by
fingerprint, which is what makes origin_host_id genuinely authoritative;
the Signer interface is the seam and no caller changes.

job_id is NOT in the request (A-22): it is minted target-side at
persist-before-acknowledge and returned in the ack.  The origin signs a
duration (budget_s), its wall-clock creation time, and the corresponding
absolute deadline.  A sender spends that absolute deadline cumulatively
on its own clock.  A target uses the timestamps only for a bounded skew /
freshness admission window, then anchors no more than the signed duration
to its OWN monotonic clock.  Thus host clock offsets and later NTP/manual
wall-clock jumps cannot silently shorten or refresh admitted work.
"""

import hashlib
import hmac
import json
import math
import time
import uuid

import convoy_identity as identity

PROTOCOL = "convoy/1"

# A signed budget is an allocation of queue/dispatch time.  It must be
# finite (checked below) and bounded: otherwise an admitted peer can
# create work that remains executable effectively forever.  One hour is
# intentionally much larger than the normal 30-second relay budget while
# still providing a finite replay horizon.  Long-running node jobs are
# unaffected -- this is the deadline to DELIVER the operation, not a
# forced execution kill.
MAX_DEADLINE_HORIZON_S = 60.0 * 60.0

# LAN hosts are not required to run perfectly synchronized clocks.  This
# is both an admission tolerance and a hard bound on how far a signed
# timestamp can extend the replay window.  It intentionally matches the
# discovery protocol's clock-skew allowance.
MAX_CLOCK_SKEW_S = 120.0

# build_envelope retains millisecond deadline rounding for wire/audit
# compatibility.  The receiver therefore permits the corresponding
# sub-millisecond arithmetic difference, but no material rewrite of the
# signed created + budget relationship.
DEADLINE_CORRELATION_TOLERANCE_S = 0.0011

# convoy/1 is direct host-to-host.  The field remains signed for the
# planned brokered/SaaS protocol, but a v1 receiver neither accepts an
# unbounded counter nor treats bool (an int subclass) as a hop count.
MAX_HOP_LIMIT = 2

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
    "created_unix",
    "budget_s",
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
    if isinstance(now, bool) or isinstance(timeout_s, bool):
        raise ValueError("created_unix and budget_s must be numbers")
    try:
        now = float(now)
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        raise ValueError("created_unix and budget_s must be numbers")
    if not math.isfinite(now) or now < 0:
        raise ValueError("created_unix must be a finite nonnegative number")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("budget_s must be a finite positive number")
    if timeout_s > MAX_DEADLINE_HORIZON_S:
        raise ValueError(
            f"budget_s must be at most {MAX_DEADLINE_HORIZON_S:.0f}s")
    try:
        convoy_id = identity.normalize_convoy_id(convoy_id)
    except identity.IdentityError as e:
        raise ValueError(e.detail or e.reason)
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
        "created_unix": now,
        "budget_s": timeout_s,
        "deadline_unix": round(now + timeout_s, 3),
        "hop_limit": hop_limit,
        "sig_alg": signer.alg,
    }
    envelope["signature"] = signer.sign(_signing_payload(envelope))
    return envelope


def verify_envelope(envelope, signer, convoy_id, my_node_id,
                    my_runtime_id=None, now=None,
                    runtime_required=False, monotonic_now=None):
    """Validate an inbound envelope and return accepted timing metadata.

    The return value is suitable for ``HostStore.create_job(...,
    accepted_timing=timing)``.  ``accepted_deadline_monotonic`` is only
    meaningful in this process; the remaining wall-clock fields are the
    durable restart fallback.  Existing callers that ignore the return
    remain source-compatible.

    Order is deliberate: cheap structural checks, then authentication,
    and only then anything that acts on the contents. EVERY refusal is a
    named EnvelopeRejected -- a malformed signed field must never escape
    as a raw TypeError/ValueError (that would be an unnamed failure the
    audit trail cannot classify).

    runtime_required: True for restart / read-modify-write /
    exclusive-batch / wake-lease operations, for which A-22 makes
    expected_runtime_id mandatory. When True, a missing
    expected_runtime_id is REFUSED, not waved through. The caller owns
    the membership test -- today that is the host app's operation
    registry entry (`runtime_required`), which is also what the
    capability digest covers, so the classification travels with the
    operation instead of being duplicated here.
    """
    now = time.time() if now is None else now
    monotonic_now = (time.monotonic() if monotonic_now is None
                     else monotonic_now)

    try:
        now = float(now)
        monotonic_now = float(monotonic_now)
    except (TypeError, ValueError):
        raise EnvelopeRejected("malformed", "receiver clock is not numeric")
    if not math.isfinite(now) or not math.isfinite(monotonic_now):
        raise EnvelopeRejected("malformed", "receiver clock is not finite")

    if not isinstance(envelope, dict):
        raise EnvelopeRejected("malformed", "envelope is not an object")
    if envelope.get("protocol") != PROTOCOL:
        raise EnvelopeRejected(
            "protocol_mismatch",
            f"expected {PROTOCOL}, got {envelope.get('protocol')!r}")

    for field in ("convoy_id", "request_id", "idempotency_key",
                  "controller_id",
                  "origin_host_id", "source_host_id", "target_node_id",
                  "operation"):
        if (not isinstance(envelope.get(field), str)
                or not envelope.get(field)):
            raise EnvelopeRejected("malformed", f"missing {field}")

    try:
        envelope_convoy_id = identity.normalize_convoy_id(
            envelope["convoy_id"])
        local_convoy_id = identity.normalize_convoy_id(convoy_id)
    except identity.IdentityError:
        raise EnvelopeRejected("malformed", "invalid convoy_id")
    if envelope_convoy_id != local_convoy_id:
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

    numeric = {}
    for field in ("created_unix", "budget_s", "deadline_unix"):
        raw = envelope.get(field)
        if isinstance(raw, bool):
            raise EnvelopeRejected("malformed", f"{field} is not a number")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            # A signed-but-nonsense timing field is a named refusal,
            # never a raw ValueError escaping the verifier.
            raise EnvelopeRejected("malformed", f"{field} is not a number")
        if not math.isfinite(value):
            # NaN is fail-open in comparisons (`nan <= now` is false),
            # while Infinity creates immortal work.  JSON accepts both
            # tokens, so the protocol itself must refuse them.
            raise EnvelopeRejected(
                "malformed", f"{field} must be a finite number")
        numeric[field] = value

    created = numeric["created_unix"]
    budget = numeric["budget_s"]
    deadline = numeric["deadline_unix"]
    if created < 0:
        raise EnvelopeRejected(
            "malformed", "created_unix must be nonnegative")
    if budget <= 0:
        raise EnvelopeRejected(
            "malformed", "budget_s must be positive")
    if budget > MAX_DEADLINE_HORIZON_S:
        raise EnvelopeRejected(
            "deadline_too_far",
            f"budget_s is {budget:.3f}s; convoy/1 permits at most "
            f"{MAX_DEADLINE_HORIZON_S:.0f}s")

    expected_deadline = created + budget
    if abs(deadline - expected_deadline) > \
            DEADLINE_CORRELATION_TOLERANCE_S:
        raise EnvelopeRejected(
            "deadline_mismatch",
            "deadline_unix does not equal created_unix + budget_s")

    # Do NOT compute `deadline - now` as the ordinary target budget: that
    # assumes synchronized wall clocks and caused a host 31 seconds ahead
    # to reject a fresh 30-second request.  Instead, timestamps define a
    # bounded admission window.  At either permitted skew extreme a fresh
    # request receives its complete signed duration.  Close to the far
    # end of the replay window it is clamped so a replay cannot extend the
    # authenticated deadline plus the protocol's fixed skew allowance.
    if now < created - MAX_CLOCK_SKEW_S:
        raise EnvelopeRejected(
            "timestamp_out_of_window",
            f"created_unix is more than {MAX_CLOCK_SKEW_S:.0f}s in the future")
    # Use the exact signed duration relationship here rather than the
    # millisecond-rounded audit deadline, otherwise a request at exactly
    # the permitted positive skew could lose a fraction of a millisecond.
    effective_deadline = expected_deadline + MAX_CLOCK_SKEW_S
    freshness_remaining = effective_deadline - now
    if freshness_remaining <= 0:
        raise EnvelopeRejected(
            "deadline_exceeded",
            "the request is outside its signed deadline and skew window")
    remaining = min(budget, freshness_remaining)

    hop_limit = envelope.get("hop_limit")
    if (isinstance(hop_limit, bool)
            or not isinstance(hop_limit, int)):
        raise EnvelopeRejected("malformed", "hop_limit must be an integer")
    if hop_limit < 1 or hop_limit > MAX_HOP_LIMIT:
        raise EnvelopeRejected(
            "hop_limit_exceeded",
            f"hop_limit must be between 1 and {MAX_HOP_LIMIT}")

    # v1 enforces origin == source: exactly one hop, no brokers yet.
    if envelope["origin_host_id"] != envelope["source_host_id"]:
        raise EnvelopeRejected(
            "hop_limit_exceeded",
            "v1 permits no relaying host between origin and target")

    # Anchor the accepted duration to this target's monotonic clock.  The
    # durable effective wall expiry includes the fixed, protocol-defined
    # skew allowance; signed_deadline_unix preserves the exact origin
    # value for audit.  A monotonic timestamp is deliberately never sent:
    # different hosts have unrelated monotonic epochs.
    return {
        "request_deadline_unix": effective_deadline,
        "signed_deadline_unix": deadline,
        "accepted_at_unix": now,
        "accepted_remaining_s": remaining,
        "accepted_expires_unix": now + remaining,
        "accepted_deadline_monotonic": monotonic_now + remaining,
    }


def remaining_budget(envelope, now=None):
    """Seconds left before the absolute deadline (never negative, never
    NaN, never raises on a malformed deadline)."""
    now = time.time() if now is None else now
    if not isinstance(envelope, dict) or isinstance(now, bool):
        return 0.0
    try:
        now = float(now)
        created = float(envelope.get("created_unix"))
        budget = float(envelope.get("budget_s"))
        deadline = float(envelope.get("deadline_unix"))
    except (TypeError, ValueError):
        return 0.0
    if any(isinstance(envelope.get(field), bool) for field in (
            "created_unix", "budget_s", "deadline_unix")):
        return 0.0
    if not all(math.isfinite(value) for value in (
            now, created, budget, deadline)):
        # A non-finite deadline yields a non-finite budget, which every
        # downstream timeout comparison then reads as "plenty of time".
        # No budget is the safe reading of an unusable deadline.
        return 0.0
    if (created < 0 or budget <= 0 or budget > MAX_DEADLINE_HORIZON_S
            or abs(deadline - (created + budget))
            > DEADLINE_CORRELATION_TOLERANCE_S):
        return 0.0
    # Sender-side cumulative accounting remains wall-clock based on the
    # sender's OWN clock.  Clamp to the signed duration so a local clock
    # rollback can never mint a larger budget than the origin authorized.
    return min(budget, max(0.0, deadline - now))
