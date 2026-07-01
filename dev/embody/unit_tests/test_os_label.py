"""
Test suite: OS label resolution in EmbodyExt.

TouchDesigner's app.osVersion reports "10" on Windows 11 (both share NT 10.0).
EmbodyExt._resolveOsLabel disambiguates via the build number so logs and
get_td_info don't mislabel Windows 11 machines as Windows 10.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestOsLabel(EmbodyTestCase):

    @property
    def _resolve(self):
        return self.embody_ext._resolveOsLabel

    # --- Windows 11 disambiguation (the bug this fixes) ---

    def test_windows_11_relabeled_by_build(self):
        # app reports name="Windows", version="10" even on Win 11.
        self.assertEqual(self._resolve('Windows', '10', 22000), 'Windows 11')
        self.assertEqual(self._resolve('Windows', '10', 22631), 'Windows 11')
        self.assertEqual(self._resolve('Windows', '10', 26100), 'Windows 11')

    def test_genuine_windows_10_unchanged(self):
        # Build below the Win 11 threshold stays Windows 10.
        self.assertEqual(self._resolve('Windows', '10', 19045), 'Windows 10')
        self.assertEqual(self._resolve('Windows', '10', 19041), 'Windows 10')

    def test_windows_no_build_probe_falls_back(self):
        # If sys.getwindowsversion() isn't available we can't tell -- leave it.
        self.assertEqual(self._resolve('Windows', '10', None), 'Windows 10')

    def test_already_labeled_11_not_double_processed(self):
        self.assertEqual(self._resolve('Windows', '11', 22631), 'Windows 11')

    # --- Non-Windows passthrough ---

    def test_macos_passthrough(self):
        self.assertEqual(self._resolve('macOS', '14.5', None), 'macOS 14.5')

    def test_empty_version_trimmed(self):
        self.assertEqual(self._resolve('Windows', '', 19045), 'Windows')

    # --- Live helper ---

    def test_os_label_returns_nonempty_string(self):
        label = self.embody_ext._osLabel()
        self.assertTrue(isinstance(label, str) and label)

    def test_os_label_never_says_windows_10_on_win11(self):
        # On whatever OS the suite runs on, the live helper must not produce
        # "Windows 10" when the running build is actually a Windows 11 build.
        import sys
        label = self.embody_ext._osLabel()
        try:
            build = sys.getwindowsversion().build
        except (AttributeError, OSError):
            build = None
        if build is not None and build >= 22000:
            self.assertNotEqual(label, 'Windows 10')
