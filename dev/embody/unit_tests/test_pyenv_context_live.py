"""
Test suite: EmbodyExt._ensurePyEnvContext -- the LIVE gating state machine
around the TD pre-cook venv context (TDPyEnvManagerContext.yaml).

The pure-Python half (render/classify/status/remove + the embody_git
footprint helpers) is pinned off-TD by test_pyenv_context.py. This suite is
the other half: the EXTENSION's decision table, driven through the real
instance, the real manifest recorder and the real _guardFileWrite chokepoint.
It therefore needs a live TouchDesigner (INVERTED skip guard vs its
pure-Python siblings: they skip in TD, this one skips under pytest).

What this pins:
- healthy venv + no context -> write + a files_created manifest record, and
  NO .gitignore unless the root is a git repo;
- a USER deletion is a tombstone (manifest record + absent file) that only
  force=True (InitEnvoy) overrides;
- a FOREIGN context is never touched, by either call shape, and never
  recorded;
- an unhealthy venv REMOVES our context and UN-records it (so the rewrite
  after the next install is not read as a user deletion);
- 'ok' is a true no-op (mtime untouched) and 'refresh' updates pythonVersion
  in place, preserving TD/palette-written keys;
- the never-raises contract of the outer wrapper;
- the git-root branch writes an ANCHORED ignore entry, LF-only, in the
  managed block.

ISOLATION: every filesystem effect is redirected into a throwaway temp root
by monkeypatching _venvPaths / _findProjectRoot ON THE INSTANCE, and
_guardFileWrite is replaced by a recording pass-through so no Advanced-mode
dialog can ever pop mid-run. The real project.folder, .embody/manifest.json
and .gitignore are never read or written. NOT destructive.
"""

import json
import os
import shutil
import sys
import tempfile

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# INVERTED guard (2026-08-20): this suite drives the LIVE extension, so TD
# is the only place it means anything. Under plain pytest it skips loudly.
_IN_TD = 'td' in sys.modules


