"""
Test suite: the fresh-install smoke orchestrator (smoke_run.py) and the
pure helpers of its in-TD half (smoke_bootstrap.py).

Pure Python, TD-import-free: both modules are loaded by file path, so
these run under pytest on the windows/macos bridge matrix. Inside TD every
test skips -- the in-TD half is exercised by the smoke itself, not here.

What this pins:
- the release .tox is chosen from the manifest, never by string order
  (`Embody-v6.2.9.tox` sorts after `Embody-v6.2.56.tox`);
- every seeded dialog title matches a real title in the source, and a
  never-consumed sentinel keeps the response store alive so an unattended
  run can never fall through to a blocking ui.messageBox;
- the flag directory comes from the smoke_run.json sidecar, so two legs
  never write the same file;
- flag parsing: PENDING is "keep waiting", SKIP is a failure, and the
  `problems` line may contain '=';
- staging never runs inside the repo and never deletes anything;
- exit codes: 0 pass, 1 verdict failed, 2 could-not-run.
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_IN_TD = 'td' in sys.modules

_DEV = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_RT = os.path.join(_DEV, 'release_testing')
_SRC = os.path.join(_DEV, 'embody', 'Embody')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if not _IN_TD:
    boot = _load('smoke_bootstrap_under_test',
                 os.path.join(_RT, 'smoke_bootstrap.py'))
    smoke = _load('smoke_run_under_test', os.path.join(_RT, 'smoke_run.py'))


class _Case(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only')
        self.root = tempfile.mkdtemp(prefix='embody_smoke_run_test_')

    def _repo(self, assets, manifest_asset=None):
        """A fake checkout with release/ holding `assets` (name -> bytes)."""
        rel = os.path.join(self.root, 'release')
        os.makedirs(rel, exist_ok=True)
        for name, data in assets.items():
            with open(os.path.join(rel, name), 'wb') as f:
                f.write(data)
        if manifest_asset:
            data = assets[manifest_asset]
            with open(os.path.join(rel, 'embody-release.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'version': manifest_asset[len('Embody-v'):-4],
                           'asset': manifest_asset, 'size': len(data),
                           'sha256': hashlib.sha256(data).hexdigest()}, f)
        return self.root


# ===========================================================================
# smoke_bootstrap.py -- the three latent defects
# ===========================================================================

class TestReleaseToxSelection(_Case):

    def test_manifest_asset_beats_string_order(self):
        """v6.2.9 sorts AFTER v6.2.56; the manifest names the real one."""
        repo = self._repo({'Embody-v6.2.9.tox': b'old',
                           'Embody-v6.2.56.tox': b'new'},
                          manifest_asset='Embody-v6.2.56.tox')
        self.assertEndsWith(boot._select_release_tox(repo),
                            'Embody-v6.2.56.tox')

    def test_missing_manifest_falls_back_to_numeric_order(self):
        repo = self._repo({'Embody-v6.2.9.tox': b'old',
                           'Embody-v6.2.56.tox': b'new',
                           'Embody-v6.10.0.tox': b'newest'})
        self.assertEndsWith(boot._select_release_tox(repo),
                            'Embody-v6.10.0.tox')

    def test_manifest_naming_a_missing_file_is_none(self):
        """A manifest pointing at a .tox that is not there must not fall
        back to a different build silently."""
        repo = self._repo({'Embody-v6.2.55.tox': b'old'})
        with open(os.path.join(repo, 'release', 'embody-release.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'asset': 'Embody-v6.2.56.tox'}, f)
        self.assertIsNone(boot._select_release_tox(repo))

    def test_explicit_path_wins(self):
        repo = self._repo({'Embody-v6.2.56.tox': b'new'},
                          manifest_asset='Embody-v6.2.56.tox')
        explicit = os.path.join(self.root, 'elsewhere.tox')
        with open(explicit, 'wb') as f:
            f.write(b'x')
        self.assertEqual(boot._select_release_tox(repo, explicit), explicit)


class TestSeededDialogs(_Case):
    """The unattended guarantee: every title the smoke seeds is a title
    the product actually shows, and one key is never consumed."""

    def _source_text(self):
        text = ''
        for name in ('EmbodyExt.py', 'envoy_setup.py',
                     os.path.join('convoy', 'ConvoyExt.py')):
            with open(os.path.join(_SRC, name), 'r', encoding='utf-8') as f:
                text += f.read()
        return text

    def test_every_seeded_title_exists_in_source(self):
        src = self._source_text()
        seeded = dict(boot.SMOKE_RESPONSES)
        seeded.update(boot.SMOKE_CONVOY_RESPONSES)
        for title in seeded:
            if title in (boot.SMOKE_SENTINEL, 'Embody'):
                continue
            self.assertIn(repr(title), src,
                          f'seeded dialog title {title!r} matches no '
                          f'ui.messageBox title in the source')

    def test_sentinel_is_present_and_never_a_real_title(self):
        self.assertIn(boot.SMOKE_SENTINEL, boot.SMOKE_RESPONSES)
        self.assertNotIn(repr(boot.SMOKE_SENTINEL), self._source_text())


class TestRunConfigSidecar(_Case):

    def test_sidecar_is_read_beside_the_template(self):
        cfg = {'run_id': 'windows-x', 'repo_root': self.root,
               'tox_path': 'a.tox', 'flags_dir': self.root}
        with open(os.path.join(self.root, 'smoke_run.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(cfg, f)
        self.assertEqual(boot._read_run_config(self.root), cfg)

    def test_missing_or_broken_sidecar_is_none(self):
        self.assertIsNone(boot._read_run_config(self.root))
        with open(os.path.join(self.root, 'smoke_run.json'), 'w') as f:
            f.write('{not json')
        self.assertIsNone(boot._read_run_config(self.root))

    def test_wrong_typed_or_blank_values_are_dropped(self):
        """A blank flags_dir must fall through, never point at the repo."""
        with open(os.path.join(self.root, 'smoke_run.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'flags_dir': '', 'repo_root': 123,
                       'run_id': 'ok-1'}, f)
        self.assertEqual(boot._read_run_config(self.root), {'run_id': 'ok-1'})

    def test_flags_dir_prefers_sidecar_then_repo(self):
        self.assertEqual(
            boot._resolve_flags_dir({'flags_dir': self.root}, '/repo'),
            self.root)
        self.assertEqual(
            boot._resolve_flags_dir(None, self.root),
            os.path.join(self.root, 'dev', 'release_testing'))

    def test_sidecar_survives_the_cleanup_sweep(self):
        self.assertIn('smoke_run.json', boot.KEEP_NAMES)
        self.assertIn('smoke_template.toe', boot.KEEP_NAMES)


# ===========================================================================
# smoke_run.py -- flag parsing
# ===========================================================================

READY_PASS = (
    'verdict=PASS\nproblems=none\nversion=6.2.56\nstatus=Enabled\n'
    'envoy_enabled=True\nenvoy_status=Running on port 9871\n'
    'updatestatus=Up to date (v6.2.56)\nautosavestatus=Idle\n'
    'filecleanup=keep\nclipboardautopaste=True\nconvoy_enabled=False\n'
    'convoy_status=Not installed\nscript_errors=\nembody_path=/Embody\n'
    'settled_after_attempts=35\nrun_id=windows-20260918-093012\n'
    'platform=win32\ntox=Embody-v6.2.56.tox\n')


class TestParseReady(_Case):

    def test_pass_and_port(self):
        r = smoke.parse_ready(READY_PASS)
        self.assertEqual(r['verdict'], 'PASS')
        self.assertEqual(r['envoy_port'], 9871)
        self.assertEqual(r['version'], '6.2.56')
        self.assertEqual(r['run_id'], 'windows-20260918-093012')

    def test_problems_line_may_contain_equals_and_semicolons(self):
        text = READY_PASS.replace(
            'verdict=PASS\nproblems=none',
            'verdict=FAIL\nproblems=Status=Scanning; Envoy not running: '
            "'Installing deps... (one-time)'")
        r = smoke.parse_ready(text)
        self.assertEqual(r['verdict'], 'FAIL')
        self.assertIn("Envoy not running: 'Installing deps... (one-time)'",
                      r['problems'])

    def test_no_port_when_envoy_not_running(self):
        r = smoke.parse_ready(READY_PASS.replace(
            'Running on port 9871', 'Error: Python environment not ready'))
        self.assertIsNone(r['envoy_port'])


class TestParseFeatures(_Case):

    LEGS = ('embody_core', 'tdn_roundtrip', 'autosave_checkpoint',
            'portable_export', 'viz_status', 'envoy_config', 'convoy')

    def _text(self, **override):
        lines = []
        for leg in self.LEGS:
            verdict, detail = override.get(leg, ('PASS', 'ok'))
            lines.append(f'{leg}={verdict}|{detail}')
        return '\n'.join(lines) + '\n'

    def test_all_pass_is_terminal_and_passing(self):
        r = smoke.parse_features(self._text())
        self.assertTrue(r['terminal'])
        self.assertTrue(r['passed'])
        self.assertEqual(r['failed'], [])

    def test_pending_is_not_terminal(self):
        r = smoke.parse_features(self._text(convoy=('PENDING', 'Installing')))
        self.assertFalse(r['terminal'])
        self.assertFalse(r['passed'])

    def test_skip_is_terminal_and_failing(self):
        r = smoke.parse_features(self._text(convoy=('SKIP', 'not reached')))
        self.assertTrue(r['terminal'])
        self.assertFalse(r['passed'])
        self.assertEqual(r['failed'], ['convoy'])

    def test_fail_detail_is_kept(self):
        r = smoke.parse_features(self._text(
            tdn_roundtrip=('FAIL', 'value 9.25 != 1.0 @ tdn:42')))
        self.assertEqual(r['legs']['tdn_roundtrip']['detail'],
                         'value 9.25 != 1.0 @ tdn:42')

    def test_missing_leg_is_not_terminal(self):
        text = self._text().replace('convoy=PASS|ok\n', '')
        self.assertFalse(smoke.parse_features(text)['terminal'])


# ===========================================================================
# smoke_run.py -- build selection, staging, exit codes
# ===========================================================================

class TestBuildSelection(_Case):

    def test_manifest_hash_is_verified(self):
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'},
                          manifest_asset='Embody-v6.2.56.tox')
        info = smoke.select_build(repo)
        self.assertEndsWith(info['tox'], 'Embody-v6.2.56.tox')
        self.assertEqual(info['version'], '6.2.56')

    def test_corrupt_tox_is_refused(self):
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'},
                          manifest_asset='Embody-v6.2.56.tox')
        with open(os.path.join(repo, 'release', 'Embody-v6.2.56.tox'),
                  'wb') as f:
            f.write(b'tampered')
        with self.assertRaises(smoke.SmokeSetupError):
            smoke.select_build(repo)

    def test_missing_manifest_is_refused(self):
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'})
        with self.assertRaises(smoke.SmokeSetupError):
            smoke.select_build(repo)

    def test_size_mismatch_alone_is_refused(self):
        """Right hash, wrong size: the size branch must fire on its own."""
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'},
                          manifest_asset='Embody-v6.2.56.tox')
        mpath = os.path.join(repo, 'release', 'embody-release.json')
        with open(mpath, encoding='utf-8') as f:
            m = json.load(f)
        m['size'] = m['size'] + 1
        with open(mpath, 'w', encoding='utf-8') as f:
            json.dump(m, f)
        with self.assertRaises(smoke.SmokeSetupError):
            smoke.select_build(repo)

    def test_manifest_without_hash_is_refused(self):
        """An asset that cannot be verified must not read green."""
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'})
        with open(os.path.join(repo, 'release', 'embody-release.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'asset': 'Embody-v6.2.56.tox'}, f)
        with self.assertRaises(smoke.SmokeSetupError):
            smoke.select_build(repo)


class TestStaging(_Case):

    def _stage(self, **kw):
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'},
                          manifest_asset='Embody-v6.2.56.tox')
        # The harness files the run copies -- fakes with a recognisable body.
        rt = os.path.join(repo, 'dev', 'release_testing')
        os.makedirs(rt, exist_ok=True)
        for name in ('smoke_template.toe', 'smoke_bootstrap.py'):
            with open(os.path.join(rt, name), 'wb') as f:
                f.write(b'fake ' + name.encode())
        # `out` must sit OUTSIDE the fake repo -- the staging guard refuses
        # anything inside it (test_refuses_to_stage_inside_the_repo).
        out = tempfile.mkdtemp(prefix='embody_smoke_run_out_')
        build = smoke.select_build(repo)
        return repo, smoke.stage_run(repo, build, out, platform='win32', **kw)

    def test_fresh_dir_with_template_bootstrap_and_sidecar(self):
        repo, run = self._stage()
        self.assertTrue(os.path.isfile(
            os.path.join(run['dir'], 'smoke_template.toe')))
        self.assertTrue(os.path.isfile(
            os.path.join(run['dir'], 'smoke_bootstrap.py')))
        with open(os.path.join(run['dir'], 'smoke_run.json'),
                  encoding='utf-8') as f:
            cfg = json.load(f)
        self.assertEqual(cfg['repo_root'], repo)
        self.assertEqual(cfg['flags_dir'], run['dir'])
        self.assertEndsWith(cfg['tox_path'], 'Embody-v6.2.56.tox')
        self.assertStartsWith(cfg['run_id'], 'win32-')

    def test_two_runs_never_share_a_directory(self):
        """Same out root, same second: the second run gets a -2 suffix and
        the first directory (and its sidecar) is still there."""
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'},
                          manifest_asset='Embody-v6.2.56.tox')
        rt = os.path.join(repo, 'dev', 'release_testing')
        os.makedirs(rt, exist_ok=True)
        for name in ('smoke_template.toe', 'smoke_bootstrap.py'):
            with open(os.path.join(rt, name), 'wb') as f:
                f.write(b'fake')
        out = tempfile.mkdtemp(prefix='embody_smoke_run_out_')
        build = smoke.select_build(repo)
        clock = lambda: 1_700_000_000.0  # noqa: E731
        a = smoke.stage_run(repo, build, out, platform='win32', now=clock)
        b = smoke.stage_run(repo, build, out, platform='win32', now=clock)
        self.assertNotEqual(a['dir'], b['dir'])
        self.assertEndsWith(b['run_id'], '-2')
        for run in (a, b):
            self.assertTrue(os.path.isfile(
                os.path.join(run['dir'], 'smoke_run.json')),
                'staging a new run must never remove an old one')

    def test_refuses_to_stage_inside_the_repo(self):
        """The 2026-07-27 incident: a smoke rooted in the repo deploys its
        AI config there and strips committed files."""
        repo = self._repo({'Embody-v6.2.56.tox': b'payload'},
                          manifest_asset='Embody-v6.2.56.tox')
        build = smoke.select_build(repo)
        with self.assertRaises(smoke.SmokeSetupError):
            smoke.stage_run(repo, build, os.path.join(repo, 'dev', 'tmp'),
                            platform='win32')


class TestExitCode(_Case):

    def test_codes(self):
        self.assertEqual(smoke.exit_code({'outcome': 'PASS'}), 0)
        self.assertEqual(smoke.exit_code({'outcome': 'FAIL'}), 1)
        self.assertEqual(smoke.exit_code({'outcome': 'ERROR'}), 2)


IS_TD = lambda pid: True  # noqa: E731


class TestOwnProcessGuard(_Case):
    """Teardown may only ever signal the TD the run launched: the pid must
    be a live TouchDesigner AND its command line must name the run dir."""

    def test_pid_with_foreign_cmdline_is_refused(self):
        ok = smoke.owns_process(4242, '/runs/win32-1', is_td=IS_TD,
                                cmdline=lambda pid: 'TouchDesigner.exe '
                                                    'C:/other/project.toe')
        self.assertFalse(ok)

    def test_pid_with_run_dir_in_cmdline_is_accepted(self):
        ok = smoke.owns_process(4242, 'C:/runs/win32-1', is_td=IS_TD,
                                cmdline=lambda pid: 'TouchDesigner.exe '
                                'C:/runs/win32-1/smoke_template.toe')
        self.assertTrue(ok)

    def test_unreadable_cmdline_is_refused(self):
        ok = smoke.owns_process(4242, '/runs/win32-1', is_td=IS_TD,
                                cmdline=lambda pid: None)
        self.assertFalse(ok)

    def test_non_td_process_is_refused_even_with_our_path(self):
        """Pid reuse: another process whose argv names the run dir."""
        ok = smoke.owns_process(4242, 'C:/runs/win32-1',
                                is_td=lambda pid: False,
                                cmdline=lambda pid: 'notepad.exe '
                                'C:/runs/win32-1/smoke_template.toe')
        self.assertFalse(ok)

    def test_argv_may_hold_the_unresolved_spelling(self):
        """macOS /var -> /private/var and Windows 8.3 short paths: argv
        holds the run dir as given, realpath resolves it differently."""
        ok = smoke.owns_process(
            4242, '/var/folders/ab/T/embody-smoke/darwin-1', is_td=IS_TD,
            cmdline=lambda pid: 'TouchDesigner /var/folders/ab/T/'
                                'embody-smoke/darwin-1/smoke_template.toe',
            realpath=lambda p: '/private' + p)
        self.assertTrue(ok)

    def test_argv_may_hold_the_resolved_spelling(self):
        ok = smoke.owns_process(
            4242, '/var/folders/ab/T/embody-smoke/darwin-1', is_td=IS_TD,
            cmdline=lambda pid: 'TouchDesigner /private/var/folders/ab/T/'
                                'embody-smoke/darwin-1/smoke_template.toe',
            realpath=lambda p: '/private' + p)
        self.assertTrue(ok)


class TestSelectReply(_Case):
    """A pushed notification on the SSE stream must never be taken as the
    tool result (the bug envoy_bridge._route_sse_frames exists for)."""

    def test_matching_id_is_picked_past_a_notification(self):
        body = ('event: message\ndata: {"jsonrpc":"2.0","method":'
                '"notifications/message","params":{"level":"info"}}\n\n'
                'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":'
                '{"content":[]}}\n')
        self.assertEqual(smoke.select_reply(body, 7)['id'], 7)

    def test_only_notifications_is_an_error(self):
        body = 'data:{"jsonrpc":"2.0","method":"notifications/message"}\n'
        self.assertIn('error', smoke.select_reply(body, 7))

    def test_plain_json_and_garbage(self):
        self.assertEqual(smoke.select_reply('{"id": 3, "result": {}}', 3)['id'],
                         3)
        self.assertIn('error', smoke.select_reply('<html>', 3))


# ===========================================================================
# smoke_bootstrap.py -- the features flag while the phase is running
# ===========================================================================

class TestFeaturesFlagText(_Case):

    def test_unreached_legs_are_pending_until_final(self):
        """The file is rewritten after every leg. A reader polling between
        two legs must keep waiting -- SKIP mid-phase read as six phantom
        failures."""
        text = boot._features_flag_text({'embody_core': ('PASS', 'ok')})
        self.assertIn('embody_core=PASS|ok', text)
        self.assertIn('convoy=PENDING|not reached', text)
        self.assertNotIn('SKIP', text)
        self.assertFalse(smoke.parse_features(text)['terminal'])

    def test_final_write_marks_unreached_as_skip(self):
        text = boot._features_flag_text({}, final=True)
        self.assertEqual(text.count('=SKIP|not reached'), 7)
        r = smoke.parse_features(text)
        self.assertTrue(r['terminal'])
        self.assertFalse(r['passed'])

    def test_order_is_feature_order(self):
        text = boot._features_flag_text({}, final=True)
        names = [l.split('=', 1)[0] for l in text.splitlines()]
        self.assertEqual(tuple(names), boot.FEATURE_ORDER)


class TestRepoRootResolution(_Case):

    def test_first_existing_candidate_wins(self):
        other = tempfile.mkdtemp(prefix='embody_smoke_other_')
        self.assertEqual(boot._resolve_repo_root([None, other], '/nope'),
                         os.path.normpath(other))

    def test_stale_sidecar_falls_through_to_env(self):
        env_repo = tempfile.mkdtemp(prefix='embody_smoke_env_')
        got = boot._resolve_repo_root(
            [os.path.join(self.root, 'gone'), env_repo], '/nope')
        self.assertEqual(got, os.path.normpath(env_repo))

    def test_fallback_when_nothing_exists(self):
        got = boot._resolve_repo_root([None, ''], os.path.join(self.root, 'x'))
        self.assertEqual(got, os.path.normpath(os.path.join(self.root, 'x')))


# ===========================================================================
# smoke_run.py -- probe, waiting, outcome, teardown, logs
# ===========================================================================

def _envoy_like_rpc(replies):
    """An `rpc` stand-in: `replies` maps tool name (or 'tools/list' /
    'initialize') to the tool's own JSON; unknown -> Envoy-shaped error."""
    def rpc(url, payload, timeout):
        method = payload['method']
        if method == 'initialize':
            return {'result': {'serverInfo': {'name': 'Envoy'}}}
        if method == 'tools/list':
            return {'result': {'tools': [{'name': n} for n in replies.get(
                'tools/list', ['create_op', 'delete_op'])]}}
        name = payload['params']['name']
        body = replies.get(name)
        if callable(body):
            body = body(payload['params']['arguments'])
        if body is None:
            body = {'error': f'{name}: not scripted'}
        return {'result': {'content': [{'type': 'text',
                                        'text': json.dumps(body)}]}}
    return rpc


