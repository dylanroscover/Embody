"""
Tests for the DESTRUCTIVE Uninstall executor (_executeUninstallPlan + helpers).

Everything runs against a throwaway temp dir -- it NEVER touches the live
project, ext.root, or any operator, so this suite is not destructive to the
project (no DESTRUCTIVE flag needed). It proves the executor removes exactly
Embody's footprint and preserves user data: edited generated files, non-marker
files, other MCP servers, and the user's own .gitignore content.
"""

import os
import json
import tempfile
import shutil


class TestUninstallExecute(EmbodyTestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='embody_uninstall_exec_')
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

    def _exists(self, rel):
        return os.path.exists(os.path.join(self.d, rel))

    def _read(self, rel):
        with open(os.path.join(self.d, rel), encoding='utf-8') as f:
            return f.read()

    def _embody_json(self, name, obj):
        os.makedirs(os.path.join(self.d, '.embody'), exist_ok=True)
        with open(os.path.join(self.d, '.embody', name), 'w', encoding='utf-8') as f:
            json.dump(obj, f)

    def _run(self, include_review=False):
        plan = self.ext._computeUninstallPlan(self.d)
        return self.ext._executeUninstallPlan(plan, include_review=include_review)

    # ---- deletes: Embody's own files go ----------------------------------

    def test_generated_file_deleted(self):
        body = self.marker + '\ngenerated'
        self._w('CLAUDE.md', body)
        self._embody_json('generated-hashes.json',
                          {'CLAUDE.md': self.ext._contentHash(body)})
        self._run()
        self.assertFalse(self._exists('CLAUDE.md'))

    def test_embody_dir_removed(self):
        self._embody_json('manifest.json', {'version': 1})   # creates .embody/
        self._w('.embody/envoy.json', '{}')
        self._run()
        self.assertFalse(self._exists('.embody'))

    def test_recorded_venv_removed(self):
        os.makedirs(os.path.join(self.d, '.venv', 'bin'), exist_ok=True)
        self._w('.venv/bin/python', '#!/bin/sh\n')
        self._embody_json('manifest.json',
                          {'version': 1, 'files_created': [], 'files_appended': [],
                           'git_config': [], 'network_ops': [],
                           'venv': {'path': '.venv', 'created': True}})
        self._run()
        self.assertFalse(self._exists('.venv'))

    # ---- user data is preserved ------------------------------------------

    def test_edited_generated_file_kept(self):
        self._w('CLAUDE.md', self.marker + '\nEDITED by user')
        self._embody_json('generated-hashes.json',
                          {'CLAUDE.md': self.ext._contentHash(self.marker + '\norig')})
        self._run(include_review=False)
        self.assertTrue(self._exists('CLAUDE.md'),
                        'an edited generated file must survive uninstall')

    def test_non_marker_file_kept(self):
        self._w('CLAUDE.md', 'the user wrote this, no marker')
        self._run()
        self.assertTrue(self._exists('CLAUDE.md'))

    def test_unrecorded_venv_kept_by_default_removed_with_review(self):
        os.makedirs(os.path.join(self.d, '.venv'), exist_ok=True)  # no manifest -> review
        self._run(include_review=False)
        self.assertTrue(self._exists('.venv'), 'unrecorded venv kept by default')
        self._run(include_review=True)
        self.assertFalse(self._exists('.venv'), 'removed only when review included')

    # ---- strips: only Embody's part is reversed --------------------------

    def test_gitignore_block_stripped_user_content_kept(self):
        self._w('.gitignore',
                'node_modules/\ndist/\n\n# Embody / Envoy (auto-managed)\n'
                '.venv/\n.mcp.json\n.embody/*\n')
        self._run()
        txt = self._read('.gitignore')
        self.assertIn('node_modules/', txt)
        self.assertIn('dist/', txt)
        self.assertNotIn('# Embody / Envoy', txt)
        self.assertNotIn('.embody/*', txt)

    def test_mcp_envoy_removed_other_server_kept(self):
        self._w('.mcp.json', json.dumps(
            {'mcpServers': {'envoy': {'type': 'stdio'}, 'other': {'url': 'x'}}}))
        self._run()
        self.assertTrue(self._exists('.mcp.json'), 'file kept: other server remains')
        cfg = json.loads(self._read('.mcp.json'))
        self.assertNotIn('envoy', cfg['mcpServers'])
        self.assertIn('other', cfg['mcpServers'])

    def test_mcp_file_deleted_when_only_envoy(self):
        self._w('.mcp.json', json.dumps({'mcpServers': {'envoy': {'type': 'stdio'}}}))
        self._run()
        self.assertFalse(self._exists('.mcp.json'),
                         'a .mcp.json holding only Embody\'s server is removed')

    # ---- guard: never remove outside the root ----------------------------

    def test_removeTreeWithin_refuses_outside_root(self):
        outside = tempfile.mkdtemp(prefix='embody_outside_')
        try:
            with open(os.path.join(outside, 'keep.txt'), 'w') as f:
                f.write('x')
            removed = self.ext._removeTreeWithin(outside, self.d)
            self.assertEqual(removed, 0)
            self.assertTrue(os.path.exists(os.path.join(outside, 'keep.txt')),
                            'must refuse to remove a path outside the root')
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    # ---- strip-block helper directly -------------------------------------

    def test_strip_marked_block_helper(self):
        text = ('*.py text eol=lf\n\n# Embody / Envoy -- normalize (auto-managed)\n'
                '*.tdn text eol=lf diff=tdn\n*.toe binary\n')
        out = self.ext._stripMarkedBlock(text, 'Embody / Envoy')
        self.assertIn('*.py text eol=lf', out)
        self.assertNotIn('Embody / Envoy', out)
        self.assertNotIn('*.toe binary', out)
