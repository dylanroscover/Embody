"""
Test suite: TDN content safety guards - DAT + storage loss detection,
combined dialog, and the Skip Once / Always Skip preference buttons.

Covers:
  A. _findAtRiskStorage detects user storage keys on TDN COMPs
  B. Control keys and runtime/skip keys are NOT flagged as at-risk
  C. Combined dialog surfaces both DATs and storage
  D. Dialog offers Skip Once + an explicit, reversible Always Skip
     (the old bare single-click "Never Ask" label stays gone); the
     "Always" buttons persist the Tdndatsafety preference
  E. Skip logs a SUCCESS summary of what was dropped
"""

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestTDNSafetyGuards(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        # The sandbox lives inside a registered TDN-strategy COMP
        # (test_sandbox in the unit_tests project), so storage we set on
        # self.sandbox is detected by _findAtRiskStorage under that parent.
        self._prev_embed_storage = self.embody.par.Embedstorageintdns.eval()
        self._prev_embed_dats = self.embody.par.Embeddatsintdns.eval()
        self.embody.par.Embedstorageintdns.val = False
        self.embody.par.Embeddatsintdns.val = False
        # Preference: 'ask' so the check routes through the prompt.
        self._prev_safety = self.embody.par.Tdndatsafety.eval()
        self.embody.par.Tdndatsafety.val = 'ask'
        # Intercept messageBox so tests never actually block on UI.
        self._captured = []
        self._orig_messageBox = self.embody_ext._messageBox

        def _stub(title, message, buttons):
            self._captured.append({'title': title, 'message': message,
                                   'buttons': list(buttons)})
            return self._scripted_choice

        # Default: index 2 == "Skip Once" (one-time skip, no persistence).
        self._scripted_choice = 2
        self.embody_ext._messageBox = _stub

    def tearDown(self):
        self.embody_ext._messageBox = self._orig_messageBox
        self.embody.par.Embedstorageintdns.val = self._prev_embed_storage
        self.embody.par.Embeddatsintdns.val = self._prev_embed_dats
        self.embody.par.Tdndatsafety.val = self._prev_safety
        super().tearDown()

    # ------------------------------------------------------------------
    # A. Storage detection
    # ------------------------------------------------------------------

    def _flatten(self, result):
        """Flatten [(comp_path, [(op_path, [keys])])] into {op_path: set(keys)}."""
        out = {}
        for _, entries in result:
            for op_path, keys in entries:
                out.setdefault(op_path, set()).update(keys)
        return out

    def test_findAtRiskStorage_detects_user_key(self):
        self.sandbox.store('my_user_key', {'some': 'data'})
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            self.assertIn(self.sandbox.path, flat,
                f'Sandbox at {self.sandbox.path} missing from result: {flat}')
            self.assertIn('my_user_key', flat[self.sandbox.path])
        finally:
            self.sandbox.unstore('my_user_key')

    def test_findAtRiskStorage_ignores_control_keys(self):
        # Control keys on an op must not surface as at-risk.
        self.sandbox.store('embed_storage_in_tdn', False)
        self.sandbox.store('embed_dats_in_tdn', False)
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            keys_on_sandbox = flat.get(self.sandbox.path, set())
            self.assertNotIn('embed_storage_in_tdn', keys_on_sandbox)
            self.assertNotIn('embed_dats_in_tdn', keys_on_sandbox)
        finally:
            self.sandbox.unstore('embed_storage_in_tdn')
            self.sandbox.unstore('embed_dats_in_tdn')

    def test_findAtRiskStorage_ignores_runtime_keys(self):
        # Keys in _STORAGE_SKIP_KEYS are runtime noise, not user data.
        self.sandbox.store('_init_complete', True)
        self.sandbox.store('hover', False)
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            keys_on_sandbox = flat.get(self.sandbox.path, set())
            self.assertNotIn('_init_complete', keys_on_sandbox)
            self.assertNotIn('hover', keys_on_sandbox)
        finally:
            self.sandbox.unstore('_init_complete')
            self.sandbox.unstore('hover')

    def test_findAtRiskStorage_empty_when_per_comp_embed_on(self):
        # Per-COMP override of embed_storage_in_tdn=True excludes the
        # enclosing TDN COMP from at-risk detection.
        # (We store on the test_sandbox TDN COMP, which is self.sandbox's
        # registered TDN parent.)
        tdn_parent = self.sandbox.parent()
        tdn_parent.store('embed_storage_in_tdn', True)
        self.sandbox.store('my_key', 'value')
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            keys_on_sandbox = flat.get(self.sandbox.path, set())
            self.assertNotIn('my_key', keys_on_sandbox,
                'embed_storage=True on the TDN parent must exclude descendants')
        finally:
            self.sandbox.unstore('my_key')
            tdn_parent.unstore('embed_storage_in_tdn')

    # ------------------------------------------------------------------
    # B. Dialog - Skip Once + explicit reversible Always Skip; both
    #    "Always" buttons persist the Tdndatsafety preference
    # ------------------------------------------------------------------

    def test_prompt_offers_skip_once_and_always_skip(self):
        self.sandbox.store('risky', 'data')
        try:
            self.embody_ext._checkTDNContentSafety()
            self.assertEqual(len(self._captured), 1,
                'Expected exactly one dialog for at-risk content')
            buttons = self._captured[0]['buttons']
            # The old bare single-click "Never Ask" / "Skip" labels are gone;
            # the persistent skip is now the explicit, reversible "Always Skip".
            self.assertNotIn('Never Ask', buttons)
            self.assertIn('Skip Once', buttons)
            self.assertIn('Always Skip', buttons)
            self.assertIn('Always Externalize', buttons)
        finally:
            self.sandbox.unstore('risky')

    def test_always_skip_persists_ignore_preference(self):
        """'Always Skip' (index 3) sets Tdndatsafety='ignore' so future
        saves don't warn, and still skips the current save."""
        self.sandbox.store('risky', 'data')
        try:
            self._scripted_choice = 3  # Always Skip
            self.embody_ext._checkTDNContentSafety()
            self.assertEqual(self.embody.par.Tdndatsafety.eval(), 'ignore',
                'Always Skip must persist the ignore preference')
        finally:
            self.sandbox.unstore('risky')

    def test_always_externalize_persists_externalize_preference(self):
        """'Always Externalize' (index 1) sets Tdndatsafety='externalize'."""
        self.sandbox.store('risky', 'data')
        try:
            self._scripted_choice = 1  # Always Externalize
            self.embody_ext._checkTDNContentSafety()
            self.assertEqual(self.embody.par.Tdndatsafety.eval(),
                             'externalize',
                'Always Externalize must persist the externalize preference')
        finally:
            self.sandbox.unstore('risky')

    def test_ignore_preference_suppresses_prompt(self):
        """With Tdndatsafety='ignore' (the result of Always Skip), no dialog
        is shown even when at-risk content exists - that is the whole point."""
        self.embody.par.Tdndatsafety.val = 'ignore'
        self.sandbox.store('risky', 'data')
        try:
            self.embody_ext._checkTDNContentSafety()
            self.assertEqual(len(self._captured), 0,
                'ignore preference must suppress the dialog entirely')
        finally:
            self.sandbox.unstore('risky')

    def test_dialog_lists_both_dats_and_storage(self):
        dat = self.sandbox.create(textDAT, 'safety_dat')
        dat.text = 'non-empty content'
        self.sandbox.store('my_storage_key', 'x')
        try:
            self.embody_ext._checkTDNContentSafety()
            self.assertEqual(len(self._captured), 1)
            msg = self._captured[0]['message']
            self.assertIn('DAT', msg)
            self.assertIn('storage', msg.lower())
            self.assertIn('my_storage_key', msg)
        finally:
            self.sandbox.unstore('my_storage_key')

    # ------------------------------------------------------------------
    # C. Skip path logs a SUCCESS summary
    # ------------------------------------------------------------------

    def test_skip_logs_success_summary(self):
        self.sandbox.store('soon_gone', 'value')
        try:
            self._scripted_choice = 2  # Skip Once
            log_count_before = self.embody_ext._log_counter
            self.embody_ext._checkTDNContentSafety()
            new_logs = [e for e in self.embody_ext._log_buffer
                        if e['id'] > log_count_before]
            # Look for a SUCCESS-level entry that names the key.
            success_entries = [e for e in new_logs
                               if e.get('level') == 'SUCCESS'
                               and 'soon_gone' in e.get('message', '')]
            self.assertTrue(success_entries,
                'Skip must log a SUCCESS summary naming dropped keys; '
                f'got: {[e.get("message", "") for e in new_logs]}')
        finally:
            self.sandbox.unstore('soon_gone')

    # ------------------------------------------------------------------
    # D. DAT type filter - skip TD-managed, keep user-authored
    # ------------------------------------------------------------------

    def _flatten_dats(self, result):
        """Flatten [(comp_path, [dat_ops])] into a set of DAT paths."""
        return {d.path for _, dats in result for d in dats}

    def test_TD_MANAGED_DAT_TYPES_membership(self):
        """The denylist must include the types that triggered the user's
        original noise (info, webrtc, folder, monitors, devices) AND must
        NOT include any callback DAT type -- callbacks hold user-authored
        Python and losing them silently is exactly what the warning
        exists to prevent."""
        types = self.embody_ext._TD_MANAGED_DAT_TYPES
        # Read-only TD-generated outputs that must be skipped
        for t in ('info', 'webrtc', 'folder', 'monitors',
                  'audiodevices', 'videodevices', 'serialdevices',
                  'mididevices'):
            self.assertIn(t, types,
                f'TD-managed DAT type {t!r} missing from skip set')
        # Callback DAT types that must NEVER be skipped
        for t in ('execute', 'parexec', 'pargroupexec', 'chopexec',
                  'datexec', 'opexec', 'panelexec'):
            self.assertNotIn(t, types,
                f'Callback DAT type {t!r} must NOT be in skip set -- '
                f'callbacks hold user-authored Python')
        # Common user-authored types that must never be skipped
        for t in ('text', 'table'):
            self.assertNotIn(t, types,
                f'User-authored type {t!r} must NOT be in skip set')

    def test_findAtRiskDATs_ignores_td_managed_folder_dat(self):
        """Functional end-to-end: a folderDAT with real rows (TD-managed
        content) must be excluded from the at-risk warning even though
        it has non-empty content. This is the user's exact scenario."""
        folder_dat = self.sandbox.create(folderDAT, 'mgr_folder')
        folder_dat.par.folder = project.folder
        folder_dat.cook(force=True)
        try:
            # Sanity: must have rows, otherwise the empty-content skip
            # would short-circuit before the type filter runs and the
            # test would pass for the wrong reason.
            self.assertGreater(folder_dat.numRows, 0,
                f'Test setup: folder DAT must have rows '
                f'(got {folder_dat.numRows})')
            flat = self._flatten_dats(self.embody_ext._findAtRiskDATs())
            self.assertNotIn(folder_dat.path, flat,
                'TD-managed folder DAT with content was incorrectly '
                'flagged as at-risk')
        finally:
            folder_dat.destroy()

    def test_findAtRiskDATs_keeps_callback_dats(self):
        """Callback DATs (executeDAT family) hold user-authored Python
        and MUST continue to surface in the at-risk warning -- losing a
        callback silently is a destructive footgun."""
        cb_dat = self.sandbox.create(chopexecuteDAT, 'safety_callback')
        cb_dat.text = (
            '# user-authored callback\n'
            'def onValueChange(channel, sampleIndex, val, prev):\n'
            '\tpass\n'
        )
        try:
            flat = self._flatten_dats(self.embody_ext._findAtRiskDATs())
            self.assertIn(cb_dat.path, flat,
                'chopexecuteDAT with user-authored callback content must '
                'surface in at-risk results -- callbacks are exactly what '
                'the warning exists to protect')
        finally:
            cb_dat.destroy()

    def test_findAtRiskDATs_flags_plain_text_dat(self):
        """Baseline: a user-authored textDAT with content must still be
        flagged. Confirms the type filter did not break the happy path."""
        text_dat = self.sandbox.create(textDAT, 'safety_text')
        text_dat.text = 'user-authored content that would be lost'
        try:
            flat = self._flatten_dats(self.embody_ext._findAtRiskDATs())
            self.assertIn(text_dat.path, flat,
                'Plain textDAT with content must still be flagged')
        finally:
            text_dat.destroy()
