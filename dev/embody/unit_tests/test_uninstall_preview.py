"""
Tests for the NON-DESTRUCTIVE Uninstall preview (_computeUninstallPlan).

Fabricates a fake Embody install in a temp dir (marker files, a hash manifest,
an install manifest, git files, .mcp.json, a venv dir) and asserts the planner
classifies each correctly WITHOUT deleting anything. The executor will consume
this same plan, so these assertions pin the reversal contract:

  - Embody-generated + unmodified       -> delete
  - marker present but edited            -> review (KEPT)
  - no marker                            -> ignored (the user's own file)
  - shared file (.gitignore / .mcp.json) -> strip only Embody's block/key

Pure-logic over an isolated temp dir -- touches no operators, NOT destructive.
"""

import os
import json
import tempfile
import shutil


class TestUninstallPreview(EmbodyTestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='embody_uninstall_test_')
        self.marker = self.embody_ext._EMBODY_MARKER

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

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

    def _embody_json(self, name, obj):
        os.makedirs(os.path.join(self.d, '.embody'), exist_ok=True)
        with open(os.path.join(self.d, '.embody', name), 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def _plan(self):
        return self.ext._computeUninstallPlan(self.d)

    def _paths(self, bucket):
        return [e['path'] for e in self._plan()[bucket]]

    # ---- classification ---------------------------------------------------

    def test_generated_unmodified_is_delete(self):
        body = self.marker + '\ngenerated body'
        self._w('CLAUDE.md', body)
        self._embody_json('generated-hashes.json',
                          {'CLAUDE.md': self.ext._contentHash(body)})
        self.assertIn('CLAUDE.md', self._paths('delete'))

    def test_generated_edited_is_review_not_delete(self):
        self._w('CLAUDE.md', self.marker + '\nEDITED by the user')
        self._embody_json('generated-hashes.json',
                          {'CLAUDE.md': self.ext._contentHash(self.marker + '\noriginal')})
        self.assertIn('CLAUDE.md', self._paths('review'))
        self.assertNotIn('CLAUDE.md', self._paths('delete'))

    def test_no_marker_file_is_ignored(self):
        self._w('CLAUDE.md', 'the user wrote this -- no Embody marker')
        allp = self._paths('delete') + self._paths('review')
        self.assertNotIn('CLAUDE.md', allp,
                         'a non-marker file must never be touched')

    def test_gitignore_block_is_strip(self):
        self._w('.gitignore', 'node_modules/\n# Embody / Envoy\n.venv/\n.mcp.json\n')
        strip = {e['path']: e for e in self._plan()['strip']}
        self.assertIn('.gitignore', strip)
        self.assertEqual(strip['.gitignore']['kind'], 'block')

    def test_mcp_json_is_strip_json_key(self):
        self._w('.mcp.json', json.dumps(
            {'mcpServers': {'envoy': {'type': 'stdio'}, 'other': {'x': 1}}}))
        strip = {e['path']: e for e in self._plan()['strip']}
        self.assertIn('.mcp.json', strip)
        self.assertEqual(strip['.mcp.json']['kind'], 'json_key')

    def test_manifest_venv_is_delete(self):
        os.makedirs(os.path.join(self.d, '.venv'), exist_ok=True)
        self._embody_json('manifest.json',
                          {'version': 1, 'files_created': [], 'files_appended': [],
                           'git_config': [], 'network_ops': [],
                           'venv': {'path': '.venv', 'created': True}})
        self.assertIn('.venv', self._paths('delete'))

    def test_manifest_git_config_is_unset(self):
        self._embody_json('manifest.json',
                          {'version': 1, 'files_created': [], 'files_appended': [],
                           'git_config': ['diff.tdn.textconv'], 'venv': None,
                           'network_ops': []})
        self.assertIn('diff.tdn.textconv', self._plan()['unset'])

    def test_embody_dir_is_delete(self):
        os.makedirs(os.path.join(self.d, '.embody'), exist_ok=True)
        self.assertIn('.embody', self._paths('delete'))

    def test_manifest_missing_created_file_reported(self):
        self._embody_json('manifest.json',
                          {'version': 1, 'files_created': ['GONE.md'],
                           'files_appended': [], 'git_config': [], 'venv': None,
                           'network_ops': []})
        self.assertIn('GONE.md', self._plan()['missing'])

    # ---- the whole point: it never deletes -------------------------------

    def test_preview_is_non_destructive(self):
        body = self.marker + '\nx'
        self._w('CLAUDE.md', body)
        self._embody_json('generated-hashes.json',
                          {'CLAUDE.md': self.ext._contentHash(body)})
        self.ext._computeUninstallPlan(self.d)
        self.ext.PreviewUninstall(self.d)
        self.assertTrue(os.path.exists(os.path.join(self.d, 'CLAUDE.md')),
                        'preview must leave every file on disk')
