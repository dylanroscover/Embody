"""
Test suite: embody_pyenv -- the shared project Python environment module.

Pure Python (TD-import-free): loads embody_pyenv.py by file path relative
to this file, so the SAME tests run under plain pytest (the windows/macos
bridge matrix -- the macos leg is the only routine signal the darwin
branches get) and inside TouchDesigner via TestRunnerExt.

What this pins:
- extras never gate the core needs-install verdict (a typo'd user package
  must not take the MCP server down), and extras stamp keys are carried
  forward by write_env_stamp (a core reinstall must not forget them);
- the declaration steward never blind-overwrites .embody/project.json
  (co-writers' keys survive any parse failure) and owns ONLY
  python.extras;
- check_extras_specs refuses Embody's core pins and TD-bundled names
  (sys.path[0] shadowing TD's own numpy is a crash class);
- install_extras constrains on the frozen core closure, isolates a bad
  spec instead of sinking the batch, and reports loaded+changed dists as
  restart-required;
- link_env / bootstrap_env co-existence contract with tdPyEnvManager
  (VIRTUAL_ENV never fought over in-process, always stripped for
  children).
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import shutil
import types

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# PYTEST-ONLY SUITE (2026-08-19). Everything here is pure Python by
# contract -- in-TD execution duplicates the pytest/CI coverage exactly
# while running inside a process this box's instability cluster keeps
# killing (three silent TD exits today; the 13:22 one died 5s into this
# suite's first-ever in-TD run, confounded by a concurrent second TD
# instance). Inside TD every test SKIPS loudly; the windows+macos CI
# matrix and local pytest are the suite's real home.
_IN_TD = 'td' in sys.modules

_MOD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'Embody', 'embody_pyenv.py')
_spec = importlib.util.spec_from_file_location('embody_pyenv_under_test',
                                               _MOD_PATH)
pyenv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pyenv)


def _make_healthy_venv(root, spec):
    """Materialize the on-disk shape environment_needs_install calls
    healthy: mcp/ + yaml/ dirs, matching pyvenv.cfg, matching stamp,
    in-range mcp dist-info, attrs 24."""
    sp = spec['site_packages']
    os.makedirs(os.path.join(sp, 'mcp'), exist_ok=True)
    os.makedirs(os.path.join(sp, 'yaml'), exist_ok=True)
    os.makedirs(os.path.join(sp, f'mcp-{spec["mcp_min_version"]}.dist-info'),
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


class _PyenvCase(EmbodyTestCase):
    """Shared tmp-root fixture + a spec whose paths live under it."""

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only '
                          '(in-TD execution adds no coverage)')
        self.root = tempfile.mkdtemp(prefix='embody_pyenv_test_')
        self.spec = pyenv.venv_paths(self.root, '2.0.0')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()


class TestSpecBuilding(_PyenvCase):

    def test_venv_paths_shape(self):
        s = self.spec
        self.assertEqual(s['venv_dir'], os.path.join(self.root, '.venv'))
        self.assertEqual(s['mcp_ceiling_major'], 3)
        self.assertDictHasKey(s, 'constraints_path')
        self.assertStartsWith(s['constraints_path'], s['venv_dir'])
        self.assertIn('pyyaml', s['deps'])
        self.assertIn('attrs<25', s['deps'])
        self.assertTrue(any(d.startswith('mcp>=2.0.0,<3')
                            for d in s['deps']))

    def test_cryptography_pin_targets(self):
        self.assertEqual(pyenv.cryptography_pin('darwin', 'x86_64'),
                         'cryptography>=3.4,<49')
        self.assertEqual(pyenv.cryptography_pin('darwin', 'arm64'),
                         'cryptography>=3.4')
        self.assertEqual(pyenv.cryptography_pin('win32', 'AMD64'),
                         'cryptography>=3.4')

    def test_stamp_arch_migration_darwin_only(self):
        self.assertTrue(pyenv.stamp_needs_arch_migration('', 'darwin'))
        self.assertFalse(pyenv.stamp_needs_arch_migration('arm64', 'darwin'))
        self.assertFalse(pyenv.stamp_needs_arch_migration('', 'win32'))


class TestNeedsInstall(_PyenvCase):

    def test_missing_mcp_needs_install(self):
        self.assertTrue(pyenv.environment_needs_install(self.spec))

    def test_healthy_venv_is_clean(self):
        _make_healthy_venv(self.root, self.spec)
        self.assertFalse(pyenv.environment_needs_install(self.spec))

    def test_extras_stamp_keys_do_not_gate(self):
        """THE regression guard: extras bookkeeping in the stamp must be
        invisible to the core verdict -- folding extras into the deps
        equality would wedge needs-install True forever."""
        _make_healthy_venv(self.root, self.spec)
        with open(self.spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        stamp['extras_applied'] = ['numpy>=2']
        stamp['extras_failed'] = {'nope': 'no solution'}
        with open(self.spec['stamp_path'], 'w', encoding='utf-8') as f:
            json.dump(stamp, f)
        self.assertFalse(pyenv.environment_needs_install(self.spec))

    def test_python_tag_mismatch_recreates(self):
        _make_healthy_venv(self.root, self.spec)
        # A tag guaranteed different from the RUNNING interpreter (3.9 was
        # a real collision on a 3.9 runner -- never hardcode a plausible
        # version here).
        with open(os.path.join(self.spec['venv_dir'], 'pyvenv.cfg'), 'w',
                  encoding='utf-8') as f:
            f.write('version_info = 2.7.18\n')
        self.assertTrue(pyenv.environment_needs_install(self.spec))
        self.assertTrue(self.spec.get('recreate_venv'))

    def test_deps_change_reinstalls_without_recreate(self):
        _make_healthy_venv(self.root, self.spec)
        with open(self.spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        stamp['deps'] = ['mcp>=1.0.0,<2']
        with open(self.spec['stamp_path'], 'w', encoding='utf-8') as f:
            json.dump(stamp, f)
        self.assertTrue(pyenv.environment_needs_install(self.spec))
        self.assertFalse(self.spec.get('recreate_venv'))


class TestStampCarry(_PyenvCase):

    def test_write_env_stamp_carries_extras_forward(self):
        os.makedirs(self.spec['venv_dir'], exist_ok=True)
        with open(self.spec['stamp_path'], 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'deps': [], 'python': 'x',
                       'extras_applied': ['pandas'],
                       'extras_failed': {'zzz': 'boom'}}, f)
        pyenv.write_env_stamp(self.spec)
        with open(self.spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        self.assertEqual(stamp['extras_applied'], ['pandas'])
        self.assertEqual(stamp['extras_failed'], {'zzz': 'boom'})
        self.assertEqual(stamp['deps'], sorted(self.spec['deps']))


class TestExtrasStatus(_PyenvCase):

    def test_status_math(self):
        os.makedirs(self.spec['venv_dir'], exist_ok=True)
        with open(self.spec['stamp_path'], 'w', encoding='utf-8') as f:
            json.dump({'extras_applied': ['a'],
                       'extras_failed': {'b': 'no solution'}}, f)
        st = pyenv.extras_status(self.spec, ['a', 'b', 'c'])
        self.assertEqual(st['to_install'], ['c'])

    def test_no_stamp_installs_everything(self):
        st = pyenv.extras_status(self.spec, ['a', 'b'])
        self.assertEqual(st['to_install'], ['a', 'b'])

    def test_declared_core_names_never_reconcile(self):
        """A hand-edited (or git-pulled) declaration must not route
        around the core-pin boundary at reconcile time."""
        st = pyenv.extras_status(self.spec, ['mcp>=99', 'pandas'])
        self.assertEqual(st['to_install'], ['pandas'])
        self.assertIn('mcp>=99', st['refused'])

    def test_unacknowledged_declarations_never_auto_install(self):
        """THE consent gate (2026-08-19 review BLOCKER): a pulled
        declaration is not consent -- only specs acknowledged on this
        machine reconcile automatically."""
        st = pyenv.extras_status(self.spec, ['pandas', 'websockets'],
                                 acknowledged=['pandas'])
        self.assertEqual(st['to_install'], ['pandas'])
        self.assertEqual(st['unacknowledged'], ['websockets'])

    def test_acknowledge_extras_roundtrip(self):
        logged = []
        ok = pyenv.acknowledge_extras(self.root, ['b', 'a'],
                                      lambda *a, **k: logged.append(a))
        self.assertTrue(ok)
        self.assertEqual(pyenv.read_acknowledged_extras(self.root),
                         ['a', 'b'])
        # merge-preserves foreign keys in local.json
        path = os.path.join(self.root, '.embody', 'local.json')
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data['td_build'] = '2025.99999'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        pyenv.acknowledge_extras(self.root, ['c'],
                                 lambda *a, **k: logged.append(a))
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['td_build'], '2025.99999')
        self.assertEqual(data['python_acknowledged_extras'],
                         ['a', 'b', 'c'])

    def test_clear_extras_failures_retries(self):
        os.makedirs(self.spec['venv_dir'], exist_ok=True)
        with open(self.spec['stamp_path'], 'w', encoding='utf-8') as f:
            json.dump({'extras_failed': {'b': 'x'}}, f)
        pyenv.clear_extras_failures(self.spec, ['b'])
        st = pyenv.extras_status(self.spec, ['b'])
        self.assertEqual(st['to_install'], ['b'])


class TestSpecChecks(_PyenvCase):

    def test_core_names_refused(self):
        accepted, refused = pyenv.check_extras_specs(
            ['mcp>=3', 'PyYAML', 'cryptography==48.0.0'], self.spec)
        self.assertEqual(accepted, [])
        self.assertLen(refused, 3)

    def test_bundled_seed_refused_without_allow_shadow(self):
        accepted, refused = pyenv.check_extras_specs(['numpy>=2'], self.spec)
        self.assertEqual(accepted, [])
        self.assertIn('numpy>=2', refused)
        accepted, refused = pyenv.check_extras_specs(
            ['numpy>=2'], self.spec, allow_shadow=True)
        self.assertEqual(accepted, ['numpy>=2'])

    def test_invalid_spec_refused(self):
        accepted, refused = pyenv.check_extras_specs(
            ['>=nonsense'], self.spec)
        self.assertEqual(accepted, [])
        self.assertLen(refused, 1)

    def test_plain_package_accepted(self):
        accepted, refused = pyenv.check_extras_specs(
            ['pandas>=2.1', 'websockets'], self.spec)
        self.assertEqual(accepted, ['pandas>=2.1', 'websockets'])
        self.assertEqual(refused, {})

    def test_canonical_names(self):
        self.assertEqual(pyenv.canonical_name('PyYAML'), 'pyyaml')
        self.assertEqual(pyenv.canonical_name('foo_bar.Baz'), 'foo-bar-baz')
        self.assertEqual(pyenv.spec_dist_name('Pillow>=10 ; os_name=="nt"'),
                         'pillow')

    def test_url_vcs_and_path_forms_refused(self):
        """2026-08-19 review BLOCKER: 'git+https://...' parsed as name
        'git' and a wheel path as name 'c' -- URL/VCS/path forms carry
        arbitrary dist names past every name check, so they are refused
        outright, at request time AND at reconcile time."""
        bad = [
            'https://evil.example/mcp-9.0-py3-none-any.whl',
            'git+https://evil.example/x.git',
            'pandas @ https://evil.example/x.whl',
            r'C:\wheels\numpy-2.0-cp311.whl',
            './local-dir/pkg',
            '--index-url=http://evil/simple',
        ]
        accepted, refused = pyenv.check_extras_specs(bad, self.spec,
                                                     allow_shadow=True)
        self.assertEqual(accepted, [])
        self.assertLen(refused, len(bad))
        reconcile_refused = pyenv.core_refused_specs(self.spec, bad)
        self.assertLen(reconcile_refused, len(bad))

    def test_valid_requirement_forms(self):
        for good in ('pandas', 'pandas>=2.1,<3', 'pkg[extra]==1.0',
                     'Pillow>=10 ; os_name=="nt"'):
            self.assertTrue(pyenv.valid_requirement(good), good)
        for bad in ('', 'git+https://h/r', 'pkg @ https://h/x.whl',
                    'a/b', '>=nonsense'):
            self.assertFalse(pyenv.valid_requirement(bad), bad)

    def test_shadow_survives_reconcile_only_with_optin(self):
        """A pulled declaration shadowing a TD-bundled package is refused
        at reconcile unless python.extras_allow_shadow records the
        opt-in (2026-08-19 review)."""
        refused = pyenv.core_refused_specs(self.spec, ['numpy>=2'])
        self.assertIn('numpy>=2', refused)
        refused = pyenv.core_refused_specs(self.spec, ['numpy>=2'],
                                           allow_shadow_names=['numpy'])
        self.assertEqual(refused, {})


class TestDeclarationSteward(_PyenvCase):

    def _pj_path(self):
        return os.path.join(self.root, '.embody', 'project.json')

    def _log(self, *a, **k):
        self._log_lines.append(a)

    def setUp(self):
        super().setUp()
        self._log_lines = []

    def test_roundtrip(self):
        ok = pyenv.write_declared_extras(self.root, ['b', 'a', 'a'],
                                         self._log)
        self.assertTrue(ok)
        self.assertEqual(pyenv.read_declared_extras(self.root), ['a', 'b'])

    def test_foreign_keys_preserved(self):
        os.makedirs(os.path.dirname(self._pj_path()), exist_ok=True)
        with open(self._pj_path(), 'w', encoding='utf-8') as f:
            json.dump({'convoy': {'id': 'c-123'},
                       'python': {'future_key': True}}, f)
        pyenv.write_declared_extras(self.root, ['numpy'], self._log)
        with open(self._pj_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['convoy'], {'id': 'c-123'})
        self.assertEqual(data['python']['future_key'], True)
        self.assertEqual(data['python']['extras'], ['numpy'])

    def test_corrupt_file_never_overwritten(self):
        os.makedirs(os.path.dirname(self._pj_path()), exist_ok=True)
        with open(self._pj_path(), 'w', encoding='utf-8') as f:
            f.write('{not json')
        ok = pyenv.write_declared_extras(self.root, ['numpy'], self._log)
        self.assertFalse(ok)
        with open(self._pj_path(), 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), '{not json')

    def test_read_missing_or_bad_is_empty(self):
        self.assertEqual(pyenv.read_declared_extras(self.root), [])
        os.makedirs(os.path.dirname(self._pj_path()), exist_ok=True)
        with open(self._pj_path(), 'w', encoding='utf-8') as f:
            json.dump({'python': {'extras': 'not-a-list'}}, f)
        self.assertEqual(pyenv.read_declared_extras(self.root), [])


class TestDistSnapshots(_PyenvCase):

    def _mk_dist(self, name, version, top_level=None):
        d = os.path.join(self.spec['site_packages'],
                         f'{name}-{version}.dist-info')
        os.makedirs(d, exist_ok=True)
        if top_level:
            with open(os.path.join(d, 'top_level.txt'), 'w',
                      encoding='utf-8') as f:
                f.write('\n'.join(top_level) + '\n')

    def test_snapshot(self):
        self._mk_dist('Foo_Bar', '1.0')
        snap = pyenv.dist_info_snapshot(self.spec['site_packages'])
        self.assertEqual(snap.get('foo-bar'), '1.0')

    def test_loaded_changed_dists(self):
        sp = self.spec['site_packages']
        self._mk_dist('embody_fake_loaded', '2.0',
                      top_level=['embody_fake_loaded_mod'])
        before = {'embody-fake-loaded': '1.0', 'embody-fake-cold': '1.0'}
        after = {'embody-fake-loaded': '2.0', 'embody-fake-cold': '2.0'}
        sys.modules['embody_fake_loaded_mod'] = types.ModuleType(
            'embody_fake_loaded_mod')
        try:
            loaded = pyenv.loaded_changed_dists(before, after, sp)
        finally:
            del sys.modules['embody_fake_loaded_mod']
        self.assertEqual(loaded, ['embody-fake-loaded'])

    def test_newly_installed_loaded_dist_flags_restart(self):
        """An allow_shadow first-time install of a package TD already has
        imported must warn -- 'changed' includes NEW dists (2026-08-19
        review: the old before-only check missed exactly the shadowing
        case)."""
        sp = self.spec['site_packages']
        self._mk_dist('embody_fake_new', '2.0',
                      top_level=['embody_fake_new_mod'])
        sys.modules['embody_fake_new_mod'] = types.ModuleType(
            'embody_fake_new_mod')
        try:
            loaded = pyenv.loaded_changed_dists(
                {}, {'embody-fake-new': '2.0'}, sp)
        finally:
            del sys.modules['embody_fake_new_mod']
        self.assertEqual(loaded, ['embody-fake-new'])

    def test_failure_reason_never_blames_core_blind(self):
        err = subprocess.CalledProcessError(
            1, ['uv'], stderr='x' * 500 + '\nNo solution found')
        reason = pyenv._failure_reason(err, 'somepkg>=9', self.spec)
        self.assertIn('somepkg>=9', reason)
        self.assertNotIn('mcp/attrs/pydantic', reason)
        self.assertIn(os.path.basename(self.spec['constraints_path']),
                      reason)

    def test_no_wheel_failure_is_actionable(self):
        err = subprocess.CalledProcessError(
            1, ['uv'], stderr='no wheels are available for sdist-only-pkg')
        reason = pyenv._failure_reason(err, 'sdist-only-pkg', self.spec)
        self.assertIn('pre-built wheel', reason)
        self.assertTrue(pyenv._is_resolver_failure(err))


class TestInstallExtras(_PyenvCase):
    """Drives install_extras with run_uv/resolve_uv patched at module
    attributes -- no subprocess, no network."""

    def setUp(self):
        super().setUp()
        os.makedirs(self.spec['site_packages'], exist_ok=True)
        self._orig_run_uv = pyenv.run_uv
        self._orig_resolve = pyenv.resolve_uv
        self.calls = []

    def tearDown(self):
        pyenv.run_uv = self._orig_run_uv
        pyenv.resolve_uv = self._orig_resolve
        super().tearDown()

    def _log(self, *a, **k):
        pass

    def _ok_run(self, uv, args, timeout=None, hardened=False):
        self.calls.append(list(args))
        return types.SimpleNamespace(stdout='', stderr='')

    def test_no_uv_defers_without_recording_failures(self):
        pyenv.resolve_uv = lambda: None
        res = pyenv.install_extras(self.spec, ['pandas'], self._log)
        self.assertEqual(res['installed'], [])
        self.assertTrue(res['deferred'])
        self.assertEqual(res['failed'], {})

    def test_batch_success_records_applied(self):
        pyenv.resolve_uv = lambda: 'uv-fake'
        pyenv.run_uv = self._ok_run
        res = pyenv.install_extras(self.spec, ['pandas', 'websockets'],
                                   self._log)
        self.assertEqual(res['installed'], ['pandas', 'websockets'])
        with open(self.spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        self.assertEqual(stamp['extras_applied'], ['pandas', 'websockets'])
        flat = sum(self.calls, [])
        # constraints file (fallback pins here) + wheels-only enforced
        self.assertIn('-c', flat)
        self.assertIn('--no-build', flat)
        # the fallback pins tmp file is cleaned up
        self.assertFalse(os.path.isfile(
            os.path.join(self.spec['venv_dir'], 'embody-pins.tmp.txt')))
        # and the cross-process lock is released
        self.assertFalse(os.path.isfile(self.spec['install_lock_path']))

    def test_frozen_constraints_preferred(self):
        os.makedirs(self.spec['venv_dir'], exist_ok=True)
        with open(self.spec['constraints_path'], 'w', encoding='utf-8') as f:
            f.write('anyio==4.4.0\n')
        pyenv.resolve_uv = lambda: 'uv-fake'
        pyenv.run_uv = self._ok_run
        pyenv.install_extras(self.spec, ['pandas'], self._log)
        flat = sum(self.calls, [])
        idx = flat.index('-c')
        self.assertEqual(flat[idx + 1], self.spec['constraints_path'])

    def test_bad_spec_isolated_from_batch(self):
        pyenv.resolve_uv = lambda: 'uv-fake'

        def fake_run(uv, args, timeout=None, hardened=False):
            if 'badpkg' in args:
                raise subprocess.CalledProcessError(
                    1, ['uv'], stderr='No solution found for badpkg')
            self.calls.append(list(args))
            return types.SimpleNamespace(stdout='', stderr='')

        pyenv.run_uv = fake_run
        res = pyenv.install_extras(self.spec, ['goodpkg', 'badpkg'],
                                   self._log)
        self.assertEqual(res['installed'], ['goodpkg'])
        self.assertIn('badpkg', res['failed'])
        with open(self.spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        self.assertIn('badpkg', stamp['extras_failed'])
        # resolver failure is suppressed from the next reconcile
        st = pyenv.extras_status(self.spec, ['goodpkg', 'badpkg'])
        self.assertEqual(st['to_install'], [])

    def _age_transients(self):
        """Rewind the transient timestamps so the 10-minute retry
        cooldown reads as elapsed."""
        with open(self.spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        for rec in (stamp.get('extras_failed_transient') or {}).values():
            rec['t'] = 0
        with open(self.spec['stamp_path'], 'w', encoding='utf-8') as f:
            json.dump(stamp, f)

    def test_transient_failure_retries_up_to_three_times(self):
        """A Windows sharing violation (or any non-resolver error) must
        NOT become a permanent refusal -- it retries on later reconciles
        (after a cooldown), capped at 3 attempts (2026-08-19 review)."""
        pyenv.resolve_uv = lambda: 'uv-fake'

        def locked_run(uv, args, timeout=None, hardened=False):
            raise subprocess.CalledProcessError(
                1, ['uv'], stderr='PermissionError: [WinError 32] sharing '
                                  'violation on pydantic_core.pyd')

        pyenv.run_uv = locked_run
        for attempt in range(1, 4):
            res = pyenv.install_extras(self.spec, ['pandas'], self._log)
            self.assertIn('pandas', res['transient'])
            # Fresh failure: cooling down, never an immediate re-burn
            # (the drain re-arm used to consume all 3 attempts in
            # seconds).
            st = pyenv.extras_status(self.spec, ['pandas'])
            self.assertEqual(st['to_install'], [],
                             f'attempt {attempt}: must cool down first')
            self._age_transients()
            st = pyenv.extras_status(self.spec, ['pandas'])
            if attempt < 3:
                self.assertEqual(st['to_install'], ['pandas'],
                                 f'attempt {attempt} should retry after '
                                 f'cooldown')
            else:
                self.assertEqual(st['to_install'], [],
                                 'attempt cap reached -- suppressed')
                self.assertEqual(st['exhausted'], ['pandas'])

    def test_malformed_spec_is_permanent_not_transient(self):
        err = subprocess.CalledProcessError(
            1, ['uv'], stderr='error: Failed to parse: `pandas>=notaversion`')
        self.assertTrue(pyenv._is_resolver_failure(err))

    def test_stolen_lock_single_winner_and_token_release(self):
        """Stale-lock steal is atomic (os.replace + confirm) and release
        is token-matched -- a second spec dict sharing the pid cannot
        free the winner's lock (2026-08-19 review TOCTOU/pid findings)."""
        os.makedirs(os.path.dirname(self.spec['install_lock_path']),
                    exist_ok=True)
        with open(self.spec['install_lock_path'], 'w',
                  encoding='utf-8') as f:
            json.dump({'pid': 999999999, 'time': 0, 'purpose': 'dead'}, f)
        self.assertTrue(pyenv.acquire_install_lock(self.spec, 'test'))
        other = dict(self.spec)
        other.pop('_lock_token', None)
        # same pid, no token: must NOT free the winner's lock
        pyenv.release_install_lock(other)
        self.assertTrue(os.path.isfile(self.spec['install_lock_path']))
        # ... and cannot acquire while the winner is live (same pid=alive)
        self.assertFalse(pyenv.acquire_install_lock(other, 'test2'))
        pyenv.release_install_lock(self.spec)
        self.assertFalse(os.path.isfile(self.spec['install_lock_path']))

    def test_lock_held_defers_cleanly(self):
        """Another process's live lock defers the install -- nothing
        recorded, specs retry next reconcile."""
        os.makedirs(os.path.dirname(self.spec['install_lock_path']),
                    exist_ok=True)
        with open(self.spec['install_lock_path'], 'w',
                  encoding='utf-8') as f:
            json.dump({'pid': os.getpid(), 'time': __import__('time').time(),
                       'purpose': 'test-peer'}, f)
        pyenv.resolve_uv = lambda: 'uv-fake'
        pyenv.run_uv = self._ok_run
        try:
            res = pyenv.install_extras(self.spec, ['pandas'], self._log)
            self.assertTrue(res['deferred'])
            self.assertEqual(res['installed'], [])
            st = pyenv.extras_status(self.spec, ['pandas'])
            self.assertEqual(st['to_install'], ['pandas'])
        finally:
            os.unlink(self.spec['install_lock_path'])

    def test_stale_lock_is_reclaimed(self):
        os.makedirs(os.path.dirname(self.spec['install_lock_path']),
                    exist_ok=True)
        with open(self.spec['install_lock_path'], 'w',
                  encoding='utf-8') as f:
            json.dump({'pid': 999999999, 'time': 0, 'purpose': 'dead'}, f)
        self.assertTrue(pyenv.acquire_install_lock(self.spec, 'test'))
        pyenv.release_install_lock(self.spec)
        self.assertFalse(os.path.isfile(self.spec['install_lock_path']))

    def test_freeze_filters_direct_refs_and_declared_extras(self):
        """The constraints snapshot keeps only name==version lines and
        excludes the user's own declared extras -- a 'pkg @ file://...'
        line poisons the whole file, and freezing the user's package
        blocks its own future upgrade behind a false 'Embody pinned it'
        (2026-08-19 review)."""
        os.makedirs(self.spec['venv_dir'], exist_ok=True)
        self.spec['declared_extras'] = ['rich>=13']
        freeze_out = ('anyio==4.4.0\n'
                      'rich==13.7.0\n'
                      'localpkg @ file:///C:/stuff/localpkg\n'
                      '-e /dev/somewhere\n')
        pyenv.run_uv = lambda uv, args, timeout=None, hardened=False: (
            types.SimpleNamespace(stdout=freeze_out, stderr=''))
        ok = pyenv.freeze_constraints(self.spec, 'uv-fake', self._log)
        self.assertTrue(ok)
        with open(self.spec['constraints_path'], 'r',
                  encoding='utf-8') as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ['anyio==4.4.0'])


