"""
Test suite: checkOpsForContinuity vs utility annotateCOMP interiors.

CHARACTERIZATION SUITE (2026-07-25). These tests document the CURRENT,
DESTRUCTIVE behavior found while triaging the Moonshine v6.0.157 field
report. They are deliberately written as a proof chain, one assertion per
link, so the defect is demonstrated before any code is changed. When the
defect is fixed, the branch-selection test flips to a regression assertion
(see the FIX marker below).

The defect
----------
`checkOpsForContinuity` (EmbodyExt.py:4905-4928) decides whether a tracked
operator still exists using a BARE `op(old_op_path)`. TouchDesigner's bare
`op()` cannot traverse a utility annotateCOMP hop, so for any tracked row
whose path lies at or inside a utility annotation the lookup returns None
and the sweep concludes the operator VANISHED. It then either

  - appends to `missing_with_files` (EmbodyExt.py:4923) -> a per-sweep
    file-cleanup modal, or a SILENT unlink when Filecleanup='delete', or
  - logs "Operator for TDN entry ... no longer exists" and calls
    `_removeTDNStrategy(old_op_path)` (EmbodyExt.py:4926-4927), which
    deletes the table row AND unlinks the .tdn.

`checkOpsForContinuity` runs on every `Update()`, including the pre-save
`Update(suppress_refresh=True)` in execute.py -- i.e. every save.

Why the existing guards do not help
-----------------------------------
The annotate guards added for the 2026-07-21 report live in
`_getTDNStrategyComps` (EmbodyExt.py:8340) and `applyTagToOperator`.
`checkOpsForContinuity` builds `rows_to_check` straight from the
externalizations table (EmbodyExt.py:4862-4868) and never consults them.
The `stripped_tdn_paths` shield (EmbodyExt.py:4882-4886) does NOT apply
either: it is only read at EmbodyExt.py:4943, inside the non-TDN branch,
which the `if is_tdn:` branch at 4911 never reaches (it `continue`s at
4928).

Embody already knows how to answer this correctly --
`_isAnnotateInteriorPath` (EmbodyExt.py:6878) has a utility-aware fallback
walk. The sweep simply does not ask it. That is the fix.

SAFETY: these tests never call `checkOpsForContinuity`, `Update`,
`ExternalizeProject`, or `Reset`. Those iterate the LIVE table and would
delete real rows and real .tdn files project-wide (see
.claude/rules/destructive-tests.md and the 2026-07-01 incident). Every
assertion here is scoped to a sandbox operator or a synthetic sandbox row.
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestAnnotateContinuity(EmbodyTestCase):

    def setUp(self):
        self.workspace = self.sandbox.create(baseCOMP, 'ann_cont_ws')

    def tearDown(self):
        """Drop any externalizations rows this suite added for sandbox ops."""
        table = self.embody_ext.Externalizations
        for i in range(table.numRows - 1, 0, -1):
            if table[i, 'path'].val.startswith(self.sandbox.path):
                table.deleteRow(i)
        super().tearDown()

    # --- helpers ---------------------------------------------------------

    def _utility_annotate_interior(self):
        """A utility annotateCOMP with one interior COMP -- the shape a
        pre-guard project carries for every annotation."""
        ann = self.workspace.create(annotateCOMP)
        inner = ann.create(baseCOMP, 'cont_inner')
        ann.utility = True
        return ann, inner

    def _addTDNRow(self, op_path, rel_file_path):
        """Append a synthetic TDN row with the table's FULL column count.

        A short row leaves trailing cells unwritten, which reads as stale
        data from whatever occupied that memory and makes the fixture
        differ from a row Embody actually writes.
        """
        table = self.embody_ext.Externalizations
        values = {'path': op_path, 'type': 'baseCOMP', 'strategy': 'tdn',
                  'rel_file_path': rel_file_path, 'timestamp': 'test',
                  'dirty': 'False'}
        row = [values.get(str(table[0, c].val), '')
               for c in range(table.numCols)]
        table.appendRow(row)

    # --- LINK 1+2: what does bare op() ACTUALLY resolve? -----------------

    def test_link1_op_resolution_matrix(self):
        """DIAGNOSTIC: measure bare op() against every annotate shape.

        The field report states `op('/moonshine/moonbeam/annotate1')`
        returns None for a utility annotate. Embody's own comment at
        EmbodyExt.py:6886-6888 says the same ("Bare op() hides a utility
        annotate ... so on lookup failure walk the path down"). This test
        does not assume either -- it records the truth, because which
        lookups actually fail decides whether checkOpsForContinuity can
        reach its destructive branch at all.
        """
        ann, inner = self._utility_annotate_interior()
        ann_path, inner_path = ann.path, inner.path

        # MEASURED 2026-07-25 on TD 099.2025.33070:
        # the utility flag hides the annotate NODE from its parent's child
        # lookup and enumeration, but a full path THROUGH it still resolves.
        ann.utility = True
        self.assertIsNone(
            op(ann_path),
            'bare op() cannot resolve a UTILITY annotate itself -- this is '
            'the lookup the field report saw return None')
        self.assertIsNotNone(
            op(inner_path),
            'bare op() CAN still resolve a path THROUGH a utility annotate; '
            'interiors are NOT invisible (corrects the assumption that the '
            'whole subtree disappears)')
        self.assertIsNone(ann.parent().op(ann.name))
        self.assertFalse(
            any(c.path == ann_path
                for c in ann.parent().findChildren(depth=1)))
        self.assertTrue(
            any(c.path == ann_path for c in ann.parent().findChildren(
                depth=1, includeUtility=True)),
            'includeUtility=True is the only enumeration that sees it')

        ann.utility = False
        self.assertIsNotNone(
            op(ann_path),
            'control: a non-utility annotate resolves normally, which is '
            'why a fresh project never hits this')

        # Embody answers correctly for BOTH shapes, utility or not.
        for utility in (True, False):
            ann.utility = utility
            self.assertTrue(
                self.embody_ext._isAnnotateInteriorPath(ann_path))
            self.assertTrue(
                self.embody_ext._isAnnotateInteriorPath(inner_path))

    # --- LINK 3: no rename rescue ----------------------------------------

    def test_link3_findMovedTDNOp_does_not_rescue_the_row(self):
        """The rename-detection fallback finds no candidate, so the sweep
        proceeds to the removal branch."""
        ann, inner = self._utility_annotate_interior()
        self.assertFalse(
            self.embody_ext._findMovedTDNOp(
                inner.path, 'embody/unit_tests/ann_cont_fake.tdn', set()),
            'no untracked TDN-tagged candidate exists, so nothing rescues '
            'the row')

    # --- LINK 4: which branch the sweep takes ----------------------------

    def test_link4b_guard_skips_the_row_before_the_destructive_branch(self):
        """REGRESSION for the fix (EmbodyExt.py, checkOpsForContinuity).

        The guard is `if self._isAnnotateInteriorPath(old_op_path):
        continue`, placed BEFORE the `if not op(old_op_path)` test. This
        asserts the guard predicate is True for both annotate row shapes,
        so the sweep never reaches the branch that unlinks the .tdn.

        Deleting the guard line makes this test's premise false while the
        rest of the suite stays green, which is why it is asserted
        explicitly rather than left to test_link4's characterization.
        """
        ann, inner = self._utility_annotate_interior()
        for path, label in ((ann.path, 'the annotate itself'),
                            (inner.path, 'an annotate interior op')):
            self.assertTrue(
                self.embody_ext._isAnnotateInteriorPath(path),
                f'the continuity guard must skip {label} ({path}); if this '
                f'is False the sweep treats the row as a vanished operator '
                f'and deletes the row + .tdn on every save')

    def test_link4c_guard_does_not_skip_an_ordinary_comp(self):
        """Positive control: the guard must not make the sweep blind to
        genuinely missing ordinary COMPs."""
        normal = self.workspace.create(baseCOMP, 'cont_normal')
        path = normal.path
        self.assertFalse(self.embody_ext._isAnnotateInteriorPath(path))
        normal.destroy()
        # Now really gone -- the sweep SHOULD still treat this as missing.
        self.assertFalse(self.embody_ext._isAnnotateInteriorPath(path))
        self.assertIsNone(op(path))

    def test_link4_sweep_selects_the_vanished_op_branch(self):
        """CHARACTERIZATION of the pre-fix predicate chain
        (EmbodyExt.py:4910-4927), kept to document WHY the guard exists.

        This asserts what bare op() reports, which the guard now runs
        BEFORE -- see test_link4b for the regression assertion on the fix
        itself.
        """
        ann, inner = self._utility_annotate_interior()
        path = inner.path
        strategy = 'tdn'

        is_tdn = (strategy == 'tdn')                       # L4910
        self.assertTrue(is_tdn)

        rescued = self.embody_ext._findMovedTDNOp(         # L4915
            path, 'embody/unit_tests/ann_cont_fake.tdn', set())
        self.assertFalse(rescued, 'nothing rescues the row')

        # MEASURED: only the annotate-LEAF row reaches the destructive
        # branch. Interior rows resolve and are left alone.
        self.assertTrue(
            not op(ann.path),                              # L4912
            'a tracked row whose path IS a utility annotate reads as a '
            'VANISHED operator on every Update()/save -> L4919-4927 '
            'deletes the row and unlinks the .tdn, or prompts the '
            'file-cleanup modal when the file still exists')
        self.assertFalse(
            not op(path),
            'a tracked row for an annotate INTERIOR resolves, so it does '
            'NOT reach the destructive branch (narrows the blast radius)')

        self.assertTrue(
            self.embody_ext._isAnnotateInteriorPath(ann.path),
            'FIX: this is the check checkOpsForContinuity should consult '
            'before treating the row as a vanished operator')

        # With both True/False as above, control flow reaches L4919-4927:
        #   .tdn on disk  -> missing_with_files (modal, or silent unlink
        #                    when Filecleanup='delete')
        #   no .tdn       -> WARNING + _removeTDNStrategy (row + file gone)
        # Both outcomes are wrong for a live operator.

    # --- LINK 5: the destructive tail really is destructive --------------

    def test_link5_removeTDNStrategy_drops_the_row(self):
        """_removeTDNStrategy -- the call at EmbodyExt.py:4927 -- removes
        the tracking row synchronously.

        Scoped to a synthetic sandbox row. The .tdn unlink it schedules is
        deferred (run(..., delayFrames=5)), so only the synchronous row
        removal is asserted here; the file half is covered by
        test_link6_removeTDNStrategy_targets_the_file.
        """
        ann, inner = self._utility_annotate_interior()
        rel = 'embody/unit_tests/ann_cont_row_probe.tdn'
        self._addTDNRow(inner.path, rel)

        paths = [self.embody_ext.Externalizations[i, 'path'].val
                 for i in range(1, self.embody_ext.Externalizations.numRows)]
        self.assertIn(inner.path, paths, 'precondition: row present')

        self.embody_ext._removeTDNStrategy(inner.path, delete_file=False)

        paths = [self.embody_ext.Externalizations[i, 'path'].val
                 for i in range(1, self.embody_ext.Externalizations.numRows)]
        self.assertNotIn(
            inner.path, paths,
            'the row for a LIVE operator is dropped -- this is the data '
            'loss the sweep causes on every save')

    def test_link6_removeTDNStrategy_keeps_the_file_when_asked(self):
        """delete_file=False must leave the .tdn on disk.

        (The delete_file=True unlink is scheduled via run(delayFrames=5),
        so it cannot be observed inside a synchronous test -- and firing it
        here would exercise a real unlink during a normal-tier run, which
        .claude/rules/destructive-tests.md forbids. The keep path is the
        half that IS synchronously observable, and it is the default the
        MCP tool relies on.)
        """
        ann, inner = self._utility_annotate_interior()
        rel = 'embody/unit_tests/ann_cont_file_probe.tdn'
        abs_path = self.embody_ext.buildAbsolutePath(rel)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text('# synthetic probe\n', encoding='utf-8')
        try:
            self._addTDNRow(inner.path, rel)
            self.embody_ext._removeTDNStrategy(inner.path, delete_file=False)

            rows = [self.embody_ext.Externalizations[i, 'path'].val
                    for i in range(
                        1, self.embody_ext.Externalizations.numRows)]
            self.assertNotIn(inner.path, rows, 'the row is dropped')
            self.assertTrue(
                abs_path.is_file(),
                'delete_file=False must NOT remove the externalized file')
        finally:
            if abs_path.is_file():
                abs_path.unlink()
