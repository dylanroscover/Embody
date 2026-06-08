"""FROZEN CONTRACTS C1 (envelope) + C2 (capability) - Python mirror.

The canonical shapes live in platform/packages/contracts/{envelope,capability}.ts.
Keep this file in lock-step with those; a change here is a contract bump that re-notifies
every dependent (scanner.py, safe_import.py, tdn_envelope.py, copy_paste_logic.py).

Pure data + validators only. NO TouchDesigner imports - this module must be unit-testable
headless (tested by dev/embody/unit_tests/test_v6_*.py). ASCII only.

NOTE: lives at dev/embody/v6/ (NOT dev/embody/Embody/_v6/) to stay OUTSIDE the externalized
Embody source tree during development, so Embody's continuity/dirty scan does not treat these
as orphaned externalizations. TD-LIVE integration (WP W2.5) decides their final home as DATs
inside the Embody COMP.
"""
from __future__ import annotations

# ---- C1: envelope --------------------------------------------------------------------------
EMBODY_TDN_MARKER = "_embody_tdn"
EMBODY_TDN_VERSION = 1
ENVELOPE_SOURCES = ("embody", "embody.tools")


def is_embody_tdn_envelope(value) -> bool:
    """Return True iff value is a valid C1 envelope dict."""
    if not isinstance(value, dict):
        return False
    return (
        value.get(EMBODY_TDN_MARKER) == EMBODY_TDN_VERSION
        and value.get("source") in ENVELOPE_SOURCES
        and isinstance(value.get("sha256"), str)
        and isinstance(value.get("tdn"), dict)
    )


# ---- C2: capability ------------------------------------------------------------------------
SCAN_VERDICTS = ("clean", "flagged", "blocked")

# Must match CapabilityCounts keys in capability.ts, in the same order.
CAPABILITY_SURFACES = (
    "execute_dats",
    "file_read_exprs",
    "web_ops",
    "extensions",
    "storage_payloads",
    "denylisted_types",
    "traversal_paths",
    "external_refs",
)


def empty_capability_counts() -> dict:
    """A zeroed CapabilityCounts dict (all surfaces -> 0)."""
    return {k: 0 for k in CAPABILITY_SURFACES}
