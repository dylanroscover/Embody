"""Pure logic for the Copy tdn / Paste tdn actions (W1.17).

Headless: no TouchDesigner imports. The TD-LIVE wiring (W2.5/W2.6) calls these and then performs
the actual clipboard write / ImportNetwork on the main thread. This module decides WHAT to do; it
never touches TD.

- build_copy_envelope: a network's TDN dict -> the _embody_tdn clipboard envelope (C1).
- plan_paste: clipboard text -> an import PLAN. Trust split per the platform plan:
    source == "embody"        -> the user's own network round-trip: DIRECT import (no inerting).
    source == "embody.tools"  -> community/untrusted: scan + DEFAULT-INERT import + capability
                                 summary, so a paste can never silently execute a stranger's code.
ASCII only.
"""
from __future__ import annotations

import tdn_envelope
import scanner
import safe_import


def build_copy_envelope(tdn: dict, source: str = "embody", slug=None, version=None) -> dict:
    """Wrap a network's TDN dict as a C1 clipboard envelope (delegates to tdn_envelope)."""
    return tdn_envelope.wrap_tdn(tdn, source, slug=slug, version=version)


def plan_paste(clipboard_text: str, trust_own_source: bool = True) -> dict:
    """Turn clipboard text into an import plan. Never raises; never executes anything.

    Returns one of:
      {"ok": False, "reason": "no_tdn"}                       - clipboard has no valid envelope
      {"ok": True, "mode": "direct"|"inert", "source", "slug", "version",
       "integrity_ok": bool, "capability": <C2 dict>, "tdn": <dict to import>,
       "summary": <inert summary or None>}
    For mode "direct" the tdn is the original payload; for mode "inert" it is the default-inert
    transform and `summary` lists what was neutralized.
    """
    env = tdn_envelope.unwrap_clipboard(clipboard_text)
    if env is None:
        return {"ok": False, "reason": "no_tdn"}

    tdn = env.get("tdn")
    source = env.get("source")
    integrity_ok = tdn_envelope.verify_envelope_integrity(env)
    capability = scanner.scan_tdn(tdn if isinstance(tdn, dict) else {})

    if source == "embody" and trust_own_source:
        return {
            "ok": True,
            "mode": "direct",
            "source": source,
            "slug": env.get("slug"),
            "version": env.get("version"),
            "integrity_ok": integrity_ok,
            "capability": capability,
            "tdn": tdn,
            "summary": None,
        }

    inert_tdn, summary = safe_import.make_inert(tdn if isinstance(tdn, dict) else {})
    return {
        "ok": True,
        "mode": "inert",
        "source": source,
        "slug": env.get("slug"),
        "version": env.get("version"),
        "integrity_ok": integrity_ok,
        "capability": capability,
        "tdn": inert_tdn,
        "summary": summary,
    }
