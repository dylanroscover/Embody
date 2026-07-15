"""
Test suite: MCP TOP capture handler in EnvoyExt.

Tests _capture_top for image capture from TOP operators.
"""

import base64

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPTopCapture(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- Happy path ---

    def test_capture_noise_top_jpeg(self):
        top = self.sandbox.create(noiseTOP, 'noise1')
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path)
        self.assertTrue(result.get('success'))
        self.assertDictHasKey(result, 'image_b64')
        self.assertDictHasKey(result, 'width')
        self.assertDictHasKey(result, 'height')
        self.assertEqual(result['format'], 'jpeg')
        self.assertGreater(result['size_bytes'], 0)

    def test_capture_noise_top_png(self):
        top = self.sandbox.create(noiseTOP, 'noise_png')
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path, format='png')
        self.assertTrue(result.get('success'))
        self.assertEqual(result['format'], 'png')

    def test_capture_returns_valid_jpeg(self):
        top = self.sandbox.create(noiseTOP, 'valid_jpg')
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path, format='jpeg')
        self.assertTrue(result.get('success'))
        image_bytes = base64.b64decode(result['image_b64'])
        # JPEG files start with FF D8
        self.assertEqual(image_bytes[0], 0xFF)
        self.assertEqual(image_bytes[1], 0xD8)

    def test_capture_returns_valid_png(self):
        top = self.sandbox.create(noiseTOP, 'valid_png')
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path, format='png')
        self.assertTrue(result.get('success'))
        image_bytes = base64.b64decode(result['image_b64'])
        # PNG files start with 89 50 4E 47
        self.assertEqual(image_bytes[:4], b'\x89PNG')

    # --- Resolution ---

    def test_capture_respects_max_resolution(self):
        top = self.sandbox.create(noiseTOP, 'big_noise')
        top.par.outputresolution = 'custom'
        top.par.resolutionw = 1024
        top.par.resolutionh = 768
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path, max_resolution=320)
        self.assertTrue(result.get('success'))
        self.assertLessEqual(max(result['width'], result['height']), 320)
        self.assertEqual(result['original_width'], 1024)
        self.assertEqual(result['original_height'], 768)

    def test_capture_native_resolution(self):
        top = self.sandbox.create(noiseTOP, 'native_noise')
        top.par.outputresolution = 'custom'
        top.par.resolutionw = 256
        top.par.resolutionh = 128
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path, max_resolution=0)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['width'], 256)
        self.assertEqual(result['height'], 128)

    def test_capture_small_top_no_resize(self):
        top = self.sandbox.create(noiseTOP, 'small_noise')
        top.par.outputresolution = 'custom'
        top.par.resolutionw = 64
        top.par.resolutionh = 64
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path, max_resolution=640)
        self.assertTrue(result.get('success'))
        # Should NOT resize since 64 < 640
        self.assertEqual(result['width'], 64)
        self.assertEqual(result['height'], 64)

    # --- Quality verdict (black/empty-frame detection) ---

    def test_quality_present_and_passes_on_noise(self):
        top = self.sandbox.create(noiseTOP, 'q_noise')
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path)
        self.assertTrue(result.get('success'))
        q = result.get('quality')
        self.assertIsInstance(q, dict)
        self.assertTrue(q.get('pass'), repr(q))
        self.assertFalse(q.get('is_black'), repr(q))
        self.assertEqual(q.get('fail_reasons'), [])
        self.assertGreater(q.get('std_luminance'), 0.0)

    def test_quality_flags_black_frame(self):
        top = self.sandbox.create(constantTOP, 'q_black')
        top.par.colorr = 0.0
        top.par.colorg = 0.0
        top.par.colorb = 0.0
        top.par.alpha = 1.0
        top.par.resolutionw = 64
        top.par.resolutionh = 64
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path)
        self.assertTrue(result.get('success'))
        q = result['quality']
        self.assertTrue(q['is_black'], repr(q))
        self.assertFalse(q['pass'], repr(q))
        self.assertIn('black_frame', q['fail_reasons'])

    def test_quality_solid_color_is_flat_but_passes(self):
        # A uniform fill is a VALID render -- flat is advisory, not a failure.
        top = self.sandbox.create(constantTOP, 'q_solid')
        top.par.colorr = 0.5
        top.par.colorg = 0.5
        top.par.colorb = 0.5
        top.par.alpha = 1.0
        top.par.resolutionw = 64
        top.par.resolutionh = 64
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path)
        q = result['quality']
        self.assertTrue(q['is_flat'], repr(q))
        self.assertFalse(q['is_black'], repr(q))
        self.assertTrue(q['pass'], repr(q))
        self.assertIn('flat_frame', q['fail_reasons'])

    def test_quality_flags_fully_transparent(self):
        top = self.sandbox.create(constantTOP, 'q_transp')
        top.par.colorr = 1.0
        top.par.colorg = 1.0
        top.par.colorb = 1.0
        top.par.alpha = 0.0
        top.par.resolutionw = 64
        top.par.resolutionh = 64
        top.cook(force=True)
        result = self.envoy._capture_top(op_path=top.path)
        q = result['quality']
        self.assertFalse(q['pass'], repr(q))
        # premultiply may zero RGB too; either verdict is a correct failure.
        self.assertTrue(
            'fully_transparent' in q['fail_reasons']
            or 'black_frame' in q['fail_reasons'], repr(q))

    # --- Error cases ---

    def test_capture_nonexistent_op(self):
        result = self.envoy._capture_top(op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')
        self.assertIn('not found', result['error'])

    def test_capture_non_top_operator(self):
        dat = self.sandbox.create(textDAT, 'not_a_top')
        result = self.envoy._capture_top(op_path=dat.path)
        self.assertDictHasKey(result, 'error')
        self.assertIn('not a TOP', result['error'])

    def test_capture_invalid_format(self):
        top = self.sandbox.create(noiseTOP, 'fmt_noise')
        result = self.envoy._capture_top(op_path=top.path, format='bmp')
        self.assertDictHasKey(result, 'error')
        self.assertIn('Unsupported format', result['error'])

    def test_capture_quality_out_of_range(self):
        top = self.sandbox.create(noiseTOP, 'qual_noise')
        result = self.envoy._capture_top(op_path=top.path, quality=1.5)
        self.assertDictHasKey(result, 'error')
        self.assertIn('Quality must be', result['error'])
