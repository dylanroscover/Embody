"""
Test suite: the TD pre-cook venv context (TDPyEnvManagerContext.yaml).

Pure Python (TD-import-free): loads embody_pyenv.py and embody_git.py by
file path relative to this file, so the SAME tests run under plain pytest
(the windows+macos CI matrix -- the macos leg is the only routine signal
the posix path-joining gets) and, nominally, inside TouchDesigner.

What this pins:
- classify_td_context reproduces the shipped helper's resolution order --
  pyproject section and legacy JSON win as FOREIGN, envName with a
  separator is a path of its own, relative installPath resolves against
  the context dir -- so Embody never rewrites a context it does not own
  (the node-pa-td hands-off contract);
- our own render round-trips as 'ours'/'ok', and survives TD's yaml
  safe_dump rewrite after a link (extra keys, unquoted plain scalars);
- pythonVersion drift asks for 'refresh', never a silent stale file;
- remove_td_context_if_ours deletes only Embody's file;
- detect_tdpyenvmanager's 'ours_unlinked' branch (our context present but
  TD did not link it) warns and points at TDPyEnvManagerHelper, without
  regressing the pre-existing 'different_env' branch;
- the two embody_git footprint helpers the ensure-step leans on:
  manifest_unrecord_created_file (a self-removal must not leave a
  tombstone that blocks the rewrite) and ensure_gitignore_entry
  (idempotent, hand-added lines count, managed block is the landing
  spot).
"""

import importlib.util
import json
import os
import sys
import tempfile
import shutil
import types

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# PYTEST-ONLY SUITE (2026-08-20). Same rationale as test_embody_pyenv.py:
# everything here is pure file I/O by contract, so in-TD execution
# duplicates the pytest/CI coverage exactly while running inside a process
# this box's instability cluster keeps killing. Inside TD every test SKIPS
# loudly; the windows+macos CI matrix and local pytest are its real home.
_IN_TD = 'td' in sys.modules

_EMBODY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Embody')


def _load(mod_name, filename):
    path = os.path.join(_EMBODY_DIR, filename)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pyenv = _load('embody_pyenv_under_test', 'embody_pyenv.py')
# embody_git.py has no module-level TD access (verified: stdlib imports
# only; every function takes `ext` first), so it loads off-TD too.
egit = _load('embody_git_under_test', 'embody_git.py')

# TD's helper rewrites the context with yaml.safe_dump after it links:
# extra keys appear and our quoted scalars come back plain. Still ours.
_TD_REWRITTEN = (
    'contextVersion: 2\n'
    'createdByVersion: 1.0.0\n'
    'active: true\n'
    'mode: Python vEnv\n'
    'envName: .venv\n'
    'installPath: .\n'
    "pythonVersion: '3.11'\n"
    'autoSetup: false\n'
    'autoSetupReqs: []\n'
    'autoSetupSyncReqs: false\n'
    'extraPaths: []\n'
)


