"""
Test suite: MCP performance monitoring in ClaudiusExt.

Tests _get_op_performance.
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestMCPPerformance(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # =========================================================================
    # _get_op_performance
    # =========================================================================

    def test_basic_return_structure(self):
        """_get_op_performance should return a dict with performance fields."""
        comp = self.sandbox.create(baseCOMP, 'perf_comp')
        result = self.claudius._get_op_performance(op_path=comp.path)
        self.assertDictHasKey(result, 'path')
        self.assertDictHasKey(result, 'cpuCookTime')
        self.assertDictHasKey(result, 'gpuCookTime')
        self.assertDictHasKey(result, 'totalCooks')

    def test_has_memory_fields(self):
        """_get_op_performance should include memory fields."""
        comp = self.sandbox.create(baseCOMP, 'mem_comp')
        result = self.claudius._get_op_performance(op_path=comp.path)
        self.assertDictHasKey(result, 'cpuMemory')
        self.assertDictHasKey(result, 'gpuMemory')

    def test_include_children(self):
        """_get_op_performance with include_children should add children fields."""
        comp = self.sandbox.create(baseCOMP, 'parent_perf')
        comp.create(baseCOMP, 'child_perf')
        result = self.claudius._get_op_performance(
            op_path=comp.path, include_children=True
        )
        self.assertDictHasKey(result, 'childrenCPUCookTime')
        self.assertDictHasKey(result, 'childrenGPUCookTime')
        self.assertDictHasKey(result, 'childrenCPUMemory')
        self.assertDictHasKey(result, 'childrenGPUMemory')

    def test_without_include_children(self):
        """_get_op_performance without include_children should omit children fields."""
        comp = self.sandbox.create(baseCOMP, 'no_children')
        result = self.claudius._get_op_performance(op_path=comp.path)
        self.assertFalse('childrenCPUCookTime' in result)

    def test_nonexistent_op(self):
        """_get_op_performance on nonexistent op should return error."""
        result = self.claudius._get_op_performance(op_path='/nonexistent/op')
        self.assertDictHasKey(result, 'error')
