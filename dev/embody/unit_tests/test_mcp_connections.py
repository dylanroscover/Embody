"""
Test suite: MCP connection handlers in EnvoyExt.

Tests _connect_ops, _disconnect_op, _get_connections.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPConnections(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- _connect_ops ---

    def test_connect_tops(self):
        noise = self.sandbox.create(noiseTOP, 'noise1')
        level = self.sandbox.create(levelTOP, 'level1')
        result = self.envoy._connect_ops(
            source_path=noise.path, dest_path=level.path)
        self.assertTrue(result.get('success'))

    def test_connect_chops(self):
        wave = self.sandbox.create(waveCHOP, 'wave1')
        math = self.sandbox.create(mathCHOP, 'math1')
        result = self.envoy._connect_ops(
            source_path=wave.path, dest_path=math.path)
        self.assertTrue(result.get('success'))

    def test_connect_nonexistent_source(self):
        level = self.sandbox.create(levelTOP, 'level2')
        result = self.envoy._connect_ops(
            source_path='/nonexistent', dest_path=level.path)
        self.assertDictHasKey(result, 'error')

    # --- _disconnect_op ---

    def test_disconnect_after_connect(self):
        noise = self.sandbox.create(noiseTOP, 'disc_noise')
        level = self.sandbox.create(levelTOP, 'disc_level')
        self.envoy._connect_ops(
            source_path=noise.path, dest_path=level.path)
        result = self.envoy._disconnect_op(op_path=level.path)
        self.assertTrue(result.get('success'))

    def test_disconnect_nonexistent(self):
        result = self.envoy._disconnect_op(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _get_connections ---

    def test_get_connections_basic(self):
        noise = self.sandbox.create(noiseTOP, 'conn_noise')
        level = self.sandbox.create(levelTOP, 'conn_level')
        self.envoy._connect_ops(
            source_path=noise.path, dest_path=level.path)
        result = self.envoy._get_connections(op_path=level.path)
        self.assertDictHasKey(result, 'inputs')

    def test_get_connections_empty(self):
        comp = self.sandbox.create(baseCOMP, 'no_conn')
        result = self.envoy._get_connections(op_path=comp.path)
        self.assertDictHasKey(result, 'inputs')

    def test_get_connections_nonexistent(self):
        result = self.envoy._get_connections(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    def test_get_connections_reports_true_connector_indices(self):
        """One entry PER CONNECTOR, index-faithful. The old reader
        walked OP.inputs -- a COMPACTED list -- so a wire on a Matte
        TOP's connector 2 with 0-1 empty reported as index 0 (live
        repro, 2026-08-12). This is an agent-facing MCP contract: the
        docstring now promises real connector indices with explicit
        nulls, and this pins it."""
        src = self.sandbox.create(noiseTOP, 'idx_src')
        mat = self.sandbox.create(matteTOP, 'idx_matte')
        src.outputConnectors[0].connect(mat.inputConnectors[2])

        result = self.envoy._get_connections(op_path=mat.path)
        inputs = result.get('inputs')
        self.assertEqual(len(inputs), 3,
                         'a Matte TOP has three fixed input connectors')
        self.assertIsNone(inputs[0]['connected_to'])
        self.assertIsNone(inputs[1]['connected_to'])
        self.assertEqual(inputs[2]['index'], 2)
        self.assertEqual(inputs[2]['connected_to'], src.path,
                         'the wire must report at its REAL connector')

    def test_get_op_inputs_are_per_connector(self):
        """get_op's 'inputs' shares the same contract: entry position ==
        connector index, null == empty connector."""
        src = self.sandbox.create(noiseTOP, 'gio_src')
        lk = self.sandbox.create(lookupTOP, 'gio_lookup')
        src.outputConnectors[0].connect(lk.inputConnectors[1])

        result = self.envoy._get_op(op_path=lk.path)
        self.assertEqual(result.get('inputs'), [None, src.path],
                         'sparse wire must surface at its real index')
