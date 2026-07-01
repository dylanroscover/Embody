"""
Test suite: live-if-clean + pure-value-expression preservation (v6 community paste).

Exercises the LIVE Collection modules (scanner + safe_import) wired into the
Collection COMP -- resolved inline per test via op.Embody.op('Collection/...').module,
never cached (TD may reinit the externalized .py at any time).

The v6 fix: a community specimen pasting must paste in WORKING. safe_import no longer
zeroes every expression -- it preserves PROVABLY PURE value expressions (par reads,
absTime, math.*, Par.eval(), arithmetic) and neutralizes only side-effecting ones.
The scanner's pure-expression policy fixes the .eval()/.store()/tdu/GLSL false
positives, and Script OPs + tox_ref shells are disarmed. CollectionExt routes a
'clean' scan to a LIVE import and anything flagged to a preserve-pure inert import.

ASCII punctuation only; tests are independent and deterministic.
"""


def _tdn(operators=None, **extra):
    t = {
        "format": "tdn", "version": "2.0", "generator": "unit-test",
        "td_build": "099.2025.32820", "network_path": "/p", "type": "baseCOMP",
        "operators": operators or [],
    }
    t.update(extra)
    return t


_BENIGN = (
    "=parent().par.Power.eval()",
    "=absTime.seconds * parent().par.Speed.eval()",
    "=math.cos(math.radians(parent().par.Sunelev.eval()))",
    "=op('ctrl').par.X.eval()",
    "=tdu.remap(absTime.seconds, 0, 1, 0, 10)",
    "=1 if me.par.Toggle.eval() else 0",
)
_MALICIOUS = (
    "=op('victim').destroy()",
    "=__import__('os').system('id')",
    "=open('/etc/passwd').read()",
    "=(f:=op('v').destroy)()",
    "=op('Code').module.do_it()",
    "=getattr(op('v'), 'destroy')()",
)


