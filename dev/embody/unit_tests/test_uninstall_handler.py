"""
Tests for the interactive Uninstall handler (UninstallHandler ->
embody_admin.uninstall_handler) -- the Uninstall pulse's CONFIRM GATE.

The handler computes the NON-DESTRUCTIVE plan, shows a ui.messageBox describing
what will happen, and runs the destructive Uninstall ONLY when the user picks
'Uninstall'. These tests drive it through the real _messageBox seeding machinery
(_smoke_test_responses keyed by the dialog title) against a THROWAWAY temp dir
via target_dir -- the live project is never touched.

The one live side effect the confirm path has -- uninstall() flipping
Envoyenable off -- is neutralized by setting _restoring_settings (which
short-circuits parexec.onValueChange, so no Envoy.Stop() fires) and restoring
the prior value, so the running Envoy server is never stopped. NOT destructive
to the project.
"""

import os
import json
import tempfile
import shutil

CONFIRM_TITLE = 'Embody -- Uninstall'          # confirm + 'nothing to do' box
DONE_TITLE = 'Embody -- Uninstall Complete'    # post-run summary box


class TestUninstallHandler(EmbodyTestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='embody_uninstall_handler_')
        self.marker = self.embody_ext._EMBODY_MARKER
        self._saved_resp = op.Embody.fetch(
            '_smoke_test_responses', None, search=False)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        if self._saved_resp is not None:
            op.Embody.store('_smoke_test_responses', self._saved_resp)
        else:
            op.Embody.unstore('_smoke_test_responses')

    @property
    def ext(self):
        return self.embody_ext

    # ---- helpers ----------------------------------------------------------

    def _w(self, rel, content):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p) or self.d, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(content)
        return p

    def _exists(self, rel):
        return os.path.exists(os.path.join(self.d, rel))

    def _embody_json(self, name, obj):
        os.makedirs(os.path.join(self.d, '.embody'), exist_ok=True)
        with open(os.path.join(self.d, '.embody', name), 'w',
                  encoding='utf-8') as f:
            json.dump(obj, f)

    def _seed_footprint(self):
        """Real, provably-Embody footprint in the temp dir: a marker-generated
        CLAUDE.md (matching hash -> classified 'delete') plus the .embody dir
        (removed wholesale)."""
        body = self.marker + '\ngenerated'
        self._w('CLAUDE.md', body)
        self._embody_json('generated-hashes.json',
                          {'CLAUDE.md': self.ext._contentHash(body)})

    def _seed(self, mapping):
        op.Embody.store('_smoke_test_responses', mapping)

    # ---- cancel: the gate holds, nothing is removed -----------------------

    def test_cancel_removes_nothing(self):
        self._seed_footprint()
        self._seed({CONFIRM_TITLE: 0})   # Cancel
        result = self.ext.UninstallHandler(target_dir=self.d)
        self.assertFalse(result.get('ran', True),
                         'Cancel must not run the uninstall')
        self.assertEqual(result.get('reason'), 'cancelled')
        self.assertTrue(self._exists('CLAUDE.md'),
                        'Cancel must delete nothing')
        self.assertTrue(self._exists('.embody'))

    def test_suppressed_defers_like_cancel(self):
        # No seed -> _messageBox returns -1 in a test/save context -> the
        # handler treats it as cancel. A save/test must NEVER uninstall silently.
        self._seed_footprint()
        op.Embody.unstore('_smoke_test_responses')
        result = self.ext.UninstallHandler(target_dir=self.d)
        self.assertFalse(result.get('ran', True),
                         'a suppressed confirm must not uninstall')
        self.assertTrue(self._exists('CLAUDE.md'))
        self.assertTrue(self._exists('.embody'))

    # ---- nothing to uninstall --------------------------------------------

    def test_nothing_to_uninstall(self):
        # Empty dir -> no footprint -> early return, destructive path unreached.
        self._seed({CONFIRM_TITLE: 0})
        result = self.ext.UninstallHandler(target_dir=self.d)
        self.assertFalse(result.get('ran', True))
        self.assertEqual(result.get('reason'), 'nothing to uninstall')

    # ---- confirm: proceeds and removes exactly the footprint --------------

    def test_confirm_uninstalls(self):
        self._seed_footprint()
        self._seed({CONFIRM_TITLE: 1, DONE_TITLE: 0})   # Uninstall
        # Seal parexec so uninstall()'s Envoyenable=0 does NOT stop the live
        # server; restore the param value afterwards.
        prev_env = op.Embody.par.Envoyenable.eval()
        prev_restoring = getattr(self.ext, '_restoring_settings', False)
        self.ext._restoring_settings = True
        try:
            result = self.ext.UninstallHandler(target_dir=self.d)
        finally:
            op.Embody.par.Envoyenable = prev_env
            self.ext._restoring_settings = prev_restoring
        self.assertTrue(result.get('ran', False),
                        'confirm must run the uninstall')
        self.assertGreaterEqual(result.get('deleted', 0), 1)
        self.assertFalse(self._exists('CLAUDE.md'),
                         'confirm must remove the generated config file')
        self.assertFalse(self._exists('.embody'),
                         'confirm must remove the .embody state dir')

    def test_confirm_keeps_edited_file_via_review(self):
        # A generated file the user EDITED (hash mismatch) is classified
        # 'review' -> kept even on confirm (include_review defaults False).
        self._w('CLAUDE.md', self.marker + '\nEDITED by user')
        self._embody_json(
            'generated-hashes.json',
            {'CLAUDE.md': self.ext._contentHash(self.marker + '\norig')})
        self._seed({CONFIRM_TITLE: 1, DONE_TITLE: 0})
        prev_env = op.Embody.par.Envoyenable.eval()
        prev_restoring = getattr(self.ext, '_restoring_settings', False)
        self.ext._restoring_settings = True
        try:
            result = self.ext.UninstallHandler(target_dir=self.d)
        finally:
            op.Embody.par.Envoyenable = prev_env
            self.ext._restoring_settings = prev_restoring
        self.assertTrue(result.get('ran', False))
        self.assertTrue(self._exists('CLAUDE.md'),
                        'an edited generated file must survive confirm')
        self.assertGreaterEqual(result.get('kept_review', 0), 1)
