"""
Test suite: TDN content safety guards — DAT + storage loss detection,
combined dialog, and removal of the single-click "Never Ask" footgun.

Covers:
  A. _findAtRiskStorage detects user storage keys on TDN COMPs
  B. Control keys and runtime/skip keys are NOT flagged as at-risk
  C. Combined dialog surfaces both DATs and storage
  D. Dialog no longer offers "Never Ask" as a button
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
            # Default: return 1 (index for "Skip").
            return self._scripted_choice

        self._scripted_choice = 1
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
    # B. Dialog — no "Never Ask" button, combined DAT + storage surface
    # ------------------------------------------------------------------

    def test_prompt_offers_no_never_ask_button(self):
        self.sandbox.store('risky', 'data')
        try:
            self.embody_ext._checkTDNContentSafety()
            self.assertEqual(len(self._captured), 1,
                'Expected exactly one dialog for at-risk content')
            buttons = self._captured[0]['buttons']
            self.assertNotIn('Never Ask', buttons,
                '"Never Ask" button must be removed — it is a silent '
                'single-click footgun')
            # Skip is still offered as an escape.
            self.assertIn('Skip', buttons)
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
            self._scripted_choice = 1  # Skip
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
