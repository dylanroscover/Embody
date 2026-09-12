"""
Test suite: TDXN helper methods (pure Python logic).

Tests _serializeValue, _valuesDiffer, _colorsDiffer,
_assembleHierarchy, _getGroupBaseName, _serializeStorageValue,
_deserializeStorageValue, _tdxn_content_equal, and _read_existing_tdxn,
plus the locked-content warning: source classification, dialog text,
Switch to TOX and MCP log-only exports (issue #108).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDXNHelpers(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDXN

    # --- _serializeValue ---

    def test_serializeValue_none_to_empty_string(self):
        self.assertEqual(self.tdn._serializeValue(None), '')

    def test_serializeValue_bool_true(self):
        self.assertTrue(self.tdn._serializeValue(True))

    def test_serializeValue_bool_false(self):
        self.assertFalse(self.tdn._serializeValue(False))

    def test_serializeValue_int_unchanged(self):
        self.assertEqual(self.tdn._serializeValue(42), 42)

    def test_serializeValue_float_whole_to_int(self):
        result = self.tdn._serializeValue(5.0)
        self.assertEqual(result, 5)
        self.assertIsInstance(result, int)

    def test_serializeValue_float_decimal_preserved(self):
        result = self.tdn._serializeValue(3.14)
        self.assertAlmostEqual(result, 3.14)

    def test_serializeValue_string_unchanged(self):
        self.assertEqual(self.tdn._serializeValue('hello'), 'hello')

    def test_serializeValue_list_recursion(self):
        result = self.tdn._serializeValue([1, 2.0, None])
        self.assertListEqual(result, [1, 2, ''])

    def test_serializeValue_tuple_becomes_list(self):
        result = self.tdn._serializeValue((1, 2))
        self.assertIsInstance(result, list)
        self.assertListEqual(result, [1, 2])

    # --- _valuesDiffer ---

    def test_valuesDiffer_same_int(self):
        self.assertFalse(self.tdn._valuesDiffer(5, 5))

    def test_valuesDiffer_different_int(self):
        self.assertTrue(self.tdn._valuesDiffer(5, 6))

    def test_valuesDiffer_none_vs_empty_string(self):
        self.assertFalse(self.tdn._valuesDiffer(None, ''))

    def test_valuesDiffer_empty_string_vs_none(self):
        self.assertFalse(self.tdn._valuesDiffer('', None))

    def test_valuesDiffer_float_precision(self):
        # 0.1 + 0.2 != 0.3 in floating point, but within 1e-9
        self.assertFalse(self.tdn._valuesDiffer(0.1 + 0.2, 0.3))

    def test_valuesDiffer_float_vs_int(self):
        self.assertFalse(self.tdn._valuesDiffer(5.0, 5))

    def test_valuesDiffer_strings_differ(self):
        self.assertTrue(self.tdn._valuesDiffer('abc', 'def'))

    def test_valuesDiffer_strings_same(self):
        self.assertFalse(self.tdn._valuesDiffer('abc', 'abc'))

    # --- _colorsDiffer ---

    def test_colorsDiffer_identical(self):
        c = (0.545, 0.545, 0.545)
        self.assertFalse(self.tdn._colorsDiffer(c, c))

    def test_colorsDiffer_within_tolerance(self):
        c1 = (0.545, 0.545, 0.545)
        c2 = (0.550, 0.540, 0.545)
        self.assertFalse(self.tdn._colorsDiffer(c1, c2))

    def test_colorsDiffer_beyond_tolerance(self):
        c1 = (0.545, 0.545, 0.545)
        c2 = (1.0, 0.0, 0.0)
        self.assertTrue(self.tdn._colorsDiffer(c1, c2))

    def test_colorsDiffer_length_mismatch(self):
        c1 = (0.5, 0.5, 0.5)
        c2 = (0.5, 0.5)
        self.assertTrue(self.tdn._colorsDiffer(c1, c2))

    # --- _assembleHierarchy ---

    def test_assembleHierarchy_flat(self):
        flat = {
            '/a': {'name': 'a', 'type': 'baseCOMP'},
            '/b': {'name': 'b', 'type': 'textDAT'},
        }
        result = self.tdn._assembleHierarchy(flat, '/')
        self.assertLen(result, 2)

    def test_assembleHierarchy_nested(self):
        flat = {
            '/parent': {'name': 'parent', 'type': 'baseCOMP'},
            '/parent/child': {'name': 'child', 'type': 'textDAT'},
        }
        result = self.tdn._assembleHierarchy(flat, '/')
        self.assertLen(result, 1)
        self.assertIn('children', result[0])
        self.assertEqual(result[0]['children'][0]['name'], 'child')

    def test_assembleHierarchy_empty(self):
        result = self.tdn._assembleHierarchy({}, '/')
        self.assertLen(result, 0)

    def test_assembleHierarchy_deeply_nested(self):
        flat = {
            '/a': {'name': 'a', 'type': 'baseCOMP'},
            '/a/b': {'name': 'b', 'type': 'baseCOMP'},
            '/a/b/c': {'name': 'c', 'type': 'textDAT'},
        }
        result = self.tdn._assembleHierarchy(flat, '/')
        self.assertLen(result, 1)
        a = result[0]
        self.assertIn('children', a)
        b = a['children'][0]
        self.assertIn('children', b)
        self.assertEqual(b['children'][0]['name'], 'c')

    # --- _serializeStorageValue ---

    def test_serializeStorageValue_int(self):
        self.assertEqual(self.tdn._serializeStorageValue(42), 42)

    def test_serializeStorageValue_float(self):
        result = self.tdn._serializeStorageValue(3.14)
        self.assertAlmostEqual(result, 3.14)

    def test_serializeStorageValue_string(self):
        self.assertEqual(self.tdn._serializeStorageValue('hello'), 'hello')

    def test_serializeStorageValue_bool(self):
        self.assertTrue(self.tdn._serializeStorageValue(True))
        self.assertFalse(self.tdn._serializeStorageValue(False))

    def test_serializeStorageValue_none(self):
        self.assertIsNone(self.tdn._serializeStorageValue(None))

    def test_serializeStorageValue_list(self):
        result = self.tdn._serializeStorageValue([1, 'a', True])
        self.assertEqual(result, [1, 'a', True])

    def test_serializeStorageValue_dict(self):
        result = self.tdn._serializeStorageValue({'k': 'v'})
        self.assertEqual(result, {'k': 'v'})

    def test_serializeStorageValue_tuple(self):
        result = self.tdn._serializeStorageValue((1, 2, 3))
        self.assertEqual(result, {'$type': 'tuple', '$value': [1, 2, 3]})

    def test_serializeStorageValue_set(self):
        result = self.tdn._serializeStorageValue({'c', 'a', 'b'})
        self.assertEqual(result['$type'], 'set')
        self.assertEqual(sorted(result['$value']), ['a', 'b', 'c'])

    def test_serializeStorageValue_bytes(self):
        import base64
        result = self.tdn._serializeStorageValue(b'\x00\x01\x02')
        self.assertEqual(result['$type'], 'bytes')
        self.assertEqual(base64.b64decode(result['$value']), b'\x00\x01\x02')

    def test_serializeStorageValue_whole_float_to_int(self):
        """Whole-number floats are normalized to int."""
        self.assertEqual(self.tdn._serializeStorageValue(42.0), 42)
        self.assertIsInstance(self.tdn._serializeStorageValue(42.0), int)

    # --- _deserializeStorageValue ---

    def test_deserializeStorageValue_primitives(self):
        self.assertEqual(self.tdn._deserializeStorageValue(42), 42)
        self.assertEqual(self.tdn._deserializeStorageValue('hi'), 'hi')
        self.assertTrue(self.tdn._deserializeStorageValue(True))
        self.assertIsNone(self.tdn._deserializeStorageValue(None))

    def test_deserializeStorageValue_list(self):
        result = self.tdn._deserializeStorageValue([1, 'a'])
        self.assertEqual(result, [1, 'a'])

    def test_deserializeStorageValue_dict(self):
        result = self.tdn._deserializeStorageValue({'k': 'v'})
        self.assertEqual(result, {'k': 'v'})

    def test_deserializeStorageValue_tuple(self):
        result = self.tdn._deserializeStorageValue(
            {'$type': 'tuple', '$value': [1, 2]})
        self.assertEqual(result, (1, 2))
        self.assertIsInstance(result, tuple)

    def test_deserializeStorageValue_set(self):
        result = self.tdn._deserializeStorageValue(
            {'$type': 'set', '$value': ['a', 'b']})
        self.assertEqual(result, {'a', 'b'})
        self.assertIsInstance(result, set)

    def test_deserializeStorageValue_bytes(self):
        import base64
        encoded = base64.b64encode(b'\xff\x00').decode('ascii')
        result = self.tdn._deserializeStorageValue(
            {'$type': 'bytes', '$value': encoded})
        self.assertEqual(result, b'\xff\x00')
        self.assertIsInstance(result, bytes)

    def test_deserializeStorageValue_unknown_type(self):
        """Unknown $type is treated as a plain dict."""
        result = self.tdn._deserializeStorageValue(
            {'$type': 'unknown', '$value': 'x'})
        self.assertIsInstance(result, dict)

    # --- _tdxn_content_equal ---

    def _make_tdxn(self, **overrides):
        """Build a minimal TDXN dict with sensible defaults."""
        base = {
            'format': 'tdn',
            'version': '1.0',
            'build': 1,
            'generator': 'Embody/5.0.200',
            'td_build': '099.2025.32280',
            'exported_at': '2026-01-01T00:00:00Z',
            'network_path': '/test',
            'options': {'include_dat_content': True},
            'operators': [
                {'name': 'noise1', 'type': 'noiseTOP'},
            ],
        }
        base.update(overrides)
        return base

    def test_tdxn_content_equal_identical(self):
        """Identical dicts (same volatile fields) returns True."""
        tdn = self._make_tdxn()
        self.assertTrue(self.tdn._tdxn_content_equal(tdn, tdn.copy()))

    def test_tdxn_content_equal_only_volatile_diff(self):
        """Dicts differing only in volatile header fields returns True."""
        a = self._make_tdxn()
        b = self._make_tdxn(
            build=99,
            generator='Embody/9.9.999',
            td_build='100.2030.99999',
            exported_at='2030-12-31T23:59:59Z',
        )
        self.assertTrue(self.tdn._tdxn_content_equal(a, b))

    def test_tdxn_content_equal_different_operators(self):
        a = self._make_tdxn()
        b = self._make_tdxn(operators=[
            {'name': 'noise1', 'type': 'noiseTOP'},
            {'name': 'null1', 'type': 'nullTOP'},
        ])
        self.assertFalse(self.tdn._tdxn_content_equal(a, b))

    def test_tdxn_content_equal_different_options(self):
        a = self._make_tdxn()
        b = self._make_tdxn(options={'include_dat_content': False})
        self.assertFalse(self.tdn._tdxn_content_equal(a, b))

    def test_tdxn_content_equal_extra_key_in_existing(self):
        """Key present in existing but not in new is detected."""
        a = self._make_tdxn()
        b = self._make_tdxn(annotations=[{'name': 'ann1'}])
        self.assertFalse(self.tdn._tdxn_content_equal(a, b))

    def test_tdxn_content_equal_extra_key_in_new(self):
        """Key present in new but not in existing is detected."""
        a = self._make_tdxn(custom_pars=[{'name': 'Speed'}])
        b = self._make_tdxn()
        self.assertFalse(self.tdn._tdxn_content_equal(a, b))

    def test_tdxn_content_equal_different_version(self):
        """Non-volatile header field 'version' difference is detected."""
        a = self._make_tdxn()
        b = self._make_tdxn(version='2.0')
        self.assertFalse(self.tdn._tdxn_content_equal(a, b))

    # --- _read_existing_tdxn ---

    def test_read_existing_tdxn_missing_file(self):
        import os, tempfile
        path = os.path.join(tempfile.gettempdir(), 'nonexistent_abc123.tdn')
        self.assertIsNone(self.tdn._read_existing_tdxn(path))

    def test_read_existing_tdxn_corrupt_file(self):
        import os, tempfile
        path = os.path.join(tempfile.gettempdir(), 'corrupt_test.tdn')
        try:
            with open(path, 'w') as f:
                f.write('not valid json {{{')
            self.assertIsNone(self.tdn._read_existing_tdxn(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_read_existing_tdxn_valid_file(self):
        import os, json, tempfile
        path = os.path.join(tempfile.gettempdir(), 'valid_test.tdn')
        data = {'format': 'tdn', 'operators': []}
        try:
            with open(path, 'w') as f:
                json.dump(data, f)
            result = self.tdn._read_existing_tdxn(path)
            self.assertIsNotNone(result)
            self.assertEqual(result['format'], 'tdn')
        finally:
            if os.path.exists(path):
                os.unlink(path)

    # --- _findLockedNonDATs boundaries (issue #53) ---

    def _lockedTop(self, parent, name):
        t = parent.create(noiseTOP, name)
        t.lock = True
        return t

    def test_findLocked_direct_child_reported(self):
        root = self.sandbox.create(baseCOMP, 'lk_root')
        t = self._lockedTop(root, 'locked_direct')
        self.assertIn(t, self.tdn._findLockedNonDATs(root))

    def test_findLocked_locked_dat_not_reported(self):
        root = self.sandbox.create(baseCOMP, 'lk_root_dat')
        d = root.create(textDAT, 'locked_dat')
        d.lock = True
        self.assertEqual(self.tdn._findLockedNonDATs(root), [])

    def test_findLocked_under_tox_tagged_child_skipped(self):
        # A nested TOX-strategy COMP preserves locked content in its own
        # .tox -- the parent's TDXN export must not warn about it.
        root = self.sandbox.create(baseCOMP, 'lk_root_tox')
        sub = root.create(baseCOMP, 'sub_tox')
        sub.tags.add(self.embody.par.Toxtag.val)
        self._lockedTop(sub, 'locked_nested')
        self.assertEqual(self.tdn._findLockedNonDATs(root), [])

    def test_findLocked_under_tdxn_tagged_child_skipped(self):
        # A nested TDXN boundary raises its own warning when IT exports.
        root = self.sandbox.create(baseCOMP, 'lk_root_tdn')
        sub = root.create(baseCOMP, 'sub_tdn')
        sub.tags.add(self.embody.par.Tdxntag.val)
        self._lockedTop(sub, 'locked_nested')
        self.assertEqual(self.tdn._findLockedNonDATs(root), [])

    def test_findLocked_under_exclude_tagged_child_skipped(self):
        # An exclude-tagged subtree is invisible to TDXN entirely.
        root = self.sandbox.create(baseCOMP, 'lk_root_excl')
        sub = root.create(baseCOMP, 'sub_excl')
        sub.tags.add(self.embody.par.Tdxnexcludetag.val)
        self._lockedTop(sub, 'locked_nested')
        self.assertEqual(self.tdn._findLockedNonDATs(root), [])

    def test_findLocked_under_untagged_child_still_reported(self):
        # Plain nested COMPs are serialized inline -- still this export's
        # responsibility, so the warning must still fire.
        root = self.sandbox.create(baseCOMP, 'lk_root_plain')
        sub = root.create(baseCOMP, 'sub_plain')
        t = self._lockedTop(sub, 'locked_nested')
        self.assertIn(t, self.tdn._findLockedNonDATs(root))

    def test_findLocked_tag_on_root_itself_ignored(self):
        # Tags on the export root itself do not hide its own children --
        # only tags strictly BETWEEN the child and the root form a boundary.
        root = self.sandbox.create(baseCOMP, 'lk_root_self')
        root.tags.add(self.embody.par.Tdxntag.val)
        t = self._lockedTop(root, 'locked_direct')
        self.assertIn(t, self.tdn._findLockedNonDATs(root))

    def test_findLocked_inside_self_referencing_master_reported(self):
        # clone=me with cloning on is a MASTER (EmbodyExt.isInsideClone),
        # not a clone interior: its locked content is this export's.
        root = self.sandbox.create(baseCOMP, 'lk_root_selfmaster')
        master = root.create(baseCOMP, 'master')
        master.par.clone.expr = 'me'  # expression: no clone-sync recursion
        master.par.enablecloning = True
        t = self._lockedTop(master, 'locked_in_master')
        self.assertFalse(self.embody_ext.isInsideClone(t),
                         'setup: isInsideClone reads this as a master')
        self.assertIn(t, self.tdn._findLockedNonDATs(root))

    # --- _warnLockedNonDATs batching + opt-out dialog ---

    def _interceptLockedDialog(self):
        """Replace _showLockedWarnDialog with a recorder; returns the list
        of captured messages. Caller MUST restore via the returned undo.
        The Switch to TOX targets each dialog offered land in
        self._targets_seen, one list per captured message."""
        captured = []
        self._targets_seen = []
        orig = self.tdn._showLockedWarnDialog

        def fake(msg, switch_targets=()):
            captured.append(msg)
            self._targets_seen.append(list(switch_targets))
        self.tdn._showLockedWarnDialog = fake
        return captured, lambda: setattr(
            self.tdn, '_showLockedWarnDialog', orig)

    def _resetLockedWarnState(self):
        self.tdn._locked_warn_batch = None
        self.tdn._locked_warn_quiet = False
        self.tdn._locked_dialog_suppress = 0

    def test_lockedwarn_batch_collects_without_dialog(self):
        root = self.sandbox.create(baseCOMP, 'lw_batch_collect')
        self._lockedTop(root, 'locked1')
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn.beginLockedWarnBatch()
            self.tdn._warnLockedNonDATs(root, context='export')
            self.assertEqual(len(captured), 0)
            self.assertEqual(len(self.tdn._locked_warn_batch), 1)
            self.assertEqual(self.tdn._locked_warn_batch[0][0], root.path)
            self.assertEqual(self.tdn._locked_warn_batch[0][1], 1)
        finally:
            restore()
            self._resetLockedWarnState()

    def test_lockedwarn_flush_shows_one_combined_dialog(self):
        root_a = self.sandbox.create(baseCOMP, 'lw_flush_a')
        root_b = self.sandbox.create(baseCOMP, 'lw_flush_b')
        self._lockedTop(root_a, 'locked_a')
        self._lockedTop(root_b, 'locked_b')
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn.beginLockedWarnBatch()
            self.tdn._warnLockedNonDATs(root_a, context='export')
            self.tdn._warnLockedNonDATs(root_b, context='export')
            self.tdn.flushLockedWarnBatch()
            self.assertEqual(len(captured), 1)
            self.assertIn(root_a.path, captured[0])
            self.assertIn(root_b.path, captured[0])
            self.assertIn('2 locked non-DAT operator(s) across 2',
                          captured[0])
            # Batch deactivated: a later lone export dialogs directly.
            self.assertIsNone(self.tdn._locked_warn_batch)
        finally:
            restore()
            self._resetLockedWarnState()

    def test_lockedwarn_flush_empty_no_dialog(self):
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn.beginLockedWarnBatch()
            self.tdn.flushLockedWarnBatch()
            self.assertEqual(len(captured), 0)
            self.assertIsNone(self.tdn._locked_warn_batch)
        finally:
            restore()
            self._resetLockedWarnState()

    def test_lockedwarn_unbatched_export_dialogs_directly(self):
        root = self.sandbox.create(baseCOMP, 'lw_direct')
        self._lockedTop(root, 'locked1')
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn._warnLockedNonDATs(root, context='export')
            self.assertEqual(len(captured), 1)
            self.assertIn(root.path, captured[0])
        finally:
            restore()
            self._resetLockedWarnState()

    def test_lockedwarn_quiet_pref_suppresses_dialog_and_batch(self):
        root = self.sandbox.create(baseCOMP, 'lw_quiet')
        self._lockedTop(root, 'locked1')
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn._locked_warn_quiet = True
            # Unbatched: no dialog.
            self.tdn._warnLockedNonDATs(root, context='export')
            self.assertEqual(len(captured), 0)
            # Batched: nothing collected either.
            self.tdn.beginLockedWarnBatch()
            self.tdn._warnLockedNonDATs(root, context='export')
            self.assertEqual(self.tdn._locked_warn_batch, [])
            self.tdn.flushLockedWarnBatch()
            self.assertEqual(len(captured), 0)
        finally:
            restore()
            self._resetLockedWarnState()

    def test_lockedwarn_dont_show_again_persists_opt_out(self):
        # Seed the auto-response: button 1 = "Don't show again".
        self.embody.store('_smoke_test_responses',
                          {'Embody -- Locked Content Warning': 1})
        pref = getattr(self.embody.par, 'Tdxnlockedwarn', None)
        orig_pref = pref.eval() if pref is not None else None
        try:
            self.tdn._showLockedWarnDialog('test message')
            self.assertTrue(self.tdn._locked_warn_quiet)
            if pref is not None:
                self.assertEqual(pref.eval(), 'quiet')
            self.assertFalse(self.tdn._lockedWarnEnabled())
        finally:
            self.embody.unstore('_smoke_test_responses')
            if pref is not None and orig_pref is not None:
                pref.val = orig_pref
            self._resetLockedWarnState()

    def test_lockedwarn_import_context_never_batches_or_dialogs(self):
        root = self.sandbox.create(baseCOMP, 'lw_import')
        self._lockedTop(root, 'locked1')
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn.beginLockedWarnBatch()
            self.tdn._warnLockedNonDATs(root, context='import')
            self.assertEqual(len(captured), 0)
            self.assertEqual(self.tdn._locked_warn_batch, [])
        finally:
            restore()
            self._resetLockedWarnState()

    # --- issue #108: source classification ---------------------------------
    # 'recooks' | 'none' | 'unknown' per locked op, traced along wires only.
    # A locked op upstream is a dead end (it comes back empty after a
    # rebuild), so each label holds for that op alone.

    def _wire(self, src, dst, index=0):
        # A COMP source wires through its output connector; connect(comp)
        # raises tdError (probed 2025.33230).
        if src.isCOMP:
            src = src.outputConnectors[0]
        dst.inputConnectors[index].connect(src)

    def _lockedIn(self, host, name='in1'):
        """A locked In SOP inside `host` -- the reporter's shape."""
        o = host.create(inSOP, name)
        o.lock = True
        return o

    def _maxLogId(self):
        return max((e['id'] for e in self.embody_ext._log_buffer), default=0)

    def _warningsSince(self, before_id):
        # Diff by entry id, never _log_buffer[-1]: the buffer is bounded
        # and other subsystems log in between.
        return [e['message'] for e in self.embody_ext._log_buffer
                if e['id'] > before_id and e['level'] == 'WARNING']

    def _shadow(self, obj, name, fn):
        """Shadow a method on an extension instance; returns the undo."""
        setattr(obj, name, fn)

        def undo():
            try:
                delattr(obj, name)
            except AttributeError:
                pass
        return undo

    def test_locked_loss_mode_follows_the_rebuild_rule(self):
        # Mirrors EmbodyExt._storageLossConsequence: Full + create on start
        # rebuilds every TDXN COMP at open (issue #108 review).
        f = type(self.tdn)._lockedLossModeFor
        self.assertEqual(f('full', True, True), 'roundtrip')
        self.assertEqual(f('full', True, False), 'roundtrip')
        self.assertEqual(f('full', False, True), 'reopen')
        self.assertEqual(f('full', False, False), 'export')
        self.assertEqual(f('export', True, True), 'export')
        g = type(self.tdn)._lockedLossModeFor.__globals__
        for table in ('_LOCKED_LOSS_TEXT', '_LOCKED_LOSS_SHORT'):
            self.assertLessEqual({'roundtrip', 'reopen', 'export'},
                                 set(g[table]), table)

    def test_lockedSource_unconnected_in_op_is_none(self):
        # Issue #108: an In SOP whose host input AND own input are unwired.
        root = self.sandbox.create(baseCOMP, 'ls_rep')
        host = root.create(baseCOMP, 'base8')
        in1 = self._lockedIn(host)
        self.assertEqual(self.tdn._lockedSourceState(in1, root), 'none')

    def test_lockedSource_in_op_fed_inside_root_recooks(self):
        root = self.sandbox.create(baseCOMP, 'ls_inside')
        box = root.create(boxSOP, 'box1')
        host = root.create(baseCOMP, 'host')
        in1 = self._lockedIn(host)
        self._wire(box, host)
        self.assertEqual(self.tdn._lockedSourceState(in1, root), 'recooks')

    def test_lockedSource_root_in_op_fed_from_outside_recooks(self):
        # A wire into the export root itself survives its rebuild.
        root = self.sandbox.create(baseCOMP, 'ls_outside')
        in1 = self._lockedIn(root)
        box = self.sandbox.create(boxSOP, 'ls_outside_box')
        self._wire(box, root)
        self.assertEqual(self.tdn._lockedSourceState(in1, root), 'recooks')

    def test_lockedSource_in_op_own_input_fallback_recooks(self):
        # Host input unwired: an In op passes its own input through
        # (probed for TOP/CHOP/SOP/POP on 2025.33230).
        root = self.sandbox.create(baseCOMP, 'ls_fallback')
        host = root.create(baseCOMP, 'host')
        in1 = self._lockedIn(host)
        box = host.create(boxSOP, 'box1')
        self._wire(box, in1)
        self.assertEqual(self.tdn._lockedSourceState(in1, root), 'recooks')

    def test_lockedSource_lone_filter_is_none(self):
        root = self.sandbox.create(baseCOMP, 'ls_lone')
        n = root.create(nullSOP, 'null1')
        n.lock = True
        self.assertEqual(self.tdn._lockedSourceState(n, root), 'none')

    def test_lockedSource_generator_recooks(self):
        root = self.sandbox.create(baseCOMP, 'ls_gen')
        t = self._lockedTop(root, 'noise1')
        self.assertEqual(self.tdn._lockedSourceState(t, root), 'recooks')

    def test_lockedSource_locked_upstream_is_dead_end(self):
        # CONSERVATIVE chain rule: the locked noise comes back empty after
        # a rebuild, so unlocking the level alone yields nothing.
        root = self.sandbox.create(baseCOMP, 'ls_dead')
        noise = self._lockedTop(root, 'noise1')
        level = root.create(levelTOP, 'level1')
        self._wire(noise, level)
        level.lock = True
        self.assertEqual(self.tdn._lockedSourceState(level, root), 'none')
        self.assertEqual(self.tdn._lockedSourceState(noise, root), 'recooks')

    def test_lockedSource_select_reference_is_unknown(self):
        root = self.sandbox.create(baseCOMP, 'ls_select')
        root.create(boxSOP, 'box1')
        sel = root.create(selectSOP, 'select1')
        sel.par.sops = 'box1'
        sel.lock = True
        self.assertEqual(self.tdn._lockedSourceState(sel, root), 'unknown')

    def test_lockedSource_objectmerge_reference_is_unknown(self):
        root = self.sandbox.create(baseCOMP, 'ls_merge')
        root.create(boxSOP, 'box1')
        om = root.create(objectmergeSOP, 'objmerge1')
        om.par.merge0sop = 'box1'
        om.lock = True
        self.assertEqual(self.tdn._lockedSourceState(om, root), 'unknown')

    def test_lockedSource_chop_auto_export_root_ignored(self):
        # Every CHOP's autoexportroot points at its parent: not a source.
        root = self.sandbox.create(baseCOMP, 'ls_chop')
        ch = root.create(noiseCHOP, 'noise1')
        ch.lock = True
        self.assertEqual(self.tdn._lockedSourceState(ch, root), 'recooks')

    def test_lockedSource_tox_boundary_is_source(self):
        root = self.sandbox.create(baseCOMP, 'ls_tox')
        sub = root.create(baseCOMP, 'sub_tox')
        sub.tags.add(self.embody.par.Toxtag.val)
        sub.create(outSOP, 'out1')
        n = root.create(nullSOP, 'null1')
        self._wire(sub, n)
        n.lock = True
        self.assertEqual(self.tdn._lockedSourceState(n, root), 'recooks')

    def test_lockedSource_nested_tdxn_locked_op_is_dead_end(self):
        root = self.sandbox.create(baseCOMP, 'ls_nested')
        sub = root.create(baseCOMP, 'sub_tdn')
        sub.tags.add(self.embody.par.Tdxntag.val)
        inner = self._lockedTop(sub, 'noise1')
        out = sub.create(outTOP, 'out1')
        self._wire(inner, out)
        n = root.create(nullTOP, 'null1')
        self._wire(sub, n)
        n.lock = True
        self.assertEqual(self.tdn._lockedSourceState(n, root), 'none')

    def test_lockedSource_slash_root_follows_wires(self):
        # A '/' root once read every wired In op as sourced; the walk must
        # still follow the host connector to the locked, empty null.
        root = self.sandbox.create(baseCOMP, 'ls_slash')
        dead = root.create(nullSOP, 'dead')
        dead.lock = True
        host = root.create(baseCOMP, 'host')
        in1 = self._lockedIn(host)
        self._wire(dead, host)
        self.assertEqual(self.tdn._lockedSourceState(in1, op('/'), {}),
                         'none')

    def test_classifyLocked_budget_exhausted_is_unknown(self):
        root = self.sandbox.create(baseCOMP, 'ls_budget')
        self._lockedTop(root, 'noise1')
        n = root.create(nullSOP, 'null1')
        n.lock = True
        locked = self.tdn._findLockedNonDATs(root)
        states = self.tdn._classifyLocked(locked, root, budget_s=0.0)
        self.assertEqual(set(states.values()), {'unknown'})

    def test_classifyLocked_memo_shared_across_findings(self):
        root = self.sandbox.create(baseCOMP, 'ls_memo')
        box = root.create(boxSOP, 'box1')
        xf = root.create(transformSOP, 'xf')
        self._wire(box, xf)
        a = root.create(nullSOP, 'a')
        b = root.create(nullSOP, 'b')
        self._wire(xf, a)
        self._wire(xf, b)
        a.lock = True
        b.lock = True
        calls = []
        orig = self.tdn._lockedLinks
        undo = self._shadow(self.tdn, '_lockedLinks', lambda o, r: (
            calls.append(o.path), orig(o, r))[1])
        try:
            states = self.tdn._classifyLocked([a, b], root, memo={})
        finally:
            undo()
        self.assertEqual([states[a.id], states[b.id]], ['recooks', 'recooks'])
        self.assertEqual(calls.count(xf.path), 1,
                         'shared upstream must be walked once per scan')

    def test_classifyLocked_walk_error_is_unknown(self):
        root = self.sandbox.create(baseCOMP, 'ls_raise')
        t = self._lockedTop(root, 'noise1')

        def boom(*args, **kwargs):
            raise RuntimeError('classifier bug')
        undo = self._shadow(self.tdn, '_lockedSourceState', boom)
        try:
            states = self.tdn._classifyLocked([t], root)
        finally:
            undo()
        self.assertEqual(states[t.id], 'unknown')

    def test_findLocked_locked_pop_reported(self):
        # A lock freezes POP points too (issue #108 probe).
        root = self.sandbox.create(baseCOMP, 'lk_pop')
        grid = root.create(gridPOP, 'grid1')
        n = root.create(nullPOP, 'null1')
        self._wire(grid, n)
        n.cook(force=True)
        n.lock = True
        self.assertIn(n, self.tdn._findLockedNonDATs(root))
        self.assertEqual(self.tdn._lockedSourceState(n, root), 'recooks')

    def test_findLocked_only_filter(self):
        root = self.sandbox.create(baseCOMP, 'lk_only')
        a = self._lockedTop(root, 'a')
        self._lockedTop(root, 'b')
        self.assertEqual(
            self.tdn._findLockedNonDATs(root, only={a.path}), [a])

    # --- issue #108: export / import call sites -----------------------------

    def _exportToTemp(self, root, **kwargs):
        # Beside root's own mirror path, under the gitignored test_sandbox/
        # folder: a %TEMP% file sits outside project.folder, so tracking it
        # logged a spurious 'Failed to track TDXN export' WARNING.
        rel = self.embody_ext._buildTDXNRelPath(root)
        path = self.embody_ext.buildAbsolutePath(
            rel.parent / ('i108_%s.tdxn' % root.name))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self.tdn.ExportNetwork(
                root_path=root.path, output_file=str(path), **kwargs)
        finally:
            if path.exists():
                path.unlink()

    def test_export_noninteractive_logs_export_text(self):
        # MCP export_network passes interactive=False: it once logged the
        # IMPORT text ('Restored lock flag ... should be unlocked').
        root = self.sandbox.create(baseCOMP, 'ex_nonint')
        host = root.create(baseCOMP, 'base8')
        self._lockedIn(host)
        captured, restore = self._interceptLockedDialog()
        before = self._maxLogId()
        try:
            result = self._exportToTemp(root, interactive=False)
        finally:
            restore()
            self._resetLockedWarnState()
        self.assertTrue(result.get('success'), result)
        self.assertEqual(captured, [])
        warns = self._warningsSince(before)
        hits = [m for m in warns if m.startswith('Locked non-DAT operators in')]
        self.assertEqual(len(hits), 1, warns)
        self.assertIn('source: none', hits[0])
        self.assertIn("externalize_op('%s', tag_type='tox')" % host.path,
                      hits[0])
        self.assertFalse(any('Restored lock flag' in m for m in warns))

    def test_export_classifier_error_still_warns(self):
        root = self.sandbox.create(baseCOMP, 'ex_raise')
        self._lockedTop(root, 'noise1')

        def boom(*args, **kwargs):
            raise RuntimeError('classifier bug')
        undo = self._shadow(self.tdn, '_classifyLocked', boom)
        before = self._maxLogId()
        try:
            result = self._exportToTemp(root, interactive=False)
        finally:
            undo()
        self.assertTrue(result.get('success'), result)
        hits = [m for m in self._warningsSince(before)
                if m.startswith('Locked non-DAT operators in')]
        self.assertEqual(len(hits), 1)
        self.assertIn('source: unknown', hits[0])

    def test_export_warning_error_never_fails_the_export(self):
        # An exception here once failed a written export and rolled the
        # caller's tag back (_handleTDXNAddition).
        root = self.sandbox.create(baseCOMP, 'ex_warnraise')
        self._lockedTop(root, 'noise1')

        def boom(*args, **kwargs):
            raise RuntimeError('warning bug')
        undo = self._shadow(self.tdn, '_warnLockedNonDATs', boom)
        before = self._maxLogId()
        try:
            result = self._exportToTemp(root, interactive=False)
        finally:
            undo()
        self.assertTrue(result.get('success'), result)
        self.assertTrue(any(m.startswith('Locked-content scan failed')
                            for m in self._warningsSince(before)))

    def test_import_names_source_after_wiring(self):
        # The import warning used to run before Phase 5 wiring, so every
        # op read unwired; now it traces the restored wire.
        src = self.sandbox.create(baseCOMP, 'im_src')
        box = src.create(boxSOP, 'box1')
        n = src.create(nullSOP, 'null1')
        self._wire(box, n)
        n.cook(force=True)
        n.lock = True
        doc = self.tdn.ExportNetwork(root_path=src.path)['tdn']
        dest = self.sandbox.create(baseCOMP, 'im_dest')
        before = self._maxLogId()
        result = self.tdn.ImportNetwork(dest.path, doc)
        self.assertTrue(result.get('success'), result)
        hits = [m for m in self._warningsSince(before)
                if m.startswith('Restored lock flag on 1')]
        self.assertEqual(len(hits), 1)
        self.assertIn('source: recooks', hits[0])

    def test_import_warns_only_about_ops_it_created(self):
        # A clear_first=False paste must not report pre-existing locked ops.
        src = self.sandbox.create(baseCOMP, 'im_src2')
        src.create(boxSOP, 'box1')
        doc = self.tdn.ExportNetwork(root_path=src.path)['tdn']
        dest = self.sandbox.create(baseCOMP, 'im_dest2')
        self._lockedTop(dest, 'kept')
        before = self._maxLogId()
        result = self.tdn.ImportNetwork(dest.path, doc)
        self.assertTrue(result.get('success'), result)
        self.assertFalse(any('Restored lock flag' in m
                             for m in self._warningsSince(before)))

    def test_import_without_shell_restore_makes_no_source_claims(self):
        src = self.sandbox.create(baseCOMP, 'im_src3')
        n = src.create(nullSOP, 'null1')
        n.lock = True
        doc = self.tdn.ExportNetwork(root_path=src.path)['tdn']
        dest = self.sandbox.create(baseCOMP, 'im_dest3')
        before = self._maxLogId()
        self.tdn.ImportNetwork(dest.path, doc, restore_tdxn_shells=False)
        hits = [m for m in self._warningsSince(before)
                if m.startswith('Restored lock flag on 1')]
        self.assertEqual(len(hits), 1)
        # Count only: no per-op sources while nested shells are empty.
        self.assertIn('sources not traced', hits[0])
        self.assertNotIn('source:', hits[0])
        self.assertNotIn(dest.op('null1').path, hits[0])

    # --- issue #108: dialog text --------------------------------------------

    def test_lockedwarn_dialog_marks_no_source_and_offers_switch(self):
        root = self.sandbox.create(baseCOMP, 'lw_nosrc')
        host = root.create(baseCOMP, 'base8')
        self._lockedIn(host)
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn._warnLockedNonDATs(root, context='export')
        finally:
            restore()
            self._resetLockedWarnState()
        self.assertEqual(len(captured), 1)
        self.assertIn('NO SOURCE (unlocking leaves it empty)', captured[0])
        self.assertIn('Switch to TOX', captured[0])
        self.assertEqual(self._targets_seen, [[host.path]])

    def test_lockedwarn_reporter_dialog_never_suggests_unlocking(self):
        # The issue's title: the old footer told the user to unlock an op
        # with nothing to re-cook from. Every line naming unlocking must
        # now say it leaves the op empty.
        root = self.sandbox.create(baseCOMP, 'lw_neg')
        host = root.create(baseCOMP, 'base8')
        self._lockedIn(host)
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn._warnLockedNonDATs(root, context='export')
        finally:
            restore()
            self._resetLockedWarnState()
        msg = captured[0]
        self.assertNotIn('re-cook from inputs', msg)
        self.assertNotIn('If a fresh cook is acceptable', msg)
        for line in msg.splitlines():
            if 'unlock' in line.lower():
                self.assertIn('leaves it empty', line)

    def test_lockedwarn_recooks_unlock_line_is_separate(self):
        root = self.sandbox.create(baseCOMP, 'lw_recook')
        self._lockedTop(root, 'noise1')
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn._warnLockedNonDATs(root, context='export')
        finally:
            restore()
            self._resetLockedWarnState()
        msg = captured[0]
        self.assertIn('the frozen snapshot is replaced, not restored', msg)
        self.assertIn('If a fresh cook is acceptable, unlock', msg)
        self.assertNotIn('To preserve locked content', msg)

    def test_lockedwarn_dialog_fits_the_screen(self):
        root = self.sandbox.create(baseCOMP, 'lw_many')
        for i in range(30):
            sub = root.create(baseCOMP, 'sub%02d' % i)
            self._lockedIn(sub, 'locked_input_with_a_long_name')
        captured, restore = self._interceptLockedDialog()
        before = self._maxLogId()
        try:
            self.tdn._warnLockedNonDATs(root, context='export')
        finally:
            restore()
            self._resetLockedWarnState()
        wrapped = self.embody_ext._wrapDialogText(captured[0])
        self.assertLessEqual(wrapped.count('\n') + 1, 35)
        self.assertIn('see the Embody log', captured[0])
        # The button tags all 30; the dialog names 4 and the log every one.
        self.assertIn('Switch to TOX tags 30 COMP(s)', captured[0])
        self.assertIn('the Embody log lists them all', captured[0])
        warn = [m for m in self._warningsSince(before)
                if m.startswith('Locked non-DAT operators in')]
        self.assertEqual(len(warn), 1)
        self.assertIn('these 30 COMPs', warn[0])
        self.assertIn(root.path + '/sub29', warn[0])

    def test_lockedwarn_toplevel_root_is_never_a_target(self):
        root = self.sandbox.create(baseCOMP, 'lw_top')
        self._lockedTop(root, 'noise1')
        undo = self._shadow(self.tdn, '_tdxnTaggedAncestor', lambda c: None)
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn._warnLockedNonDATs(root, context='export')
        finally:
            restore()
            undo()
            self._resetLockedWarnState()
        self.assertEqual(self._targets_seen, [[]])
        self.assertIn('no child COMP that can be switched', captured[0])

    # --- issue #108: Switch to TOX button -----------------------------------

    def _recordSwitch(self):
        rec = []
        undo = self._shadow(
            self.tdn, '_scheduleLockedSwitch',
            lambda paths, attempt=0, delay=5: rec.append(
                (list(paths), attempt, delay)))
        return rec, undo

    def test_lockedwarn_switch_button_schedules(self):
        self.embody.store('_smoke_test_responses',
                          {'Embody -- Locked Content Warning': 2})
        rec, undo = self._recordSwitch()
        try:
            self.tdn._showLockedWarnDialog('m', switch_targets=('/x',))
            quiet = self.tdn._locked_warn_quiet
        finally:
            undo()
            self.embody.unstore('_smoke_test_responses')
            self._resetLockedWarnState()
        self.assertEqual(rec, [(['/x'], 0, 5)])
        self.assertFalse(quiet)

    def test_lockedwarn_switch_index_without_targets_is_noop(self):
        self.embody.store('_smoke_test_responses',
                          {'Embody -- Locked Content Warning': 2})
        rec, undo = self._recordSwitch()
        try:
            self.tdn._showLockedWarnDialog('m')
            quiet = self.tdn._locked_warn_quiet
        finally:
            undo()
            self.embody.unstore('_smoke_test_responses')
            self._resetLockedWarnState()
        self.assertEqual(rec, [])
        self.assertFalse(quiet)

    def test_lockedwarn_unanswered_dialog_schedules_nothing(self):
        # A closed or suppressed box returns -1: never a switch. Seeded, so
        # the answer never depends on how the runner was invoked.
        self.embody.store('_smoke_test_responses',
                          {'Embody -- Locked Content Warning': -1})
        rec, undo = self._recordSwitch()
        try:
            self.tdn._showLockedWarnDialog('m', switch_targets=('/x',))
        finally:
            undo()
            self.embody.unstore('_smoke_test_responses')
            self._resetLockedWarnState()
        self.assertEqual(rec, [])

    def test_lockedwarn_suppression_blocks_dialog(self):
        root = self.sandbox.create(baseCOMP, 'lw_suppress')
        self._lockedTop(root, 'noise1')
        captured, restore = self._interceptLockedDialog()
        try:
            with self.tdn.suppressLockedDialogs():
                self.tdn._warnLockedNonDATs(root, context='export')
                self.assertEqual(len(captured), 0)
            self.assertEqual(self.tdn._locked_dialog_suppress, 0)
            self.tdn._warnLockedNonDATs(root, context='export')
            self.assertEqual(len(captured), 1)
        finally:
            restore()
            self._resetLockedWarnState()

    def test_lockedwarn_flush_combined_lists_targets(self):
        root_a = self.sandbox.create(baseCOMP, 'lw_tg_a')
        root_b = self.sandbox.create(baseCOMP, 'lw_tg_b')
        sub_a = root_a.create(baseCOMP, 'sub')
        sub_b = root_b.create(baseCOMP, 'sub')
        self._lockedIn(sub_a)
        self._lockedIn(sub_b)
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn.beginLockedWarnBatch()
            self.tdn._warnLockedNonDATs(root_a, context='export')
            self.tdn._warnLockedNonDATs(root_b, context='export')
            self.tdn.flushLockedWarnBatch()
        finally:
            restore()
            self._resetLockedWarnState()
        self.assertEqual(len(captured), 1)
        self.assertIn('2 locked non-DAT operator(s) across 2', captured[0])
        self.assertEqual(sorted(self._targets_seen[0]),
                         sorted([sub_a.path, sub_b.path]))

    def test_lockedwarn_flush_while_suppressed_is_log_only(self):
        root = self.sandbox.create(baseCOMP, 'lw_flush_sup')
        self._lockedTop(root, 'noise1')
        captured, restore = self._interceptLockedDialog()
        try:
            self.tdn.beginLockedWarnBatch()
            self.tdn._warnLockedNonDATs(root, context='export')
            with self.tdn.suppressLockedDialogs():
                self.tdn.flushLockedWarnBatch()
            self.assertEqual(captured, [])
            self.assertIsNone(self.tdn._locked_warn_batch)
        finally:
            restore()
            self._resetLockedWarnState()

    # --- issue #108: switch targets and refusals ----------------------------

    def test_lockedSwitchTargets_collapse_nested(self):
        root = self.sandbox.create(baseCOMP, 'st_nest')
        a = root.create(baseCOMP, 'a')
        b = a.create(baseCOMP, 'b')
        self._lockedTop(a, 'x1')
        self._lockedTop(b, 'x2')
        self._lockedTop(root, 'x3')
        undo = self._shadow(self.tdn, '_tdxnTaggedAncestor', lambda c: None)
        try:
            locked = self.tdn._findLockedNonDATs(root)
            targets, uncovered = self.tdn._lockedSwitchTargets(locked, root)
        finally:
            undo()
        self.assertEqual(targets, [a.path])
        self.assertEqual(uncovered, 1)

    def test_lockedSwitchTargets_cascade_root(self):
        # The sandbox chain is TDXN-tagged, so a finding directly in the
        # root may switch the root itself (its ancestor writes tox_ref).
        root = self.sandbox.create(baseCOMP, 'st_cascade')
        self._lockedTop(root, 'x1')
        self.assertIsNotNone(self.tdn._tdxnTaggedAncestor(root))
        locked = self.tdn._findLockedNonDATs(root)
        self.assertEqual(self.tdn._lockedSwitchTargets(locked, root),
                         ([root.path], 0))

    def test_lockedSwitchRefusal_embody_and_system(self):
        self.assertIsNotNone(self.tdn._lockedSwitchRefusal(self.embody))
        self.assertIsNotNone(
            self.tdn._lockedSwitchRefusal(self.embody.parent()))
        self.assertEqual(self.tdn._lockedSwitchRefusal(op('/sys')),
                         'a TouchDesigner system COMP')

    def test_lockedSwitchRefusal_external_tox_and_tagged_descendant(self):
        ext = self.sandbox.create(baseCOMP, 'sr_ext')
        ext.par.enableexternaltox = False
        ext.par.externaltox = 'nowhere/i108_missing.tox'
        self.assertTrue(
            (self.tdn._lockedSwitchRefusal(ext) or '').startswith(
                'already links an external .tox'))
        holder = self.sandbox.create(baseCOMP, 'sr_hold')
        inner = holder.create(baseCOMP, 'inner')
        inner.tags.add(self.embody.par.Toxtag.val)
        self.assertTrue(
            (self.tdn._lockedSwitchRefusal(holder) or '').startswith(
                'contains the externalized COMP'))

    # --- issue #108: deferred switch ----------------------------------------

    def test_switch_rearms_while_a_save_is_running(self):
        rec, undo = self._recordSwitch()
        self.embody.store('_suppress_dialogs', True)
        try:
            self.assertEqual(self.tdn._lockedSwitchBusyReason(),
                             'a project save')
            self.assertEqual(
                self.tdn.switchLockedCompsToTOX(['/x'], 0), [])
        finally:
            self.embody.unstore('_suppress_dialogs')
            undo()
        self.assertEqual(rec, [(['/x'], 1, 30)])

    def test_switch_gives_up_after_max_attempts(self):
        rec, undo = self._recordSwitch()
        before = self._maxLogId()
        self.embody.store('_suppress_dialogs', True)
        try:
            self.assertEqual(
                self.tdn.switchLockedCompsToTOX(['/x'], 10), [])
        finally:
            self.embody.unstore('_suppress_dialogs')
            undo()
        self.assertEqual(rec, [])
        self.assertTrue(any(
            m.startswith('Switch to TOX gave up')
            and "externalize_op('/x', tag_type='tox')" in m
            for m in self._warningsSince(before)))

    def test_switch_skips_perform_mode_before_waiting(self):
        # Waiting cannot end Perform Mode: skip at once, never re-arm.
        ext_class = type(self.embody_ext)
        orig_perform = ext_class.__dict__['_performMode']
        rec, undo = self._recordSwitch()
        before = self._maxLogId()
        self.embody.store('_suppress_dialogs', True)
        try:
            ext_class._performMode = property(lambda self_: True)
            done = self.tdn.switchLockedCompsToTOX(['/x'], 0)
        finally:
            ext_class._performMode = orig_perform
            self.embody.unstore('_suppress_dialogs')
            undo()
        self.assertEqual(done, [])
        self.assertEqual(rec, [])
        self.assertTrue(any(m.startswith('Switch to TOX skipped: Embody is')
                            for m in self._warningsSince(before)))

    def test_update_chain_killed_by_perform_mode_clears_state(self):
        # A dead chain left _updd_state set, so every later Switch to TOX
        # waited out 10 x 30 frames on 'an Update sweep'; a batch it left
        # open swallowed every later locked-content dialog.
        ext = self.embody_ext
        ext_class = type(ext)
        orig_perform = ext_class.__dict__['_performMode']
        state_was = getattr(ext, '_updd_state', None)
        gen = getattr(ext, '_updd_gen', 0)
        ext._updd_gen = gen  # unset until the first deferred Update
        captured, restore = self._interceptLockedDialog()
        try:
            ext._updd_state = {'phase': 'export', 'batch_open': True}
            self.tdn.beginLockedWarnBatch()
            self.tdn._locked_warn_batch.append(('/x', 1, {'none': 1}, (), 1))
            ext_class._performMode = property(lambda self_: True)
            ext._updateChunk(gen)
            cleared = ext._updd_state is None
            batch = self.tdn._locked_warn_batch
        finally:
            ext_class._performMode = orig_perform
            ext._updd_state = state_was
            restore()
            self._resetLockedWarnState()
        self.assertTrue(cleared)
        self.assertIsNone(batch)
        self.assertEqual(captured, [])

    def test_switch_core_failure_logs_manual_command(self):
        # Detached from a run() string: a failure must reach the Embody
        # log with the command that finishes the switch.
        comp = self.sandbox.create(baseCOMP, 'sw_fail')

        def boom(*args, **kwargs):
            raise RuntimeError('handleAddition bug')
        undo = self._shadow(self.embody_ext, 'handleAddition', boom)
        before = self._maxLogId()
        try:
            done = self.tdn._switchLockedCore([comp.path])
        finally:
            undo()
        self.assertEqual(done, [])
        self.assertTrue(any(
            m.startswith('Switch to TOX failed for %s' % comp.path)
            and "externalize_op('%s', tag_type='tox')" % comp.path in m
            for m in self._warningsSince(before)))

    def test_switch_core_skips_refused_targets(self):
        ext = self.sandbox.create(baseCOMP, 'sw_ext')
        ext.par.enableexternaltox = False
        ext.par.externaltox = 'nowhere/i108_missing.tox'
        before = self._maxLogId()
        done = self.tdn._switchLockedCore(
            [self.sandbox.path + '/sw_gone', self.embody.parent().path,
             '/sys', ext.path])
        self.assertEqual(done, [])
        skipped = [m for m in self._warningsSince(before)
                   if m.startswith('Switch to TOX skipped for')]
        self.assertEqual(len(skipped), 4)
        self.assertNotIn(self.embody.par.Toxtag.val, ext.tags)

    def test_switch_core_tags_writes_and_parent_refs(self):
        # Targeted: tag + handleAddition + ONE parent saveTDXN -- never a
        # project-wide Update().
        ext_class = type(self.embody_ext)
        orig_update = ext_class.Update
        update_calls = []
        cascade_was = self.embody.par.Tdxncascade.eval()
        root = self.sandbox.create(baseCOMP, 'sw_root')
        child = root.create(baseCOMP, 'base8')
        self._lockedIn(child)
        captured, restore = self._interceptLockedDialog()
        try:
            self.embody.par.Tdxncascade = False
            self.assertTrue(self.embody_ext.applyTagToOperator(
                root, self.embody.par.Tdxntag.val))
            # Else the ancestor walk re-exports the COMMITTED test_sandbox
            # receipt with this test's content in it.
            self.assertIn(root.path, self.tdn._getTDXNExternalizedPaths())
            ext_class.Update = (
                lambda self_, *a, **k: update_calls.append(1))
            done = self.tdn._switchLockedCore([child.path])
        finally:
            ext_class.Update = orig_update
            self.embody.par.Tdxncascade = cascade_was
            restore()
            self._resetLockedWarnState()
        self.assertEqual(done, [child.path])
        self.assertEqual(update_calls, [])
        self.assertIn(self.embody.par.Toxtag.val, child.tags)
        self.assertNotEqual(child.par.externaltox.eval(), '')
        self.assertEqual(self.tdn._findLockedNonDATs(root), [])
        # The parent's .tdxn on disk now points at the child's .tox.
        rel = self.embody_ext._getStrategyFilePath(root.path, 'tdn')
        self.assertTrue(rel)
        text = self.embody_ext.buildAbsolutePath(rel).read_text(
            encoding='utf-8')
        doc = self.tdn.tdxn_load(text)
        entry = next(d for d in doc['operators'] if d.get('name') == 'base8')
        self.assertIn('tox_ref', entry)

    # --- issue #108: cascade and MCP ----------------------------------------

    def test_cascade_skips_tox_tagged_child(self):
        # A Switch to TOX result must survive a later cascade: retagging it
        # TDXN would delete its .tox through mutual exclusivity.
        parent = self.sandbox.create(baseCOMP, 'cs_parent')
        keep = parent.create(baseCOMP, 'plain')
        tox = parent.create(baseCOMP, 'switched')
        tox.tags.add(self.embody.par.Toxtag.val)
        calls = []
        undo = self._shadow(self.embody_ext, 'applyTagToOperator',
                            lambda o, t: calls.append(o.path))
        try:
            self.embody_ext._cascadeTDXNTag(parent)
        finally:
            undo()
        self.assertIn(keep.path, calls)
        self.assertNotIn(tox.path, calls)

    def test_mcp_externalize_op_never_shows_locked_dialog(self):
        ext_class = type(self.embody_ext)
        orig_update = ext_class.Update
        cascade_was = self.embody.par.Tdxncascade.eval()
        root = self.sandbox.create(baseCOMP, 'mcp_root')
        host = root.create(baseCOMP, 'base8')
        self._lockedIn(host)
        captured, restore = self._interceptLockedDialog()
        before = self._maxLogId()
        try:
            self.embody.par.Tdxncascade = False
            ext_class.Update = lambda self_, *a, **k: None
            result = self.embody.ext.Envoy._externalize_op(
                op_path=root.path, tag_type=self.embody.par.Tdxntag.val)
            depth = self.tdn._locked_dialog_suppress
        finally:
            ext_class.Update = orig_update
            self.embody.par.Tdxncascade = cascade_was
            restore()
            self._resetLockedWarnState()
        self.assertTrue(result.get('success'), result)
        self.assertEqual(captured, [])
        self.assertEqual(depth, 0)
        self.assertTrue(any(
            m.startswith('Locked non-DAT operators in %s' % root.path)
            for m in self._warningsSince(before)))

    def test_mcp_save_externalization_never_shows_locked_dialog(self):
        cascade_was = self.embody.par.Tdxncascade.eval()
        root = self.sandbox.create(baseCOMP, 'mcp_save_root')
        host = root.create(baseCOMP, 'base8')
        self._lockedIn(host)
        captured, restore = self._interceptLockedDialog()
        try:
            self.embody.par.Tdxncascade = False
            with self.tdn.suppressLockedDialogs():
                self.assertTrue(self.embody_ext.applyTagToOperator(
                    root, self.embody.par.Tdxntag.val))
            self.assertIn(root.path, self.tdn._getTDXNExternalizedPaths())
            before = self._maxLogId()
            result = self.embody.ext.Envoy._save_externalization(
                op_path=root.path)
        finally:
            self.embody.par.Tdxncascade = cascade_was
            restore()
            self._resetLockedWarnState()
        self.assertTrue(result.get('success'), result)
        self.assertEqual(captured, [])
        self.assertTrue(any(
            m.startswith('Locked non-DAT operators in %s' % root.path)
            for m in self._warningsSince(before)))

    def test_auto_externalize_never_shows_locked_dialog(self):
        # create_op/copy_op/create_extension tag through autoExternalizeNewOp
        # (Envoy only): a copied COMP's TDXN export logs, never a modal.
        cascade_was = self.embody.par.Tdxncascade.eval()
        tdxn_tag = self.embody.par.Tdxntag.val
        root = self.sandbox.create(baseCOMP, 'ae_root')
        host = root.create(baseCOMP, 'base8')
        self._lockedIn(host)
        # The sandbox chain is TDXN-tagged, which the boundary rule skips.
        undo = self._shadow(self.embody_ext, '_autoExternalizeTagFor',
                            lambda o: tdxn_tag)
        captured, restore = self._interceptLockedDialog()
        before = self._maxLogId()
        try:
            self.embody.par.Tdxncascade = False
            applied = self.embody_ext.autoExternalizeNewOp(root)
        finally:
            undo()
            self.embody.par.Tdxncascade = cascade_was
            restore()
            self._resetLockedWarnState()
        self.assertEqual(applied, tdxn_tag)
        self.assertEqual(captured, [])
        self.assertTrue(any(
            m.startswith('Locked non-DAT operators in %s' % root.path)
            for m in self._warningsSince(before)))

    def test_auto_externalize_flush_suppresses_locked_dialog(self):
        # The deferred Update a DAT auto-externalization schedules. A save
        # flag left stuck by an earlier suite would take the re-arm branch
        # and schedule a REAL Update mid-run; the base tearDown restores it.
        ext_class = type(self.embody_ext)
        orig_update = ext_class.Update
        pending_was = getattr(self.embody_ext, '_auto_ext_flush_pending', False)
        self.embody.unstore('_suppress_dialogs')
        seen = []
        try:
            ext_class.Update = (lambda self_, *a, **k: seen.append(
                self.tdn._locked_dialog_suppress))
            self.embody_ext._autoExternalizeFlush()
            depth = self.tdn._locked_dialog_suppress
        finally:
            ext_class.Update = orig_update
            self.embody_ext._auto_ext_flush_pending = pending_was
            self._resetLockedWarnState()
        self.assertEqual(seen, [1])
        self.assertEqual(depth, 0)

    def tearDown(self):
        # Switch/MCP tests track sandbox COMPs: delete their files (all
        # under the gitignored test_sandbox/ folder) while still tracked,
        # then drop the rows.
        table = self.embody_ext.Externalizations
        if table is not None:
            for i in range(table.numRows - 1, 0, -1):
                if table[i, 'path'].val.startswith(self.sandbox.path):
                    rel = self.embody_ext._cellVal(i, 'rel_file_path')
                    if rel and '/test_sandbox/' in (
                            '/' + rel.replace('\\', '/')):
                        try:
                            self.embody_ext.safeDeleteFile(
                                self.embody_ext.buildAbsolutePath(rel))
                        except Exception:
                            pass
                    table.deleteRow(i)
        super().tearDown()
