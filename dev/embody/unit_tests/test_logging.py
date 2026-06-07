"""
Test suite: Logging system in EmbodyExt.

Tests Log, Debug, Info, Warn, Error methods and ring buffer behavior.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestLogging(EmbodyTestCase):

    # --- Log ---

    def test_log_increments_counter(self):
        initial = self.embody_ext._log_counter
        self.embody_ext.Log('test message', 'INFO')
        self.assertEqual(self.embody_ext._log_counter, initial + 1)

    def test_log_appends_to_buffer(self):
        initial_len = len(self.embody_ext._log_buffer)
        self.embody_ext.Log('buffer test', 'INFO')
        new_len = len(self.embody_ext._log_buffer)
        if initial_len < self.embody_ext._log_buffer.maxlen:
            self.assertEqual(new_len, initial_len + 1)
        else:
            # Buffer is full — length stays at maxlen, newest entry is appended
            self.assertEqual(new_len, self.embody_ext._log_buffer.maxlen)
        self.assertEqual(self.embody_ext._log_buffer[-1]['message'], 'buffer test')

    def test_log_buffer_entry_structure(self):
        self.embody_ext.Log('structure test', 'WARNING')
        entry = self.embody_ext._log_buffer[-1]
        self.assertDictHasKey(entry, 'id')
        self.assertDictHasKey(entry, 'timestamp')
        self.assertDictHasKey(entry, 'level')
        self.assertDictHasKey(entry, 'source')
        self.assertDictHasKey(entry, 'message')
        self.assertEqual(entry['message'], 'structure test')
        self.assertEqual(entry['level'], 'WARNING')

    def test_log_level_stored_correctly(self):
        for level in ['INFO', 'WARNING', 'ERROR', 'SUCCESS', 'DEBUG']:
            self.embody_ext.Log(f'test {level}', level)
            self.assertEqual(self.embody_ext._log_buffer[-1]['level'], level)

    # --- Convenience methods ---

    def test_debug_logs_at_debug_level(self):
        self.embody_ext.Debug('debug msg')
        self.assertEqual(self.embody_ext._log_buffer[-1]['level'], 'DEBUG')

    def test_info_logs_at_info_level(self):
        self.embody_ext.Info('info msg')
        self.assertEqual(self.embody_ext._log_buffer[-1]['level'], 'INFO')

    def test_warn_logs_at_warning_level(self):
        self.embody_ext.Warn('warn msg')
        self.assertEqual(self.embody_ext._log_buffer[-1]['level'], 'WARNING')

    def test_error_logs_at_error_level(self):
        self.embody_ext.Error('error msg')
        self.assertEqual(self.embody_ext._log_buffer[-1]['level'], 'ERROR')

    # --- Ring buffer behavior ---

    def test_buffer_max_size_200(self):
        self.assertEqual(self.embody_ext._log_buffer.maxlen, 200)

    def test_buffer_ids_monotonic(self):
        self.embody_ext.Log('first', 'INFO')
        id1 = self.embody_ext._log_buffer[-1]['id']
        self.embody_ext.Log('second', 'INFO')
        id2 = self.embody_ext._log_buffer[-1]['id']
        self.assertGreater(id2, id1)
