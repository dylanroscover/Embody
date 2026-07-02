"""
Tests for the Dropped .tox Expression handler and its Toxdropexpr preference.

When a .tox is dragged into a network, TouchDesigner auto-writes a default
expression into the COMP's External .tox parameter
(me.parent().fileFolder + '/' + ...). EmbodyExt._checkExternalToxPar detects
these and routes user COMPs through _resolveToxdropExternals, which honors the
Toxdropexpr menu (clean / ignore / ask).

These tests exercise the isolated _resolveToxdropExternals(list) so they act
ONLY on their own sandbox COMPs -- never a project-wide scan/reset -- plus the
self-healed param, the always-clean of Embody's own descendants, and the
dialog-body truncation that keeps the buttons reachable.
"""


class TestToxdropExpr(EmbodyTestCase):

    # The exact substring the detector keys on must be present.
    DEPRECATED_EXPR = "me.parent().fileFolder + '/' + 'x.tox'"
    DIALOG_TITLE = 'Dropped .tox Expression Detected'

    def setUp(self):
        self._internal = None
        self._saved_pref = self.embody.par.Toxdropexpr.eval()
        self._saved_resp = self.embody.fetch(
            '_smoke_test_responses', None, search=False)

    def tearDown(self):
        # Sandbox children are auto-destroyed by the base tearDown; the internal
        # COMP lives under Embody, so drop it explicitly.
        try:
            if self._internal:
                self._internal.destroy()
        except Exception:
            pass
        self.embody.par.Toxdropexpr = self._saved_pref
        if self._saved_resp is not None:
            self.embody.store('_smoke_test_responses', self._saved_resp)
        else:
            self.embody.unstore('_smoke_test_responses')
        # Base tearDown clears the sandbox.
        for child in list(self.sandbox.children):
            try:
                child.destroy()
            except Exception:
                pass

    # ----- helpers ---------------------------------------------------------

    def _make(self, name='dropped1'):
        comp = self.sandbox.create(baseCOMP, name)
        comp.par.externaltox.expr = self.DEPRECATED_EXPR
        return comp

    def _seed(self, button_index):
        self.embody.store(
            '_smoke_test_responses', {self.DIALOG_TITLE: button_index})

    def _dropped(self, comp):
        return "me.parent().fileFolder + '/' +" in (comp.par.externaltox.expr or '')

    # ----- param self-heal -------------------------------------------------

    def test_param_is_menu_with_expected_entries(self):
        """The Toxdropexpr menu (authored on the Embody page) has the expected
        ask/clean/ignore entries and defaults to ask."""
        p = getattr(self.embody.par, 'Toxdropexpr', None)
        self.assertIsNotNone(p, 'Toxdropexpr param must exist on the Embody COMP')
        self.assertEqual(p.style, 'Menu')
        self.assertEqual(list(p.menuNames), ['ask', 'clean', 'ignore'])
        self.assertEqual(p.default, 'ask')

    # ----- ask-path dialog branches ---------------------------------------

    def test_ask_clean_resets_without_persisting(self):
        """Clean (button 0) clears the expr but leaves the preference at ask."""
        self.embody.par.Toxdropexpr = 'ask'
        comp = self._make()
        self._seed(0)
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertFalse(self._dropped(comp), 'Clean must clear the expression')
        self.assertEqual(self.embody.par.Toxdropexpr.eval(), 'ask',
                         'one-time Clean must not change the preference')

    def test_ask_ignore_leaves_without_persisting(self):
        """Ignore (button 1) leaves the expr and the preference at ask."""
        self.embody.par.Toxdropexpr = 'ask'
        comp = self._make()
        self._seed(1)
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertTrue(self._dropped(comp), 'Ignore must leave the expression')
        self.assertEqual(self.embody.par.Toxdropexpr.eval(), 'ask')

    def test_always_clean_resets_and_persists(self):
        """Always Clean (button 2) clears now and persists preference=clean."""
        self.embody.par.Toxdropexpr = 'ask'
        comp = self._make()
        self._seed(2)
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertFalse(self._dropped(comp))
        self.assertEqual(self.embody.par.Toxdropexpr.eval(), 'clean',
                         'Always Clean must persist preference=clean')

    def test_always_ignore_leaves_and_persists(self):
        """Always Ignore (button 3) leaves now and persists preference=ignore."""
        self.embody.par.Toxdropexpr = 'ask'
        comp = self._make()
        self._seed(3)
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertTrue(self._dropped(comp))
        self.assertEqual(self.embody.par.Toxdropexpr.eval(), 'ignore',
                         'Always Ignore must persist preference=ignore')

    def test_dialog_dismissed_is_noop(self):
        """A suppressed/closed dialog (_messageBox -> -1) changes nothing."""
        self.embody.par.Toxdropexpr = 'ask'
        comp = self._make()
        # No seeded response + a live runner -> _messageBox returns -1.
        self.embody.unstore('_smoke_test_responses')
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertTrue(self._dropped(comp),
                        'a dismissed/suppressed dialog must not clear anything')
        self.assertEqual(self.embody.par.Toxdropexpr.eval(), 'ask')

    # ----- silent (non-ask) preferences -----------------------------------

    def test_preference_clean_is_silent(self):
        """preference=clean resets with no dialog consulted (no seed present)."""
        self.embody.par.Toxdropexpr = 'clean'
        comp = self._make()
        self.embody.unstore('_smoke_test_responses')
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertFalse(self._dropped(comp),
                         "'clean' must reset silently")

    def test_preference_ignore_is_silent(self):
        """preference=ignore leaves the expr with no dialog consulted."""
        self.embody.par.Toxdropexpr = 'ignore'
        comp = self._make()
        self.embody.unstore('_smoke_test_responses')
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertTrue(self._dropped(comp),
                        "'ignore' must leave the expression silently")

    # ----- Embody's own descendants are always cleaned --------------------

    def test_internal_descendant_always_cleaned(self):
        """A COMP INSIDE Embody is cleaned by _checkExternalToxPar regardless of
        preference. preference=ignore keeps the blast radius to internal only --
        no user (external) COMP anywhere in the project is touched."""
        self.embody.par.Toxdropexpr = 'ignore'
        self._internal = self.embody.create(baseCOMP, '_test_toxdrop_internal')
        self._internal.par.externaltox.expr = self.DEPRECATED_EXPR
        self.embody.unstore('_smoke_test_responses')
        self.embody_ext._checkExternalToxPar()
        self.assertFalse(self._dropped(self._internal),
                         "Embody's own descendants must always be cleaned")

    # ----- truncation ------------------------------------------------------

    def test_dialog_body_truncates(self):
        """With more COMPs than the cap, the dialog lists at most the cap and
        collapses the rest to '... and N more' so the buttons stay reachable."""
        self.embody.par.Toxdropexpr = 'ask'
        cap = self.embody_ext._MAX_TOXDROP_LISTED
        overflow = 5
        comps = [self._make(f'dropped{i}') for i in range(cap + overflow)]

        emb = self.embody_ext  # resolve once so the instance shadow sticks
        captured = {}

        def _capture(title, message, buttons):
            captured['message'] = message
            captured['buttons'] = buttons
            return 1  # Ignore -> no mutation

        emb._messageBox = _capture
        try:
            emb._resolveToxdropExternals(comps)
        finally:
            try:
                del emb._messageBox
            except Exception:
                pass

        message = captured.get('message', '')
        listed = message.count('  - ')
        self.assertEqual(listed, cap,
                         f'exactly {cap} paths should be listed, got {listed}')
        self.assertIn(f'... and {overflow} more', message,
                      'overflow must collapse to "... and N more"')
        self.assertEqual(len(captured.get('buttons', [])), 4,
                         'the dialog must offer all four buttons')
