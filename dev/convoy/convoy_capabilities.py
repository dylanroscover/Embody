"""Capability + compatibility digest for Convoy (A-23). Pure, no I/O.

The problem this solves: a controller on machine A asks node B to run an
operation. A and B may be on different Embody builds, where that
operation's arguments, gating, or behaviour differ. Sending the request
blind risks silently doing the wrong thing. So B advertises a MANIFEST --
the operations it exposes and a per-operation compatibility digest -- and
A refuses to send an operation whose digest it does not recognize.

A-23 scopes the digest to the operation's REGISTRY ENTRY (name, schema,
gating), NOT the whole server version: two builds that differ elsewhere
but expose an identical `query_network` share a digest and interoperate.
That keeps the mesh usable across the mixed-build reality of a studio.
"""

import hashlib
import json


def operation_digest(name, schema=None, gating=None, side_effects=None):
    """Stable digest over the parts of an operation that affect
    compatibility. Two operations with the same name/schema/gating/effect
    profile produce the same digest on any machine and any build.

    Deliberately NOT hashed: help text, ordering, cosmetic metadata --
    they change across builds without changing behaviour, and folding
    them in would fragment the mesh for no safety gain.
    """
    material = {
        "name": name,
        "schema": schema or {},
        "gating": gating or {},
        "side_effects": side_effects or {},
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return "op1-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class CapabilityManifest:
    """What one node exposes: {operation_name -> digest}, plus the
    protocol version. Built target-side.

    manifest_digest() is the ENABLER for A-23's "cache manifests by
    digest": a controller stores the digest it last saw and skips
    re-fetching when it is unchanged. The cache itself lives in the
    controller (the bridge/ConvoyExt), not here -- this class provides
    the stable digest that makes the cache possible, and does not claim
    to implement the cache."""

    def __init__(self, protocol, node_id, operations=None):
        self.protocol = protocol
        self.node_id = node_id
        self.operations = dict(operations or {})   # name -> digest

    def add(self, name, digest):
        self.operations[name] = digest

    def manifest_digest(self):
        """One digest over the whole manifest, so a controller can tell
        'unchanged since last time' with a single comparison and skip
        re-fetching."""
        blob = json.dumps(
            {"protocol": self.protocol, "operations": self.operations},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "mf1-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]

    def to_dict(self):
        return {"protocol": self.protocol, "node_id": self.node_id,
                "operations": self.operations,
                "manifest_digest": self.manifest_digest()}

    @classmethod
    def from_dict(cls, data):
        return cls(data.get("protocol"), data.get("node_id"),
                   data.get("operations"))


class CompatibilityError(Exception):
    """A controller's view of an operation does not match the target's."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def check_compatible(controller_expected_digest, target_manifest, operation):
    """Refuse BEFORE sending if the controller's expectation of an
    operation does not match what the target actually exposes.

    Returns None on success. Raises CompatibilityError otherwise -- the
    caller must not fall back to sending anyway, which is the whole point
    of the check.
    """
    target_digest = target_manifest.operations.get(operation)
    if target_digest is None:
        raise CompatibilityError(
            "operation_not_exposed",
            f"target does not expose {operation!r}")
    if controller_expected_digest != target_digest:
        raise CompatibilityError(
            "incompatible_operation",
            f"{operation!r}: controller expected "
            f"{controller_expected_digest!r}, target has {target_digest!r}")
    return None