class TestProbeMcp(_Case):

    def _happy(self):
        state = {'present': False}

        def create(args):
            state['present'] = True
            return {'success': True, 'path': '/smoke_mcp_probe'}

        def query(args):
            return {'operators': (['smoke_mcp_probe'] if state['present']
                                  else [])}

        def delete(args):
            state['present'] = False
            return {'success': True}

        return {
            'tools/list': ['create_op', 'set_parameter', 'query_network',
                           'get_op_errors', 'delete_op'],
            'create_op': create, 'set_parameter': {'success': True},
            'query_network': query,
            'get_op_errors': {'errorCount': 0, 'errors': []},
            'delete_op': delete,
        }

    def test_happy_path_runs_every_step(self):
        r = smoke.probe_mcp(9871, timeout=5, clock=lambda: 0.0,
                            sleep=lambda s: None,
                            rpc=_envoy_like_rpc(self._happy()))
        self.assertTrue(r['ok'], r['error'])
        self.assertEqual([s['step'] for s in r['steps']],
                         ['initialize', 'tools/list', 'create_op',
                          'set_parameter', 'query_network', 'get_op_errors',
                          'delete_op', 'query_network', 'verify_delete'])
        self.assertEqual(r['tools'], 5)

    def test_missing_error_count_is_a_failure(self):
        replies = self._happy()
        replies['get_op_errors'] = {'errors': []}  # no errorCount key
        r = smoke.probe_mcp(9871, timeout=5, clock=lambda: 0.0,
                            sleep=lambda s: None, rpc=_envoy_like_rpc(replies))
        self.assertFalse(r['ok'])
        self.assertIn('errorCount', r['error'])

    def test_delete_that_does_not_land_is_a_failure(self):
        replies = self._happy()
        replies['delete_op'] = {'success': True}  # claims ok, op stays
        r = smoke.probe_mcp(9871, timeout=5, clock=lambda: 0.0,
                            sleep=lambda s: None, rpc=_envoy_like_rpc(replies))
        self.assertFalse(r['ok'])
        self.assertIn('still listed', r['error'])

    def test_envoy_error_reply_fails_the_step(self):
        replies = self._happy()
        replies['set_parameter'] = {'error': 'Parameter not found: value0'}
        r = smoke.probe_mcp(9871, timeout=5, clock=lambda: 0.0,
                            sleep=lambda s: None, rpc=_envoy_like_rpc(replies))
        self.assertFalse(r['ok'])
        self.assertIn('Parameter not found', r['error'])

    def test_identity_mismatch_refuses_to_mutate(self):
        """A stale or collided port could be the developer's live project:
        no create_op until project.folder is the run dir."""
        replies = self._happy()
        calls = []
        replies['execute_python'] = lambda a: {'success': True,
                                               'result': 'C:/dev/Embody/dev'}
        orig_create = replies['create_op']
        replies['create_op'] = lambda a: (calls.append('create'),
                                          orig_create(a))[1]
        r = smoke.probe_mcp(9870, timeout=5, clock=lambda: 0.0,
                            sleep=lambda s: None, rpc=_envoy_like_rpc(replies),
                            expect_dir='C:/runs/win32-1')
        self.assertFalse(r['ok'])
        self.assertIn('refusing to mutate', r['error'])
        self.assertEqual(calls, [], 'must not create anything on a foreign TD')

    def test_identity_match_proceeds(self):
        replies = self._happy()
        replies['execute_python'] = lambda a: {'success': True,
                                               'result': self.root}
        r = smoke.probe_mcp(9871, timeout=5, clock=lambda: 0.0,
                            sleep=lambda s: None, rpc=_envoy_like_rpc(replies),
                            expect_dir=self.root)
        self.assertTrue(r['ok'], r['error'])
        self.assertIn('identity', [s['step'] for s in r['steps']])

    def test_budget_exhaustion_stops_the_probe(self):
        t = {'now': 0.0}

        def slow_rpc(url, payload, timeout):
            t['now'] += 20  # every call eats 20s of a 30s budget
            return _envoy_like_rpc(self._happy())(url, payload, timeout)

        r = smoke.probe_mcp(9871, timeout=30, clock=lambda: t['now'],
                            sleep=lambda s: None, rpc=slow_rpc)
        self.assertFalse(r['ok'])
        self.assertIn('budget', r['error'])

    def test_no_handshake_within_timeout(self):
        t = {'now': 0.0}

        def rpc(url, payload, timeout):
            raise OSError('connection refused')

        r = smoke.probe_mcp(9871, timeout=5, clock=lambda: t['now'],
                            sleep=lambda s: t.__setitem__('now', t['now'] + s),
                            rpc=rpc)
        self.assertFalse(r['ok'])
        self.assertIn('no MCP handshake', r['error'])

    def test_unparseable_reply_fails_closed(self):
        self.assertIn('error', smoke._tool_result({'raw': '<html>'}))
        self.assertIn('error', smoke._tool_result({'result': {}}))
        self.assertIn('error', smoke._tool_result(
            {'result': {'content': [{'type': 'text', 'text': 'not json'}]}}))
        self.assertIn('error', smoke._tool_result(
            {'result': {'content': [{'type': 'text', 'text': '[1, 2]'}]}}))
        self.assertEqual(smoke._tool_result(
            {'result': {'content': [{'type': 'text', 'text': '{"a": 1}'}]}}),
            {'a': 1})


