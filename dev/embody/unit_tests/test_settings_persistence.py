"""
Test suite: .embody/config.json settings persistence.

Regression coverage for issue #18 -- _PERSISTED_PARAMS is a frozenset, so
Python's hash randomization gave each TD process a different iteration order,
producing a different (but valid) JSON ordering on every session.
_saveSettings now sorts keys so the file is byte-stable across runs.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestSettingsPersistence(EmbodyTestCase):

    def _read_config_bytes(self):
        from pathlib import Path
        path = Path(str(self.embody_ext._settingsPath()))
        return path.read_bytes() if path.is_file() else None

    def test_save_settings_is_byte_stable(self):
        """Two consecutive saves must produce identical bytes."""
        self.embody_ext._saveSettings()
        first = self._read_config_bytes()
        self.embody_ext._saveSettings()
        second = self._read_config_bytes()
        self.assertEqual(first, second)

    def test_param_keys_sorted_alphabetically(self):
        """params dict must serialize in sorted order regardless of frozenset iteration."""
        import json
        self.embody_ext._saveSettings()
        data = json.loads(self._read_config_bytes().decode('utf-8'))
        keys = list(data['params'].keys())
        self.assertEqual(keys, sorted(keys))

    def test_top_level_keys_sorted(self):
        """Top-level keys (version, params) must also serialize in sorted order."""
        import json
        self.embody_ext._saveSettings()
        data = json.loads(self._read_config_bytes().decode('utf-8'))
        keys = list(data.keys())
        self.assertEqual(keys, sorted(keys))
