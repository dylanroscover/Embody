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
        # Session-scoped Ignore memory: isolate every test from answers
        # given in earlier tests (sandbox COMP paths repeat across tests).
        self._saved_ignored = getattr(
            self.embody_ext, '_toxdrop_ignored_session', set())
        self.embody_ext._toxdrop_ignored_session = set()

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
        self.embody_ext._toxdrop_ignored_session = self._saved_ignored
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

    # ----- session-scoped Ignore (issue #60) -------------------------------

    def test_plain_ignore_remembered_for_session(self):
        """After a plain Ignore, the same COMPs are not re-prompted on the
        next sweep this session (issue #60 nagging loop)."""
        self.embody.par.Toxdropexpr = 'ask'
        comp = self._make()
        self._seed(1)
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertIn(comp.path, self.embody_ext._toxdrop_ignored_session,
                      'plain Ignore must record the paths for the session')

        # Second sweep: the dialog must not even fire for the same COMP.
        emb = self.embody_ext  # resolve once so the instance shadow sticks
        calls = []

        def _unexpected(title, message, buttons):
            calls.append(title)
            return -1

        emb._messageBox = _unexpected
        try:
            emb._resolveToxdropExternals([comp])
        finally:
            try:
                del emb._messageBox
            except Exception:
                pass
        self.assertEqual(calls, [],
                         'session-ignored COMPs must not re-prompt')
        self.assertTrue(self._dropped(comp),
                        'the expression must still be untouched')

    def test_dismissed_dialog_not_remembered(self):
        """A dismissed/suppressed dialog (-1) must NOT mark COMPs ignored --
        the sweep re-offers on a later pass (existing contract)."""
        self.embody.par.Toxdropexpr = 'ask'
        comp = self._make()
        self.embody.unstore('_smoke_test_responses')
        self.embody_ext._resolveToxdropExternals([comp])
        self.assertNotIn(
            comp.path,
            getattr(self.embody_ext, '_toxdrop_ignored_session', set()),
            'a dismissed dialog is not an answer and must not be remembered')

    # ----- tdn_exclude opt-out (issue #60) ---------------------------------

    def test_exclude_tag_ancestry_helper(self):
        """_hasExcludeTagInAncestry honors the tag on the COMP itself AND on
        any ancestor (the tag marks a whole app-managed subtree)."""
        tag = self.embody.par.Tdnexcludetag.eval()
        parent = self.sandbox.create(baseCOMP, 'excl_parent')
        child = parent.create(baseCOMP, 'excl_child')
        plain = self.sandbox.create(baseCOMP, 'excl_plain')

        self.assertFalse(self.embody_ext._hasExcludeTagInAncestry(plain),
                         'untagged COMP with untagged ancestors: not excluded')
        parent.tags.add(tag)
        self.assertTrue(self.embody_ext._hasExcludeTagInAncestry(parent),
                        'own tag must exclude')
        self.assertTrue(self.embody_ext._hasExcludeTagInAncestry(child),
                        'an ancestor tag must exclude the whole subtree')
        self.assertFalse(self.embody_ext._hasExcludeTagInAncestry(plain),
                         'untagged sibling must stay unaffected')

    def test_excluded_comps_skipped_by_sweep(self):
        """_checkExternalToxPar must not route tdn_exclude'd COMPs (or their
        descendants) to the resolver -- tagged subtrees are invisible to the
        dropped-.tox sweep (issue #60)."""
        tag = self.embody.par.Tdnexcludetag.eval()
        excluded = self._make('excl_dropped')
        excluded.tags.add(tag)
        inner = excluded.create(baseCOMP, 'excl_inner')
        inner.par.externaltox.expr = self.DEPRECATED_EXPR
        included = self._make('incl_dropped')

        emb = self.embody_ext  # resolve once so the instance shadow sticks
        captured = []

        def _capture_resolve(external):
            captured.extend(c.path for c in external)

        emb._resolveToxdropExternals = _capture_resolve
        try:
            emb._checkExternalToxPar()
        finally:
            try:
                del emb._resolveToxdropExternals
            except Exception:
                pass

        self.assertNotIn(excluded.path, captured,
                         'a tagged COMP must be invisible to the sweep')
        self.assertNotIn(inner.path, captured,
                         "a tagged COMP's descendant must be invisible too")
        self.assertIn(included.path, captured,
                      'untagged COMPs must still reach the resolver')

    def test_shouldskip_honors_ancestor_tag(self):
        """ExternalizeProject's _shouldSkipOp must skip untagged
        descendants inside a tdn_exclude'd tree -- the docs promise
        ancestry semantics, and own-tag-only would re-tag app-managed
        subtrees on a full-project externalize (issue #60)."""
        tag = self.embody.par.Tdnexcludetag.eval()
        root = self.sandbox.create(baseCOMP, 'skip_root')
        root.tags.add(tag)
        inner = root.create(baseCOMP, 'skip_inner')
        plain = self.sandbox.create(baseCOMP, 'skip_plain')
        self.assertTrue(self.embody_ext._shouldSkipOp(root, set()),
                        'the tagged COMP itself must be skipped')
        self.assertTrue(self.embody_ext._shouldSkipOp(inner, set()),
                        'untagged descendants of a tagged COMP must be skipped')
        self.assertFalse(self.embody_ext._shouldSkipOp(plain, set()),
                         'untagged COMPs outside the tree must not be skipped')
