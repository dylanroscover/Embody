"""
Test suite: TDN dirty detection via network fingerprint.

Regression test for the bug where _computeTDNFingerprint captured only
structural/visual properties (name, type, position, color, tags, flags,
comment, connections, annotations) and IGNORED parameter values -- so a
parameter edit on a TDN-strategy COMP, whether on the COMP's own top-level
custom pars or on a child operator, did not change the fingerprint and was
never flagged dirty. Only structural/layout edits were detected.

These tests pin the fix: parameter edits (constant, expression) on the root
and on children must change the fingerprint, structural edits must still
change it, and Embody-managed About metadata (Build/Date/Touchbuild) must
NOT change it (so build bumps don't cause spurious dirty).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNFingerprint(EmbodyTestCase):

    def _make_comp(self):
        """A base COMP with one child and a top-level custom float par."""
        comp = self.sandbox.create(baseCOMP, 'fp_comp')
        child = comp.create(constantCHOP, 'child1')
        child.nodeX, child.nodeY = 0, 0
        pg = comp.appendCustomPage('Test')
        p = pg.appendFloat('Testval', label='Test Value')[0]
        p.default = 0.0
        p.val = 0.0
        return comp, child

    def _fp(self, comp):
        return self.embody_ext._computeTDNFingerprint(comp)

    # --- parameter changes (the regression) ---

    def test_top_level_param_change_detected(self):
        comp, _ = self._make_comp()
        before = self._fp(comp)
        comp.par.Testval = 5.0
        self.assertNotEqual(
            before, self._fp(comp),
            'Top-level parameter change must change the TDN fingerprint')

    def test_child_param_change_detected(self):
        comp, child = self._make_comp()
        before = self._fp(comp)
        child.par.value0 = 3.0
        self.assertNotEqual(
            before, self._fp(comp),
            'Child operator parameter change must change the TDN fingerprint')

    def test_expression_change_detected(self):
        comp, _ = self._make_comp()
        comp.par.Testval = 1.0
        before = self._fp(comp)
        comp.par.Testval.expr = 'absTime.frame'
        self.assertNotEqual(
            before, self._fp(comp),
            'Switching a par to expression mode must change the fingerprint')

    # --- structural changes still detected ---

    def test_structural_move_still_detected(self):
        comp, child = self._make_comp()
        before = self._fp(comp)
        child.nodeX += 100
        self.assertNotEqual(
            before, self._fp(comp),
            'Structural (move) change must still change the fingerprint')

    # --- About-page metadata excluded (no spurious dirty on build bumps) ---

    def test_build_par_bump_ignored(self):
        comp, _ = self._make_comp()
        ab = comp.appendCustomPage('About')
        bp = ab.appendInt('Build')[0]
        bp.default = 1
        bp.val = 1
        before = self._fp(comp)
        comp.par.Build = 99
        self.assertEqual(
            before, self._fp(comp),
            'Build/Date/Touchbuild bumps must NOT change the fingerprint')

    # --- determinism ---

    def test_fingerprint_stable_when_unchanged(self):
        comp, _ = self._make_comp()
        self.assertEqual(
            self._fp(comp), self._fp(comp),
            'Fingerprint must be deterministic for an unchanged network')

    # --- baseline primed at externalize time (no lazy-on-first-scan gap) ---

    def test_baseline_primed_on_externalize(self):
        """TDN externalization must prime the dirty-detection baseline
        immediately -- not lazily on the first _isTDNDirty scan. Otherwise a
        param edit landing between externalize and that first scan would be
        absorbed into the baseline and the COMP would wrongly read clean."""
        import os
        emb = self.embody_ext
        comp = self.sandbox.create(baseCOMP, 'fp_prime')
        comp.create(constantCHOP, 'c')
        rel = None
        try:
            emb.applyTagToOperator(comp, self.embody.par.Tdntag.eval())
            tbl = emb.Externalizations
            for r in range(1, tbl.numRows):
                if tbl[r, 'path'].val == comp.path:
                    rel = tbl[r, 'rel_file_path'].val
                    break
            # Baseline must exist right after externalize, with NO scan between.
            self.assertIn(
                comp.path, emb._tdn_fingerprints,
                'externalization must prime the TDN fingerprint baseline')
            self.assertFalse(
                emb._isTDNDirty(comp),
                'a freshly externalized COMP must read clean')
        finally:
            if rel:
                try:
                    p = str(emb.buildAbsolutePath(emb.normalizePath(rel)))
                    if os.path.isfile(p):
                        os.remove(p)
                except Exception:
                    pass
                try:
                    emb.RemoveListerRow(comp.path, rel, delete_file=True)
                except Exception:
                    pass
            emb._tdn_fingerprints.pop(comp.path, None)
            try:
                emb.param_tracker.removeComp(comp)
            except Exception:
                pass


class TestTDNDirtyState(EmbodyTestCase):
    """dirtyHandler clean-clearing (Fix #5) and DirtyCount strategy-awareness
    (Fix #4) for TDN-strategy COMPs.

    These swap a synthetic externalizations table (a TDN row pointing at a
    real sandbox COMP) so the table-driven dirty paths run without any file
    I/O or real externalization. The table par is restored in tearDown.
    """

    def setUp(self):
        super().setUp()
        self._orig_table = self.embody.par.Externalizations.eval()
        self._orig_tdnmode = self.embody.par.Tdnmode.eval()
        self._primed = None

    def tearDown(self):
        self.embody.par.Externalizations = self._orig_table.path
        self.embody.par.Tdnmode = self._orig_tdnmode
        if self._primed is not None:
            self.embody_ext._tdn_fingerprints.pop(self._primed, None)
        super().tearDown()

    def _tdn_table(self, comp_path, dirty=''):
        """Build a synthetic table with one TDN-strategy row and swap it in."""
        t = self.sandbox.create(tableDAT, 'synthetic_externalizations')
        t.clear()
        t.appendRow(['path', 'strategy', 'dirty'])
        t.appendRow([comp_path, 'tdn', dirty])
        self.embody.par.Externalizations = t.path
        return t

    # --- Fix #5: passive scan clears a stale dirty flag on revert ---

    def test_dirtyHandler_clears_stale_dirty_when_clean(self):
        comp = self.sandbox.create(baseCOMP, 'revert_comp')
        comp.create(constantCHOP, 'c')
        t = self._tdn_table(comp.path, dirty='True')  # stale 'dirty' from a prior scan
        self.embody.par.Tdnmode = 'full'
        # Prime the baseline so the live network reads CLEAN (matches baseline).
        self.embody_ext._storeTDNFingerprint(comp)
        self._primed = comp.path
        # Passive scan: the COMP is clean now, so the stale flag must clear.
        self.embody_ext.dirtyHandler(False)
        self.assertEqual(
            t[comp.path, 'dirty'].val, '',
            'A clean TDN COMP must have its stale dirty flag cleared by the '
            'passive scan (otherwise the indicator sticks after a revert)')

    def test_dirtyHandler_marks_dirty_when_changed(self):
        comp = self.sandbox.create(baseCOMP, 'change_comp')
        comp.create(constantCHOP, 'c')
        t = self._tdn_table(comp.path, dirty='')
        self.embody.par.Tdnmode = 'full'
        self.embody_ext._storeTDNFingerprint(comp)
        self._primed = comp.path
        # Mutate the network so it diverges from the baseline.
        comp.create(constantCHOP, 'c2')
        self.embody_ext.dirtyHandler(False)
        self.assertEqual(
            t[comp.path, 'dirty'].val, 'True',
            'A structurally changed TDN COMP must be flagged dirty')

    # --- Fix #4: DirtyCount trusts the table for TDN COMPs, not oper.dirty ---

    def test_DirtyCount_clean_tdn_comp_not_counted(self):
        comp = self.sandbox.create(baseCOMP, 'count_clean')
        comp.create(constantCHOP, 'c')
        self._tdn_table(comp.path, dirty='')
        self.assertEqual(
            self.embody_ext.DirtyCount(), 0,
            'A clean TDN COMP (table dirty="") must NOT be counted')

    def test_DirtyCount_counts_dirty_tdn_comp_from_table(self):
        # The decisive case: the table says 'True' while live oper.dirty is
        # False. The OLD DirtyCount counted COMPs only via oper.dirty or a
        # 'Par' table value, so it MISSED a 'True' TDN row (and, in real use
        # where oper.dirty is always True for TDN COMPs, OVER-counted clean
        # ones). The strategy-aware branch reads the table value for TDN COMPs
        # regardless of oper.dirty.
        comp = self.sandbox.create(baseCOMP, 'count_dirty')
        comp.create(constantCHOP, 'c')
        self.assertFalse(comp.dirty,
            'precondition: synthetic sandbox COMP reads oper.dirty=False, so '
            'only the table-driven branch can produce a nonzero count here')
        self._tdn_table(comp.path, dirty='True')
        self.assertEqual(
            self.embody_ext.DirtyCount(), 1,
            'A TDN COMP flagged dirty in the table must be counted even when '
            'live oper.dirty is False')


class TestTDNFingerprintPersistence(EmbodyTestCase):
    """The fingerprint cache must live in ownerComp storage, not on the
    extension instance. An instance dict is wiped by every extension reinit
    (any source edit), and the next sweep's assume-clean seeding then adopts
    unsaved changes as the new baseline -- silently clearing real dirty
    state from the manager (2026-07-20: 13 dirty COMPs vanished this way).
    """

    def test_cache_is_storage_backed(self):
        cache = self.embody_ext._tdn_fingerprints
        self.assertIs(
            cache,
            self.embody.fetch('_tdn_fingerprints', None, search=False),
            'fingerprint cache must be the ownerComp storage dict itself, '
            'so it survives extension reinit')

    def test_mutations_land_in_storage(self):
        key = '/__fp_persistence_probe__'
        try:
            self.embody_ext._tdn_fingerprints[key] = ('probe',)
            stored = self.embody.fetch(
                '_tdn_fingerprints', None, search=False)
            self.assertEqual(
                stored.get(key), ('probe',),
                'in-place mutations must be visible through storage')
        finally:
            self.embody_ext._tdn_fingerprints.pop(key, None)

    def test_runtime_keys_excluded_from_tdn_export(self):
        # Storage-backed runtime state must never serialize into a .tdn.
        tdn_mod = self.embody.op('TDNExt').module
        # _suppress_dialogs: project.save() stores it True for the save
        # window and the TDN export runs INSIDE that window -- without the
        # exclusion every save bakes it into Embody.tdn and a later TDN
        # restore suppresses dialogs for the whole session.
        for key in ('_tdn_fingerprints', 'expand_order', 'git_status',
                    '_suppress_dialogs'):
            self.assertIn(
                key, tdn_mod.SKIP_STORAGE_KEYS,
                f'runtime storage key {key!r} must be skipped by TDN export')
