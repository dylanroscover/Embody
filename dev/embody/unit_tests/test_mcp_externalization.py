"""
Test suite: MCP externalization integration handlers in EnvoyExt.

Tests _externalize_op, _remove_externalization_tag,
_get_externalizations, _get_externalization_status, _save_externalization.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPExternalization(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    def tearDown(self):
        """Clean up externalizations table rows for sandbox ops."""
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    # --- _get_externalizations ---

    def test_get_externalizations_returns_list(self):
        result = self.envoy._get_externalizations()
        self.assertDictHasKey(result, 'externalizations')
        self.assertIsInstance(result['externalizations'], list)

    def test_get_externalizations_has_entries(self):
        result = self.envoy._get_externalizations()
        self.assertGreater(len(result['externalizations']), 0)

    def test_get_externalizations_entry_structure(self):
        result = self.envoy._get_externalizations()
        if result['externalizations']:
            entry = result['externalizations'][0]
            self.assertDictHasKey(entry, 'path')
            self.assertDictHasKey(entry, 'type')

    # --- _get_externalization_status ---

    def test_get_externalization_status_existing(self):
        # Use Embody itself as a known externalized op
        result = self.envoy._get_externalization_status(
            op_path=self.embody.path)
        # Should return some status info
        self.assertNotIn('error', result)

    def test_get_externalization_status_nonexistent(self):
        result = self.envoy._get_externalization_status(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _externalize_op ---

    def test_externalize_op_comp(self):
        comp = self.sandbox.create(baseCOMP, 'tag_ext_comp')
        result = self.envoy._externalize_op(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_externalize_op_nonexistent(self):
        result = self.envoy._externalize_op(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _remove_externalization_tag ---

    def test_remove_externalization_tag(self):
        comp = self.sandbox.create(baseCOMP, 'untag_comp')
        # Tag it first
        self.envoy._externalize_op(op_path=comp.path)
        # Now remove
        result = self.envoy._remove_externalization_tag(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_remove_externalization_tag_nonexistent(self):
        result = self.envoy._remove_externalization_tag(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- TDXN strategy round-trip (ghost-row regression, 2026-07-24) ---

    def _deleteExportedFile(self, rel_path):
        """Best-effort disk cleanup for a file the test exported."""
        if not rel_path:
            return
        try:
            fp = self.embody_ext.buildAbsolutePath(
                self.embody_ext.normalizePath(rel_path)).resolve()
            if fp.is_file():
                fp.unlink()
        except Exception:
            pass

    def test_externalize_op_tdxn_reports_tdxn_file(self):
        """REGRESSION: tag_type='tdn' must report the .tdn file.

        The old handler read par.externaltox for every COMP, reporting a
        bogus .tox filename for TDXN-strategy externalizations.
        """
        comp = self.sandbox.create(baseCOMP, 'tdn_file_report')
        result = self.envoy._externalize_op(op_path=comp.path, tag_type='tdn')
        self.assertTrue(result.get('success'),
            f"externalize failed: {result.get('error')}")
        reported = str(result.get('file', ''))
        # A fresh externalization mints the current suffix (.tdxn as of
        # v6.1.0). The regression this guards is reporting a bogus .tox,
        # so assert the network-file suffix, never .tox.
        self.assertTrue(reported.endswith('.tdxn'),
            f"tdn externalization must report a .tdxn file, got {reported!r}")
        self.assertFalse(reported.endswith('.tox'),
            f'must not report a .tox for a TDXN externalization: {reported!r}')
        self.envoy._remove_externalization_tag(op_path=comp.path)
        self._deleteExportedFile(reported)

    def test_remove_externalization_tag_tdxn_prunes_row(self):
        """REGRESSION: TDXN untag must remove the table row + breadcrumb.

        The Update sweep deliberately excludes TDXN comps from subtraction
        detection (their lifecycle belongs to RemoveTDXNEntry), so the old
        raw tag-strip + Update() path left a ghost row that Refresh kept
        resurrecting (found live 2026-07-24).
        """
        comp = self.sandbox.create(baseCOMP, 'tdn_ghost_row')
        ext_result = self.envoy._externalize_op(
            op_path=comp.path, tag_type='tdn')
        self.assertTrue(ext_result.get('success'),
            f"externalize failed: {ext_result.get('error')}")
        tdxn_tag = self.embody.par.Tdxntag.eval()
        self.assertIn(tdxn_tag, comp.tags, 'Precondition: comp tagged tdn')

        result = self.envoy._remove_externalization_tag(op_path=comp.path)
        self.assertTrue(result.get('success'),
            f"untag failed: {result.get('error')}")
        self.assertIn(tdxn_tag, result.get('removed_tags', []))

        self.assertNotIn(tdxn_tag, comp.tags,
            'TDXN untag must strip the tag')
        rows = [self.embody_ext.Externalizations[i, 'path'].val
                for i in range(1, self.embody_ext.Externalizations.numRows)]
        self.assertNotIn(comp.path, rows,
            'TDXN untag must delete the tracking row (ghost-row regression)')
        self.assertIsNone(comp.fetch('_tdn_rel_path', None, search=False),
            'TDXN untag must clear the _tdn_rel_path breadcrumb')
        self._deleteExportedFile(ext_result.get('file'))

    # --- DAT auto-detection ---

    def test_tag_textdat_defaults_to_py(self):
        """textDAT with default language should auto-tag as py."""
        dat = self.sandbox.create(textDAT, 'auto_py')
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_textdat_python_language(self):
        """textDAT with language=python should tag as py."""
        dat = self.sandbox.create(textDAT, 'lang_py')
        dat.par.language = 'python'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_textdat_glsl_language(self):
        """textDAT with language=glsl should tag as glsl."""
        dat = self.sandbox.create(textDAT, 'lang_glsl')
        dat.par.language = 'glsl'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Glsltag.eval())

    def test_tag_textdat_json_language(self):
        """textDAT with language=json should tag as json."""
        dat = self.sandbox.create(textDAT, 'lang_json')
        dat.par.language = 'json'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Jsontag.eval())

    def test_tag_textdat_xml_language(self):
        """textDAT with language=xml should tag as xml."""
        dat = self.sandbox.create(textDAT, 'lang_xml')
        dat.par.language = 'xml'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Xmltag.eval())

    def test_tag_textdat_plaintext_language_defaults_to_py(self):
        """textDAT with language='text' (Plain Text) still defaults to py."""
        dat = self.sandbox.create(textDAT, 'lang_txt')
        dat.par.language = 'text'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_tabledat_auto(self):
        """tableDAT should auto-tag as tsv."""
        dat = self.sandbox.create(tableDAT, 'auto_tsv')
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Tsvtag.eval())

    def test_tag_executedat_auto(self):
        """executeDAT should auto-tag as py."""
        dat = self.sandbox.create(executeDAT, 'auto_exec')
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_explicit_type_overrides_language(self):
        """Explicit tag_type should override auto-detection."""
        dat = self.sandbox.create(textDAT, 'explicit_txt')
        dat.par.language = 'python'
        result = self.envoy._externalize_op(
            op_path=dat.path, tag_type='txt')
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], 'txt')

    # --- _save_externalization ---

    def test_save_externalization_comp(self):
        """Force-saving an externalized COMP writes its file and succeeds."""
        comp = self.sandbox.create(baseCOMP, 'save_comp')
        self.envoy._externalize_op(op_path=comp.path)  # TOX strategy
        result = self.envoy._save_externalization(op_path=comp.path)
        self.assertTrue(result.get('success'),
            f'save_externalization failed: {result.get("error")}')
        self.assertEqual(result['path'], comp.path)
        # Clean up the tag + file (keeps the externalization folder tidy).
        self.envoy._remove_externalization_tag(op_path=comp.path)

    def test_save_externalization_nonexistent(self):
        result = self.envoy._save_externalization(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    def test_save_externalization_unsynced_dat(self):
        """A DAT with no file-sync isn't externalized -- save must error."""
        dat = self.sandbox.create(textDAT, 'unsynced_dat')
        result = self.envoy._save_externalization(op_path=dat.path)
        self.assertDictHasKey(result, 'error')

    def test_save_externalization_unsupported_family(self):
        """Non-COMP, non-DAT operators are unsupported for save."""
        chop = self.sandbox.create(constantCHOP, 'save_chop')
        result = self.envoy._save_externalization(op_path=chop.path)
        self.assertDictHasKey(result, 'error')

    # --- Re-externalization mints the current suffix (field 2026-09-06) ---
    #
    # remove(delete_file=True) + externalize again must land .tdxn. The
    # suffix comes from the ROW (_trackedTDXNSuffix) AND from DISK
    # (_handleTDXNAddition adopts a legacy .tdn found beside the minted
    # path), and _removeTDXNStrategy defers its unlink 5 frames -- so a
    # removal and a re-externalization sharing a frame see a disk state
    # nothing else produces, and the export adopted the condemned file.
    # Every other suffix test moves the file to match the row first, so
    # none of them can reach this state. A test body is always one frame,
    # so the sequential case below has the batch's timing and is the
    # minimal regression.

    def _tdxnRow(self, comp_path):
        """The rel_file_path this COMP's TDXN row currently tracks."""
        return self.embody_ext._getStrategyFilePath(comp_path, 'tdn') or ''

    def _tdxnTwins(self, rel):
        """(.tdxn, .tdn) absolute paths for one tracked TDXN rel path."""
        stem = rel[:rel.rfind('.')]
        return tuple(
            self.embody_ext.buildAbsolutePath(
                self.embody_ext.normalizePath(stem + s)).resolve()
            for s in ('.tdxn', '.tdn'))

    def _assertReExternalizedTdxn(self, comp, reported, how):
        """Row, reported file and disk must all say .tdxn -- and only .tdxn."""
        self.assertTrue(str(reported).endswith('.tdxn'),
            '%s: re-externalization reported %r' % (how, reported))
        rel = self._tdxnRow(comp.path)
        self.assertTrue(rel.endswith('.tdxn'),
            '%s: the row tracks %r -- a removed row must MINT the current '
            'suffix, never fall back to legacy .tdn' % (how, rel))
        modern, legacy = self._tdxnTwins(rel)
        self.assertTrue(modern.is_file(),
            '%s: nothing on disk at the tracked path %s' % (how, modern))
        # The legacy twin may still be on disk: its unlink is deferred 5
        # frames and a test body never advances one. Only a twin with NO
        # pending unlink is a real leak -- and the bug's own signature is
        # the row/report above, not this.
        stem_rel = rel[:rel.rfind('.')] + '.tdn'
        if legacy.is_file():
            self.assertTrue(self.embody_ext._unlinkPending(stem_rel),
                '%s: a legacy .tdn twin survives at %s with no pending '
                'unlink -- it was adopted, not condemned' % (how, legacy))

    def _tdxnComp(self, name):
        """A TDXN-externalized sandbox COMP with content.

        Content matters: an operator-empty COMP takes the
        _refusesEmptyTDXNOverwrite branch instead of a real export.
        """
        if not self.embody_ext._projectSavedOnDisk():
            self.skipTest('project never saved -- externalize defers the write')
        comp = self.sandbox.create(baseCOMP, name)
        comp.create(constantTOP, 'content')
        first = self.envoy._externalize_op(op_path=comp.path, tag_type='tdn')
        self.assertTrue(first.get('success'), repr(first.get('error')))
        self.assertTrue(str(first.get('file', '')).endswith('.tdxn'),
            'precondition: a first externalization mints .tdxn, got %r'
            % first.get('file'))
        # ARM the bug: it needs a LEGACY .tdn on disk beside the minted
        # path, which is what _handleTDXNAddition's adoption branch looks
        # for. A freshly externalized COMP has no .tdn at all, so without
        # this the tests pass whether the guard exists or not (verified
        # 2026-09-06 by removing the guard and watching them stay green).
        rel = self._tdxnRow(comp.path)
        legacy = rel[:-len('.tdxn')] + '.tdn'
        modern_abs = self.embody_ext.buildAbsolutePath(
            self.embody_ext.normalizePath(rel))
        legacy_abs = self.embody_ext.buildAbsolutePath(
            self.embody_ext.normalizePath(legacy))
        if modern_abs.is_file():
            modern_abs.replace(legacy_abs)
        self.embody_ext._updateRowCells(
            comp.path, {'rel_file_path': legacy}, strategy='tdn')
        self.assertTrue(legacy_abs.is_file(),
                        'precondition: a legacy .tdn must exist to adopt')
        return comp

    def test_re_externalize_after_removal_mints_tdxn(self):
        """Remove with delete_file, externalize again -> .tdxn, no .tdn twin."""
        comp = self._tdxnComp('reext_serial')

        removed = self.envoy._remove_externalization_tag(
            op_path=comp.path, delete_file=True)
        self.assertTrue(removed.get('success'), repr(removed.get('error')))
        self.assertEqual(self._tdxnRow(comp.path), '',
                         'precondition: the removal must drop the row')

        again = self.envoy._externalize_op(op_path=comp.path, tag_type='tdn')
        self.assertTrue(again.get('success'), repr(again.get('error')))
        self._assertReExternalizedTdxn(comp, again.get('file'), 'serial')

        self.envoy._remove_externalization_tag(op_path=comp.path)
        self._deleteExportedFile(again.get('file'))

    def test_re_externalize_inside_one_batch_mints_tdxn(self):
        """The same pair interleaved in ONE batch_operations call.

        batch_operations dispatches every entry synchronously in a single
        main-thread call, so both halves share a frame and the removal's
        deferred unlink has NOT run when the re-externalization picks its
        suffix. This is the shape that shipped .tdn from a batch while two
        separate MCP calls gave .tdxn.
        """
        comp = self._tdxnComp('reext_batch')

        result = self.envoy._batch_operations(operations=[
            {'tool': 'remove_externalization_tag', 'params': {
                'op_path': comp.path, 'delete_file': True}},
            {'tool': 'externalize_op', 'params': {
                'op_path': comp.path, 'tag_type': 'tdn'}},
        ])
        self.assertTrue(result['success'], repr(result['results']))
        self.assertEqual(result['count'], 2)
        reported = result['results'][1].get('file', '')
        self._assertReExternalizedTdxn(comp, reported, 'batched')

        self.envoy._remove_externalization_tag(op_path=comp.path)
        self._deleteExportedFile(reported)
