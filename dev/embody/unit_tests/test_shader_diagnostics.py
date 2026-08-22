"""
Test suite: GLSL compile diagnostics on get_op_errors.

TD does NOT surface shader compile failures through op.errors() -- it emits
only a warning ("The GLSL Shader has compile errors (Use Info DAT to see
details)") and writes the real text to the auto-docked <name>_info DAT. The
project lost a full debug cycle to this once: a `half` reserved-word error
rendered TD's fallback while op.errors() reported "(none)"
(.claude/rules/multi-agent-review.md:15).

get_op_errors now reads that docked Info DAT and reports a 'shaderErrors'
key. The three properties that matter:
  - a BROKEN shader is reported (the incident)
  - a HEALTHY shader is not (no false positives)
  - a NON-shader returns None, never an empty "clean" result (no silent
    false negative -- CLAUDE.md "fail loud")
"""

BROKEN_PIXEL = (
    'out vec4 fragColor;\n'
    'void main()\n'
    '{\n'
    '\thalf x = 1.0;\n'          # 'half' is a GLSL reserved word
    '\tfragColor = vec4(x);\n'
    '}\n'
)

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestShaderDiagnostics(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.read = self.embody.op('envoy_read').module

    def _broken_glsl(self, name='glsl_broken'):
        g = self.sandbox.create(glslTOP, name)
        self.sandbox.op(f'{name}_pixel').text = BROKEN_PIXEL
        g.cook(force=True)
        return g

    # ------------------------------------------------------------------
    # The incident
    # ------------------------------------------------------------------

    def test_compile_error_invisible_to_op_errors(self):
        """Pins the PREMISE: if TD ever starts reporting GLSL compile errors
        through errors(), this whole feature can be reconsidered."""
        g = self._broken_glsl()
        self.assertEqual(g.errors(recurse=False), '',
            'TD now reports GLSL compile errors via errors() -- revisit '
            'shader_compile_log, it may be redundant')

    def test_broken_shader_is_reported(self):
        g = self._broken_glsl()
        r = self.envoy._get_op_errors(g.path, False)
        msgs = ' '.join(e['message'] for e in r.get('shaderErrors') or [])
        self.assertIn('half', msgs)
        self.assertIn('Reserved word', msgs)
        self.assertTrue(r['hasErrors'],
            'a shader that failed to compile must not read as error-free')

    def test_reported_entry_shape_matches_errors(self):
        """Entries must match get_op_errors' errors[] shape so the
        write-effect differ can consume them unchanged."""
        g = self._broken_glsl()
        entry = (self.envoy._get_op_errors(g.path, False)['shaderErrors'])[0]
        for key in ('nodePath', 'nodeName', 'opType', 'message'):
            self.assertIn(key, entry)
        self.assertEqual(entry['nodePath'], g.path)
        self.assertIn('_info', entry['infoDat'])

    # ------------------------------------------------------------------
    # False positives / negatives
    # ------------------------------------------------------------------

    def test_healthy_shader_reports_nothing(self):
        g = self.sandbox.create(glslTOP, 'glsl_ok')
        g.cook(force=True)
        r = self.envoy._get_op_errors(g.path, False)
        self.assertNotIn('shaderErrors', r,
            'a compiling shader must not carry a shaderErrors key')

    def test_non_shader_returns_none_not_clean(self):
        """None means "not a shader"; an empty dict would be a silent
        false negative for an op whose Info DAT we simply could not find."""
        lvl = self.sandbox.create(levelTOP, 'level_plain')
        self.assertIsNone(self.read.shader_compile_log(lvl))

    # ------------------------------------------------------------------
    # Discovery robustness
    # ------------------------------------------------------------------

    def test_info_dat_found_after_host_rename(self):
        """Discovery is by dat.type == 'info', not by '<name>_info': TD
        renames only the host, so name-matching would go stale here."""
        g = self._broken_glsl('glsl_before')
        g.name = 'glsl_after'
        g.cook(force=True)
        log = self.read.shader_compile_log(g)
        # DISCOVERY is the property under test. Whether the Info DAT's text
        # still holds the error afterwards is TD's recook timing, not ours:
        # the docks keep their OLD names (verified -- a renamed glsl_before
        # still docks glsl_before_info), which is exactly why discovery is by
        # dat.type and not by name.
        self.assertIsNotNone(log, 'info DAT lost after host rename')
        self.assertIn('_info', log['infoDat'])
        self.assertNotIn('glsl_after', log['infoDat'],
            'dock kept its original name -- name-matching would have failed')

    def test_recurse_finds_shader_below_a_comp(self):
        inner = self.sandbox.create(baseCOMP, 'inner')
        g = inner.create(glslTOP, 'glsl_deep')
        inner.op('glsl_deep_pixel').text = BROKEN_PIXEL
        g.cook(force=True)
        r = self.envoy._get_op_errors(self.sandbox.path, True)
        paths = {e['nodePath'] for e in r.get('shaderErrors') or []}
        self.assertIn(g.path, paths)

    def test_no_recurse_stays_local(self):
        inner = self.sandbox.create(baseCOMP, 'inner2')
        g = inner.create(glslTOP, 'glsl_deep2')
        inner.op('glsl_deep2_pixel').text = BROKEN_PIXEL
        g.cook(force=True)
        r = self.envoy._get_op_errors(self.sandbox.path, False)
        self.assertNotIn('shaderErrors', r)
