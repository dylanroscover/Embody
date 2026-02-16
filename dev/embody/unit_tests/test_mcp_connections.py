"""
Test suite: MCP connection handlers in ClaudiusExt.

Tests _connect_ops, _disconnect_op, _get_connections.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPConnections(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _connect_ops ---

    def test_connect_tops(self):
        noise = self.sandbox.create(noiseTOP, 'noise1')
        level = self.sandbox.create(levelTOP, 'level1')
        result = self.claudius._connect_ops(
            source_path=noise.path, dest_path=level.path)
        self.assertTrue(result.get('success'))

    def test_connect_chops(self):
        wave = self.sandbox.create(waveCHOP, 'wave1')
        math = self.sandbox.create(mathCHOP, 'math1')
        result = self.claudius._connect_ops(
            source_path=wave.path, dest_path=math.path)
        self.assertTrue(result.get('success'))

    def test_connect_nonexistent_source(self):
        level = self.sandbox.create(levelTOP, 'level2')
        result = self.claudius._connect_ops(
            source_path='/nonexistent', dest_path=level.path)
        self.assertDictHasKey(result, 'error')

    # --- _disconnect_op ---

    def test_disconnect_after_connect(self):
        noise = self.sandbox.create(noiseTOP, 'disc_noise')
        level = self.sandbox.create(levelTOP, 'disc_level')
        self.claudius._connect_ops(
            source_path=noise.path, dest_path=level.path)
        result = self.claudius._disconnect_op(op_path=level.path)
        self.assertTrue(result.get('success'))

    def test_disconnect_nonexistent(self):
        result = self.claudius._disconnect_op(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _get_connections ---

    def test_get_connections_basic(self):
        noise = self.sandbox.create(noiseTOP, 'conn_noise')
        level = self.sandbox.create(levelTOP, 'conn_level')
        self.claudius._connect_ops(
            source_path=noise.path, dest_path=level.path)
        result = self.claudius._get_connections(op_path=level.path)
        self.assertDictHasKey(result, 'inputs')

    def test_get_connections_empty(self):
        comp = self.sandbox.create(baseCOMP, 'no_conn')
        result = self.claudius._get_connections(op_path=comp.path)
        self.assertDictHasKey(result, 'inputs')

    def test_get_connections_nonexistent(self):
        result = self.claudius._get_connections(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')