class _ContextCase(EmbodyTestCase):
    """Shared tmp-root fixture + a spec whose paths live under it."""

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only '
                          '(in-TD execution adds no coverage)')
        self.root = tempfile.mkdtemp(prefix='embody_pyenv_ctx_test_')
        self.spec = pyenv.venv_paths(self.root, '2.0.0')
        self.venv_dir = self.spec['venv_dir']

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def _write_ctx(self, text, where=None, name=None):
        path = os.path.join(where or self.root,
                            name or pyenv.TD_CONTEXT_FILENAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        return path


class TestClassifyTdContext(_ContextCase):

    def test_filename_constant(self):
        self.assertEqual(pyenv.TD_CONTEXT_FILENAME,
                         'TDPyEnvManagerContext.yaml')

    def test_empty_dir_is_absent(self):
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'absent')
        self.assertEqual(
            pyenv.td_context_status(self.root, self.venv_dir, '3.11'),
            'absent')

    def test_our_own_render_round_trips(self):
        pyenv.write_td_context(self.root, '3.11')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'ours')
        self.assertEqual(
            pyenv.td_context_status(self.root, self.venv_dir, '3.11'), 'ok')

    def test_td_normalized_rewrite_still_ours(self):
        """The link REWRITES the file -- if that rewrite read as foreign,
        Embody would stop maintaining its own context after one session."""
        self._write_ctx(_TD_REWRITTEN)
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'ours')
        self.assertEqual(
            pyenv.td_context_status(self.root, self.venv_dir, '3.11'), 'ok')

    def test_python_version_drift_asks_for_refresh(self):
        pyenv.write_td_context(self.root, '3.11')
        self.assertEqual(
            pyenv.td_context_status(self.root, self.venv_dir, '3.12'),
            'refresh')

    def test_other_env_name_is_foreign(self):
        self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                        'envName: other_env\ninstallPath: .\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'foreign')
        self.assertEqual(
            pyenv.td_context_status(self.root, self.venv_dir, '3.11'),
            'foreign')

    def test_conda_mode_is_foreign(self):
        self._write_ctx('contextVersion: 2\nmode: Conda Env\n'
                        'envName: .venv\ninstallPath: .\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'foreign')

    def test_install_path_redirect_resolves_to_venv(self):
        """Relative installPath resolves against the CONTEXT dir (TD's cwd
        at load), so a redirect landing on our venv is still ours."""
        self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                        'envName: .venv\ninstallPath: sub\n')
        venv = os.path.join(self.root, 'sub', '.venv')
        self.assertEqual(pyenv.classify_td_context(self.root, venv), 'ours')

    def test_env_name_with_separator_ignores_install_path(self):
        """envName carrying a separator is a path of its own -- it must NOT
        be joined under installPath (that misread would claim a foreign
        env as ours)."""
        self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                        'envName: envs/mine\ninstallPath: .\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'foreign')

    def test_legacy_json_mirrors_helper_precedence(self):
        """The helper reads/migrates the legacy JSON ONLY when no yaml
        exists (postInit:106) -- a stray JSON beside Embody's yaml must
        not orphan the live context (review 2026-08-20), while a JSON
        alone stays hands-off."""
        self._write_ctx('{"mode": "Python vEnv", "envName": ".venv"}',
                        name='TDPyEnvManagerContext.json')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'foreign')
        pyenv.write_td_context(self.root, '3.11')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'ours')

    def test_pyproject_section_wins_precedence(self):
        pyenv.write_td_context(self.root, '3.11')
        pj = os.path.join(self.root, 'pyproject.toml')
        with open(pj, 'w', encoding='utf-8') as f:
            f.write('[project]\nname = "x"\n\n'
                    '[tool.touchdesigner.TDPyEnvManagerContext]\n'
                    'mode = "Python vEnv"\nenvName = ".venv"\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'foreign')

    def test_pyproject_without_section_is_harmless(self):
        pyenv.write_td_context(self.root, '3.11')
        pj = os.path.join(self.root, 'pyproject.toml')
        with open(pj, 'w', encoding='utf-8') as f:
            f.write('[project]\nname = "x"\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'ours')

    def test_pyproject_inline_table_spelling_is_foreign(self):
        """tomllib (what the helper LOADS with) accepts the inline-table
        spelling the section-header regex misses -- Embody must go
        hands-off on it too (review 2026-08-20)."""
        pyenv.write_td_context(self.root, '3.11')
        pj = os.path.join(self.root, 'pyproject.toml')
        with open(pj, 'w', encoding='utf-8') as f:
            f.write('[tool.touchdesigner]\n'
                    'TDPyEnvManagerContext = { envName = "otherenv" }\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'foreign')

    def test_null_mode_stays_ours(self):
        """TD's write-back can emit 'mode: null' (AsContext does not
        filter Nones for yaml); linkEnv treats any non-Conda mode as
        vEnv, so Embody must not orphan such a file."""
        self._write_ctx('contextVersion: 2\nmode: null\n'
                        'envName: .venv\ninstallPath: .\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'ours')

    def test_quoted_scalars_parse(self):
        self._write_ctx('contextVersion: 2\nmode: \'Python vEnv\'\n'
                        'envName: ".venv"\ninstallPath: "."\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'ours')

    def test_absolute_env_name_matching_venv_is_ours(self):
        self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                        f'envName: {self.venv_dir}\ninstallPath: .\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'ours')


    def test_nested_key_cannot_shadow_top_level(self):
        """Column-0 anchor regression (review 2026-08-20): an indented
        envName under a nested mapping flipped a FOREIGN context to ours,
        which would have let remove_td_context_if_ours delete it."""
        self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                        'extraPaths:\n'
                        '  - envName: .venv\n'
                        '  - installPath: .\n'
                        'envName: /somewhere/else/prod_env\n')
        self.assertEqual(
            pyenv.classify_td_context(self.root, self.venv_dir), 'foreign')
        self.assertFalse(
            pyenv.remove_td_context_if_ours(self.root, self.venv_dir))


class TestRemoveTdContext(_ContextCase):

    def test_removes_ours(self):
        path = pyenv.write_td_context(self.root, '3.11')
        self.assertTrue(
            pyenv.remove_td_context_if_ours(self.root, self.venv_dir))
        self.assertFalse(os.path.isfile(path))

    def test_foreign_untouched(self):
        path = self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                               'envName: other_env\ninstallPath: .\n')
        self.assertFalse(
            pyenv.remove_td_context_if_ours(self.root, self.venv_dir))
        self.assertTrue(os.path.isfile(path))

    def test_absent_is_false(self):
        self.assertFalse(
            pyenv.remove_td_context_if_ours(self.root, self.venv_dir))


class TestDetectIntegration(_ContextCase):

    def test_ours_unlinked_warns_about_helper(self):
        """Our context present but TD never linked it: the pre-cook wiring
        silently did nothing, so the notice must name the helper log."""
        pyenv.write_td_context(self.root, '3.11')
        finding = pyenv.detect_tdpyenvmanager(self.root, self.venv_dir)
        self.assertTrue(finding['present'])
        self.assertEqual(finding['kind'], 'ours_unlinked')
        level, msg = pyenv.tdpyenvmanager_notice(finding)
        self.assertEqual(level, 'WARNING')
        self.assertIn('TDPyEnvManagerHelper', msg)

    def test_foreign_context_stays_different_env(self):
        """Regression guard: 'ours_unlinked' must not swallow the
        pre-existing unlinked-foreign branch."""
        self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                        'envName: other_env\ninstallPath: .\n')
        finding = pyenv.detect_tdpyenvmanager(self.root, self.venv_dir)
        self.assertEqual(finding['kind'], 'different_env')
        level, _msg = pyenv.tdpyenvmanager_notice(finding)
        self.assertEqual(level, 'WARNING')


    def test_ours_linked_is_silent(self):
        """The healthy expected state (Embody's context linked by TD) must
        not log a tdPyEnvManager attribution every session (review
        2026-08-20); a user-authored .venv link keeps its INFO."""
        pyenv.write_td_context(self.root, '3.11')
        helper = types.SimpleNamespace(HasLinkedCustomEnv=True,
                                       envPath=self.venv_dir)
        app_obj = types.SimpleNamespace(pyEnvHelper=helper)
        finding = pyenv.detect_tdpyenvmanager(self.root, self.venv_dir,
                                              app_obj)
        self.assertEqual(finding['kind'], 'ours_linked')
        self.assertIsNone(pyenv.tdpyenvmanager_notice(finding))

    def test_coexisting_foreign_context_beats_ours_unlinked(self):
        """A foreign context anywhere in scope must keep the loud
        different_env warning -- ours_unlinked only when EVERY context
        found is Embody's own (review 2026-08-20)."""
        sub = os.path.join(self.root, 'dev')
        os.makedirs(sub, exist_ok=True)
        sub_spec = pyenv.venv_paths(sub, '2.0.0')
        pyenv.write_td_context(sub, '3.11')  # ours, in project.folder
        self._write_ctx('contextVersion: 2\nmode: Python vEnv\n'
                        'envName: C:/prod/other_env\n')  # foreign, at root
        finding = pyenv.detect_tdpyenvmanager(
            self.root, sub_spec['venv_dir'], extra_dirs=[sub])
        self.assertEqual(finding['kind'], 'different_env')


class TestRenderTdContext(_ContextCase):

    def test_render_shape(self):
        text = pyenv.render_td_context('3.11')
        self.assertEqual(text.splitlines(), [
            'contextVersion: 2',
            'mode: Python vEnv',
            'envName: .venv',
            'installPath: .',
            "pythonVersion: '3.11'",
            'autoSetup: false',
        ])
        self.assertEndsWith(text, '\n')

    def test_render_is_pure_ascii(self):
        text = pyenv.render_td_context('3.11')
        text.encode('ascii')  # raises on any non-ASCII glyph

    def test_refresh_preserves_td_written_keys(self):
        """A pythonVersion bump must not clobber keys TD or the palette
        wrote back (extraPaths, autoSetupReqs, active) -- surgical line
        update only (review 2026-08-20)."""
        path = self._write_ctx(_TD_REWRITTEN.replace(
            'extraPaths: []', 'extraPaths:\n- scripts/shared'))
        pyenv.refresh_td_context(self.root, '3.12')
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        self.assertIn("pythonVersion: '3.12'", text)
        self.assertNotIn("pythonVersion: '3.11'", text)
        self.assertIn('- scripts/shared', text)
        self.assertIn('createdByVersion: 1.0.0', text)
        self.assertEqual(
            pyenv.td_context_status(self.root, self.venv_dir, '3.12'), 'ok')

    def test_written_file_uses_lf_newlines(self):
        """TD reads this on every platform; CRLF would ride into the
        helper's parse and into git diffs on the shared repo."""
        path = pyenv.write_td_context(self.root, '3.11')
        with open(path, 'rb') as f:
            raw = f.read()
        self.assertNotIn(b'\r', raw)
        self.assertEqual(raw.decode('utf-8'), pyenv.render_td_context('3.11'))


class TestGitFootprintHelpers(_ContextCase):
    """embody_git helpers take `ext` first -- a SimpleNamespace stub covers
    everything load/save_install_manifest and ensure_gitignore_entry
    reach for (ext.Log + the _INSTALL_MANIFEST class attr)."""

    def setUp(self):
        super().setUp()
        self.ext = types.SimpleNamespace(
            Log=lambda *a, **k: None,
            _INSTALL_MANIFEST='.embody/manifest.json')

    def _created(self):
        return egit.load_install_manifest(self.ext, self.root)['files_created']

    def test_manifest_record_unrecord_roundtrip(self):
        path = os.path.join(self.root, 'TDPyEnvManagerContext.yaml')
        egit.manifest_record_created_file(self.ext, self.root, path)
        self.assertIn('TDPyEnvManagerContext.yaml', self._created())
        egit.manifest_unrecord_created_file(self.ext, self.root, path)
        self.assertNotIn('TDPyEnvManagerContext.yaml', self._created())

    def test_unrecord_unknown_path_is_noop(self):
        egit.manifest_record_created_file(
            self.ext, self.root, os.path.join(self.root, 'kept.txt'))
        egit.manifest_unrecord_created_file(
            self.ext, self.root, os.path.join(self.root, 'never-recorded'))
        self.assertEqual(self._created(), ['kept.txt'])

    def _gi(self):
        return os.path.join(self.root, '.gitignore')

    def test_creates_gitignore_with_managed_header(self):
        self.assertTrue(egit.ensure_gitignore_entry(
            self.ext, self.root, 'TDPyEnvManagerContext.yaml'))
        with open(self._gi(), 'r', encoding='utf-8') as f:
            text = f.read()
        self.assertIn('# Embody / Envoy (auto-managed)', text)
        self.assertIn('TDPyEnvManagerContext.yaml', text)

    def test_second_call_is_a_no_op(self):
        egit.ensure_gitignore_entry(self.ext, self.root,
                                    'TDPyEnvManagerContext.yaml')
        with open(self._gi(), 'rb') as f:
            before = f.read()
        self.assertFalse(egit.ensure_gitignore_entry(
            self.ext, self.root, 'TDPyEnvManagerContext.yaml'))
        with open(self._gi(), 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_hand_added_line_anywhere_counts(self):
        """A user who already ignored it must not get a duplicate in a
        managed block they never asked for."""
        with open(self._gi(), 'w', encoding='utf-8', newline='\n') as f:
            f.write('# my stuff\nnode_modules/\n\n'
                    'TDPyEnvManagerContext.yaml\n')
        with open(self._gi(), 'rb') as f:
            before = f.read()
        self.assertFalse(egit.ensure_gitignore_entry(
            self.ext, self.root, 'TDPyEnvManagerContext.yaml'))
        with open(self._gi(), 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_unanchored_line_covers_anchored_entry(self):
        """Production writes the ANCHORED form ('/x' or 'dev/x'); a
        hand-added unanchored basename already ignores the file at every
        level and must count as coverage (review 2026-08-20)."""
        with open(self._gi(), 'w', encoding='utf-8', newline='\n') as f:
            f.write('# my stuff\nTDPyEnvManagerContext.yaml\n')
        with open(self._gi(), 'rb') as f:
            before = f.read()
        for entry in ('/TDPyEnvManagerContext.yaml',
                      'dev/TDPyEnvManagerContext.yaml'):
            self.assertFalse(
                egit.ensure_gitignore_entry(self.ext, self.root, entry))
        with open(self._gi(), 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_write_keeps_lf_newlines(self):
        """Windows regression (review 2026-08-20): write_text without
        newline='\\n' rewrote the user's whole .gitignore as CRLF."""
        with open(self._gi(), 'w', encoding='utf-8', newline='\n') as f:
            f.write('# mine\nnode_modules/\n')
        self.assertTrue(egit.ensure_gitignore_entry(
            self.ext, self.root, '/TDPyEnvManagerContext.yaml'))
        with open(self._gi(), 'rb') as f:
            self.assertNotIn(b'\r', f.read())

    def test_entry_lands_inside_existing_managed_block(self):
        with open(self._gi(), 'w', encoding='utf-8', newline='\n') as f:
            f.write('# Embody / Envoy (auto-managed)\n.venv/\n'
                    '.embody/local.json\n\n# my stuff\nnode_modules/\n')
        self.assertTrue(egit.ensure_gitignore_entry(
            self.ext, self.root, 'TDPyEnvManagerContext.yaml'))
        with open(self._gi(), 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, [
            '# Embody / Envoy (auto-managed)',
            '.venv/',
            '.embody/local.json',
            'TDPyEnvManagerContext.yaml',
            '',
            '# my stuff',
            'node_modules/',
        ])
