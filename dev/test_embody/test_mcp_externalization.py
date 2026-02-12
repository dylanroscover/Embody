"""
Test suite: MCP externalization integration handlers in ClaudiusExt.

Tests _tag_for_externalization, _remove_externalization_tag,
_get_externalizations, _get_externalization_status.
"""

runner_mod = op('TestRunner').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPExternalization(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _get_externalizations ---

    def test_get_externalizations_returns_list(self):
        result = self.claudius._get_externalizations()
        self.assertDictHasKey(result, 'externalizations')
        self.assertIsInstance(result['externalizations'], list)

    def test_get_externalizations_has_entries(self):
        result = self.claudius._get_externalizations()
        self.assertGreater(len(result['externalizations']), 0)

    def test_get_externalizations_entry_structure(self):
        result = self.claudius._get_externalizations()
        if result['externalizations']:
            entry = result['externalizations'][0]
            self.assertDictHasKey(entry, 'path')
            self.assertDictHasKey(entry, 'type')

    # --- _get_externalization_status ---

    def test_get_externalization_status_existing(self):
        # Use Embody itself as a known externalized op
        result = self.claudius._get_externalization_status(
            op_path=self.embody.path)
        # Should return some status info
        self.assertNotIn('error', result)

    def test_get_externalization_status_nonexistent(self):
        result = self.claudius._get_externalization_status(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _tag_for_externalization ---

    def test_tag_for_externalization_comp(self):
        comp = self.sandbox.create(baseCOMP, 'tag_ext_comp')
        result = self.claudius._tag_for_externalization(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_tag_for_externalization_nonexistent(self):
        result = self.claudius._tag_for_externalization(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _remove_externalization_tag ---

    def test_remove_externalization_tag(self):
        comp = self.sandbox.create(baseCOMP, 'untag_comp')
        # Tag it first
        self.claudius._tag_for_externalization(op_path=comp.path)
        # Now remove
        result = self.claudius._remove_externalization_tag(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_remove_externalization_tag_nonexistent(self):
        result = self.claudius._remove_externalization_tag(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')
