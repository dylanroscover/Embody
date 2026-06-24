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
    "op('button').par.Reset.pulse()",
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

    def test_scoped_parentshortcut_is_kept(self):
        node = tdn([{"name": "c", "type": "baseCOMP",
                     "parameters": {"parentshortcut": "Scene"}}])
        inert, summary = safe_import.make_inert(node, is_pure_expr=PURE)
        self.assertEqual(summary["global_shortcuts_stripped"], 0)
        self.assertEqual(inert["operators"][0]["parameters"]["parentshortcut"], "Scene")


if __name__ == "__main__":
    unittest.main()