class TestWaitFor(_Case):

    def test_returns_when_predicate_holds(self):
        path = os.path.join(self.root, 'ready.flag')
        t = {'now': 0.0}

        def sleep(s):
            t['now'] += s
            if t['now'] >= 4:
                with open(path, 'w') as f:
                    f.write('verdict=PASS\ntox=x\n')

        text, ok = smoke.wait_for(path, 30, lambda x: 'tox=' in x,
                                  clock=lambda: t['now'], sleep=sleep)
        self.assertTrue(ok)
        self.assertIn('verdict=PASS', text)

    def test_deadline_keeps_partial_text(self):
        path = os.path.join(self.root, 'features.flag')
        with open(path, 'w') as f:
            f.write('embody_core=PASS|ok\nconvoy=PENDING|installing\n')
        t = {'now': 0.0}
        text, ok = smoke.wait_for(path, 10, lambda x: False,
                                  clock=lambda: t['now'],
                                  sleep=lambda s: t.__setitem__(
                                      'now', t['now'] + s))
        self.assertFalse(ok)
        self.assertIn('PENDING', text)

    def test_zero_budget_still_reads_an_existing_flag(self):
        path = os.path.join(self.root, 'ready.flag')
        with open(path, 'w') as f:
            f.write('verdict=PASS\ntox=x\n')
        text, ok = smoke.wait_for(path, 0, lambda x: 'tox=' in x,
                                  clock=lambda: 0.0, sleep=lambda s: None)
        self.assertTrue(ok)


