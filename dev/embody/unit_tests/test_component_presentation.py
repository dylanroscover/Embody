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

    def test_show_custom_only_is_set(self):
        """EmbodyExt.__init__ must leave showCustomOnly True on the COMP."""
        self.assertTrue(self.embody.showCustomOnly)

    def test_custom_pages_exist_for_filtered_dialog(self):
        """With built-in pages filtered out, the dialog must not be empty:
        the core custom pages have to exist."""
        pages = {p.name for p in self.embody.customPages}
        for required in ('Embody', 'Envoy', 'Advanced', 'About'):
            self.assertIn(required, pages)
