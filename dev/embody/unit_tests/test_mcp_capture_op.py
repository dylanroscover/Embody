"""
Test suite: capture_op -- any operator, a TOP natively, every other family
through a transient OP Viewer TOP.

A freshly aimed viewer is empty for its first frames (probed 2026-09-05:
all-zero at frames 0, 1, 3; populated by 10), so a non-TOP capture returns a
deferral marker and EnvoyExt._onRefresh finishes it on a later frame. A
synchronous test cannot wait frames, so these exercise the two halves: the
marker, and continue(final=True) which captures whatever is there and
cleans up.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPCaptureOp(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    def _viewer_leftovers(self):
        return [c.name for c in self.embody.children
                if c.name.startswith('_envoy_viewer_')]

    # --- TOPs take the native path, no viewer ---

    def test_top_captures_natively(self):
        top = self.sandbox.create(noiseTOP, 'op_noise')
        result = self.envoy._capture_op(op_path=top.path)
        self.assertDictHasKey(result, 'success')
        self.assertNotIn('captured_via', result)
        self.assertNotIn('_defer', result)
        self.assertEqual(self._viewer_leftovers(), [])

    # --- non-TOPs: deferral + continuation ---

    def test_non_top_defers_with_a_continuation(self):
        dat = self.sandbox.create(textDAT, 'op_dat')
        dat.text = 'hello'
        marker = self.envoy._capture_op(op_path=dat.path)
        self.assertDictHasKey(marker, '_defer')
        spec = marker['_defer']
        self.assertTrue(callable(spec.get('continue')))
        self.assertGreaterEqual(int(spec.get('frames', 0)), 1)
        self.assertEqual(len(self._viewer_leftovers()), 1,
                         'the viewer exists while the capture is pending')
        result = spec['continue'](final=True)
        self.assertEqual(self._viewer_leftovers(), [],
                         'the continuation destroys the viewer')
        self.assertDictHasKey(result, 'success')
        self.assertEqual(result.get('captured_via'), 'opviewerTOP')
        self.assertEqual(result.get('family'), 'DAT')
        self.assertEqual(result.get('op_type'), 'textDAT')
        self.assertGreater(result['width'], 0)
        self.assertGreater(result['height'], 0)

    def test_continuation_defers_until_rendered(self):
        """Without final=True an unrendered viewer asks for more frames
        rather than shipping a black frame."""
        chop = self.sandbox.create(constantCHOP, 'op_chop')
        marker = self.envoy._capture_op(op_path=chop.path)
        again = marker['_defer']['continue']()
        self.assertNotIn('error', again)
        if '_defer' in again:
            final = again['_defer']['continue'](final=True)
            self.assertDictHasKey(final, 'success')
        self.assertEqual(self._viewer_leftovers(), [])

    def test_viewer_size_follows_max_resolution(self):
        dat = self.sandbox.create(textDAT, 'op_sized')
        marker = self.envoy._capture_op(op_path=dat.path, max_resolution=480)
        result = marker['_defer']['continue'](final=True)
        self.assertEqual(result['original_width'], 480)
        self.assertEqual(result['original_height'], 270)

    def test_every_family_is_accepted(self):
        for cls, name in ((constantCHOP, 'fam_chop'), (boxSOP, 'fam_sop'),
                          (textDAT, 'fam_dat'), (baseCOMP, 'fam_comp')):
            marker = self.envoy._capture_op(op_path=self.sandbox.create(cls, name).path)
            self.assertDictHasKey(marker, '_defer', name)
            result = marker['_defer']['continue'](final=True)
            self.assertDictHasKey(result, 'success', name)
        self.assertEqual(self._viewer_leftovers(), [])

    # --- guards ---

    def test_nonexistent_op(self):
        result = self.envoy._capture_op(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')
        self.assertIn('not found', result['error'])

    def test_invalid_format_and_quality(self):
        dat = self.sandbox.create(textDAT, 'op_guard')
        self.assertIn('Unsupported format',
                      self.envoy._capture_op(op_path=dat.path, format='bmp')['error'])
        self.assertIn('Quality must be',
                      self.envoy._capture_op(op_path=dat.path, quality=2.0)['error'])
        self.assertEqual(self._viewer_leftovers(), [],
                         'a refused call must not leave a viewer behind')

    # --- plumbing shared with capture_top ---

    def test_viewer_leftover_sweep(self):
        stray = self.embody.create(opviewerTOP, '_envoy_viewer_stray')
        self.assertIn('_envoy_viewer_stray', self._viewer_leftovers())
        read = self.embody.op('envoy_read').module
        self.assertEqual(read.sweep_viewer_leftovers(self.envoy), 1)
        self.assertEqual(self._viewer_leftovers(), [])

    def test_deferral_predicate(self):
        envoy_mod = self.embody.op('EnvoyExt').module
        self.assertTrue(envoy_mod._is_deferred({'_defer': {'frames': 2, 'continue': lambda: None}}))
        self.assertFalse(envoy_mod._is_deferred({'success': True}))
        self.assertFalse(envoy_mod._is_deferred({'_defer': 'no'}))
        self.assertFalse(envoy_mod._is_deferred(None))