class TestComputeOutcome(_Case):

    READY = {'verdict': 'PASS'}
    FEATS = {'passed': True, 'terminal': True, 'failed': []}
    MCP = {'ok': True}
    TD = {'ok': True, 'method': 'mcp'}

    def test_all_gates_pass(self):
        self.assertEqual(smoke.compute_outcome(
            self.READY, self.FEATS, self.MCP, self.TD), 'PASS')

    def test_teardown_failure_is_not_green(self):
        self.assertEqual(smoke.compute_outcome(
            self.READY, self.FEATS, self.MCP,
            {'ok': False, 'method': 'refused'}), 'FAIL')

    def test_mcp_failure_is_not_green(self):
        self.assertEqual(smoke.compute_outcome(
            self.READY, self.FEATS, {'ok': False}, self.TD), 'FAIL')
        self.assertEqual(smoke.compute_outcome(
            self.READY, self.FEATS, None, self.TD, no_mcp=True), 'PASS')

    def test_ready_or_features_failure(self):
        self.assertEqual(smoke.compute_outcome(
            {'verdict': 'FAIL'}, None, None, self.TD), 'FAIL')
        self.assertEqual(smoke.compute_outcome(
            self.READY, {'passed': False}, self.MCP, self.TD), 'FAIL')

    def test_run_stamp_mismatch_is_refused(self):
        with self.assertRaises(smoke.SmokeSetupError):
            smoke.check_run_stamp({'run_id': 'win32-old'}, 'win32-new')
        smoke.check_run_stamp({'run_id': ''}, 'win32-new')  # hand-run: ok


