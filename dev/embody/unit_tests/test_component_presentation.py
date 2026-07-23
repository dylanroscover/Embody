"""
Test suite: shipped-component presentation invariants.

Embody follows the POPX pattern for parameter dialogs: showCustomOnly=True
on the shipped COMP so users see only Embody's custom pages (Embody, Tags,
TDN, Envoy, Logs, UI, Shortcuts, Advanced, About), not those plus TD's
Layout/Panel/Look/Children/Drag-Drop/Extensions/Common. The flag is a pure
dialog filter -- built-in pages stay functional and reachable -- and is
(re)applied in EmbodyExt.__init__ so every deployed copy converges on it
after any extension init, not only fresh authored state.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestComponentPresentation(EmbodyTestCase):

    def test_show_custom_only_follows_toggle_default(self):
        """With 'Show Built-in Pars' off (default), __init__ must leave
        showCustomOnly True on the COMP."""
        self.assertFalse(bool(self.embody.par.Showbuiltinpars.eval()),
                         'suite assumes the shipped default (off)')
        self.assertTrue(self.embody.showCustomOnly)

    def test_show_builtin_pars_toggle_exists_with_wiring(self):
        """The Advanced-page 'Show Built-in Pars' toggle (issue #77) exists
        with sane authoring, and both application paths carry the mapping.

        The live flip itself cannot be asserted same-frame: Parameter
        Execute callbacks apply at the frame boundary, after a synchronous
        test method has already returned (verified live 2026-07-23: the
        flag flips one frame after the par change, both directions). So
        this pins the parameter contract plus the wiring in BOTH code
        paths -- parexec (live flips) and EmbodyExt.__init__ (deployed-
        copy convergence) -- rather than racing the deferred callback."""
        p = self.embody.par.Showbuiltinpars
        self.assertEqual(p.style, 'Toggle')
        self.assertEqual(p.page.name, 'Advanced')
        self.assertEqual(p.default, False)
        self.assertTrue(p.help, 'help text is mandatory')
        parexec_src = self.embody.op('parexec').text
        self.assertIn("par.name == 'Showbuiltinpars'", parexec_src)
        self.assertIn('showCustomOnly = not bool(par.eval())', parexec_src)
        ext_src = self.embody.op('EmbodyExt').text
        self.assertIn(
            'showCustomOnly = not bool(self.my.par.Showbuiltinpars.eval())',
            ext_src)

    def test_custom_pages_exist_for_filtered_dialog(self):
        """With built-in pages filtered out, the dialog must not be empty:
        the core custom pages have to exist."""
        pages = {p.name for p in self.embody.customPages}
        for required in ('Embody', 'Envoy', 'Advanced', 'About'):
            self.assertIn(required, pages)
