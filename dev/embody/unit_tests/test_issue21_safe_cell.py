"""
Test suite: Issue #21 — safe externalizations-table cell access.

The reporter hit `'NoneType' object has no attribute 'val'` from five
distinct call sites after a partial ExternalizeProject cascade left the
externalizations table in an inconsistent state. The crashes traced to
unguarded `table[row, col].val` reads — TD returns None when the column
doesn't exist or a row-key lookup misses, and `.val` on None throws.

These tests build a synthetic externalizations table that's missing
required columns (or has short rows), swap Embody's Externalizations
parameter to point at it for the duration of the test, exercise each
crash site, then restore.

Each test drops ONLY columns that the production code reads downstream
of the row-match — otherwise the test would short-circuit before the
unguarded `.val` call and pass even without the fix.

Also covers Fix 3: onProjectPreSave must contain exceptions so TD can
finish writing the .toe (pre-fix, an unhandled exception there caused
the save to truncate to 0 bytes).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestIssue21SafeCellAccess(EmbodyTestCase):
    """The 5 .val crash sites must tolerate a malformed externalizations table."""

    def setUp(self):
        super().setUp()
        self._original_table = self.embody.par.Externalizations.eval()
        self._bad_table = None

    def tearDown(self):
        # Restore the real table FIRST so other tests aren't poisoned even
        # if sandbox cleanup fails. Fail loud if restore fails — silent
        # restore failure could mask cross-test contamination.
        original_path = self._original_table.path
        self.embody.par.Externalizations = original_path
        if self.embody.par.Externalizations.eval() is not self._original_table:
            raise AssertionError(
                f"Externalizations restore failed: param value "
                f"{self.embody.par.Externalizations.eval()} != "
                f"{self._original_table}")
        super().tearDown()

    def _buildBadTable(self, drop_cols=(), short_row_at=None, copy_rows=3,
                       extra_rows=()):
        """Build a synthetic externalizations table.

        Args:
            drop_cols: column names to omit from both header and data rows
            short_row_at: row index (0-based among DATA rows) to truncate to
                its first 2 cells, simulating a partially-written row
            copy_rows: how many rows to copy from the live table
            extra_rows: extra full rows (lists) to append at the end
        """
        live = self._original_table
        headers = [live[0, c].val for c in range(live.numCols)]
        keep = [c for c, h in enumerate(headers) if h not in drop_cols]

        bad = self.sandbox.create(tableDAT, 'bad_externalizations')
        bad.clear()
        bad.appendRow([headers[c] for c in keep])

        for i in range(1, min(copy_rows + 1, live.numRows)):
            row = [live[i, c].val for c in keep]
            if short_row_at is not None and (i - 1) == short_row_at:
                row = row[:2]  # truncate to first 2 cells
            bad.appendRow(row)

        for extra in extra_rows:
            bad.appendRow(list(extra))

        self.embody.par.Externalizations = bad.path
        self._bad_table = bad
        return bad

    # =====================================================================
    # The 5 crash sites — drop ONLY columns read downstream of row-match
    # so the unguarded .val sites are actually exercised
    # =====================================================================

    # Crash site 1: getExternalizedOps reads strategy, type, and path
    # in that order. Drop strategy + type, keep path so the bad reads
    # actually happen.

    def test_getExternalizedOps_tolerates_missing_strategy_and_type(self):
        # strategy missing -> falls through to legacy branch, which reads
        # type unguarded. Both missing => the legacy .val crashes pre-fix.
        self._buildBadTable(drop_cols=('strategy', 'type'))
        result = self.embody_ext.getExternalizedOps(COMP)
        self.assertIsInstance(result, list)

    def test_getExternalizedOps_tolerates_short_row_when_strategy_requested(self):
        # Pre-fix: when caller passes strategy='tox' AND the strategy
        # COLUMN exists in headers (so has_strategy_col=True), the
        # per-row read self.Externalizations[i, 'strategy'].val is
        # unguarded. A short row that lacks that cell crashes.
        # The previous test that "dropped strategy" was a false positive:
        # has_strategy_col went False and the read was never reached.
        self._buildBadTable(drop_cols=(), short_row_at=0, copy_rows=2)
        result = self.embody_ext.getExternalizedOps(COMP, strategy='tox')
        self.assertIsInstance(result, list)

    # Crash site 2: cleanupAllDuplicateRows reads path unguarded.

    def test_cleanupAllDuplicateRows_tolerates_missing_path(self):
        self._buildBadTable(drop_cols=('path',))
        self.embody_ext.cleanupAllDuplicateRows()

    # Crash site 3: cleanupDuplicateRows matches on path, then reads type
    # and timestamp for matching rows. Need a row that MATCHES the queried
    # path so the type/timestamp reads execute, then make those columns
    # missing to force the unguarded crash.

    def test_cleanupDuplicateRows_tolerates_missing_type_and_timestamp_when_path_matches(self):
        # Build a table with valid path column but missing type/timestamp,
        # then query a path that will match a row.
        bad = self._buildBadTable(drop_cols=('type', 'timestamp'),
                                  copy_rows=0)
        # Inject 2 rows with the same path (matched) and a few short cells
        # for the remaining columns.
        match_path = '/sandbox/duplicate/test'
        headers = [bad[0, c].val for c in range(bad.numCols)]
        n_cols = len(headers)
        path_idx = headers.index('path')
        for _ in range(2):
            row = [''] * n_cols
            row[path_idx] = match_path
            bad.appendRow(row)
        # cleanupDuplicateRows must not raise on the missing type/timestamp
        # reads even though both rows match.
        result = self.embody_ext.cleanupDuplicateRows(match_path)
        # If it crashed pre-fix, it never got to return anything.
        # Post-fix it returns the kept row index (an int) since both match.
        self.assertIsNotNone(result)

    # Crash site 4: RemoveListerRow matches on path AND rel_file_path.
    # Need a row that matches the query path so rel_file_path is read.

    def test_RemoveListerRow_tolerates_missing_rel_file_when_path_matches(self):
        bad = self._buildBadTable(drop_cols=('rel_file_path',), copy_rows=0)
        match_path = '/sandbox/remove/test'
        headers = [bad[0, c].val for c in range(bad.numCols)]
        n_cols = len(headers)
        path_idx = headers.index('path')
        row = [''] * n_cols
        row[path_idx] = match_path
        bad.appendRow(row)
        # Production: matches path, then tries to read rel_file_path -> crash pre-fix.
        self.embody_ext.RemoveListerRow(
            match_path, '', delete_file=False)

    # Crash site 5: checkOpsForContinuity has its own try/except wrapper
    # that swallows AttributeError and logs "Error in checkOpsForContinuity".
    # The assertion target is therefore the log line, not the call itself.

    def test_checkOpsForContinuity_does_not_log_internal_error_on_malformed_table(self):
        # Build a table whose path column exists but other required cells
        # are missing on injected rows — drives unguarded reads beyond the
        # path check. Inject our own malformed rows so the test doesn't
        # depend on the live table having any data.
        bad = self._buildBadTable(drop_cols=('rel_file_path', 'type', 'strategy'),
                                  copy_rows=0)
        headers = [bad[0, c].val for c in range(bad.numCols)]
        n_cols = len(headers)
        path_idx = headers.index('path')
        # Inject a row with a non-existent path so production tries to
        # read the missing rel_file_path/type/strategy cells.
        row = [''] * n_cols
        row[path_idx] = '/no/such/op'
        bad.appendRow(row)

        # Capture Log calls. Use **kwargs so we accept whatever signature
        # the real Log uses (e.g. _depth is passed internally for source
        # attribution).
        log_calls = []
        ext = self.embody_ext
        original_log = type(ext).Log
        def capturing_log(self_, msg, level='INFO', details=None, **kwargs):
            log_calls.append((level, str(msg)))
            return original_log(self_, msg, level, details, **kwargs)
        type(ext).Log = capturing_log
        try:
            ext.checkOpsForContinuity('externalizations')
        finally:
            type(ext).Log = original_log

        # Pre-fix this would have logged "Error in checkOpsForContinuity"
        # via the function's bare except. Post-fix the safe cell reads
        # should produce no such log.
        internal_errors = [
            (lvl, msg) for lvl, msg in log_calls
            if 'Error in checkOpsForContinuity' in msg
        ]
        self.assertEqual(len(internal_errors), 0,
            f"Expected no internal-error log, got: {internal_errors}")

    # =====================================================================
    # _cellVal helper contract
    # =====================================================================

    def test_cellVal_returns_default_for_missing_column(self):
        self._buildBadTable(drop_cols=('strategy',))
        val = self.embody_ext._cellVal(1, 'strategy')
        self.assertEqual(val, '')

    def test_cellVal_returns_default_for_missing_row_key(self):
        # Row-key lookup that doesn't match any row's first column
        val = self.embody_ext._cellVal('/no/such/path', 'dirty')
        self.assertEqual(val, '')

    def test_cellVal_reads_real_cell_on_well_formed_table(self):
        # Header row, col 0 — always 'path'
        val = self.embody_ext._cellVal(0, 0)
        self.assertEqual(val, 'path')

    def test_cellVal_returns_default_for_short_row_cell(self):
        # Row exists but has fewer cells than the header declares — the
        # missing cell read must return the default, not crash.
        bad = self._buildBadTable(drop_cols=(), short_row_at=0, copy_rows=2)
        # Row 1 is the short row; col -1 (last header) was dropped from
        # that row. _cellVal must tolerate this.
        last_header = bad[0, bad.numCols - 1].val
        val = self.embody_ext._cellVal(1, last_header)
        self.assertEqual(val, '')


class TestIssue21PreSaveBoundary(EmbodyTestCase):
    """onProjectPreSave must not propagate exceptions (would truncate .toe)."""

    def test_onProjectPreSave_contains_Update_exception(self):
        execute_mod = op('/embody/Embody/execute').module
        ext_class = type(self.embody_ext)
        original_update = ext_class.Update

        def boom(self, suppress_refresh=False):
            raise Exception("simulated mid-Update crash (issue #21)")
        ext_class.Update = boom
        try:
            # Pre-fix this re-raised. Post-fix it's caught + logged so TD's
            # underlying save can still write the .toe.
            execute_mod.onProjectPreSave()
        finally:
            ext_class.Update = original_update

    def test_onProjectPreSave_contains_arbitrary_exception_in_pipeline(self):
        # Simulate a failure inside the TDN export phase, not Update itself.
        # The whole pipeline is wrapped — any exception below the Perform
        # Mode early-return must be contained.
        execute_mod = op('/embody/Embody/execute').module
        ext_class = type(self.embody_ext)
        original = ext_class._getTDNStrategyComps

        def boom(self):
            raise RuntimeError("simulated mid-pipeline crash")
        ext_class._getTDNStrategyComps = boom
        try:
            execute_mod.onProjectPreSave()
        finally:
            ext_class._getTDNStrategyComps = original

    def test_strip_loop_pre_stages_stripped_paths_before_crashing(self):
        # Round-2 Agent 5 / Round-3 stress test: when StripCompChildren raises
        # mid-loop, the new fail-safe wrapper (Fix 3) catches it so the .toe
        # saves cleanly — but the live session loses children unless the
        # restore list was pre-staged BEFORE the strip. The pre-fix code wrote
        # `_tdn_stripped_paths` only after the whole loop finished, so a mid-
        # strip crash left post-save with no restore list. The fix moves the
        # store call ABOVE the loop, using `exported_by_depth` directly.
        #
        # This test patches the upstream phases out and provides a controlled
        # `exported` list + a StripCompChildren that crashes on the 2nd call,
        # then asserts the storage holds the full pre-staged list.

        embody = self.embody
        ext_class = type(self.embody_ext)
        tdn_class = type(self.embody.ext.TDN)

        # Capture originals. _read_existing_tdn and _tdn_content_equal are
        # @staticmethod — must capture/restore via __dict__ to preserve the
        # descriptor; assigning via cls.attr would silently strip the
        # staticmethod and turn it into a regular method, breaking every
        # other caller that does TDNExt._read_existing_tdn(path).
        orig_tdnmode = embody.par.Tdnmode.eval()
        orig_strip_on_save = bool(embody.par.Tdnstriponsave.eval())
        orig_update = ext_class.Update
        orig_get_tdn = ext_class._getTDNStrategyComps
        orig_safety = ext_class._checkTDNContentSafety
        orig_export = tdn_class.ExportNetwork
        orig_read = tdn_class.__dict__['_read_existing_tdn']  # staticmethod descriptor
        orig_strip = ext_class.StripCompChildren

        # Fake fixtures: pretend 3 TDN COMPs exist and were exported
        fake_tdn_comps = [
            ('/__test_issue21_pre_stage/c0', 'fake/c0.tdn'),
            ('/__test_issue21_pre_stage/c1', 'fake/c1.tdn'),
            ('/__test_issue21_pre_stage/c2', 'fake/c2.tdn'),
        ]

        # Track which strip calls happened
        strip_calls = []
        def crashing_strip(self_, comp):
            strip_calls.append(comp.path if comp else '<None>')
            if len(strip_calls) == 2:
                raise Exception("simulated mid-strip crash (Tier 1.5 test)")
            # Don't actually strip — fake comps may not exist

        # Pre-stage cleanup: ensure no stale storage
        embody.unstore('_tdn_stripped_paths')
        embody.unstore('_tdn_pane_restore')

        # Apply patches
        embody.par.Tdnmode = 'full'
        embody.par.Tdnstriponsave = True
        ext_class.Update = lambda self_, suppress_refresh=False: None
        ext_class._getTDNStrategyComps = lambda self_: list(fake_tdn_comps)
        ext_class._checkTDNContentSafety = lambda self_: None
        ext_class.StripCompChildren = crashing_strip
        # Make every "comp exists" check return a stub object that has a path
        # and findChildren so the export loop accepts them. Easiest hack: skip
        # the export by making _read_existing_tdn return a matching dict so
        # the "skip if unchanged" branch fires. But we still need op(comp_path)
        # to return something. Build a sandbox COMP with the fake names.
        sandbox = self.sandbox.create(baseCOMP, '__test_issue21_pre_stage')
        for path, _ in fake_tdn_comps:
            name = path.rsplit('/', 1)[-1]
            comp = sandbox.create(baseCOMP, name)
            # Give it a child so has_children is True (Phase 1 skips empty COMPs)
            comp.create(baseCOMP, 'placeholder')
        # Re-point our fake paths to the sandbox COMPs
        fake_tdn_comps[:] = [
            (sandbox.op('c0').path, 'fake/c0.tdn'),
            (sandbox.op('c1').path, 'fake/c1.tdn'),
            (sandbox.op('c2').path, 'fake/c2.tdn'),
        ]
        # Fake export: succeed with a stable dict so the "content unchanged"
        # path fires (skips actual file write)
        def fake_export(self_, root_path=None, output_file=None, **kwargs):
            return {'success': True, 'tdn': {'version': '1.4', 'root': root_path}}
        tdn_class.ExportNetwork = fake_export
        # Fake read-existing — wrap in staticmethod() to preserve the
        # descriptor (otherwise other tests calling _read_existing_tdn via
        # an instance break with "takes 1 positional argument but 2 given").
        tdn_class._read_existing_tdn = staticmethod(lambda path: {'version': '1.4'})
        # Force content-equal to True so the write path is skipped — every
        # fake export takes the unchanged-skip branch and appends to `exported`
        orig_equal = tdn_class.__dict__['_tdn_content_equal']  # staticmethod descriptor
        tdn_class._tdn_content_equal = staticmethod(lambda a, b: True)

        execute_mod = op('/embody/Embody/execute').module

        try:
            # Run the hook. Fix 3 should catch the crashing_strip exception.
            execute_mod.onProjectPreSave()

            # THE ASSERTION: storage must hold the full pre-staged list
            stored = embody.fetch('_tdn_stripped_paths', None, search=False)
            self.assertIsNotNone(stored,
                "Pre-stage failed: _tdn_stripped_paths is None after mid-strip crash. "
                "Post-save would have nothing to restore.")
            # Compare paths only (rel_file_path was synthetic)
            stored_paths = [entry[0] for entry in stored]
            expected_paths = [p for p, _ in fake_tdn_comps]
            self.assertEqual(set(stored_paths), set(expected_paths),
                f"Expected all {len(expected_paths)} paths pre-staged, got: {stored_paths}")

            # And the crash was the expected one (the loop did reach strip #2)
            self.assertEqual(len(strip_calls), 2,
                f"Expected 2 strip calls before crash, got {len(strip_calls)}")
        finally:
            # Restore everything
            ext_class.Update = orig_update
            ext_class._getTDNStrategyComps = orig_get_tdn
            ext_class._checkTDNContentSafety = orig_safety
            ext_class.StripCompChildren = orig_strip
            tdn_class.ExportNetwork = orig_export
            tdn_class._read_existing_tdn = orig_read
            tdn_class._tdn_content_equal = orig_equal
            embody.par.Tdnmode = orig_tdnmode
            embody.par.Tdnstriponsave = orig_strip_on_save
            embody.unstore('_tdn_stripped_paths')
            embody.unstore('_tdn_pane_restore')

    def test_onProjectPreSave_contains_exception_in_preamble(self):
        # Round-2 Agent 1 finding: the unstores and Perform Mode check used
        # to sit OUTSIDE the try/except. After widening the boundary, an
        # exception in the preamble — anywhere before the helper call —
        # must also be contained.
        #
        # TD COMPs don't allow monkey-patching their methods (unstore is
        # read-only). Instead patch the EmbodyExt class so that
        # `parent.Embody.ext.Embody._performMode` raises during attribute
        # access — that lookup happens inside the preamble (line ~107).
        execute_mod = op('/embody/Embody/execute').module
        ext_class = type(self.embody_ext)
        # _performMode is a property in the class — replace it temporarily
        original = ext_class.__dict__.get('_performMode')

        def boom_perform(self_):
            raise Exception("simulated preamble crash via _performMode access")
        ext_class._performMode = property(boom_perform)
        try:
            execute_mod.onProjectPreSave()
        finally:
            if original is not None:
                ext_class._performMode = original
            else:
                # Defensive: if _performMode wasn't a property, just delete our addition
                del ext_class._performMode