class TestQuitSmokeTd(_Case):

    def _quit(self, port=9871, owns=True, mcp_ok=True, dies_after=2,
              hard=(True, 'TouchDesigner (PID 7) was force-killed')):
        t = {'now': 0.0}
        state = {'alive': True, 'mcp_calls': 0, 'hard_calls': 0}

        def mcp_quit(p):
            state['mcp_calls'] += 1
            if not mcp_ok:
                raise OSError('refused')

        def alive(pid):
            if state['mcp_calls'] and mcp_ok and t['now'] >= dies_after:
                state['alive'] = False
            return state['alive']

        def hard_quit(pid):
            state['hard_calls'] += 1
            state['alive'] = False
            return hard

        if owns:
            def with_cmd(pid):
                return f'TouchDesigner {self.root}/smoke.toe'
        else:
            def with_cmd(pid):
                return 'TouchDesigner /elsewhere.toe'
        orig = smoke.process_cmdline
        smoke.process_cmdline = with_cmd
        try:
            r = smoke.quit_smoke_td(
                7, self.root, port, alive=alive, mcp_quit=mcp_quit,
                hard_quit=hard_quit, clock=lambda: t['now'],
                sleep=lambda s: t.__setitem__('now', t['now'] + s),
                is_td=IS_TD)
        finally:
            smoke.process_cmdline = orig
        return r, state

    def test_mcp_quit_is_preferred_and_reported(self):
        r, state = self._quit()
        self.assertTrue(r['ok'])
        self.assertEqual(r['method'], 'mcp')
        self.assertEqual(state['hard_calls'], 0)

    def test_falls_back_to_pid_scoped_quit_and_names_the_force(self):
        r, state = self._quit(mcp_ok=False)
        self.assertTrue(r['ok'])
        self.assertEqual(r['method'], 'forced')
        self.assertEqual(state['hard_calls'], 1)

    def test_graceful_close_is_reported_as_close(self):
        r, _ = self._quit(mcp_ok=False,
                          hard=(True, 'TouchDesigner (PID 7) exited gracefully'))
        self.assertEqual(r['method'], 'close')

    def test_foreign_process_is_refused_before_anything(self):
        r, state = self._quit(owns=False)
        self.assertFalse(r['ok'])
        self.assertEqual(r['method'], 'refused')
        self.assertEqual((state['mcp_calls'], state['hard_calls']), (0, 0))

    def test_no_port_skips_mcp(self):
        r, state = self._quit(port=None, hard=(True, 'exited gracefully'))
        self.assertEqual(state['mcp_calls'], 0)
        self.assertEqual(r['method'], 'close')


