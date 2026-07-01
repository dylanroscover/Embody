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
        """Scan a community TDN dict and return the import plan (live or inert).

        Reached only for source == "embody.tools" -- TDNExt unwraps the envelope
        and hands over the inner tdn. Nothing here executes.

        Live-if-scanned-clean: a specimen whose only expressions are PROVABLY PURE
        value reads (par reads, absTime, math.*, Par.eval(), arithmetic) and which
        has no execute-DAT / extension / IO / storage / denylisted surface scans
        'clean' and imports LIVE -- fully working, the whole point of the gallery.
        Anything 'flagged'/'blocked' is disarmed by make_inert, which now PRESERVES
        the pure expressions (so the specimen still renders) and neutralizes only
        the genuinely side-effecting surfaces. The purity predicate is the scanner's
        own is_pure_value_expression, so the verdict and the neutralization agree.
        """
        tdn = tdn if isinstance(tdn, dict) else {}
        scanner = self.ownerComp.op('scanner').module
        safe_import = self.ownerComp.op('safe_import').module
        capability = scanner.scan_tdn(tdn)
        if capability.get('verdict') == 'clean':
            return {'mode': 'live', 'tdn': tdn,
                    'capability': capability, 'summary': safe_import._empty_summary()}
        inert_tdn, summary = safe_import.make_inert(
            tdn, is_pure_expr=scanner.is_pure_value_expression)
        return {'mode': 'inert', 'tdn': inert_tdn,
                'capability': capability, 'summary': summary}
