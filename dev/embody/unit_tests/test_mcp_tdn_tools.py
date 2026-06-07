"""
Test suite: MCP TDN network tools.

Covers:
  A. read_tdn returns a valid TDN dict for a representative COMP
  B. read_tdn include_dat_content toggle
  C. read_tdn succeeds in all three Tdnmode values (off / export / full)
  D. Token-budget regression: read_tdn payload is materially smaller than
     an equivalent get_op walk (locks in the central MCP-efficiency claim)
  E. export_network / import_network MCP handlers round-trip a network
"""

import json

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestMCPTDNTools(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        # Build a small deterministic fixture: a baseCOMP with a few
        # distinct operator types inside. Sandbox gets cleaned in tearDown.
        self.fixture = self.sandbox.create(baseCOMP, 'tdn_tools_fixture')
        self.fixture.create(noiseTOP, 'noise')
        self.fixture.create(levelTOP, 'level')
        self.fixture.create(nullTOP, 'null')
        self.fixture.create(waveCHOP, 'wave')
        self.fixture.create(textDAT, 'notes')

    # ------------------------------------------------------------------
    # A. Basic shape
    # ------------------------------------------------------------------

    def test_read_tdn_returns_tdn_dict(self):
        result = self.embody.ext.Envoy._read_tdn(comp_path=self.fixture.path)
        self.assertTrue(result.get('success'),
            f'read_tdn failed: {result.get("error")}')
        tdn = result.get('tdn')
        self.assertIsNotNone(tdn, 'read_tdn must return a tdn payload')
        self.assertEqual(tdn.get('format'), 'tdn')
        self.assertIn('operators', tdn)
        self.assertIn('version', tdn)

    def test_read_tdn_lists_fixture_children(self):
        result = self.embody.ext.Envoy._read_tdn(comp_path=self.fixture.path)
        names = {o['name'] for o in result['tdn']['operators']}
        self.assertTrue({'noise', 'level', 'null', 'wave', 'notes'} <= names,
            f'Expected fixture children in tdn.operators, got: {names}')

    # ------------------------------------------------------------------
    # B. Options
    # ------------------------------------------------------------------

    def test_read_tdn_include_dat_content_toggle(self):
        dat = self.fixture.op('notes')
        dat.text = 'MARKER_CONTENT_42'
        with_content = self.embody.ext.Envoy._read_tdn(
            comp_path=self.fixture.path, include_dat_content=True)
        serialized = json.dumps(with_content['tdn'])
        self.assertIn('MARKER_CONTENT_42', serialized,
            'DAT content missing when include_dat_content=True')

        without = self.embody.ext.Envoy._read_tdn(
            comp_path=self.fixture.path, include_dat_content=False)
        serialized = json.dumps(without['tdn'])
        self.assertNotIn('MARKER_CONTENT_42', serialized,
            'DAT content leaked when include_dat_content=False')

    # ------------------------------------------------------------------
    # C. Mode-agnostic read
    # ------------------------------------------------------------------

    def test_read_tdn_works_in_all_modes(self):
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        mode_was = self.embody.par.Tdnmode.eval()
        parexec.par.active = False
        try:
            for mode in ('off', 'export', 'full'):
                self.embody.par.Tdnmode.val = mode
                result = self.embody.ext.Envoy._read_tdn(comp_path=self.fixture.path)
                self.assertTrue(result.get('success'),
                    f'read_tdn failed in mode={mode}: {result.get("error")}')
                self.assertIn('operators', result['tdn'])
        finally:
            self.embody.par.Tdnmode.val = mode_was
            self.embody_ext._applyTdnModeGating()
            parexec.par.active = was_active

    # ------------------------------------------------------------------
    # D. Token-budget regression
    # ------------------------------------------------------------------

    def test_read_tdn_is_materially_smaller_than_get_op_walk(self):
        """Lock in the claim: read_tdn uses materially fewer chars than
        walking the same subtree via get_op per operator.

        Floor: 5x reduction. Real-world networks hit 20-90x; keeping the
        floor conservative so this test doesn't flake on tiny fixtures.
        """
        tdn_result = self.embody.ext.Envoy._read_tdn(comp_path=self.fixture.path)
        self.assertTrue(tdn_result.get('success'))
        tdn_chars = len(json.dumps(tdn_result['tdn']))

        # Walk fixture children via _get_op and sum the payload sizes.
        get_op_chars = 0
        for child in self.fixture.children:
            op_result = self.embody.ext.Envoy._get_op(op_path=child.path)
            get_op_chars += len(json.dumps(op_result))

        self.assertGreater(get_op_chars, 0,
            'get_op walk produced no payload -- test is broken')
        ratio = get_op_chars / max(1, tdn_chars)
        self.assertGreater(ratio, 5,
            f'Expected read_tdn to be >5x smaller than get_op walk. '
            f'tdn={tdn_chars} chars, get_op_sum={get_op_chars} chars, '
            f'ratio={ratio:.2f}x')

    # ------------------------------------------------------------------
    # E. export_network / import_network round-trip
    # ------------------------------------------------------------------

    def test_export_network_in_memory(self):
        """_export_network with output_file=None returns a TDN dict (no disk)."""
        result = self.embody.ext.Envoy._export_network(
            root_path=self.fixture.path, output_file=None)
        self.assertTrue(result.get('success'),
            f'export_network failed: {result.get("error")}')
        self.assertEqual(result['tdn'].get('format'), 'tdn')

    def test_export_network_nonexistent(self):
        result = self.embody.ext.Envoy._export_network(
            root_path='/nonexistent', output_file=None)
        self.assertDictHasKey(result, 'error')

    def test_export_then_import_round_trips(self):
        """Round-trip through the MCP handlers: export the fixture, then
        import it into a fresh COMP and confirm every child reappears."""
        export = self.embody.ext.Envoy._export_network(
            root_path=self.fixture.path, output_file=None)
        self.assertTrue(export.get('success'),
            f'export failed: {export.get("error")}')

        target = self.sandbox.create(baseCOMP, 'import_target')
        result = self.embody.ext.Envoy._import_network(
            target_path=target.path, tdn=export['tdn'], clear_first=True)
        self.assertTrue(result.get('success'),
            f'import failed: {result.get("error")}')

        names = {c.name for c in target.children}
        self.assertTrue({'noise', 'level', 'null', 'wave', 'notes'} <= names,
            f'Imported children missing from target: {names}')

    def test_import_network_invalid_tdn(self):
        target = self.sandbox.create(baseCOMP, 'import_bad')
        result = self.embody.ext.Envoy._import_network(
            target_path=target.path, tdn={'not_operators': 1})
        self.assertDictHasKey(result, 'error')

    def test_import_network_nonexistent_target(self):
        result = self.embody.ext.Envoy._import_network(
            target_path='/nonexistent', tdn={'operators': []})
        self.assertDictHasKey(result, 'error')
