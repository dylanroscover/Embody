"""Helpers for the frozen C1 _embody_tdn clipboard envelope."""
from __future__ import annotations

import hashlib
import json

from contracts import (
    EMBODY_TDN_MARKER,
    EMBODY_TDN_VERSION,
    ENVELOPE_SOURCES,
    is_embody_tdn_envelope,
)


def canonical_tdn_bytes(tdn: dict) -> bytes:
    """Return the canonical JSON byte representation used for TDN hashing."""
    return json.dumps(
        tdn,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def tdn_sha256(tdn: dict) -> str:
    """Return the hex sha256 digest for a TDN dict."""
    return hashlib.sha256(canonical_tdn_bytes(tdn)).hexdigest()


def wrap_tdn(tdn: dict, source: str, slug=None, version=None) -> dict:
    """Build a valid C1 clipboard envelope around a TDN dict."""
    if source not in ENVELOPE_SOURCES:
        raise ValueError(f"Invalid envelope source: {source}")

    envelope = {
        EMBODY_TDN_MARKER: EMBODY_TDN_VERSION,
        "source": source,
        "sha256": tdn_sha256(tdn),
        "tdn": tdn,
    }
    if slug is not None:
        envelope["slug"] = slug
    if version is not None:
        envelope["version"] = version
    return envelope


def to_clipboard_str(envelope: dict) -> str:
    """Serialize an envelope for clipboard transport."""
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def unwrap_clipboard(text: str) -> dict | None:
    """Parse clipboard text and return a C1 envelope dict, or None."""
    try:
        value = json.loads(text)
    except Exception:
        return None
    if is_embody_tdn_envelope(value):
        return value
    return None


def verify_envelope_integrity(envelope: dict) -> bool:
    """Return True iff the envelope sha256 matches its TDN payload."""
    try:
        return tdn_sha256(envelope["tdn"]) == envelope["sha256"]
    except Exception:
        return False
