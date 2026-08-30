"""New-behavior tests: live-if-clean + pure-value-expression preservation.

Covers the v6 community-paste fix where safe_import preserves PROVABLY PURE value
expressions (par reads, absTime, math.*, Par.eval(), arithmetic) instead of zeroing
every expression, the scanner's pure-expression verdict (no more .eval()/.store()/
tdu/GLSL false positives), Script-OP + tox_ref disarming, and purity-aware is_inert.

Pure unittest, no TD imports. Run: python3 -m unittest tests.test_safe_import_pure
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import safe_import
import scanner

PURE = scanner.is_pure_value_expression


def tdn(operators=None, **extra):
    t = {
        "format": "tdn", "version": "2.0", "generator": "test",
        "td_build": "099.2025.32820", "network_path": "/p", "type": "baseCOMP",
        "operators": operators or [],
    }
    t.update(extra)
    return t


# Idioms that MUST be preserved (pure value reads/compute).
BENIGN = [
    "parent().par.Power.eval()",
    "absTime.seconds * parent().par.Speed.eval()",
    "math.cos(math.radians(parent().par.Sunelev.eval()))",
    "me.par.Tx.eval() + me.par.Ty.eval()",
    "op('ctrl').par.X.eval()",
    "tdu.remap(absTime.seconds, 0, 1, 0, 10)",
    "1 if me.par.Toggle.eval() else 0",
    "max(0.0, parent().par.Glow.eval())",
    "ipar.Geo.Tx",
    "0 if hasattr(me, 'EncloseOPs') and me.EncloseOPs else 1",
]
# Patterns that MUST be neutralized (side-effecting / not provably pure).
MALICIOUS = [
    "op('victim').destroy()",
    "op('button').par.reset.pulse()",
    "__import__('os').system('id')",
    "eval('1+1')",
    "open('/etc/passwd').read()",
    "(lambda:0).__globals__['__builtins__']['__import__']('os')",
    "(f:=op('v').destroy)()",
    "op('Code').module.do_it()",
    "parent().storage.update({'k': 'v'})",
    "getattr(op('v'), 'destroy')()",
    "[c for c in ().__class__.__mro__[1].__subclasses__()][0]",
    "mod.tools.run()",
]


class TestPurityValidator(unittest.TestCase):
    def test_benign_idioms_are_pure(self):
        for s in BENIGN:
            self.assertTrue(PURE(s), "benign wrongly blocked: %s" % s)

    def test_malicious_idioms_are_not_pure(self):
        for s in MALICIOUS:
            self.assertFalse(PURE(s), "malicious wrongly allowed: %s" % s)

    def test_empty_and_garbage_fail_closed(self):
        for s in ("", "   ", "def f(): pass", "x =", None):
            self.assertFalse(PURE(s))


class TestMakeInertPreservesPure(unittest.TestCase):
    def _glsl(self):
        return tdn([{
            "name": "g", "type": "glslTOP",
            "sequences": {"vec": [
                {"name": "uP", "valuex": "=parent().par.Power.eval()",
                 "valuey": "=absTime.seconds"},
            ]},
            "parameters": {"resolutionw": "=op('v').destroy()"},
        }])

    def test_pure_sequence_exprs_preserved_dangerous_neutralized(self):
        inert, summary = safe_import.make_inert(self._glsl(), is_pure_expr=PURE)
        g = inert["operators"][0]
        self.assertEqual(g["sequences"]["vec"][0]["valuex"], "=parent().par.Power.eval()")
        self.assertEqual(g["sequences"]["vec"][0]["valuey"], "=absTime.seconds")
        self.assertEqual(g["parameters"]["resolutionw"], 0)  # dangerous -> neutralized
        self.assertEqual(summary["exprs_neutralized"], 1)

    def test_without_predicate_everything_is_neutralized(self):
        inert, summary = safe_import.make_inert(self._glsl())  # no injection
        g = inert["operators"][0]
        self.assertEqual(g["sequences"]["vec"][0]["valuex"], 0)
        self.assertEqual(g["sequences"]["vec"][0]["valuey"], 0)
        self.assertEqual(summary["exprs_neutralized"], 3)

    def test_custom_par_default_and_menusource_gated(self):
        t = tdn([{
            "name": "b", "type": "baseCOMP",
            "custom_pars": {"P": [
                {"name": "A", "style": "Float", "value": "=parent().par.X.eval()"},
                {"name": "B", "style": "Float", "default": "=op('v').destroy()"},
            ]},
        }])
        inert, _ = safe_import.make_inert(t, is_pure_expr=PURE)
        defs = inert["operators"][0]["custom_pars"]["P"]
        self.assertEqual(defs[0]["value"], "=parent().par.X.eval()")  # pure preserved
        self.assertEqual(defs[1]["default"], 0)                        # dangerous gone


class TestScannerExpressionPurity(unittest.TestCase):
    def _flagged_expr(self, expr):
        return scanner.scan_tdn(tdn([
            {"name": "l", "type": "levelTOP", "parameters": {"opacity": expr}}]))

    def test_pure_param_exprs_do_not_flag(self):
        for s in BENIGN:
            res = self._flagged_expr("=" + s)
            self.assertEqual(res["counts"]["file_read_exprs"], 0, "FP on: %s" % s)

    def test_dangerous_param_exprs_flag(self):
        for s in MALICIOUS:
            res = self._flagged_expr("=" + s)
            self.assertGreaterEqual(res["counts"]["file_read_exprs"], 1, "missed: %s" % s)

    def test_par_eval_idiom_scans_clean(self):
        res = scanner.scan_tdn(tdn([
            {"name": "g", "type": "glslTOP",
             "sequences": {"vec": [{"name": "u", "valuex": "=parent().par.Power.eval()"}]}}]))
        self.assertEqual(res["verdict"], "clean")


class TestScannerGlslAndData(unittest.TestCase):
    def test_glsl_textdat_by_language_not_python(self):
        res = scanner.scan_tdn(tdn([
            {"name": "px", "type": "textDAT", "parameters": {"language": "glsl"},
             "dat_content": "uniform vec4 u;\nvoid main(){ }"}]))
        self.assertEqual(res["counts"]["execute_dats"], 0)
        self.assertEqual(res["verdict"], "clean")

    def test_glsl_textdat_by_extension_not_python(self):
        res = scanner.scan_tdn(tdn([
            {"name": "px", "type": "textDAT", "parameters": {"extension": "frag"},
             "dat_content": "// shader\nuniform vec4 u; void main(){}"}]))
        self.assertEqual(res["counts"]["execute_dats"], 0)

    def test_python_textdat_with_import_still_flags(self):
        res = scanner.scan_tdn(tdn([
            {"name": "code", "type": "textDAT",
             "dat_content": "import os\nos.system('id')"}]))
        self.assertGreaterEqual(res["counts"]["execute_dats"], 1)


class TestScannerAndInertScriptOps(unittest.TestCase):
    def test_script_top_scans_flagged(self):
        res = scanner.scan_tdn(tdn([{"name": "s", "type": "scriptTOP"}]))
        self.assertGreaterEqual(res["counts"]["execute_dats"], 1)
        self.assertEqual(res["verdict"], "flagged")

    def test_script_op_is_bypassed(self):
        inert, summary = safe_import.make_inert(
            tdn([{"name": "s", "type": "scriptCHOP"}]), is_pure_expr=PURE)
        self.assertIn("bypass", inert["operators"][0].get("flags", []))
        self.assertEqual(summary["script_ops_bypassed"], 1)


class TestToxRef(unittest.TestCase):
    def test_tox_ref_scans_flagged(self):
        res = scanner.scan_tdn(tdn([{"name": "c", "type": "baseCOMP", "tox_ref": "x.tox"}]))
        self.assertGreaterEqual(res["counts"]["external_refs"], 1)

    def test_tox_ref_stripped_by_make_inert(self):
        inert, summary = safe_import.make_inert(
            tdn([{"name": "c", "type": "baseCOMP", "tox_ref": "x.tox"}]), is_pure_expr=PURE)
        self.assertNotIn("tox_ref", inert["operators"][0])
        self.assertEqual(summary["external_refs_stripped"], 1)


class TestIsInertPurityAware(unittest.TestCase):
    def _pure_net(self):
        return tdn([{"name": "l", "type": "levelTOP",
                     "parameters": {"opacity": "=parent().par.X.eval()"}}])

    def test_pure_net_is_inert_with_predicate(self):
        self.assertTrue(safe_import.is_inert(self._pure_net(), is_pure_expr=PURE))

    def test_pure_net_not_inert_without_predicate(self):
        self.assertFalse(safe_import.is_inert(self._pure_net()))

    def test_dangerous_net_not_inert_even_with_predicate(self):
        net = tdn([{"name": "l", "type": "levelTOP",
                    "parameters": {"opacity": "=op('v').destroy()"}}])
        self.assertFalse(safe_import.is_inert(net, is_pure_expr=PURE))

    def test_make_inert_result_is_inert(self):
        inert, _ = safe_import.make_inert(self._pure_net(), is_pure_expr=PURE)
        self.assertTrue(safe_import.is_inert(inert, is_pure_expr=PURE))


class TestPaletteTrust(unittest.TestCase):
    PALETTE = "op.TDAnnotate.mod.AnnotateExt.AnnotateExt(me)"
    FOREIGN = "op('./Evil').module.Evil(me)"

    def _comp_with_ext(self, obj, **node_extra):
        node = {"name": "c", "type": "annotateCOMP",
                "sequences": {"ext": [{"object": obj, "name": "E", "promote": True}]}}
        node.update(node_extra)
        return tdn([node])

    def test_palette_extension_scans_clean(self):
        self.assertEqual(scanner.scan_tdn(self._comp_with_ext(self.PALETTE))["verdict"], "clean")

    def test_palette_extension_not_disabled(self):
        inert, summary = safe_import.make_inert(self._comp_with_ext(self.PALETTE), is_pure_expr=PURE)
        self.assertEqual(summary["extensions_disabled"], 0)
        self.assertTrue(inert["operators"][0]["sequences"]["ext"][0].get("object"))

    def test_foreign_extension_flagged_and_disabled(self):
        self.assertEqual(scanner.scan_tdn(self._comp_with_ext(self.FOREIGN))["verdict"], "flagged")
        inert, summary = safe_import.make_inert(self._comp_with_ext(self.FOREIGN), is_pure_expr=PURE)
        self.assertEqual(summary["extensions_disabled"], 1)

    def test_opshortcut_hijack_is_stripped(self):
        # An attacker registering op.TDAnnotate to repoint the trusted ref at their
        # code: the global shortcut must be stripped so the palette ref stays real.
        tdn_hijack = self._comp_with_ext(self.PALETTE, parameters={"opshortcut": "TDAnnotate"})
        inert, summary = safe_import.make_inert(tdn_hijack, is_pure_expr=PURE)
        self.assertEqual(summary["global_shortcuts_stripped"], 1)
        self.assertNotIn("opshortcut", inert["operators"][0].get("parameters", {}))

    # The palette-trust check was a SUBSTRING search until 2026-08-30, so any
    # object string merely CONTAINING op.TD<Name> was trusted. Both payloads
    # below then reported extensions:0 AND survived make_inert with the
    # extension still enabled -- attacker code that runs on import. Found by the
    # C8 parity corpus. Trust is a strict full match now.
    BYPASS_COMMENT = "op('./Evil').module.Evil(me)  # op.TDFunctions"
    BYPASS_BRANCH = "op('./Evil').module.Evil(me) if True else op.TDModules"

    def test_palette_substring_in_a_comment_is_not_trusted(self):
        t = self._comp_with_ext(self.BYPASS_COMMENT)
        self.assertEqual(scanner.scan_tdn(t)["counts"]["extensions"], 1)
        _, summary = safe_import.make_inert(t, is_pure_expr=PURE)
        self.assertEqual(summary["extensions_disabled"], 1)

    def test_palette_substring_in_a_dead_branch_is_not_trusted(self):
        t = self._comp_with_ext(self.BYPASS_BRANCH)
        self.assertEqual(scanner.scan_tdn(t)["counts"]["extensions"], 1)
        _, summary = safe_import.make_inert(t, is_pure_expr=PURE)
        self.assertEqual(summary["extensions_disabled"], 1)

    def test_genuine_palette_refs_stay_trusted(self):
        """The hardening must not break real palette components."""
        for obj in ("op.TDAnnotate.mod.AnnotateExt.AnnotateExt(me)",
                    "op.TDModules.mod.TDFunctions"):
            with self.subTest(obj=obj):
                t = self._comp_with_ext(obj)
                self.assertEqual(scanner.scan_tdn(t)["counts"]["extensions"], 0)
                _, summary = safe_import.make_inert(t, is_pure_expr=PURE)
                self.assertEqual(summary["extensions_disabled"], 0)

    def test_scoped_parentshortcut_is_kept(self):
        node = tdn([{"name": "c", "type": "baseCOMP",
                     "parameters": {"parentshortcut": "Scene"}}])
        inert, summary = safe_import.make_inert(node, is_pure_expr=PURE)
        self.assertEqual(summary["global_shortcuts_stripped"], 0)
        self.assertEqual(inert["operators"][0]["parameters"]["parentshortcut"], "Scene")


class TestIssue94ReviewBypasses(unittest.TestCase):
    """Two bypasses that reached LIVE import with verdict clean (2026-08-29).

    Everything here goes through plan_community_paste -- the exact function
    CollectionExt calls -- because the second bypass lived in the gate itself:
    a clean verdict skipped make_inert, and make_inert was the only place the
    opshortcut strip ran.
    """
    EVIL = {"name": "Evil", "type": "textDAT",
            "dat_content": "class Evil:\n    def __init__(self, o):\n        pass\n"}

    def _plan(self, t):
        return safe_import.plan_community_paste(t, scanner.scan_tdn, PURE)

    def test_flat_ext0object_is_counted_and_disabled(self):
        t = tdn([{"name": "c", "type": "baseCOMP",
                  "parameters": {"ext0object": "op('./Evil').module.Evil(me)",
                                 "ext0promote": True},
                  "children": [self.EVIL]}])
        plan = self._plan(t)
        self.assertEqual(plan["capability"]["counts"]["extensions"], 1)
        self.assertEqual(plan["mode"], "inert")
        self.assertEqual(plan["summary"]["extensions_disabled"], 1)
        params = plan["tdn"]["operators"][0]["parameters"]
        self.assertNotIn("ext0object", params)
        self.assertNotIn("ext0promote", params)

    def test_flat_ext_in_type_defaults_is_disabled(self):
        t = tdn([{"name": "c", "type": "baseCOMP", "children": [self.EVIL]}],
                type_defaults={"baseCOMP": {"parameters": {
                    "ext0object": "op('./Evil').module.Evil(me)", "ext0promote": True}}})
        inert, summary = safe_import.make_inert(t, is_pure_expr=PURE)
        self.assertEqual(summary["extensions_disabled"], 1)
        self.assertNotIn("ext0object", inert["type_defaults"]["baseCOMP"]["parameters"])

    def test_attacker_named_td_shortcut_is_not_trusted(self):
        """op.TDEvil is not in the allowlist: counted, disabled, shortcut gone."""
        t = tdn([{"name": "c", "type": "baseCOMP",
                  "parameters": {"opshortcut": "TDEvil"},
                  "sequences": {"ext": [{"object": "op.TDEvil.mod.Evil.Evil(me)",
                                         "name": "E", "promote": True}]},
                  "children": [self.EVIL]}])
        plan = self._plan(t)
        self.assertEqual(plan["capability"]["counts"]["extensions"], 1)
        self.assertEqual(plan["mode"], "inert")
        self.assertEqual(plan["summary"]["extensions_disabled"], 1)
        self.assertNotIn("opshortcut", plan["tdn"]["operators"][0]["parameters"])

    def test_live_path_strips_global_shortcuts_too(self):
        """A CLEAN network still cannot register op.X -- including a real TD name."""
        t = tdn([{"name": "c", "type": "baseCOMP",
                  "parameters": {"opshortcut": "TDResources"}}])
        plan = self._plan(t)
        self.assertEqual(plan["mode"], "live")
        self.assertEqual(plan["summary"]["global_shortcuts_stripped"], 1)
        self.assertNotIn("opshortcut", plan["tdn"]["operators"][0]["parameters"])
        self.assertIn("opshortcut", t["operators"][0]["parameters"],
                      "the caller's dict must not be mutated")

    def test_type_defaults_shortcut_is_stripped_on_the_live_path(self):
        t = tdn([{"name": "c", "type": "baseCOMP"}],
                type_defaults={"baseCOMP": {"parameters": {"opshortcut": "TDModules"}}})
        plan = self._plan(t)
        self.assertEqual(plan["mode"], "live")
        self.assertNotIn("opshortcut", plan["tdn"]["type_defaults"]["baseCOMP"]["parameters"])

    def test_non_string_extension_object_is_disabled_not_ignored(self):
        t = tdn([{"name": "c", "type": "baseCOMP",
                  "sequences": {"ext": [{"object": ["op('./Evil')"], "name": "E",
                                         "promote": True}]},
                  "children": [self.EVIL]}])
        plan = self._plan(t)
        self.assertEqual(plan["capability"]["counts"]["extensions"], 1)
        self.assertEqual(plan["summary"]["extensions_disabled"], 1)
        self.assertEqual(plan["tdn"]["operators"][0]["sequences"]["ext"], [{}])

    def test_genuine_palette_extension_still_imports_live(self):
        t = tdn([{"name": "c", "type": "annotateCOMP",
                  "sequences": {"ext": [{"object": "op.TDAnnotate.mod.AnnotateExt.AnnotateExt(me)",
                                         "name": "E", "promote": True}]}}])
        plan = self._plan(t)
        self.assertEqual(plan["mode"], "live")
        self.assertTrue(plan["tdn"]["operators"][0]["sequences"]["ext"][0]["object"])

    def test_the_two_palette_copies_are_identical(self):
        """scanner.py and safe_import.py each carry the allowlist + regex."""
        self.assertEqual(scanner.TD_PALETTE_SHORTCUTS, safe_import.TD_PALETTE_SHORTCUTS)
        self.assertEqual(scanner._TD_PALETTE_REF.pattern, safe_import._TD_PALETTE_REF.pattern)


if __name__ == "__main__":
    unittest.main()
