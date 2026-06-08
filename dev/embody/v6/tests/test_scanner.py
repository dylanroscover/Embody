import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contracts
import scanner


def make_tdn(operators=None, **overrides):
    tdn = {
        "format": "tdn",
        "version": "1.4",
        "generator": "unit-test",
        "td_build": "099.2025.32820",
        "exported_at": "2026-06-08T00:00:00Z",
        "network_path": "/test",
        "options": {
            "include_dat_content": True,
            "include_storage": True,
        },
        "type": "baseCOMP",
        "operators": operators or [],
    }
    tdn.update(overrides)
    return tdn


class TestScanner(unittest.TestCase):
    def assert_all_evidence_bounded(self, result):
        for finding in result["findings"]:
            self.assertLessEqual(len(finding["evidence"]), 200)

    def test_clean_source_to_null_network(self):
        tdn = make_tdn(
            [
                {"name": "source1", "type": "constantTOP"},
                {"name": "null1", "type": "nullTOP", "inputs": ["source1"]},
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "clean")
        self.assertEqual(result["counts"], contracts.empty_capability_counts())
        self.assertEqual(result["findings"], [])

    def test_execute_dat_with_code_flags_execute_surface(self):
        tdn = make_tdn(
            [
                {
                    "name": "execute1",
                    "type": "executeDAT",
                    "dat_content": "def onStart():\n    return\n",
                    "dat_content_format": "text",
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["execute_dats"], 1)
        self.assert_all_evidence_bounded(result)

    def test_expression_param_that_reads_file_flags_file_read_expr(self):
        tdn = make_tdn(
            [
                {
                    "name": "level1",
                    "type": "levelTOP",
                    "parameters": {
                        "opacity": "=open('local.txt').read()",
                    },
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["file_read_exprs"], 1)

    def test_webclient_dat_counts_web_ops_and_denylisted_types(self):
        tdn = make_tdn([{"name": "web1", "type": "webclientDAT"}])

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["web_ops"], 1)
        self.assertGreaterEqual(result["counts"]["denylisted_types"], 1)

    def test_comp_with_extension_counts_extensions(self):
        tdn = make_tdn(
            [
                {
                    "name": "base1",
                    "type": "baseCOMP",
                    "sequences": {
                        "ext": [
                            {
                                "object": "op('./BaseExt').module.BaseExt(me)",
                                "name": "BaseExt",
                                "promote": True,
                            }
                        ]
                    },
                    "children": [
                        {
                            "name": "BaseExt",
                            "type": "textDAT",
                            "dat_content": "class BaseExt:\n    pass\n",
                            "dat_content_format": "text",
                        }
                    ],
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["extensions"], 1)

    def test_non_empty_storage_payload_counts_storage_payloads(self):
        tdn = make_tdn(
            [
                {
                    "name": "base1",
                    "type": "baseCOMP",
                    "storage": {"payload": "data"},
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["storage_payloads"], 1)

    def test_traversal_file_param_counts_traversal_paths(self):
        tdn = make_tdn(
            [
                {
                    "name": "text1",
                    "type": "textDAT",
                    "parameters": {
                        "file": "../secrets.txt",
                    },
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["traversal_paths"], 1)

    def test_oversized_input_is_blocked(self):
        tdn = make_tdn(
            [
                {
                    "name": "text1",
                    "type": "textDAT",
                    "dat_content": "x" * (scanner.MAX_SERIALIZED_TDN_BYTES + 1),
                    "dat_content_format": "text",
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "blocked")
        self.assertTrue(result["findings"])
        self.assert_all_evidence_bounded(result)

    def test_evasion_nested_comp_child_is_scanned(self):
        tdn = make_tdn(
            [
                {
                    "name": "outer",
                    "type": "baseCOMP",
                    "children": [
                        {
                            "name": "inner",
                            "type": "baseCOMP",
                            "children": [
                                {
                                    "name": "execute1",
                                    "type": "executeDAT",
                                    "dat_content": "import os\nos.system('id')\n",
                                    "dat_content_format": "text",
                                }
                            ],
                        }
                    ],
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["execute_dats"], 1)

    def test_evasion_expression_dynamic_import_is_scanned(self):
        tdn = make_tdn(
            [
                {
                    "name": "math1",
                    "type": "mathCHOP",
                    "parameters": {
                        "postadd": "=getattr(__import__('os'), 'system')('id')",
                    },
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["file_read_exprs"], 1)

    def test_evasion_storage_payload_is_scanned(self):
        tdn = make_tdn(
            [
                {
                    "name": "base1",
                    "type": "baseCOMP",
                    "storage": {
                        "payload": "eval(open('../secret.py').read())",
                    },
                }
            ]
        )

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result["verdict"], "flagged")
        self.assertGreaterEqual(result["counts"]["storage_payloads"], 1)

    def test_external_ref_comp_flags_external_refs(self):
        # A COMP that references external content (tdn_ref/tox_ref) cannot be scanned
        # inline -> must be surfaced so the submit pipeline can require self-containment.
        for key in ("tdn_ref", "tox_ref"):
            tdn = make_tdn(
                [{"name": "child1", "type": "baseCOMP", key: "child1.tdn"}]
            )
            result = scanner.scan_tdn(tdn)
            self.assertEqual(result["verdict"], "flagged", key)
            self.assertGreaterEqual(result["counts"]["external_refs"], 1, key)
            self.assert_all_evidence_bounded(result)

    def test_clean_network_has_zero_external_refs(self):
        tdn = make_tdn([{"name": "null1", "type": "nullTOP"}])
        result = scanner.scan_tdn(tdn)
        self.assertEqual(result["counts"]["external_refs"], 0)

    def test_internal_scan_error_fails_closed(self):
        # If the internal walk raises, the scanner must return "blocked", never "clean".
        original = scanner._scan_tdn_root
        scanner._scan_tdn_root = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            result = scanner.scan_tdn(make_tdn([{"name": "null1", "type": "nullTOP"}]))
        finally:
            scanner._scan_tdn_root = original
        self.assertEqual(result["verdict"], "blocked")
        self.assertTrue(any(f["detail"].startswith("scanner aborted") for f in result["findings"]))


if __name__ == "__main__":
    unittest.main()