class TestEnvWiring(_PyenvCase):

    def setUp(self):
        super().setUp()
        self._saved_path = os.environ.get('PATH')
        self._saved_venv = os.environ.get('VIRTUAL_ENV')
        self._had_links = hasattr(sys, '_embody_pyenv_dll_links')
        self._saved_links = getattr(sys, '_embody_pyenv_dll_links', None)

    def tearDown(self):
        if self._saved_path is not None:
            os.environ['PATH'] = self._saved_path
        if self._saved_venv is None:
            os.environ.pop('VIRTUAL_ENV', None)
        else:
            os.environ['VIRTUAL_ENV'] = self._saved_venv
        links = getattr(sys, '_embody_pyenv_dll_links', None)
        if isinstance(links, dict):
            for norm, handle in list(links.items()):
                if not self._had_links or norm not in (self._saved_links or {}):
                    try:
                        handle.close()
                    except Exception:
                        pass
                    links.pop(norm, None)
        if not self._had_links and hasattr(sys, '_embody_pyenv_dll_links'):
            if not sys._embody_pyenv_dll_links:
                delattr(sys, '_embody_pyenv_dll_links')
        elif self._had_links:
            sys._embody_pyenv_dll_links = self._saved_links
        super().tearDown()

    def _mk_bin(self):
        bin_dir = os.path.join(
            self.spec['venv_dir'],
            'Scripts' if sys.platform.startswith('win') else 'bin')
        os.makedirs(bin_dir, exist_ok=True)
        # Pre-seed the handle registry so link_env NEVER calls the real
        # os.add_dll_directory from a test: mutating the live DLL search
        # path inside TouchDesigner's process is a crash-risk class
        # (2026-08-19: prime suspect for the mid-suite TD crash -- these
        # tests run in-process under TestRunnerExt). PATH and VIRTUAL_ENV
        # behavior is still fully asserted; the real DLL registration is
        # exercised by the production init path, not by tests.
        norm = os.path.normcase(os.path.normpath(bin_dir))
        links = getattr(sys, '_embody_pyenv_dll_links', None)
        if links is None:
            links = {}
            sys._embody_pyenv_dll_links = links
        links.setdefault(norm, types.SimpleNamespace(close=lambda: None))
        return bin_dir

    def test_link_env_prepends_path_once(self):
        bin_dir = self._mk_bin()
        os.environ.pop('VIRTUAL_ENV', None)
        self.assertTrue(pyenv.link_env(self.spec))
        self.assertTrue(pyenv.link_env(self.spec))  # idempotent
        entries = os.environ['PATH'].split(os.pathsep)
        norm = os.path.normcase(os.path.normpath(bin_dir))
        hits = [p for p in entries
                if os.path.normcase(os.path.normpath(p)) == norm]
        self.assertLen(hits, 1)
        self.assertEqual(os.environ['VIRTUAL_ENV'], self.spec['venv_dir'])

    def test_link_env_never_fights_foreign_virtual_env(self):
        self._mk_bin()
        os.environ['VIRTUAL_ENV'] = '/somebody/elses/env'
        pyenv.link_env(self.spec)
        self.assertEqual(os.environ['VIRTUAL_ENV'], '/somebody/elses/env')

    def test_link_env_missing_venv_is_false(self):
        self.assertFalse(pyenv.link_env({'venv_dir':
                                         os.path.join(self.root, 'nope')}))

    def test_bootstrap_env_strips_redirectors(self):
        os.environ['VIRTUAL_ENV'] = '/somebody/elses/env'
        env = pyenv.bootstrap_env(UV_LINK_MODE='copy')
        self.assertNotIn('VIRTUAL_ENV', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertEqual(env['PYTHONIOENCODING'], 'utf-8')
        self.assertEqual(env['UV_LINK_MODE'], 'copy')

    def test_td_bundled_excludes_own_venv(self):
        sp = self.spec['site_packages']
        os.makedirs(os.path.join(sp, 'somepkg-1.0.dist-info'), exist_ok=True)
        sys.path.insert(0, sp)
        try:
            names = pyenv.td_bundled_dist_names(self.spec['venv_dir'])
        finally:
            sys.path.remove(sp)
        self.assertNotIn('somepkg', names)
        self.assertIn('numpy', names)  # seed

    def test_td_bundled_sibling_prefix_not_excluded(self):
        """'.venv2' must not ride '.venv's exclusion (2026-08-19 review:
        bare startswith matched the sibling and silently weakened the
        shadow refusal)."""
        sibling = os.path.join(self.root, '.venv2', 'Lib', 'site-packages')
        os.makedirs(os.path.join(sibling, 'siblingpkg-1.0.dist-info'),
                    exist_ok=True)
        sys.path.insert(0, sibling)
        try:
            names = pyenv.td_bundled_dist_names(self.spec['venv_dir'])
        finally:
            sys.path.remove(sibling)
        self.assertIn('siblingpkg', names)

    def test_bootstrap_env_hardened_scrubs_uv_pip(self):
        os.environ['UV_INDEX_URL'] = 'http://evil.example/simple'
        os.environ['PIP_INDEX_URL'] = 'http://evil.example/simple'
        try:
            env = pyenv.hardened_env(UV_LINK_MODE='copy')
            self.assertNotIn('UV_INDEX_URL', env)
            self.assertNotIn('PIP_INDEX_URL', env)
            self.assertEqual(env['UV_LINK_MODE'], 'copy')
            # the plain bootstrap env keeps them (core path: corporate
            # mirrors stay working)
            env = pyenv.bootstrap_env()
            self.assertIn('UV_INDEX_URL', env)
        finally:
            os.environ.pop('UV_INDEX_URL', None)
            os.environ.pop('PIP_INDEX_URL', None)


class TestModulePurity(_PyenvCase):
    """AST lint: embody_pyenv's whole contract is 'pure Python, no TD
    objects' -- worker threads call it on that promise. Any bare use of
    a TD builtin is a thread-safety regression, caught here instead of
    as a silent corruption in the field (precedent:
    test_worker_run_lint.py)."""

    TD_BUILTINS = {'op', 'opex', 'mod', 'parent', 'project', 'ui', 'run',
                   'app', 'absTime', 'tdu', 'debug', 'me', 'iop', 'ipar'}

    def test_no_td_builtin_names(self):
        import ast
        with open(_MOD_PATH, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in self.TD_BUILTINS:
                # a local assignment/parameter of the same name is fine;
                # ast.Load of a never-assigned TD builtin is the bug.
                offenders.append((node.id, node.lineno))
        # Filter names that are genuinely bound locally (params, locals):
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx,
                                                           ast.Store):
                bound.add(node.id)
        offenders = [(n, ln) for n, ln in offenders if n not in bound]
        self.assertEqual(offenders, [],
                         f'TD builtins referenced in embody_pyenv: '
                         f'{offenders}')


class TestTdPyEnvManagerDetection(_PyenvCase):

    def test_absent_is_silent(self):
        finding = pyenv.detect_tdpyenvmanager(self.root,
                                              self.spec['venv_dir'])
        self.assertFalse(finding['present'])
        self.assertIsNone(pyenv.tdpyenvmanager_notice(finding))

    def test_same_venv_is_info(self):
        helper = types.SimpleNamespace(
            HasLinkedCustomEnv=True, envPath=self.spec['venv_dir'])
        app_obj = types.SimpleNamespace(pyEnvHelper=helper)
        finding = pyenv.detect_tdpyenvmanager(
            self.root, self.spec['venv_dir'], app_obj)
        self.assertEqual(finding['kind'], 'same_venv')
        level, msg = pyenv.tdpyenvmanager_notice(finding)
        self.assertEqual(level, 'INFO')

    def test_different_env_is_warning_with_path(self):
        other = os.path.join(self.root, 'other_env')
        helper = types.SimpleNamespace(HasLinkedCustomEnv=True,
                                       envPath=other)
        app_obj = types.SimpleNamespace(pyEnvHelper=helper)
        finding = pyenv.detect_tdpyenvmanager(
            self.root, self.spec['venv_dir'], app_obj)
        self.assertEqual(finding['kind'], 'different_env')
        level, msg = pyenv.tdpyenvmanager_notice(finding)
        self.assertEqual(level, 'WARNING')
        self.assertIn('other_env', msg)

    def test_stale_context_file_detected_without_helper(self):
        with open(os.path.join(self.root, 'TDPyEnvManagerContext.yaml'),
                  'w', encoding='utf-8') as f:
            f.write('active: false\nmode: Python vEnv\nenvName: .venv\n')
        finding = pyenv.detect_tdpyenvmanager(self.root,
                                              self.spec['venv_dir'])
        self.assertTrue(finding['present'])
        self.assertTrue(any(f.endswith('TDPyEnvManagerContext.yaml')
                            for f in finding['context_files']))

    def test_conda_mode_is_handoff_warning(self):
        with open(os.path.join(self.root, 'TDPyEnvManagerContext.yaml'),
                  'w', encoding='utf-8') as f:
            f.write('active: true\nmode: Conda Env\nenvName: base\n')
        finding = pyenv.detect_tdpyenvmanager(self.root,
                                              self.spec['venv_dir'])
        self.assertEqual(finding['kind'], 'conda')
        level, msg = pyenv.tdpyenvmanager_notice(finding)
        self.assertEqual(level, 'WARNING')
        self.assertIn('CONDA', msg)
