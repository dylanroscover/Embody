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
