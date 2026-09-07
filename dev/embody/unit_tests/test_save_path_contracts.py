"""
Test suite: save-path write contracts (the v6.0.243 optimization work).

These cover code production now depends on that nothing asserted:

  - `_updateRowCells` is the SOLE writer for build/timestamp/dirty/position in
    Save(), SaveTDXN(), _updatePositionInTable() and TDXNExt._trackTDXNExport().
    It returns False silently when a lookup misses, so a broken resolve drops a
    build stamp with no error raised anywhere.
  - `_dirtyHandlerDeferred` / `_sweepTDXNDirtyChunk` replaced the synchronous
    `dirtyHandler(False)` on the Refresh path. The pre-existing dirty tests call
    `dirtyHandler(False)` DIRECTLY -- a path production no longer takes -- so the
    passive pipeline behind the manager's badges could stop landing flags
    entirely with the suite still green.
  - `cleanupAllDuplicateRows` keeps the most recent row PER (path, type). A
    rewrite that kept the OLDEST, or that grouped by path alone and destroyed a
    legitimate tox+tdn pair, satisfied every assertion that existed.

The single-row-write property itself (one replaceRow instead of N cell writes)
is a performance characteristic measured separately; what is pinned here is the
BEHAVIOUR that batching must not change.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_COLS = ['path', 'type', 'strategy', 'rel_file_path', 'timestamp', 'dirty',
         'build', 'touch_build', 'node_x', 'node_y', 'node_color']


class _SyntheticTable(EmbodyTestCase):
    """Swaps a synthetic externalizations table in; restores it in tearDown."""

    def setUp(self):
        super().setUp()
        self._orig_table = self.embody.par.Externalizations.eval()
        self._orig_link = self.embody.par.Externalizations.val  # restore the VALUE: .path is absolute

    def tearDown(self):
        self.embody.par.Externalizations = self._orig_link
        super().tearDown()

    def _mkrow(self, path, **kw):
        vals = dict.fromkeys(_COLS, '')
        vals['path'] = path
        vals.update(kw)
        return [vals[c] for c in _COLS]

    def _table(self, rows, cols=None):
        t = self.sandbox.create(tableDAT, 'synthetic_ext')
        t.clear()
        t.appendRow(list(cols or _COLS))
        for r in rows:
            t.appendRow(list(r))
        self.embody.par.Externalizations = t.path
        return t


class TestUpdateRowCells(_SyntheticTable):

    def test_applies_every_change_to_the_named_row(self):
        t = self._table([self._mkrow('/a', build='1'), self._mkrow('/b')])
        ok = self.embody_ext._updateRowCells(
            '/a', {'build': '7', 'timestamp': 'TS', 'dirty': 'True'})
        self.assertTrue(ok)
        self.assertEqual('7', t['/a', 'build'].val)
        self.assertEqual('TS', t['/a', 'timestamp'].val)
        self.assertEqual('True', t['/a', 'dirty'].val)
        self.assertEqual('', t['/b', 'build'].val, 'other rows untouched')

    def test_no_op_when_nothing_changed(self):
        """replaceRow touches the .tsv even for identical values, so an
        unchanged update must not write at all."""
        t = self._table([self._mkrow('/a', build='7')])
        before = t.text
        ok = self.embody_ext._updateRowCells('/a', {'build': '7'})
        self.assertFalse(ok, 'an unchanged update must report no write')
        self.assertEqual(before, t.text)

    def test_missing_row_is_a_no_op(self):
        t = self._table([self._mkrow('/a')])
        before = t.text
        self.assertFalse(
            self.embody_ext._updateRowCells('/no/such/path', {'build': '9'}))
        self.assertEqual(before, t.text)

    def test_unknown_column_is_ignored_without_widening_the_table(self):
        t = self._table([self._mkrow('/a')])
        cols_before = t.numCols
        ok = self.embody_ext._updateRowCells(
            '/a', {'dirty': 'True', 'not_a_column': 'x'})
        self.assertTrue(ok)
        self.assertEqual('True', t['/a', 'dirty'].val)
        self.assertEqual(cols_before, t.numCols,
                         'an unknown key must never append a column')

    def test_int_row_key_and_path_key_address_the_same_row(self):
        t = self._table([self._mkrow('/a'), self._mkrow('/b')])
        self.embody_ext._updateRowCells(2, {'dirty': 'ByIndex'})
        self.assertEqual('ByIndex', t['/b', 'dirty'].val)
        self.embody_ext._updateRowCells('/b', {'dirty': 'ByPath'})
        self.assertEqual('ByPath', t['/b', 'dirty'].val)

    def test_short_row_survives_an_update(self):
        """Issue-#21 shape: a half-written row with fewer cells than columns
        must not corrupt its neighbours when written through."""
        t = self._table([self._mkrow('/a')])
        t.appendRow(['/short', 'base'])          # deliberately short
        ok = self.embody_ext._updateRowCells('/short', {'dirty': 'True'})
        self.assertTrue(ok)
        self.assertEqual('True', t['/short', 'dirty'].val)
        self.assertEqual('/short', t['/short', 'path'].val)
        self.assertEqual('/a', t[1, 'path'].val, 'neighbour row intact')


class TestDeferredDirtySweep(_SyntheticTable):
    """The passive dirty pipeline Refresh() actually uses."""

    def setUp(self):
        super().setUp()
        self._orig_tdxnmode = self.embody.par.Tdxnmode.eval()
        self.embody.par.Tdxnmode = 'full'
        self._primed = []

    def tearDown(self):
        self.embody.par.Tdxnmode = self._orig_tdxnmode
        for p in self._primed:
            self.embody_ext._tdn_fingerprints.pop(p, None)
        for attr in ('_DIRTY_SWEEP_BUDGET_MS',):
            if attr in self.embody_ext.__dict__:
                del self.embody_ext.__dict__[attr]
        super().tearDown()

    def _drain(self):
        """Run the sweep to completion in one call (no run() scheduling)."""
        self.embody_ext.__dict__['_DIRTY_SWEEP_BUDGET_MS'] = 100000.0
        self.embody_ext._dirtyHandlerDeferred()
        self.embody_ext._sweepTDXNDirtyChunk(self.embody_ext._dirty_gen)

    def _two_comps(self):
        clean = self.sandbox.create(baseCOMP, 'sweep_clean')
        clean.create(constantCHOP, 'c')
        dirty = self.sandbox.create(baseCOMP, 'sweep_dirty')
        dirty.create(constantCHOP, 'c')
        t = self._table([self._mkrow(clean.path, type='base', strategy='tdn'),
                         self._mkrow(dirty.path, type='base', strategy='tdn')])
        for comp in (clean, dirty):
            self.embody_ext._storeTDXNFingerprint(comp)
            self._primed.append(comp.path)
        dirty.create(constantCHOP, 'c2')   # diverge from its baseline
        return t, clean, dirty

    def test_matches_the_synchronous_handler_exactly(self):
        """The headline: the deferred sweep must land what dirtyHandler(False)
        landed, since production no longer calls the synchronous one."""
        t, clean, dirty = self._two_comps()
        self.embody_ext.dirtyHandler(False)
        expected = t.text
        t[clean.path, 'dirty'] = ''
        t[dirty.path, 'dirty'] = ''
        self._drain()
        self.assertEqual(expected, t.text)

    def test_marks_dirty_and_clears_clean(self):
        # Runtime-only since 2026-08-20: the sweep flags DirtyState and
        # the tsv cell stays blank by contract.
        t, clean, dirty = self._two_comps()
        self._drain()
        self.assertEqual('True', self.embody_ext.dirtyState(dirty.path))
        self.assertEqual('', self.embody_ext.dirtyState(clean.path))
        self.assertEqual('', t[dirty.path, 'dirty'].val,
                         'dirty never persists into the table')

    def test_budget_is_checked_after_the_work_so_it_always_advances(self):
        """A budget checked BEFORE the work would re-arm forever and the sweep
        would never finish."""
        t, clean, dirty = self._two_comps()
        self.embody_ext.__dict__['_DIRTY_SWEEP_BUDGET_MS'] = 0.0
        self.embody_ext._dirtyHandlerDeferred()
        self.embody_ext._sweepTDXNDirtyChunk(self.embody_ext._dirty_gen)
        self.assertGreaterEqual(self.embody_ext._dirty_idx, 1)

    def test_a_superseded_generation_does_nothing(self):
        t, clean, dirty = self._two_comps()
        self.embody_ext._dirtyHandlerDeferred()
        stale = self.embody_ext._dirty_gen
        self.embody_ext._dirtyHandlerDeferred()      # supersedes
        before = t.text
        self.embody_ext._sweepTDXNDirtyChunk(stale)
        self.assertEqual(0, self.embody_ext._dirty_idx)
        self.assertEqual(before, t.text)

    def test_perform_mode_stops_the_sweep(self):
        t, clean, dirty = self._two_comps()
        self.embody_ext._dirtyHandlerDeferred()
        gen = self.embody_ext._dirty_gen
        before = t.text
        cls = type(self.embody_ext)
        orig = cls._performMode
        try:
            cls._performMode = property(lambda s: True)
            self.embody_ext._sweepTDXNDirtyChunk(gen)
        finally:
            cls._performMode = orig
        self.assertEqual(0, self.embody_ext._dirty_idx)
        self.assertEqual(before, t.text)

    def test_a_deleted_operator_in_the_queue_is_skipped(self):
        t, clean, dirty = self._two_comps()
        self.embody_ext.__dict__['_DIRTY_SWEEP_BUDGET_MS'] = 100000.0
        self.embody_ext._dirtyHandlerDeferred()
        self.embody_ext._dirty_queue = ['/no/such/comp', dirty.path]
        self.embody_ext._dirty_idx = 0
        self.embody_ext._sweepTDXNDirtyChunk(self.embody_ext._dirty_gen)
        self.assertEqual('True', self.embody_ext.dirtyState(dirty.path),
                         'a vanished path must not abort the rest of the queue')

    def test_root_and_excluded_comps_never_enter_the_queue(self):
        comp = self.sandbox.create(baseCOMP, 'sweep_excluded')
        comp.tags = [self.embody.par.Tdxnexcludetag.eval()]
        self._table([self._mkrow('/', type='base', strategy='tdn'),
                     self._mkrow(comp.path, type='base', strategy='tdn')])
        self.embody_ext._dirtyHandlerDeferred()
        queued = list(getattr(self.embody_ext, '_dirty_queue', []))
        self.assertNotIn('/', queued)
        self.assertNotIn(comp.path, queued)


class TestDuplicateRowSemantics(_SyntheticTable):

    def test_keeps_the_most_recent_row_per_path_and_strategy(self):
        """A tox row and a tdn row for one path are NOT duplicates.

        Both carry the same `type` (the OP type, e.g. 'base') -- only
        `strategy` distinguishes them. Grouping on `type` put a legitimate
        pair in one group and deleted the older row on every Refresh.
        """
        t = self._table([
            self._mkrow('/dup', type='base', strategy='tox',
                        timestamp='2020-01-01 00:00:00 UTC'),
            self._mkrow('/dup', type='base', strategy='tox',
                        timestamp='2030-01-01 00:00:00 UTC'),
            self._mkrow('/dup', type='base', strategy='tdn',
                        timestamp='2020-01-01 00:00:00 UTC'),
        ])
        self.embody_ext.cleanupAllDuplicateRows()
        kept = [(t[i, 'strategy'].val, t[i, 'timestamp'].val)
                for i in range(1, t.numRows)]
        self.assertEqual(
            2, len(kept),
            'the tox row and the tdn row are different externalizations of '
            'one COMP -- both must survive')
        self.assertIn(('tox', '2030-01-01 00:00:00 UTC'), kept,
                      'the NEWEST tox row must be the survivor')
        self.assertIn(('tdn', '2020-01-01 00:00:00 UTC'), kept,
                      'a different strategy is not a duplicate')

    def test_deletes_across_several_groups_without_shifting_the_wrong_rows(self):
        """Stale rows are deleted highest index first; ascending deletion would
        silently remove the WRONG rows while still shrinking the table."""
        rows = []
        for name in ('/x', '/y', '/z'):
            rows.append(self._mkrow(name, type='base', strategy='tox',
                                    timestamp='2020-01-01 00:00:00 UTC'))
            rows.append(self._mkrow(name, type='base', strategy='tox',
                                    timestamp='2030-01-01 00:00:00 UTC'))
        t = self._table(rows)
        self.embody_ext.cleanupAllDuplicateRows()
        survivors = sorted((t[i, 'path'].val, t[i, 'timestamp'].val)
                           for i in range(1, t.numRows))
        self.assertEqual(
            [('/x', '2030-01-01 00:00:00 UTC'),
             ('/y', '2030-01-01 00:00:00 UTC'),
             ('/z', '2030-01-01 00:00:00 UTC')],
            survivors)


class TestRestampNodeGeometry(_SyntheticTable):
    """node_x / node_y / node_color used to be written ONLY at track time.

    _restorePositionFromTable replays those three columns verbatim when it
    rebuilds a missing operator, so a stale row put the op back at its old
    coordinates in its old colour. Field 2026-09-07: 145 rows in the dev
    project were stale, 12 of them still carrying TD's default grey after
    the operator had been tagged. Refresh now restamps them.
    """

    def _live(self, name):
        """A real sandbox DAT at a known position and colour."""
        d = self.sandbox.create(textDAT, name)
        d.nodeX, d.nodeY = 1234, -567
        d.color = (0.9, 0.3, 0.4)
        return d

    def test_restamps_a_row_whose_operator_has_moved(self):
        d = self._live('restamp_moved')
        t = self._table([self._mkrow(d.path, node_x='0', node_y='0',
                                     node_color='0.6700,0.6700,0.6700')])
        self.assertEqual(1, self.embody_ext._restampNodeGeometry())
        self.assertEqual('1234', t[1, 'node_x'].val)
        self.assertEqual('-567', t[1, 'node_y'].val)
        self.assertEqual('0.9000,0.3000,0.4000', t[1, 'node_color'].val)

    def test_is_idempotent_and_writes_nothing_when_already_current(self):
        d = self._live('restamp_current')
        self._table([self._mkrow(d.path, node_x='1234', node_y='-567',
                                 node_color='0.9000,0.3000,0.4000')])
        self.assertEqual(0, self.embody_ext._restampNodeGeometry(),
                         'a current row must not be rewritten -- the table is '
                         'syncfile-backed, so every write costs a file sync')

    def test_a_missing_operator_is_left_alone(self):
        """The restore path owns missing ops; clobbering their stored
        geometry with nothing would lose the coordinates it restores from."""
        t = self._table([self._mkrow('/no/such/op', node_x='42', node_y='7',
                                     node_color='0.1000,0.2000,0.3000')])
        self.assertEqual(0, self.embody_ext._restampNodeGeometry())
        self.assertEqual('42', t[1, 'node_x'].val)
        self.assertEqual('0.1000,0.2000,0.3000', t[1, 'node_color'].val)

    def test_covers_embody_s_own_subtree(self):
        """checkOpsForContinuity MUST skip Embody's own rows; this sweep must
        NOT -- that subtree is exactly where the stale rows were found."""
        t = self._table([self._mkrow(self.embody.path + '/templates',
                                     node_x='0', node_y='0',
                                     node_color='0.6700,0.6700,0.6700')])
        self.assertEqual(1, self.embody_ext._restampNodeGeometry())
        self.assertNotEqual('0.6700,0.6700,0.6700', t[1, 'node_color'].val)

    def test_no_geometry_columns_is_a_no_op(self):
        """A legacy table without the position columns must not raise."""
        self._table([['/x', 'base', 'tox', 'x.tox', '', '', '', '']],
                    cols=['path', 'type', 'strategy', 'rel_file_path',
                          'timestamp', 'dirty', 'build', 'touch_build'])
        self.assertEqual(0, self.embody_ext._restampNodeGeometry())
