"""
Test suite: MCP project-level performance monitoring in EnvoyExt.

Tests _get_project_performance and _get_performance_hotspots.
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestMCPProjectPerformance(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # =========================================================================
    # _get_project_performance — return structure
    # =========================================================================

    def test_basic_return_structure(self):
        """_get_project_performance should return timing, memory, frameHealth, gpu, performMode."""
        result = self.envoy._get_project_performance()
        self.assertDictHasKey(result, 'timing')
        self.assertDictHasKey(result, 'memory')
        self.assertDictHasKey(result, 'frameHealth')
        self.assertDictHasKey(result, 'gpu')
        self.assertIn('performMode', result)

    def test_timing_fields(self):
        """timing section should have fps, frameTimeMs, cookRate."""
        result = self.envoy._get_project_performance()
        timing = result['timing']
        self.assertDictHasKey(timing, 'fps')
        self.assertDictHasKey(timing, 'frameTimeMs')
        self.assertDictHasKey(timing, 'cookRate')
        self.assertDictHasKey(timing, 'cookRealTime')
        self.assertDictHasKey(timing, 'timeSliceMs')
        self.assertDictHasKey(timing, 'timeSliceStep')

    def test_memory_fields(self):
        """memory section should have GPU and CPU memory."""
        result = self.envoy._get_project_performance()
        mem = result['memory']
        self.assertDictHasKey(mem, 'gpuMemUsedMB')
        self.assertDictHasKey(mem, 'totalGpuMemMB')
        self.assertDictHasKey(mem, 'cpuMemUsedMB')

    def test_frame_health_fields(self):
        """frameHealth should include droppedFrames, activeOps, totalOps."""
        result = self.envoy._get_project_performance()
        health = result['frameHealth']
        self.assertDictHasKey(health, 'droppedFrames')
        self.assertDictHasKey(health, 'cookedLastFrame')
        self.assertDictHasKey(health, 'activeOps')
        self.assertDictHasKey(health, 'totalOps')

    def test_gpu_fields(self):
        """gpu section should include chip and board temperature."""
        result = self.envoy._get_project_performance()
        self.assertDictHasKey(result['gpu'], 'chipTemperatureC')
        self.assertDictHasKey(result['gpu'], 'boardTemperatureC')

    def test_fps_is_positive(self):
        """FPS should be a positive number when TD is running."""
        result = self.envoy._get_project_performance()
        self.assertGreater(result['timing']['fps'], 0)

    def test_cook_rate_is_positive(self):
        """cookRate should reflect the project's target frame rate."""
        result = self.envoy._get_project_performance()
        self.assertGreater(result['timing']['cookRate'], 0)

    def test_total_ops_is_positive(self):
        """totalOps should be > 0 in any running project."""
        result = self.envoy._get_project_performance()
        self.assertGreater(result['frameHealth']['totalOps'], 0)

    def test_perform_chop_exists(self):
        """The permanent Perform CHOP should exist inside Embody."""
        perform = self.embody.op('_envoy_perform')
        self.assertIsNotNone(perform)

    # =========================================================================
    # _get_performance_hotspots — optional hotspot analysis
    # =========================================================================

    def test_hotspots_off_by_default(self):
        """Result should not have hotspots key when include_hotspots=0."""
        result = self.envoy._get_project_performance(include_hotspots=0)
        self.assertFalse('hotspots' in result)

    def test_hotspots_returns_list(self):
        """With include_hotspots > 0, result should have hotspots list."""
        result = self.envoy._get_project_performance(include_hotspots=5)
        self.assertDictHasKey(result, 'hotspots')
        self.assertIsInstance(result['hotspots'], list)

    def test_hotspots_fields(self):
        """Each hotspot entry should have path, name, and cook time fields."""
        result = self.envoy._get_project_performance(include_hotspots=5)
        if len(result['hotspots']) > 0:
            entry = result['hotspots'][0]
            self.assertDictHasKey(entry, 'path')
            self.assertDictHasKey(entry, 'name')
            self.assertDictHasKey(entry, 'cpuCookTimeMs')
            self.assertDictHasKey(entry, 'gpuCookTimeMs')
            self.assertDictHasKey(entry, 'combinedCookTimeMs')
            self.assertDictHasKey(entry, 'cpuMemoryBytes')
            self.assertDictHasKey(entry, 'gpuMemoryBytes')

    def test_hotspots_sorted_descending(self):
        """Hotspots should be sorted by combinedCookTimeMs descending."""
        result = self.envoy._get_project_performance(include_hotspots=10)
        hotspots = result.get('hotspots', [])
        for i in range(len(hotspots) - 1):
            self.assertGreaterEqual(
                hotspots[i]['combinedCookTimeMs'],
                hotspots[i + 1]['combinedCookTimeMs']
            )

    def test_hotspots_respects_top_n(self):
        """Hotspots list should not exceed the requested count."""
        result = self.envoy._get_project_performance(include_hotspots=2)
        self.assertLessEqual(len(result['hotspots']), 2)
