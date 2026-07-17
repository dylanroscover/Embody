"""
Test suite: Editable keyboard shortcuts (issue #50).

Covers the shortcuts module DAT -- combo normalization/display, event-combo
building, dispatch-table construction, TouchShortcuts.txt reserved-list
parsing, conflict validation, and the recorder's arm / preview / commit /
cancel / expiry state machine -- plus the persistence whitelist and the
parexec handler logic.

The live Parameter Execute path fires on the NEXT frame after a par change,
so these tests invoke the handler functions synchronously (parexec.module
.onValueChange) instead of waiting on frames. Par values on the live Embody
COMP are snapshotted in setUp and restored in tearDown, so a failed
assertion can never leave custom bindings behind.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestShortcuts(EmbodyTestCase):

    @property
    def sc(self):
        # Resolve live on every access -- the module DAT can reinit on sync.
        return self.embody.op('shortcuts').module

    def _clearRecording(self):
        if self.embody.fetch(self.sc._REC_KEY, None, search=False) is not None:
            self.embody.unstore(self.sc._REC_KEY)

    def setUp(self):
        m = self.sc
        self._saved = {p: str(self.embody.par[p].eval()) for p in m.SHORTCUT_PARS}
        self._saved_tagger = str(self.embody.par.Shortcuttagger.eval())
        # Pin the parexec suppression gate OPEN: the test_parexec_* tests
        # call onValueChange directly, and its guard reads live shared state
        # (_restoring_settings on the ext, the _init_complete store) that
        # other suites legitimately toggle mid-run -- the polluter behind
        # these tests failing ONLY in full runs while passing 48/48 in
        # isolation (2026-07-16). Snapshot both and restore in tearDown.
        ext = self.embody.ext.Embody
        self._prev_restoring = getattr(ext, '_restoring_settings', False)
        self._prev_init_complete = self.embody.fetch(
            '_init_complete', None, search=False)
        # A setUp failure skips tearDown (TestRunnerExt), so any mutation
        # after the snapshot must restore on its own if it blows up.
        try:
            ext._restoring_settings = False
            self.embody.store('_init_complete', True)
            self._clearRecording()
            # Deterministic starting state for every test.
            m.resetDefaults(self.embody)
        except Exception:
            self.tearDown()
            raise

    def tearDown(self):
        for name, val in self._saved.items():
            self.embody.par[name].val = val
        self.embody.par.Shortcuttagger = self._saved_tagger
        self._clearRecording()
        # Restore the parexec gate exactly as found (never cache the ext
        # reference -- resolve live; a reinit may have replaced it).
        ext = self.embody.ext.Embody
        ext._restoring_settings = getattr(self, '_prev_restoring', False)
        prev_init = getattr(self, '_prev_init_complete', True)
        if prev_init is None:
            try:
                self.embody.unstore('_init_complete')
            except Exception:
                pass
        else:
            self.embody.store('_init_complete', prev_init)

    # -- Normalization ---------------------------------------------------

    def test_normalize_valid_forms(self):
        # ctrl and cmd are DISTINCT modifiers naming physical keys; typed
        # tokens are canonicalized but never swapped for each other.
        m = self.sc
        self.assertEqual(m.normalize('Cmd + Shift + O'), 'cmd+shift+o')
        self.assertEqual(m.normalize('CTRL-ALT-E'), 'ctrl+alt+e')
        self.assertEqual(m.normalize('shift+ctrl+x'), 'ctrl+shift+x')
        self.assertEqual(m.normalize('option+e'), 'alt+e')
        self.assertEqual(m.normalize('lctrl.lshift.o'), 'ctrl+shift+o')
        self.assertEqual(m.normalize('lcmd+o'), 'cmd+o')
        self.assertEqual(m.normalize('f5'), 'F5')
        self.assertEqual(m.normalize('ctrl+F5'), 'ctrl+F5')
        self.assertEqual(m.normalize('space'), 'space')
        self.assertEqual(m.normalize('cmd+alt+pageup'), 'cmd+alt+pageup')
        # Both physical modifiers may combine in one combo
        self.assertEqual(m.normalize('shift+cmd+ctrl+x'), 'ctrl+cmd+shift+x')
        # The spellings are DIFFERENT bindings
        self.assertNotEqual(m.normalize('cmd+shift+o'), m.normalize('ctrl+shift+o'))

    def test_normalize_empty_means_disabled(self):
        m = self.sc
        self.assertEqual(m.normalize(''), '')
        self.assertEqual(m.normalize(None), '')
        self.assertEqual(m.normalize('   '), '')

    def test_normalize_invalid_forms(self):
        m = self.sc
        self.assertIsNone(m.normalize('ctrl+shift'))      # modifier-only
        self.assertIsNone(m.normalize('cmd'))             # modifier-only
        self.assertIsNone(m.normalize('ctrl+a+b'))        # two triggers
        self.assertIsNone(m.normalize('esc'))             # esc is cancel-only
        self.assertIsNone(m.normalize('ctrl+esc'))
        self.assertIsNone(m.normalize('ctrl+notakey'))    # unknown named key
        self.assertIsNone(m.normalize('f13'))             # out of F-key range

    def test_normalize_idempotent(self):
        m = self.sc
        for raw in ('Cmd + Shift + O', 'ALT+F5', 'shift+ctrl+x', 'space'):
            once = m.normalize(raw)
            self.assertEqual(m.normalize(once), once)

    # -- Event combos and display ----------------------------------------

    def test_combo_from_event(self):
        # Physical Ctrl and physical Cmd are reported as themselves.
        m = self.sc
        self.assertEqual(m.comboFromEvent('o', True, False, True, False),
                         'ctrl+shift+o')
        self.assertEqual(m.comboFromEvent('o', False, False, True, True),
                         'cmd+shift+o')
        self.assertEqual(m.comboFromEvent('F5', False, False, False, False),
                         'F5')
        self.assertEqual(m.comboFromEvent('k', True, True, False, False),
                         'ctrl+alt+k')
        # Both held at once is a valid (distinct) combo
        self.assertEqual(m.comboFromEvent('k', True, False, False, True),
                         'ctrl+cmd+k')

    def test_matchform_platform_behavior(self):
        # matchForm is the behavior space: exact on macOS (Ctrl and Cmd are
        # distinct keys), cmd->ctrl folded on PC (no Cmd key exists there).
        m = self.sc
        self.assertEqual(m.matchForm(''), '')
        self.assertEqual(m._ctrlFold('cmd+shift+o'), 'ctrl+shift+o')
        self.assertEqual(m._ctrlFold('ctrl+cmd+x'), 'ctrl+x')
        if m._isMac():
            self.assertEqual(m.matchForm('cmd+shift+o'), 'cmd+shift+o')
            self.assertEqual(m.matchForm('ctrl+shift+o'), 'ctrl+shift+o')
            # Distinct bindings dispatch on their own physical key only
            self.embody.par.Shortcutmanager.val = 'ctrl+shift+o'
            self.assertEqual(
                m.actionForEvent(self.embody, 'o', True, False, True, False),
                'Shortcutmanager')
            self.assertIsNone(
                m.actionForEvent(self.embody, 'o', False, False, True, True))
            self.embody.par.Shortcutmanager.val = 'cmd+shift+o'
            self.assertEqual(
                m.actionForEvent(self.embody, 'o', False, False, True, True),
                'Shortcutmanager')
            self.assertIsNone(
                m.actionForEvent(self.embody, 'o', True, False, True, False))
        else:
            # PC: Mac-authored cmd bindings fold to ctrl and still fire
            self.assertEqual(m.matchForm('cmd+shift+o'), 'ctrl+shift+o')
            self.embody.par.Shortcutmanager.val = 'cmd+shift+o'
            self.assertEqual(
                m.actionForEvent(self.embody, 'o', True, False, True, False),
                'Shortcutmanager')

    def test_recorded_and_typed_share_canonical_space(self):
        # A recorded event and a typed string for the same physical keys
        # MUST produce identical combo strings, or the recorded binding
        # would never match in buildDispatch (and parexec normalization
        # would rewrite/revert what the recorder just committed).
        m = self.sc
        # F-key casing, whichever case the Keyboard In DAT delivers
        self.assertEqual(m.comboFromEvent('f5', True, False, False, False),
                         m.normalize('ctrl+F5'))
        self.assertEqual(m.comboFromEvent('F5', True, False, False, False),
                         m.normalize('ctrl+f5'))
        # Named keys must be typeable AND survive normalization untouched
        for named in ('pageup', 'printscreen', 'space', 'period'):
            recorded = m.comboFromEvent(named, True, False, False, False)
            self.assertEqual(m.normalize(recorded), recorded)

    def test_display_behavior_form(self):
        # display() renders the combo as it BEHAVES here: verbatim tokens on
        # macOS (Ctrl and Cmd are distinct), cmd folded to Ctrl on PC.
        m = self.sc
        self.assertEqual(m.display('ctrl+shift+o'), 'Ctrl+Shift+O')
        if m._isMac():
            self.assertEqual(m.display('cmd+shift+o'), 'Cmd+Shift+O')
            self.assertEqual(m.display('ctrl+cmd+k'), 'Ctrl+Cmd+K')
        else:
            self.assertEqual(m.display('cmd+shift+o'), 'Ctrl+Shift+O')
        self.assertEqual(m.display('ctrl+F5'), 'Ctrl+F5')
        self.assertEqual(m.display('alt+shift+k'), 'Alt+Shift+K')
        self.assertEqual(m.display('F5'), 'F5')
        self.assertEqual(m.display(''), 'unassigned')

    def test_display_roundtrip_retypeable(self):
        # Invariant: typing back what the UI displays reproduces the same
        # BEHAVIOR (matchForm space) on this platform.
        m = self.sc
        for stored in ('ctrl+shift+o', 'cmd+shift+o', 'ctrl+alt+e', 'cmd+F5',
                       'alt+space', 'ctrl+cmd+x', 'F12'):
            self.assertEqual(m.normalize(m.display(stored)),
                             m.matchForm(stored))

    # -- Dispatch table ----------------------------------------------------

    def test_dispatch_defaults(self):
        m = self.sc
        table = m.buildDispatch(self.embody)
        expected = {m.matchForm(default): par
                    for par, _label, default in m.ACTIONS}
        self.assertEqual(table, expected)

    def test_dispatch_skips_empty(self):
        m = self.sc
        self.embody.par.Shortcutmanager.val = ''
        table = m.buildDispatch(self.embody)
        self.assertNotIn(m.matchForm(m.DEFAULTS['Shortcutmanager']), table)
        self.assertNotIn('Shortcutmanager', table.values())

    def test_dispatch_first_wins_on_duplicate(self):
        # Duplicates are blocked at edit time, but a stale config can still
        # carry one -- buildDispatch keeps first-in-ACTIONS as a safety.
        m = self.sc
        default = m.DEFAULTS['Shortcutmanager']
        self.embody.par.Shortcutcopytdn.val = default  # raw .val: no parexec
        table = m.buildDispatch(self.embody)
        self.assertEqual(table[m.matchForm(default)], 'Shortcutmanager')

    def test_action_names_wired_in_callbacks(self):
        # Every dispatchable par name must appear in the keyboardin
        # callbacks' _runAction chain, or a binding would silently no-op.
        text = self.embody.op('keyboardin_callbacks').text
        for par_name in self.sc.SHORTCUT_PARS:
            self.assertIn(f"'{par_name}'", text)

    def test_onkey_routing_end_to_end(self):
        # Drive the REAL onKey with synthetic events, intercepting
        # _runAction on the callbacks module so no live action fires.
        kb = self.embody.op('keyboardin_callbacks').module
        fired = []
        orig = kb._runAction
        kb._runAction = lambda par_name: fired.append(par_name)
        try:
            def key(k, ctrl=False, alt=False, shift=False, cmd=False,
                    state=True):
                kb.onKey(None, k, '', alt, False, False, ctrl, ctrl, False,
                         shift, shift, False, state, 0, cmd, cmd, False)

            mac = self.sc._isMac()
            def primary(k, shift=False, state=True):
                key(k, ctrl=not mac, cmd=mac, shift=shift, state=state)

            # Default manager binding dispatches on the platform primary
            primary('o', shift=True)
            if mac:
                # The OTHER physical modifier is a different binding on mac
                key('o', ctrl=True, shift=True)
            # Keyup, modifier-only, unbound, and wrong-modifier events do not
            primary('o', shift=True, state=False)
            key('lctrl', ctrl=True)
            primary('q', shift=True)
            primary('o')
            self.assertEqual(fired, ['Shortcutmanager'])

            # A disabled binding stops dispatching
            fired.clear()
            self.embody.par.Shortcutmanager.val = ''
            primary('o', shift=True)
            self.assertEqual(fired, [])

            # While a recording is armed, keys are consumed, never dispatched;
            # a combo bound to ANOTHER action is refused (recorder stays
            # armed), and a free combo commits.
            fired.clear()
            self.sc.resetDefaults(self.embody)
            self.embody.store('_smoke_test_responses', {'Embody': 0})
            self.sc.arm(self.embody, 'Shortcutrefresh')
            primary('u', shift=True)  # Update All's combo -> refused + alert
            self.assertEqual(fired, [])
            self.assertIsNotNone(self.sc.recordingActive(self.embody))
            key('F9', ctrl=True, alt=True)  # free -> commits
            self.assertEqual(fired, [])
            self.assertEqual(str(self.embody.par.Shortcutrefresh.eval()),
                             'ctrl+alt+F9')
            self.assertIsNone(self.sc.recordingActive(self.embody))
        finally:
            kb._runAction = orig
            if self.embody.fetch('_smoke_test_responses', None,
                                 search=False) is not None:
                self.embody.unstore('_smoke_test_responses')

    # -- Reserved TD combos ------------------------------------------------

    def test_reserved_td_combos(self):
        import os
        m = self.sc
        res = m.reservedTdCombos()
        # TD factory built-ins that must be caught (app rows have an empty
        # command column -- the parser must still count them). A user
        # override file can legitimately remap/disable these, so only
        # assert them on a machine without one.
        if not os.path.exists(app.preferencesFolder + '/TouchShortcuts.txt'):
            for combo in ('ctrl+s', 'ctrl+shift+s', 'ctrl+z', 'ctrl+shift+z'):
                self.assertIn(combo, res)
        # Embody's own defaults must NOT be TD built-ins.
        for _par, _label, default in m.ACTIONS:
            self.assertNotIn(default, res)

    def test_combo_from_td_key(self):
        m = self.sc
        self.assertEqual(m._comboFromTdKey('ctrl.shift.s'), 'ctrl+shift+s')
        self.assertEqual(m._comboFromTdKey('F1'), 'F1')
        self.assertEqual(m._comboFromTdKey('.'), '.')
        self.assertIsNone(m._comboFromTdKey('000'))
        self.assertIsNone(m._comboFromTdKey(''))

    # -- Validation ----------------------------------------------------------

    def test_duplicate_of(self):
        m = self.sc
        default = m.DEFAULTS['Shortcutmanager']
        self.assertEqual(m.duplicateOf(self.embody, 'Shortcutcopytdn', default),
                         'Shortcutmanager')
        # A par never duplicates itself
        self.assertIsNone(m.duplicateOf(self.embody, 'Shortcutmanager', default))
        self.assertIsNone(m.duplicateOf(self.embody, 'Shortcutmanager', ''))
        # validate() no longer reports duplicates (they are blocked instead)
        self.assertEqual(
            m.validate(self.embody, 'Shortcutcopytdn', 'ctrl+alt+F9'), [])
        if m._isMac():
            # The other physical modifier is a DIFFERENT binding on macOS
            other = default.replace('cmd', 'ctrl')
            self.assertIsNone(
                m.duplicateOf(self.embody, 'Shortcutcopytdn', other))

    def test_validate_reserved_warns(self):
        m = self.sc
        warnings = m.validate(self.embody, 'Shortcutmanager', 'ctrl+s')
        self.assertTrue(any('built-in' in w for w in warnings))
        # TD's table is written ctrl-form but means Cmd on macOS -- a cmd
        # binding must warn too
        warnings = m.validate(self.embody, 'Shortcutmanager', 'cmd+s')
        self.assertTrue(any('built-in' in w for w in warnings))

    def test_validate_clean_combo(self):
        m = self.sc
        self.assertEqual(m.validate(self.embody, 'Shortcutmanager',
                                    'ctrl+alt+F9'), [])

    def test_validate_empty_no_warnings(self):
        self.assertEqual(self.sc.validate(self.embody, 'Shortcutmanager', ''), [])

    # -- Recorder state machine ---------------------------------------------

    def test_arm_and_active(self):
        m = self.sc
        m.arm(self.embody, 'Shortcutrefresh')
        rec = m.recordingActive(self.embody)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['target'], 'Shortcutrefresh')

    def test_arm_unknown_par_ignored(self):
        m = self.sc
        m.arm(self.embody, 'Notapar')
        self.assertIsNone(m.recordingActive(self.embody))

    def test_arm_refused_when_shortcuts_disabled(self):
        # With the master toggle off the Keyboard In DAT is inactive, so an
        # armed recorder could never receive a key -- arm() must refuse.
        m = self.sc
        prior = bool(self.embody.par.Enablekeyboardshortcuts.eval())
        try:
            self.embody.par.Enablekeyboardshortcuts.val = False
            m.arm(self.embody, 'Shortcutrefresh')
            self.assertIsNone(m.recordingActive(self.embody))
        finally:
            self.embody.par.Enablekeyboardshortcuts.val = prior

    def test_expired_recording_is_inactive(self):
        m = self.sc
        m.arm(self.embody, 'Shortcutrefresh')
        rec = self.embody.fetch(m._REC_KEY, None, search=False)
        rec['deadline'] = 0  # force expiry
        self.embody.store(m._REC_KEY, rec)
        self.assertIsNone(m.recordingActive(self.embody))
        # recordingActive clears the stale entry
        self.assertIsNone(self.embody.fetch(m._REC_KEY, None, search=False))

    def test_esc_cancels(self):
        m = self.sc
        m.arm(self.embody, 'Shortcutrefresh')
        before = str(self.embody.par.Shortcutrefresh.eval())
        consumed = m.handleRecordingKey(self.embody, 'esc',
                                        False, False, False, False, True)
        self.assertTrue(consumed)
        self.assertIsNone(m.recordingActive(self.embody))
        self.assertEqual(str(self.embody.par.Shortcutrefresh.eval()), before)

    def test_modifier_preview_keeps_recording(self):
        m = self.sc
        m.arm(self.embody, 'Shortcutrefresh')
        consumed = m.handleRecordingKey(self.embody, 'lctrl',
                                        True, False, False, False, True)
        self.assertTrue(consumed)
        self.assertIsNotNone(m.recordingActive(self.embody))

    def test_keyup_consumed_while_armed(self):
        m = self.sc
        m.arm(self.embody, 'Shortcutrefresh')
        consumed = m.handleRecordingKey(self.embody, 'k',
                                        True, True, False, False, False)
        self.assertTrue(consumed)
        self.assertIsNotNone(m.recordingActive(self.embody))

    def test_commit_on_nonmodifier_keydown(self):
        m = self.sc
        m.arm(self.embody, 'Shortcutrefresh')
        consumed = m.handleRecordingKey(self.embody, 'k',
                                        True, True, False, False, True)
        self.assertTrue(consumed)
        self.assertEqual(str(self.embody.par.Shortcutrefresh.eval()),
                         'ctrl+alt+k')
        self.assertIsNone(m.recordingActive(self.embody))

    def test_commit_refuses_unbindable_key(self):
        # '-' is a grammar separator: committing it would announce success
        # and then silently revert on the next-frame normalization pass.
        # The recorder must refuse it and STAY armed for another attempt.
        m = self.sc
        m.arm(self.embody, 'Shortcutrefresh')
        before = str(self.embody.par.Shortcutrefresh.eval())
        consumed = m.handleRecordingKey(self.embody, '-',
                                        True, False, False, False, True)
        self.assertTrue(consumed)
        self.assertIsNotNone(m.recordingActive(self.embody))
        self.assertEqual(str(self.embody.par.Shortcutrefresh.eval()), before)
        # A bindable key afterwards still commits normally
        m.handleRecordingKey(self.embody, 'k', True, False, False, False, True)
        self.assertEqual(str(self.embody.par.Shortcutrefresh.eval()), 'ctrl+k')

    def test_commit_refuses_duplicate(self):
        # Pressing a combo already assigned to another action must refuse,
        # ALERT via the (auto-respondable) message box, and re-arm with a
        # fresh deadline -- one combo drives exactly one action.
        m = self.sc
        self.embody.store('_smoke_test_responses', {'Embody': 0})
        try:
            m.arm(self.embody, 'Shortcutrefresh')
            gen_before = self.embody.fetch(m._REC_KEY, None, search=False)['gen']
            before = str(self.embody.par.Shortcutrefresh.eval())
            # Press the Update All default (shift + platform primary mod + u)
            consumed = m.handleRecordingKey(self.embody, 'u',
                                            not m._isMac(), False, True,
                                            m._isMac(), True)
            self.assertTrue(consumed)
            rec = self.embody.fetch(m._REC_KEY, None, search=False)
            self.assertIsNotNone(m.recordingActive(self.embody))
            self.assertGreater(rec['gen'], gen_before)  # re-armed fresh
            self.assertEqual(str(self.embody.par.Shortcutrefresh.eval()),
                             before)
            # The alert actually fired: the seeded response was consumed
            self.assertIsNone(self.embody.fetch('_smoke_test_responses',
                                                None, search=False))
            # A free combo afterwards commits normally
            m.handleRecordingKey(self.embody, 'F9', True, True, False,
                                 False, True)
            self.assertEqual(str(self.embody.par.Shortcutrefresh.eval()),
                             'ctrl+alt+F9')
            self.assertIsNone(m.recordingActive(self.embody))
        finally:
            if self.embody.fetch('_smoke_test_responses', None,
                                 search=False) is not None:
                self.embody.unstore('_smoke_test_responses')

    def test_inactive_recorder_passes_through(self):
        m = self.sc
        consumed = m.handleRecordingKey(self.embody, 'k',
                                        True, False, False, False, True)
        self.assertFalse(consumed)

    def test_stale_expire_timer_cannot_kill_next_recording(self):
        # Back-to-back recordings: the first pulse's pending _expire timer
        # (fires ~10.5s later) must not disarm the SECOND recording. Guarded
        # by the monotonic generation counter that survives unstore.
        m = self.sc
        m.arm(self.embody, 'Shortcutmanager')
        first_gen = self.embody.fetch(m._REC_KEY, None, search=False)['gen']
        m.handleRecordingKey(self.embody, 'k', True, False, False, False, True)
        m.arm(self.embody, 'Shortcutrefresh')
        second = self.embody.fetch(m._REC_KEY, None, search=False)
        self.assertGreater(second['gen'], first_gen)
        m._expire(self.embody.path, first_gen)  # the stale timer firing
        self.assertIsNotNone(m.recordingActive(self.embody))
        self.assertEqual(m.recordingActive(self.embody)['target'],
                         'Shortcutrefresh')

    # -- parexec handler (invoked synchronously) ------------------------------

    def test_parexec_normalizes_typed_input(self):
        pe = self.embody.op('parexec').module
        par = self.embody.par.Shortcutmanager
        par.val = 'CMD + SHIFT + O'
        pe.onValueChange(par, 'cmd+shift+o')
        self.assertEqual(str(par.eval()), 'cmd+shift+o')

    def test_parexec_reverts_invalid_input(self):
        pe = self.embody.op('parexec').module
        par = self.embody.par.Shortcutmanager
        par.val = 'total garbage here'
        pe.onValueChange(par, 'ctrl+shift+o')
        # Reverts to normalize(prev) verbatim
        self.assertEqual(str(par.eval()), 'ctrl+shift+o')

    def test_parexec_invalid_prev_falls_back_to_default(self):
        # If prev is ALSO garbage (corrupted config restored while the
        # handler was suppressed), reverting to it would ping-pong forever.
        # The revert target must be the factory default instead.
        m = self.sc
        pe = self.embody.op('parexec').module
        par = self.embody.par.Shortcutmanager
        par.val = 'new garbage'
        pe.onValueChange(par, 'old garbage')
        self.assertEqual(str(par.eval()),
                         m.normalize(m.DEFAULTS['Shortcutmanager']))

    def test_parexec_rejects_duplicate(self):
        # Typing a combo held by another action reverts to the previous
        # value with a warning -- duplicates are blocked, not just warned.
        m = self.sc
        pe = self.embody.op('parexec').module
        par = self.embody.par.Shortcutcopytdn
        own_default = m.DEFAULTS['Shortcutcopytdn']
        par.val = m.DEFAULTS['Shortcutupdateall']  # already held
        pe.onValueChange(par, own_default)
        self.assertEqual(str(par.eval()), own_default)

    def test_parexec_record_pulse_arms(self):
        m = self.sc
        pe = self.embody.op('parexec').module
        pe.onPulse(self.embody.par.Recordmanager)
        rec = m.recordingActive(self.embody)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['target'], 'Shortcutmanager')

    # -- Persistence, pars, defaults ------------------------------------------

    def test_persisted_params_whitelist(self):
        m = self.sc
        persisted = self.embody_ext._PERSISTED_PARAMS
        for name in m.SHORTCUT_PARS + ('Shortcuttagger', 'Enablekeyboardshortcuts'):
            self.assertIn(name, persisted)

    def test_all_pars_exist(self):
        m = self.sc
        for name in m.SHORTCUT_PARS:
            self.assertIsNotNone(getattr(self.embody.par, name))
        for name in m.RECORD_PARS:
            self.assertIsNotNone(getattr(self.embody.par, name))
        self.assertIsNotNone(self.embody.par.Shortcuttagger)
        self.assertIsNotNone(self.embody.par.Resetshortcuts)
        self.assertIsNotNone(self.embody.par.Enablekeyboardshortcuts)

    def test_record_pars_mapping(self):
        m = self.sc
        self.assertEqual(m.RECORD_PARS['Recordmanager'], 'Shortcutmanager')
        self.assertEqual(set(m.RECORD_PARS.values()), set(m.SHORTCUT_PARS))

    def test_tagger_menu_matches_module(self):
        # Menu entries are PHYSICAL keys served live via menuSource: macOS
        # offers Ctrl AND Cmd (distinct keys there); PC offers Ctrl only
        # (no Cmd key exists). Same saved .toe, right choices per platform.
        import sys
        m = self.sc
        mp = self.embody.par.Shortcuttagger
        self.assertEqual(tuple(mp.menuNames), m.TAGGER_MENU_NAMES)
        self.assertEqual(tuple(mp.menuLabels), m.TAGGER_MENU_LABELS)
        self.assertIn('taggerMenu', str(mp.menuSource))
        joined = ' '.join(m.TAGGER_MENU_LABELS)
        self.assertIn('Ctrl', joined)  # Ctrl is offered on EVERY platform
        if sys.platform == 'darwin':
            self.assertIn('lcmd', m.TAGGER_MENU_NAMES)
            self.assertIn('Cmd', joined)
            # Apple keyboards have no right Ctrl key -- never offer it
            self.assertNotIn('rctrl', m.TAGGER_MENU_NAMES)
        else:
            self.assertNotIn('lcmd', m.TAGGER_MENU_NAMES)
            self.assertNotIn('Cmd', joined)
            self.assertIn('rctrl', m.TAGGER_MENU_NAMES)

    def test_tagger_key_fold(self):
        # Ctrl and Cmd are separate choices on macOS (exact match); keys a
        # platform's keyboards lack fold to their closest existing key:
        # Cmd->Ctrl on PC, right-Ctrl->left-Ctrl on Mac.
        m = self.sc
        self.assertTrue(m.taggerKeyMatches('lctrl', 'lctrl'))
        self.assertTrue(m.taggerKeyMatches('lalt', 'lalt'))
        self.assertFalse(m.taggerKeyMatches('lctrl', 'lalt'))
        self.assertFalse(m.taggerKeyMatches('lalt', 'lcmd'))
        self.assertFalse(m.taggerKeyMatches('off', 'lctrl'))
        if m._isMac():
            # Ctrl vs Cmd: distinct physical keys -- no cross-matching
            self.assertFalse(m.taggerKeyMatches('lctrl', 'lcmd'))
            self.assertTrue(m.taggerKeyMatches('lcmd', 'lcmd'))
            self.assertFalse(m.taggerKeyMatches('lcmd', 'lctrl'))
            self.assertEqual(m.taggerDisplayKey('lcmd'), 'lcmd')
            # No right Ctrl on Apple keyboards: PC-authored 'rctrl' acts as
            # left Ctrl (and a right-Ctrl press from an extended keyboard
            # folds the same way)
            self.assertTrue(m.taggerKeyMatches('rctrl', 'lctrl'))
            self.assertTrue(m.taggerKeyMatches('lctrl', 'rctrl'))
            self.assertEqual(m.taggerDisplayKey('rctrl'), 'lctrl')
        else:
            # Both Ctrls exist on PC -- left/right stay distinct
            self.assertFalse(m.taggerKeyMatches('lctrl', 'rctrl'))
            # Graceful degradation for Mac-authored Cmd values
            self.assertTrue(m.taggerKeyMatches('lcmd', 'lctrl'))
            self.assertTrue(m.taggerKeyMatches('rcmd', 'rctrl'))
            self.assertFalse(m.taggerKeyMatches('lcmd', 'rctrl'))
            self.assertEqual(m.taggerDisplayKey('lcmd'), 'lctrl')
            self.assertEqual(m.taggerDisplayKey('rctrl'), 'rctrl')
        self.assertEqual(m.taggerDisplayKey('lctrl'), 'lctrl')

    def test_onkey_tagger_tap_starts_timer(self):
        # A single tap of the STORED physical key must start the double-tap
        # timer and dispatch NO action; a different modifier must not.
        kb = self.embody.op('keyboardin_callbacks').module
        timer = self.embody.op('timer1')
        fired = []
        orig = kb._runAction
        kb._runAction = lambda par_name: fired.append(par_name)
        prior_active = timer.par.active.eval()

        def tap(key, ctrl=False, cmd=False):
            kb.onKey(None, key, '', False, False, False, ctrl, ctrl, False,
                     False, False, False, True, 0, cmd, cmd, False)

        try:
            # timer1's 'active' is a MENU par (eval() returns a string, so
            # truthiness is useless) -- assert via menuIndex: the tagger
            # code sets it to 1 to arm, 0 disarms.
            timer.par.active.menuIndex = 0
            if self.sc._isMac():
                # Cmd tap must NOT trigger a stored-lctrl tagger on macOS
                tap('lcmd', cmd=True)
                self.assertEqual(timer.par.active.menuIndex, 0)
            tap('lctrl', ctrl=True)
            self.assertEqual(timer.par.active.menuIndex, 1)
            self.assertEqual(fired, [])
        finally:
            kb._runAction = orig
            timer.par.active = prior_active

    def test_reset_defaults(self):
        m = self.sc
        self.embody.par.Shortcutmanager.val = 'ctrl+alt+F9'
        self.embody.par.Shortcuttagger = 'off'
        m.resetDefaults(self.embody)
        for par_name, _label, default in m.ACTIONS:
            self.assertEqual(str(self.embody.par[par_name].eval()),
                             m.normalize(default))
        self.assertEqual(str(self.embody.par.Shortcuttagger.eval()),
                         m.TAGGER_DEFAULT)

    # -- Help block --------------------------------------------------------

    def test_help_block_lists_bindings(self):
        m = self.sc
        block = m.helpBlock(self.embody)
        # Combos render in behavior form for this platform
        self.assertIn(m.display(m.DEFAULTS['Shortcutupdateall']), block)
        # The tagger line renders the platform-idiomatic key name
        # ('lcmd-lcmd' on macOS for the stored 'lctrl')
        d = m.taggerDisplayKey('lctrl')
        self.assertIn(f'{d}-{d}', block)
        self.assertTrue(max(len(l) for l in block.splitlines()) <= 70)

    def test_help_block_empty_state(self):
        m = self.sc
        for par_name in m.SHORTCUT_PARS:
            self.embody.par[par_name].val = ''
        self.embody.par.Shortcuttagger = 'off'
        self.assertEqual(m.helpBlock(self.embody),
                         '(no keyboard shortcuts assigned)')

    # -- Render-surface guards ------------------------------------------------

    def test_help_template_carries_tokens(self):
        # If a token is dropped or typo'd in text_help.py, the help panel
        # silently loses its live-binding render -- guard the contract
        # between the template and the parexec Help handler.
        text = self.embody.op('help/text_help').text
        self.assertIn('{{SHORTCUTS}}', text)
        self.assertIn('{{TAGGERTAP}}', text)
        self.assertIn('{{SC:Shortcutupdateall}}', text)
        # No stale hardcoded combos left in the template prose
        self.assertNotIn('ctrl-shift-u', text)
        self.assertNotIn('lctrl-lctrl', text)

    def test_toolbar_tooltip_tokens_resolve(self):
        # Every [Shortcutxxx] token in the toolbar config must name a real
        # par, and the resolver must render the live binding.
        import re
        m = self.sc
        config_text = self.embody.op('toolbar/toolbar_config').text
        tokens = re.findall(r'\[(Shortcut[a-z]+)\]', config_text)
        self.assertTrue(tokens)
        for t in tokens:
            self.assertIn(t, m.SHORTCUT_PARS)
        tb = self.embody.op('toolbar')
        rendered = tb.ext.ToolbarExt._resolveShortcutTokens(
            'x [Shortcutupdateall]')
        self.assertEqual(rendered,
                         f"x ({m.display(m.DEFAULTS['Shortcutupdateall'])})")