class TestLogsAndConvoyState(_Case):

    def test_collect_logs_spans_rotated_files(self):
        logs = os.path.join(self.root, 'logs')
        os.makedirs(logs)
        with open(os.path.join(logs, 'a.4.toe.log'), 'w') as f:
            f.write('00:01 WARNING EmbodyExt: venv NOT wired\n00:02 INFO boot\n')
        os.utime(os.path.join(logs, 'a.4.toe.log'), (1, 1))
        with open(os.path.join(logs, 'a.5.toe.log'), 'w') as f:
            f.write('00:03 INFO after save\n00:04 ERROR ConvoyExt: x\n')
        got = smoke.collect_logs(self.root, lines=3)
        self.assertEqual(len(got['warnings']), 2)
        self.assertIn('venv NOT wired', got['warnings'][0])
        self.assertEqual(got['tail'].count('\n'), 3)
        self.assertIn('after save', got['tail'])

    def test_collect_logs_without_logs_dir(self):
        self.assertEqual(smoke.collect_logs(self.root),
                         {'tail': '', 'warnings': []})

    def test_bootstrap_mirror_log_is_read_before_embody_logs(self):
        """Before the feature phase's save there is no Embody log at all;
        the bootstrap's mirror is the only evidence of a wedged startup."""
        with open(os.path.join(self.root, 'bootstrap.log'), 'w') as f:
            f.write('11:45:40 [smoke-test] ERROR: release .tox not found\n')
        got = smoke.collect_logs(self.root)
        self.assertIn('release .tox not found', got['tail'])

    def test_convoy_install_state(self):
        self.assertEqual(smoke.convoy_install_state(None, None), 'absent')
        self.assertEqual(smoke.convoy_install_state(None, 5.0), 'fresh_install')
        self.assertEqual(smoke.convoy_install_state(3.0, 5.0), 'fresh_install')
        self.assertEqual(smoke.convoy_install_state(5.0, 5.0), 'reused')

    def test_convoy_data_dir_per_platform(self):
        self.assertEndsWith(smoke.convoy_data_dir(
            'win32', env={'LOCALAPPDATA': 'C:/LA'}, home='C:/h'),
            os.path.join('C:/LA', 'EmbodyConvoy'))
        self.assertIn('Application Support', smoke.convoy_data_dir(
            'darwin', env={}, home='/Users/x'))
        self.assertIn('.local', smoke.convoy_data_dir(
            'linux', env={}, home='/home/x'))
