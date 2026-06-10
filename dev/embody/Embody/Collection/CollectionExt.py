"""CollectionExt -- embody.tools community/platform integration.

Owns the untrusted-content layer for community TDN (source == "embody.tools"):
the capability scanner and the default-inert safe-import. Trusted own-network
Copy/Paste lives in TDNExt and never reaches here. The scan/inert logic lives in
the self-contained `scanner` and `safe_import` DATs beside this extension, loaded
independently (no cross-import chain). This class is the thin TD glue, and the
future home of the rest of the platform client (browse / fetch / submit).
ASCII only.
"""


class CollectionExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    def ScanTdn(self, tdn):
        """Return the C2 capability report for a TDN dict."""
        return self.ownerComp.op('scanner').module.scan_tdn(tdn if isinstance(tdn, dict) else {})

    def PlanCommunityPaste(self, tdn):
        """Scan + default-inert a community TDN dict; return the import plan.

        Reached only for source == "embody.tools" -- TDNExt unwraps the envelope
        and hands over the inner tdn. Nothing here executes; it returns the inert
        payload for TDNExt to import.
        """
        tdn = tdn if isinstance(tdn, dict) else {}
        capability = self.ownerComp.op('scanner').module.scan_tdn(tdn)
        inert_tdn, summary = self.ownerComp.op('safe_import').module.make_inert(tdn)
        return {'mode': 'inert', 'tdn': inert_tdn,
                'capability': capability, 'summary': summary}
