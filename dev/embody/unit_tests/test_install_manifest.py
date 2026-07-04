"""
Tests for the install manifest (.embody/manifest.json) -- the record of Embody's
project footprint that Uninstall/Deinit reverses precisely and safely.

Pure-logic tests over an isolated temp dir. They exercise the recorder helpers
(_manifestRelPath / _manifestRecordCreatedFile / _manifestRecordAppendedFile /
_manifestRecordVenv / _manifestRecordGitConfig) directly, so they never touch
the live project's manifest, any operator, or ext.root state -- NOT destructive.

The core contract: a file that lives UNDER the manifest root records as a POSIX
relative path; anything outside records absolute (so a repo .gitignore records
cleanly even when the project is a subdir). Shared files (.gitignore, .mcp.json)
record as appended blocks/keys so Uninstall reverses only Embody's addition,
never the user's file. Every recorder dedups.
"""

import os
import json
import tempfile
import shutil


class TestInstallManifest(EmbodyTestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='embody_manifest_test_')

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    @property
    def ext(self):
        return self.embody_ext

    def _load(self):
        p = os.path.join(self.d, '.embody', 'manifest.json')
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                return json.load(f)
        return None

    # ----- path model ------------------------------------------------------

    def test_relpath_under_root_is_relative_posix(self):
        rel = self.ext._manifestRelPath(self.d, os.path.join(self.d, 'a', 'b.txt'))
        self.assertEqual(rel, 'a/b.txt')

    def test_relpath_outside_root_is_absolute(self):
        outside = os.path.join(os.path.dirname(self.d), 'OUTSIDE.cfg')
        rel = self.ext._manifestRelPath(self.d, outside)
        self.assertTrue(os.path.isabs(rel),
                        'a path outside the manifest root must stay absolute')

    def test_relpath_accepts_relative_input(self):
        # A path already relative to the root records relative unchanged.
        self.assertEqual(self.ext._manifestRelPath(self.d, 'CLAUDE.md'), 'CLAUDE.md')

    # ----- skeleton / load tolerance --------------------------------------

    def test_fresh_load_returns_full_skeleton(self):
        m = self.ext._loadInstallManifest(self.d)
        for k in ('version', 'files_created', 'files_appended', 'git_config',
                  'venv', 'network_ops'):
            self.assertIn(k, m, f'skeleton missing key {k!r}')

    def test_load_tolerates_partial_manifest(self):
        # An older/partial manifest must load without KeyError and backfill the
        # missing keys from the skeleton (forward-compatible reads).
        os.makedirs(os.path.join(self.d, '.embody'), exist_ok=True)
        with open(os.path.join(self.d, '.embody', 'manifest.json'),
                  'w', encoding='utf-8') as f:
            json.dump({'files_created': ['old.md']}, f)
        m = self.ext._loadInstallManifest(self.d)
        self.assertEqual(m['files_created'], ['old.md'])
        self.assertIn('files_appended', m)
        self.assertIn('git_config', m)

    # ----- created files ---------------------------------------------------

    def test_created_file_recorded_and_deduped(self):
        self.ext._manifestRecordCreatedFile(self.d, 'CLAUDE.md')
        self.ext._manifestRecordCreatedFile(self.d, 'CLAUDE.md')  # dedup
        self.assertEqual(self._load()['files_created'], ['CLAUDE.md'])

    def test_created_file_outside_root_stored_absolute(self):
        outside = os.path.join(os.path.dirname(self.d), 'OUTSIDE.cfg')
        self.ext._manifestRecordCreatedFile(self.d, outside)
        stored = self._load()['files_created'][0]
        self.assertTrue(os.path.isabs(stored),
                        'a created file outside the root records absolute')

    # ----- appended files (block vs json_key) -----------------------------

    def test_appended_block_recorded(self):
        self.ext._manifestRecordAppendedFile(
            self.d, os.path.join(self.d, '.gitignore'), '# Embody / Envoy')
        entry = self._load()['files_appended'][0]
        self.assertEqual(entry['path'], '.gitignore')
        self.assertEqual(entry['kind'], 'block')
        self.assertEqual(entry['marker'], '# Embody / Envoy')

    def test_appended_json_key_recorded(self):
        self.ext._manifestRecordAppendedFile(
            self.d, os.path.join(self.d, '.mcp.json'), 'mcpServers.envoy',
            kind='json_key')
        entry = self._load()['files_appended'][0]
        self.assertEqual(entry['kind'], 'json_key')
        self.assertEqual(entry['marker'], 'mcpServers.envoy')

    def test_appended_deduped_by_path(self):
        g = os.path.join(self.d, '.gitignore')
        self.ext._manifestRecordAppendedFile(self.d, g, '# Embody / Envoy')
        self.ext._manifestRecordAppendedFile(self.d, g, '# Embody / Envoy')
        self.assertEqual(len(self._load()['files_appended']), 1)

    # ----- venv ------------------------------------------------------------

    def test_venv_recorded_once(self):
        v = os.path.join(self.d, '.venv')
        self.ext._manifestRecordVenv(self.d, v)
        self.ext._manifestRecordVenv(self.d, v)  # no overwrite / no dup
        self.assertEqual(self._load()['venv'], {'path': '.venv', 'created': True})

    # ----- git config ------------------------------------------------------

    def test_git_config_list_and_str_deduped(self):
        self.ext._manifestRecordGitConfig(
            self.d, ['diff.tdn.textconv', 'diff.tdn.cachetextconv'])
        self.ext._manifestRecordGitConfig(self.d, 'diff.tdn.textconv')  # str + dedup
        self.assertEqual(self._load()['git_config'],
                         ['diff.tdn.textconv', 'diff.tdn.cachetextconv'])