class TestEnsurePyEnvContextLive(EmbodyTestCase):

    # ----- fixture ---------------------------------------------------------

    def setUp(self):
        super().setUp()
        if not _IN_TD:
            self.skipTest('live-extension suite -- runs inside TouchDesigner '
                          'only (the pure half is test_pyenv_context.py)')
        if getattr(app, 'pyEnvHelper', None) is None:
            self.skipTest('this TD build has no app.pyEnvHelper -- '
                          '_ensurePyEnvContext is a deliberate no-op here')
        self.pyenv = self.embody.op('embody_pyenv').module
        self.egit = self.embody.op('embody_git').module
        # realpath: manifest_rel_path resolve()s its inputs, so a symlinked
        # or 8.3 temp dir would record ABSOLUTE and break every lookup.
        self.root = os.path.realpath(
            tempfile.mkdtemp(prefix='embody_pyenv_ctx_live_'))
        self.spec = self.pyenv.venv_paths(self.root,
                                          self.ext._MCP_MIN_VERSION)
        self.ctx_path = os.path.join(self.root,
                                     self.pyenv.TD_CONTEXT_FILENAME)
        self.guard_calls = []
        self._patched = {}
        self._patch('_venvPaths', lambda: self.spec)
        self._patch('_findProjectRoot', lambda: self.root)
        self._patch('_guardFileWrite', self._recordingGuard)

    def tearDown(self):
        ext = self.ext
        for name, (had, prior) in self._patched.items():
            try:
                if had:
                    setattr(ext, name, prior)
                else:
                    delattr(ext, name)
            except Exception:
                pass
        self._patched = {}
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    @property
    def ext(self):
        return self.embody_ext

    def _patch(self, name, fn):
        """Instance-level seam swap; tearDown restores/removes it. Records
        whether the instance had its OWN attribute (vs the class's bound
        method) so a restore never leaves a stale bound method behind."""
        ext = self.ext
        if name not in self._patched:   # re-patching must not capture a patch
            self._patched[name] = (name in vars(ext), vars(ext).get(name))
        setattr(ext, name, fn)

    def _recordingGuard(self, category, action, details, apply_fn, mode=None):
        """Pass-through stand-in for _guardFileWrite: records the call and
        applies, so an Advanced-mode modal can never pop during a run."""
        self.guard_calls.append((category, action, list(details or [])))
        apply_fn()
        return True

    # ----- helpers ---------------------------------------------------------

    def _make_healthy_venv(self):
        """The on-disk shape environment_needs_install calls healthy (recipe
        shared with test_embody_pyenv._make_healthy_venv)."""
        spec = self.spec
        sp = spec['site_packages']
        os.makedirs(os.path.join(sp, 'mcp'), exist_ok=True)
        os.makedirs(os.path.join(sp, 'yaml'), exist_ok=True)
        os.makedirs(
            os.path.join(sp, f'mcp-{spec["mcp_min_version"]}.dist-info'),
            exist_ok=True)
        os.makedirs(os.path.join(sp, 'attrs-24.2.0.dist-info'), exist_ok=True)
        with open(os.path.join(spec['venv_dir'], 'pyvenv.cfg'), 'w',
                  encoding='utf-8') as f:
            f.write(f'version_info = {spec["python_tag"]}.0\n')
        with open(spec['stamp_path'], 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'deps': sorted(spec['deps']),
                       'python': spec['python_tag'],
                       'machine': spec['machine'],
                       'arch': spec['machine']}, f)
        self.assertFalse(
            self.pyenv.environment_needs_install(spec),
            'fixture rot: the fabricated venv must read HEALTHY')

    def _write_ctx(self, text):
        with open(self.ctx_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    def _read_ctx(self):
        with open(self.ctx_path, 'r', encoding='utf-8') as f:
            return f.read()

    def _created(self):
        return self.egit.load_install_manifest(
            self.ext, self.root).get('files_created', [])

    @property
    def _rel(self):
        return self.pyenv.TD_CONTEXT_FILENAME

    def _ours_text(self, python_version, extra=''):
        return ('contextVersion: 2\n'
                'mode: Python vEnv\n'
                'envName: .venv\n'
                'installPath: .\n'
                f"pythonVersion: '{python_version}'\n"
                'autoSetup: false\n' + extra)

    # ----- 1. write + record ----------------------------------------------

    def test_healthy_no_context_writes_and_records(self):
        self._make_healthy_venv()
        self.ext._ensurePyEnvContext()
        self.assertTrue(os.path.isfile(self.ctx_path),
                        'a healthy venv with no context must get one written')
        self.assertEqual(
            self._read_ctx(),
            self.pyenv.render_td_context(self.spec['python_tag']),
            'the written context must be exactly render_td_context output')
        self.assertIn(self._rel, self._created(),
                      'the write must record in the install manifest')
        self.assertEqual(len(self.guard_calls), 1,
                         'the write must go through _guardFileWrite')
        self.assertFalse(
            os.path.exists(os.path.join(self.root, '.gitignore')),
            'no .git at the root -> no .gitignore may be created')

    # ----- 2/3. user-deletion tombstone -----------------------------------

    def test_user_deleted_context_is_not_rewritten(self):
        self._make_healthy_venv()
        self.ext._ensurePyEnvContext()
        os.remove(self.ctx_path)
        self.guard_calls = []
        self.ext._ensurePyEnvContext()
        self.assertFalse(
            os.path.exists(self.ctx_path),
            'absent + still recorded = a USER deletion; it must stay deleted')
        self.assertEqual(self.guard_calls, [],
                         'a tombstoned context must not even reach the guard')

    def test_force_reasserts_user_deleted_context(self):
        self._make_healthy_venv()
        self.ext._ensurePyEnvContext()
        os.remove(self.ctx_path)
        self.ext._ensurePyEnvContext(force=True)
        self.assertTrue(os.path.isfile(self.ctx_path),
                        'force=True (InitEnvoy) re-asserts over the tombstone')
        self.assertEqual(
            self._read_ctx(),
            self.pyenv.render_td_context(self.spec['python_tag']))

    # ----- 4. foreign context is untouchable ------------------------------

    def test_foreign_context_untouched_by_plain_and_force(self):
        self._make_healthy_venv()
        # envName resolves to a DIFFERENT env -> foreign by classify.
        self._write_ctx('contextVersion: 2\n'
                        'mode: Python vEnv\n'
                        'envName: .othervenv\n'
                        'installPath: .\n'
                        "pythonVersion: '3.9'\n"
                        'autoSetup: false\n')
        self.assertEqual(
            self.pyenv.classify_td_context(self.root, self.spec['venv_dir']),
            'foreign', 'fixture rot: this context must classify foreign')
        with open(self.ctx_path, 'rb') as f:
            before = f.read()
        self.ext._ensurePyEnvContext()
        self.ext._ensurePyEnvContext(force=True)
        with open(self.ctx_path, 'rb') as f:
            after = f.read()
        self.assertEqual(before, after,
                         'a foreign context must never be modified')
        self.assertNotIn(self._rel, self._created(),
                         'a foreign context must never be recorded as ours')
        self.assertEqual(self.guard_calls, [],
                         'a foreign context must never reach the write guard')

    # ----- 5. unhealthy venv -> remove + un-record ------------------------

    def test_unhealthy_venv_removes_context_and_unrecords(self):
        self._make_healthy_venv()
        self.ext._ensurePyEnvContext()
        self.assertIn(self._rel, self._created())
        shutil.rmtree(self.spec['venv_dir'], ignore_errors=True)
        self.ext._ensurePyEnvContext()
        self.assertFalse(
            os.path.exists(self.ctx_path),
            'TD must not pre-cook-link a venv Embody refuses to wire')
        self.assertNotIn(
            self._rel, self._created(),
            'a self-removal must un-record, or the post-install rewrite '
            'would read as a user deletion')

    # ----- 6. 'ok' is a true no-op ----------------------------------------

    def test_current_context_is_left_untouched(self):
        self._make_healthy_venv()
        self.ext._ensurePyEnvContext()
        before = (os.stat(self.ctx_path).st_mtime_ns, self._read_ctx())
        self.guard_calls = []
        self.ext._ensurePyEnvContext()
        after = (os.stat(self.ctx_path).st_mtime_ns, self._read_ctx())
        self.assertEqual(before, after,
                         'a current context must not be rewritten (mtime)')
        self.assertEqual(self.guard_calls, [],
                         'a no-op must never prompt/guard')

    # ----- 7. refresh preserves TD-written keys ---------------------------

    def test_version_drift_refreshes_in_place_preserving_extra_keys(self):
        self._make_healthy_venv()
        self._write_ctx(self._ours_text('2.7', extra='extraPaths:\n- keepme\n'))
        tag = self.spec['python_tag']
        self.assertEqual(
            self.pyenv.td_context_status(self.root, self.spec['venv_dir'],
                                         tag),
            'refresh', 'fixture rot: drifted pythonVersion must ask refresh')
        self.ext._ensurePyEnvContext()
        text = self._read_ctx()
        self.assertIn(f"pythonVersion: '{tag}'", text,
                      'refresh must update pythonVersion')
        self.assertNotIn("pythonVersion: '2.7'", text,
                         'the stale pythonVersion must be gone')
        self.assertIn('- keepme', text,
                      'TD/palette-written keys must survive a refresh')

    # ----- 8. never-raises contract ---------------------------------------

    def test_never_raises_when_the_spec_blows_up(self):
        def _boom():
            raise RuntimeError('venv spec unavailable')

        self._patch('_venvPaths', _boom)
        try:
            self.ext._ensurePyEnvContext()
        except Exception as e:
            raise AssertionError(
                f'_ensurePyEnvContext must never raise, got {e!r}')
        self.assertFalse(os.path.exists(self.ctx_path),
                         'a failed ensure must not leave a context behind')

    # ----- 9. git root -> anchored, LF-only ignore entry -------------------

    def test_git_root_gains_anchored_lf_gitignore_entry(self):
        os.makedirs(os.path.join(self.root, '.git'), exist_ok=True)
        self._make_healthy_venv()
        self.ext._ensurePyEnvContext()
        gi = os.path.join(self.root, '.gitignore')
        self.assertTrue(os.path.isfile(gi),
                        'a git root must get the ignore entry')
        with open(gi, 'rb') as f:
            raw = f.read()
        self.assertNotIn(b'\r', raw, '.gitignore must be written LF-only')
        lines = raw.decode('utf-8').split('\n')
        self.assertIn('# Embody / Envoy (auto-managed)', lines,
                      'the entry belongs in the managed block')
        idx = lines.index('# Embody / Envoy (auto-managed)')
        self.assertEqual(
            lines[idx + 1], '/' + self.pyenv.TD_CONTEXT_FILENAME,
            'the entry must be ANCHORED so foreign contexts elsewhere in '
            'the repo stay commit-able')