class CollectionPureExpressionTests(EmbodyTestCase):
    """Live-module contracts for the preserve-pure community paste."""

    def _modules(self):
        collection = self.embody.op('Collection')
        if collection is None:
            raise SkipTest('Collection component not present')
        si = collection.op('safe_import')
        sc = collection.op('scanner')
        if si is None or sc is None or si.module is None or sc.module is None:
            raise SkipTest('Collection scanner/safe_import not present')
        return si.module, sc.module

    # ---- purity validator -------------------------------------------------

    def test_benign_idioms_are_pure_malicious_are_not(self):
        _si, sc = self._modules()
        pure = sc.is_pure_value_expression
        for s in _BENIGN:
            self.assertTrue(pure(s[1:]), 'benign blocked: %s' % s)
        for s in _MALICIOUS:
            self.assertFalse(pure(s[1:]), 'malicious allowed: %s' % s)

    # ---- safe_import preserves pure, neutralizes dangerous ----------------

    def test_make_inert_preserves_pure_sequence_exprs(self):
        si, sc = self._modules()
        tdn = _tdn([{
            "name": "g", "type": "glslTOP",
            "sequences": {"vec": [{"name": "u",
                "valuex": "=parent().par.Power.eval()",
                "valuey": "=op('v').destroy()"}]},
        }])
        inert, summary = si.make_inert(tdn, is_pure_expr=sc.is_pure_value_expression)
        block = inert["operators"][0]["sequences"]["vec"][0]
        self.assertEqual(block["valuex"], "=parent().par.Power.eval()")  # preserved
        self.assertEqual(block["valuey"], 0)                              # neutralized
        self.assertEqual(summary["exprs_neutralized"], 1)

    def test_make_inert_without_predicate_neutralizes_all(self):
        si, _sc = self._modules()
        tdn = _tdn([{"name": "l", "type": "levelTOP",
                     "parameters": {"opacity": "=parent().par.X.eval()"}}])
        inert, summary = si.make_inert(tdn)
        self.assertEqual(inert["operators"][0]["parameters"]["opacity"], 0)
        self.assertEqual(summary["exprs_neutralized"], 1)

    # ---- scanner verdict --------------------------------------------------

    def test_par_eval_idiom_scans_clean(self):
        _si, sc = self._modules()
        res = sc.scan_tdn(_tdn([{"name": "g", "type": "glslTOP",
            "sequences": {"vec": [{"name": "u", "valuex": "=parent().par.Power.eval()"}]}}]))
        self.assertEqual(res["verdict"], "clean", res["findings"])

    def test_dangerous_expr_scans_flagged(self):
        _si, sc = self._modules()
        res = sc.scan_tdn(_tdn([{"name": "l", "type": "levelTOP",
            "parameters": {"opacity": "=op('v').destroy()"}}]))
        self.assertGreaterEqual(res["counts"]["file_read_exprs"], 1)

    def test_glsl_shader_dat_not_flagged_as_python(self):
        _si, sc = self._modules()
        for params in ({"language": "glsl"}, {"extension": "frag"}):
            res = sc.scan_tdn(_tdn([{"name": "px", "type": "textDAT",
                "parameters": params, "dat_content": "uniform vec4 u; void main(){}"}]))
            self.assertEqual(res["counts"]["execute_dats"], 0, params)

    # ---- Script OPs + tox_ref --------------------------------------------

    def test_script_op_flagged_and_bypassed(self):
        si, sc = self._modules()
        tdn = _tdn([{"name": "s", "type": "scriptTOP"}])
        self.assertGreaterEqual(sc.scan_tdn(tdn)["counts"]["execute_dats"], 1)
        inert, summary = si.make_inert(tdn, is_pure_expr=sc.is_pure_value_expression)
        self.assertIn("bypass", inert["operators"][0].get("flags", []))
        self.assertEqual(summary["script_ops_bypassed"], 1)

    def test_tox_ref_flagged_and_stripped(self):
        si, sc = self._modules()
        tdn = _tdn([{"name": "c", "type": "baseCOMP", "tox_ref": "x.tox"}])
        self.assertGreaterEqual(sc.scan_tdn(tdn)["counts"]["external_refs"], 1)
        inert, summary = si.make_inert(tdn, is_pure_expr=sc.is_pure_value_expression)
        self.assertNotIn("tox_ref", inert["operators"][0])
        self.assertEqual(summary["external_refs_stripped"], 1)

    # ---- TD palette trust + opshortcut hijack defense --------------------

    def test_palette_extension_trusted_foreign_disabled(self):
        si, sc = self._modules()
        palette = _tdn([{"name": "a", "type": "annotateCOMP", "sequences": {"ext": [
            {"object": "op.TDAnnotate.mod.AnnotateExt.AnnotateExt(me)", "name": "E"}]}}])
        foreign = _tdn([{"name": "b", "type": "baseCOMP", "sequences": {"ext": [
            {"object": "op('./Evil').module.Evil(me)", "name": "E"}]}}])
        self.assertEqual(sc.scan_tdn(palette)["verdict"], "clean")
        self.assertEqual(sc.scan_tdn(foreign)["verdict"], "flagged")
        pi, ps = si.make_inert(palette, is_pure_expr=sc.is_pure_value_expression)
        self.assertEqual(ps["extensions_disabled"], 0)
        fi, fs = si.make_inert(foreign, is_pure_expr=sc.is_pure_value_expression)
        self.assertEqual(fs["extensions_disabled"], 1)

    def test_opshortcut_hijack_stripped(self):
        si, sc = self._modules()
        hijack = _tdn([{"name": "evil", "type": "baseCOMP",
                        "parameters": {"opshortcut": "TDAnnotate"},
                        "sequences": {"ext": [
                            {"object": "op.TDAnnotate.mod.AnnotateExt.AnnotateExt(me)", "name": "x"}]}}])
        inert, summary = si.make_inert(hijack, is_pure_expr=sc.is_pure_value_expression)
        self.assertEqual(summary["global_shortcuts_stripped"], 1)
        self.assertNotIn("opshortcut", inert["operators"][0].get("parameters", {}))

    def test_palette_annotate_specimen_imports_live(self):
        collection = self.embody.op('Collection')
        if collection is None:
            raise SkipTest('Collection component not present')
        try:
            coll = collection.ext.Collection
        except Exception:
            raise SkipTest('CollectionExt not initialized')
        # A specimen whose only "extension" is a standard palette Annotate scans
        # clean and pastes LIVE -- no false "untrusted" flag.
        spec = _tdn([
            {"name": "a", "type": "annotateCOMP", "sequences": {"ext": [
                {"object": "op.TDAnnotate.mod.AnnotateExt.AnnotateExt(me)", "name": "E"}]}},
            {"name": "n", "type": "noiseTOP"},
        ])
        plan = coll.PlanCommunityPaste(spec)
        self.assertEqual(plan["mode"], "live")
        self.assertEqual(plan["capability"]["verdict"], "clean")

    # ---- is_inert purity-aware -------------------------------------------

    def test_is_inert_is_purity_aware(self):
        si, sc = self._modules()
        net = _tdn([{"name": "l", "type": "levelTOP",
                     "parameters": {"opacity": "=parent().par.X.eval()"}}])
        self.assertTrue(si.is_inert(net, is_pure_expr=sc.is_pure_value_expression))
        self.assertFalse(si.is_inert(net))

    # ---- CollectionExt live-if-clean routing ------------------------------

    def test_plan_clean_specimen_imports_live(self):
        collection = self.embody.op('Collection')
        if collection is None:
            raise SkipTest('Collection component not present')
        try:
            coll = collection.ext.Collection
        except Exception:
            raise SkipTest('CollectionExt not initialized')
        clean = _tdn([
            {"name": "noise1", "type": "noiseTOP"},
            {"name": "l", "type": "levelTOP", "inputs": ["noise1"],
             "parameters": {"opacity": "=parent().par.X.eval()"}},
        ])
        plan = coll.PlanCommunityPaste(clean)
        self.assertEqual(plan["mode"], "live")
        self.assertEqual(plan["capability"]["verdict"], "clean")
        # live mode hands back the tdn unchanged -> the pure expr survives.
        l = next(o for o in plan["tdn"]["operators"] if o["name"] == "l")
        self.assertEqual(l["parameters"]["opacity"], "=parent().par.X.eval()")

    def test_plan_flagged_specimen_disarms_but_preserves_pure(self):
        collection = self.embody.op('Collection')
        if collection is None:
            raise SkipTest('Collection component not present')
        try:
            coll = collection.ext.Collection
        except Exception:
            raise SkipTest('CollectionExt not initialized')
        flagged = _tdn([
            {"name": "s", "type": "scriptTOP",
             "parameters": {"opacity": "=parent().par.X.eval()"}},
        ])
        plan = coll.PlanCommunityPaste(flagged)
        self.assertEqual(plan["mode"], "inert")
        s = plan["tdn"]["operators"][0]
        self.assertIn("bypass", s.get("flags", []))                       # script disarmed
        self.assertEqual(s["parameters"]["opacity"], "=parent().par.X.eval()")  # pure kept
