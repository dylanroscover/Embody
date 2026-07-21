"""
Test suite: annotation guards -- annotations and their internals are never
tagged, externalized, tracked, or enumerated as per-op boundaries.

An annotateCOMP round-trips exclusively through the parent TDN COMP's
semantic `annotations:` section (export _exportAnnotations / import Phase
7a). Its internal widget ops (the annotation/back/body/title containers
and their color/i/help tables) are TD-managed stock content cloned from
/sys/TDTox/TDAnnotate. Before these guards, a non-utility annotation (as
Envoy's create_annotation used to make) was an ordinary COMP subtree:
ExternalizeProject's flat walks, Tdncascade, and the pre-save at-risk-DAT
sweep could tag its internals as their own TDN/source boundaries -- whose
reconstruction then gutted the widget (empty color table -> float(None)
cook errors) and stranded orphan files on disk (the TDN annotation
double-serialization report, 2026-07-21).
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestAnnotationGuards(EmbodyTestCase):

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'ann_guard_ws')

    def tearDown(self):
        """Clean up externalizations table rows for sandbox ops."""
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    def _annotate(self, parent=None):
        """Create an annotateCOMP (TD ignores the name arg for annotates --
        it lands as annotateN)."""
        return (parent or self.workspace).create(annotateCOMP)

    def _table_paths(self):
        return [self.embody_ext.Externalizations[i, 'path'].val
                for i in range(1, self.embody_ext.Externalizations.numRows)]

    # =========================================================================
    # _isInsideAnnotate / _isAnnotateInteriorPath helpers
    # =========================================================================

    def test_isInsideAnnotate_true_for_interior_op(self):
        ann = self._annotate()
        inner = ann.create(textDAT)
        self.assertTrue(self.embody_ext._isInsideAnnotate(inner))

    def test_isInsideAnnotate_false_for_sibling(self):
        sib = self.workspace.create(textDAT, 'guard_sib')
        self.assertFalse(self.embody_ext._isInsideAnnotate(sib))

    def test_isInsideAnnotate_false_for_annotate_itself(self):
        """The annotate ITSELF is not 'inside' an annotate -- callers that
        need to refuse the annotate too check its type separately."""
        ann = self._annotate()
        self.assertFalse(self.embody_ext._isInsideAnnotate(ann))

    def test_isAnnotateInteriorPath_live_resolution(self):
        ann = self._annotate()
        inner = ann.create(baseCOMP, 'guard_inner')
        self.assertTrue(self.embody_ext._isAnnotateInteriorPath(inner.path))
        self.assertFalse(
            self.embody_ext._isAnnotateInteriorPath(self.workspace.path))

    def test_isAnnotateInteriorPath_through_utility_hop(self):
        """Bare op() hides a utility annotate; the helper must still detect
        interior paths via its includeUtility fallback walk."""
        ann = self._annotate()
        inner = ann.create(baseCOMP, 'guard_inner_util')
        ann.utility = True
        self.assertTrue(self.embody_ext._isAnnotateInteriorPath(inner.path))

    def test_isAnnotateInteriorPath_unresolvable_is_false(self):
        """When the live network cannot testify, behave as before (no skip)."""
        self.assertFalse(
            self.embody_ext._isAnnotateInteriorPath('/no/such/path/here'))

    # =========================================================================
    # applyTagToOperator chokepoint
    # =========================================================================

    def test_applyTag_refuses_annotate(self):
        ann = self._annotate()
        tdn_tag = self.embody.par.Tdntag.val
        self.assertFalse(self.embody_ext.applyTagToOperator(ann, tdn_tag))
        self.assertNotIn(tdn_tag, ann.tags)
        self.assertNotIn(ann.path, self._table_paths())

    def test_applyTag_refuses_annotate_interior_comp(self):
        ann = self._annotate()
        inner = ann.create(baseCOMP, 'guard_tag_comp')
        tdn_tag = self.embody.par.Tdntag.val
        self.assertFalse(self.embody_ext.applyTagToOperator(inner, tdn_tag))
        self.assertNotIn(tdn_tag, inner.tags)
        self.assertNotIn(inner.path, self._table_paths())

    def test_applyTag_refuses_annotate_interior_dat(self):
        ann = self._annotate()
        inner = ann.create(textDAT)
        inner.text = 'stock-ish content'
        py_tag = self.embody.par.Pytag.val
        self.assertFalse(self.embody_ext.applyTagToOperator(inner, py_tag))
        self.assertNotIn(py_tag, inner.tags)

    def test_applyTag_still_tags_normal_comp(self):
        """Positive control: the guard must not refuse ordinary COMPs."""
        comp = self.workspace.create(baseCOMP, 'guard_normal')
        tox_tag = self.embody.par.Toxtag.val
        self.assertTrue(self.embody_ext.applyTagToOperator(comp, tox_tag))
        self.assertIn(tox_tag, comp.tags)

    # =========================================================================
    # Cascade, auto-externalize, at-risk sweep
    # =========================================================================

    def test_cascade_skips_annotate_child(self):
        """_cascadeTDNTag over a parent whose only child COMP is an annotate
        must tag nothing (and not raise)."""
        parent = self.workspace.create(baseCOMP, 'cascade_parent')
        ann = self._annotate(parent)
        self.embody_ext._cascadeTDNTag(parent)
        tdn_tag = self.embody.par.Tdntag.val
        self.assertNotIn(tdn_tag, ann.tags)
        self.assertNotIn(ann.path, self._table_paths())

    def test_autoExternalizeTagFor_skips_annotate_interior(self):
        """The auto-externalize decision returns None for annotate interiors.
        (In this sandbox the already-externalized-ancestor guard also skips
        them; if BOTH guards regress this fails.)"""
        prev = self.embody.par.Autoexternalize.eval()
        try:
            self.embody.par.Autoexternalize = 'both'
            ann = self._annotate()
            inner_comp = ann.create(baseCOMP, 'auto_inner')
            inner_dat = ann.create(textDAT)
            self.assertIsNone(
                self.embody_ext._autoExternalizeTagFor(inner_comp))
            self.assertIsNone(
                self.embody_ext._autoExternalizeTagFor(inner_dat))
        finally:
            self.embody.par.Autoexternalize = prev

    def test_findAtRiskDATs_skips_annotate_interior(self):
        """With a (synthetic) TDN row for the workspace and embed-DATs off,
        the at-risk sweep must flag a loose sibling DAT but never a DAT
        inside an annotation widget."""
        table = self.embody_ext.Externalizations
        table.appendRow([
            self.workspace.path, self.workspace.OPType, 'tdn',
            'embody/unit_tests/at_risk_fake.tdn', 'test', 'False', '', ''])
        try:
            self.workspace.store('embed_dats_in_tdn', False)
            loose = self.workspace.create(textDAT, 'at_risk_loose')
            loose.text = 'user content that would be lost'
            ann = self._annotate()
            interior = ann.create(tableDAT)
            interior.clear()
            interior.appendRow(['focus', 'font'])
            result = self.embody_ext._findAtRiskDATs()
            ws_dats = []
            for comp_path, dats in result:
                if comp_path == self.workspace.path:
                    ws_dats = [d.path for d in dats]
            self.assertIn(loose.path, ws_dats,
                          'loose sibling DAT must still be flagged at-risk')
            self.assertNotIn(interior.path, ws_dats,
                             'annotate-interior DAT must never be flagged')
        finally:
            self.workspace.unstore('embed_dats_in_tdn')

    # =========================================================================
    # _getTDNStrategyComps legacy-row filter
    # =========================================================================

    def test_getTDNStrategyComps_filters_annotate_interior_rows(self):
        """A legacy tsv row pointing inside an annotation widget must be
        skipped by the enumerator (neither reconstructed nor re-exported)."""
        ann = self._annotate()
        legacy = ann.create(baseCOMP, 'legacy_interior')
        table = self.embody_ext.Externalizations
        table.appendRow([
            legacy.path, legacy.OPType, 'tdn',
            'embody/unit_tests/legacy_interior_fake.tdn', 'test', 'False',
            '', ''])
        paths = [p for p, _ in self.embody_ext._getTDNStrategyComps()]
        self.assertNotIn(legacy.path, paths)

    def test_getTDNStrategyComps_filters_row_AT_nonutility_annotate(self):
        """A legacy row whose path IS the annotate itself must be filtered
        even when the annotate is non-utility (the shape old cascade /
        ExternalizeProject runs produced on pre-fix Envoy annotations)."""
        ann = self._annotate()  # .create() => utility=False (legacy shape)
        table = self.embody_ext.Externalizations
        table.appendRow([
            ann.path, ann.OPType, 'tdn',
            'embody/unit_tests/legacy_at_annotate_fake.tdn', 'test', 'False',
            '', ''])
        paths = [p for p, _ in self.embody_ext._getTDNStrategyComps()]
        self.assertNotIn(ann.path, paths)

    def test_getTDNStrategyComps_filters_row_AT_utility_annotate(self):
        """Same row shape with the annotate utility=True (hidden from bare
        op()) -- the walk branch must catch the annotate leaf."""
        ann = self._annotate()
        ann.utility = True
        table = self.embody_ext.Externalizations
        table.appendRow([
            ann.path, ann.OPType, 'tdn',
            'embody/unit_tests/legacy_at_util_annotate_fake.tdn', 'test',
            'False', '', ''])
        paths = [p for p, _ in self.embody_ext._getTDNStrategyComps()]
        self.assertNotIn(ann.path, paths)

    def test_isAnnotateInteriorPath_true_for_annotate_leaf_both_flags(self):
        """Both branches of _isAnnotateInteriorPath must agree that a path
        AT an annotate is inert, regardless of the utility flag."""
        ann = self._annotate()
        self.assertTrue(self.embody_ext._isAnnotateInteriorPath(ann.path))
        ann.utility = True
        self.assertTrue(self.embody_ext._isAnnotateInteriorPath(ann.path))

    def test_tagSetter_add_refuses_annotate(self):
        """The tagger-UI chokepoint (TagSetter) must refuse the ADD branch
        for annotates and their internals -- while its REMOVE branch stays
        open for legacy cleanup."""
        ann = self._annotate()
        inner = ann.create(baseCOMP, 'tagsetter_inner')
        tdn_tag = self.embody.par.Tdntag.val
        self.assertFalse(self.embody_ext.TagSetter(ann, tdn_tag))
        self.assertNotIn(tdn_tag, ann.tags)
        self.assertFalse(self.embody_ext.TagSetter(inner, tdn_tag))
        self.assertNotIn(tdn_tag, inner.tags)
        # REMOVE branch: a legacy-tagged annotate must still untag.
        ann.tags.add(tdn_tag)
        self.assertTrue(self.embody_ext.TagSetter(ann, tdn_tag))
        self.assertNotIn(tdn_tag, ann.tags)

    def test_getTDNStrategyComps_keeps_normal_rows(self):
        """Positive control: an ordinary sandbox COMP row still enumerates."""
        comp = self.workspace.create(baseCOMP, 'normal_row_comp')
        table = self.embody_ext.Externalizations
        table.appendRow([
            comp.path, comp.OPType, 'tdn',
            'embody/unit_tests/normal_row_fake.tdn', 'test', 'False', '', ''])
        paths = [p for p, _ in self.embody_ext._getTDNStrategyComps()]
        self.assertIn(comp.path, paths)
