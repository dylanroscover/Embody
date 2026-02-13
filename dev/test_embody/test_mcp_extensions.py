"""
Test suite: MCP extension creation handler in ClaudiusExt.

Tests _create_extension with various configurations.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPExtensions(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _create_extension ---

    def test_create_extension_new_comp(self):
        result = self.claudius._create_extension(
            parent_path=self.sandbox.path,
            class_name='TestExt')
        self.assertTrue(result.get('success'))
        self.assertDictHasKey(result, 'comp_path')
        self.assertDictHasKey(result, 'dat_path')

    def test_create_extension_existing_comp(self):
        comp = self.sandbox.create(baseCOMP, 'ext_target')
        result = self.claudius._create_extension(
            parent_path=comp.path,
            class_name='MyExt',
            existing_comp=True)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['comp_path'], comp.path)

    def test_create_extension_custom_code(self):
        code = '''class CustomExt:
    def __init__(self, ownerComp):
        self.ownerComp = ownerComp

    def MyMethod(self):
        return 42
'''
        result = self.claudius._create_extension(
            parent_path=self.sandbox.path,
            class_name='CustomExt',
            code=code)
        self.assertTrue(result.get('success'))

    def test_create_extension_nonexistent_parent(self):
        result = self.claudius._create_extension(
            parent_path='/nonexistent',
            class_name='BadExt')
        self.assertDictHasKey(result, 'error')

    def test_create_extension_invalid_class_name(self):
        result = self.claudius._create_extension(
            parent_path=self.sandbox.path,
            class_name='123-invalid')
        self.assertDictHasKey(result, 'error')

    def test_create_extension_reports_ext_index(self):
        result = self.claudius._create_extension(
            parent_path=self.sandbox.path,
            class_name='SlotTest')
        self.assertDictHasKey(result, 'ext_index')
        self.assertGreaterEqual(result['ext_index'], 0)
        self.assertLessEqual(result['ext_index'], 3)
