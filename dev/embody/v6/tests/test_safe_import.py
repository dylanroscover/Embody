import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import safe_import


def base_tdn(operators=None, **extra):
    tdn = {
        "format": "tdn",
        "version": "1.4",
        "generator": "test",
        "td_build": "099.2025.32820",
        "exported_at": "2026-06-08T00:00:00Z",
        "network_path": "/project",
        "type": "baseCOMP",
        "options": {"include_dat_content": True, "include_storage": True},
        "operators": operators or [],
    }
    tdn.update(extra)
    return tdn


def collect_structure(tdn):
    paths = set()
    connections = {}

    def walk(op_def, parent):
        if not isinstance(op_def, dict):
            return
        path = parent.rstrip("/") + "/" + op_def["name"]
        paths.add(path)
        connections[path] = (
            copy.deepcopy(op_def.get("inputs")),
            copy.deepcopy(op_def.get("comp_inputs")),
        )
        for child in op_def.get("children", []):
            walk(child, path)

    for op_def in tdn.get("operators", []):
        walk(op_def, tdn.get("network_path", "/"))
    return paths, connections


class TestSafeImport(unittest.TestCase):
    def assert_summary_counts(self, summary, **expected):
        defaults = {
            "execute_dats_disabled": 0,
            "exprs_neutralized": 0,
            "extensions_disabled": 0,
            "io_ops_bypassed": 0,
            "storage_removed": 0,
        }
        defaults.update(expected)
        for key, value in defaults.items():
            self.assertEqual(summary[key], value, key)

    def test_execute_dat_is_deactivated_and_content_is_kept(self):
        tdn = base_tdn([
            {
                "name": "exec1",
                "type": "executeDAT",
                "parameters": {"active": True},
                "dat_content": "def onStart():\n    print('run')",
                "dat_content_format": "text",
            }
        ])

        inert, summary = safe_import.make_inert(tdn)

        exec_op = inert["operators"][0]
        self.assertEqual(exec_op["parameters"]["active"], False)
        self.assertEqual(exec_op["dat_content"], tdn["operators"][0]["dat_content"])
        self.assert_summary_counts(summary, execute_dats_disabled=1)
        self.assertTrue(safe_import.is_inert(inert))

    def test_expression_and_bind_parameters_become_constants_with_details(self):
        tdn = base_tdn([
            {
                "name": "exprs",
                "type": "constantCHOP",
                "parameters": {
                    "value0": "=absTime.frame",
                    "name0": "~op('source').par.name",
                    "literal_eq": "==literal",
                    "literal_bind": "~~literal",
                },
                "custom_pars": {
                    "Controls": [
                        {"name": "Speed", "style": "Float", "value": "=absTime.seconds"},
                        {"name": "Label", "style": "Str", "value": "~op('label')"},
                        {"name": "Enabled", "style": "Toggle", "value": "=1"},
                        {"name": "Pair", "style": "XY", "values": ["=1", 2]},
                    ],
                    "About": {
                        "$t": "about",
                        "Build": "=42",
                        "Name": "~op('name')",
                    },
                },
                "sequences": {
                    "const": [
                        {"value": "=me.time.frame", "name": "~parent().name"},
                    ]
                },
            }
        ])

        inert, summary = safe_import.make_inert(tdn)
        op_def = inert["operators"][0]

        self.assertEqual(op_def["parameters"]["value0"], 0)
        self.assertEqual(op_def["parameters"]["name0"], "")
        self.assertEqual(op_def["parameters"]["literal_eq"], "==literal")
        self.assertEqual(op_def["parameters"]["literal_bind"], "~~literal")
        self.assertEqual(op_def["custom_pars"]["Controls"][0]["value"], 0)
        self.assertEqual(op_def["custom_pars"]["Controls"][1]["value"], "")
        self.assertEqual(op_def["custom_pars"]["Controls"][2]["value"], False)
        self.assertEqual(op_def["custom_pars"]["Controls"][3]["values"], [0, 2])
        self.assertEqual(op_def["custom_pars"]["About"]["Build"], 0)
        self.assertEqual(op_def["custom_pars"]["About"]["Name"], "")
        self.assertEqual(op_def["sequences"]["const"][0]["value"], 0)
        self.assertEqual(op_def["sequences"]["const"][0]["name"], "")

        originals = [detail.get("original") for detail in summary["details"]]
        self.assertIn("=absTime.frame", originals)
        self.assertIn("~op('source').par.name", originals)
        self.assert_summary_counts(summary, exprs_neutralized=10)
        self.assertTrue(safe_import.is_inert(inert))

    def test_comp_extensions_are_disabled_without_removing_children(self):
        tdn = base_tdn([
            {
                "name": "owner",
                "type": "baseCOMP",
                "sequences": {
                    "ext": [
                        {
                            "object": "op('./OwnerExt').module.OwnerExt(me)",
                            "name": "OwnerExt",
                            "promote": True,
                        },
                        {},
                    ]
                },
                "children": [
                    {
                        "name": "OwnerExt",
                        "type": "textDAT",
                        "dat_content": "class OwnerExt:\n    pass",
                        "dat_content_format": "text",
                    }
                ],
            }
        ])

        inert, summary = safe_import.make_inert(tdn)

        owner = inert["operators"][0]
        self.assertEqual(owner["sequences"]["ext"], [{}, {}])
        self.assertEqual(owner["children"][0]["name"], "OwnerExt")
        self.assertEqual(owner["children"][0]["dat_content"], "class OwnerExt:\n    pass")
        self.assert_summary_counts(summary, extensions_disabled=1)
        self.assertTrue(safe_import.is_inert(inert))

    def test_io_operator_is_bypassed(self):
        tdn = base_tdn([
            {
                "name": "client1",
                "type": "webclientDAT",
                "flags": ["viewer"],
                "parameters": {"url": "https://example.test"},
            }
        ])

        inert, summary = safe_import.make_inert(tdn)

        flags = inert["operators"][0]["flags"]
        self.assertIn("viewer", flags)
        self.assertIn("bypass", flags)
        self.assert_summary_counts(summary, io_ops_bypassed=1)
        self.assertTrue(safe_import.is_inert(inert))

    def test_storage_and_startup_storage_are_quarantined(self):
        tdn = base_tdn(
            [
                {
                    "name": "stored",
                    "type": "baseCOMP",
                    "storage": {"payload": {"$type": "bytes", "$value": "AAEC"}},
                    "startup_storage": {"mode": "auto"},
                }
            ],
            storage={"root_payload": 1},
        )

        inert, summary = safe_import.make_inert(tdn)

        self.assertNotIn("storage", inert)
        self.assertNotIn("storage", inert["operators"][0])
        self.assertNotIn("startup_storage", inert["operators"][0])
        originals = [detail.get("original") for detail in summary["details"]]
        self.assertIn({"storage": {"root_payload": 1}}, originals)
        self.assertIn({"startup_storage": {"mode": "auto"}}, originals)
        self.assert_summary_counts(summary, storage_removed=3)
        self.assertTrue(safe_import.is_inert(inert))

    def test_input_is_not_mutated_idempotent_and_structure_is_preserved(self):
        tdn = base_tdn([
            {
                "name": "container1",
                "type": "baseCOMP",
                "parameters": {"w": "=absTime.frame"},
                "children": [
                    {
                        "name": "exec1",
                        "type": "executeDAT",
                        "inputs": ["source1"],
                        "comp_inputs": ["panel1"],
                        "dat_content": "def onFrameStart(frame):\n    return",
                        "dat_content_format": "text",
                    },
                    {
                        "name": "client1",
                        "type": "webclientDAT",
                        "inputs": ["exec1"],
                    },
                ],
            }
        ])
        original = copy.deepcopy(tdn)
        before_structure = collect_structure(tdn)

        inert, summary = safe_import.make_inert(tdn)
        inert_again, second_summary = safe_import.make_inert(inert)

        self.assertEqual(tdn, original)
        self.assertIsNot(inert, tdn)
        self.assertEqual(inert_again, inert)
        self.assert_summary_counts(second_summary)
        self.assertEqual(collect_structure(inert), before_structure)
        self.assert_summary_counts(
            summary,
            execute_dats_disabled=1,
            exprs_neutralized=1,
            io_ops_bypassed=1,
        )
        self.assertTrue(safe_import.is_inert(inert))

    def test_type_defaults_and_root_surfaces_are_neutralized(self):
        tdn = base_tdn(
            [
                {
                    "name": "movie1",
                    "type": "moviefileinTOP",
                }
            ],
            parameters={"resizecomp": "=me"},
            type_defaults={
                "moviefileinTOP": {
                    "parameters": {"file": "~op('path')"},
                    "flags": ["viewer"],
                }
            },
            par_templates={
                "dynamic": [
                    {
                        "name": "Choice",
                        "style": "Menu",
                        "menuSource": "=op('menu')",
                    }
                ]
            },
        )

        inert, summary = safe_import.make_inert(tdn)

        self.assertEqual(inert["parameters"]["resizecomp"], "")
        self.assertEqual(inert["type_defaults"]["moviefileinTOP"]["parameters"]["file"], "")
        self.assertEqual(inert["par_templates"]["dynamic"][0]["menuSource"], "")
        self.assertIn("bypass", inert["operators"][0]["flags"])
        self.assert_summary_counts(summary, exprs_neutralized=3, io_ops_bypassed=1)
        self.assertTrue(safe_import.is_inert(inert))

    def test_malformed_tdn_does_not_raise(self):
        malformed = {
            "network_path": "/bad",
            "parameters": "bad",
            "operators": [
                None,
                {"name": "client", "type": "webclientDAT", "flags": "bad"},
            ],
        }

        inert, summary = safe_import.make_inert(malformed)

        self.assertIn("bypass", inert["operators"][1]["flags"])
        self.assertFalse(safe_import.is_inert(None))
        self.assertTrue(isinstance(safe_import.is_inert(malformed), bool))
        self.assert_summary_counts(summary, io_ops_bypassed=1)


if __name__ == "__main__":
    unittest.main()
