"""
Unit tests for the AGENT-tier runner machinery in TestRunnerExt.

NORMAL tier (no AGENT attr): these run in every standard suite pass. They
exercise the pieces of the async agent runner that work without frame
scheduling - tier discovery/gating, job launch/poll/finish primitives against
tiny real subprocesses, timeout kill, and verdict classification. The full
frame-driven path (RunAgentTests) is exercised by the AGENT suites themselves.

Subprocesses here use the project venv python from .mcp.json (SkipTest when
absent) and finish in well under a second - the brief bounded polling sleeps
below are acceptable one-offs on the main thread.
"""

import json
import os
import sys
import time

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase
AgentTestCase = runner_mod.AgentTestCase


def _find_venv_python():
    """The project venv python from .mcp.json's envoy entry, or None."""
    folder = project.folder
    for _ in range(5):
        candidate = os.path.join(folder, '.mcp.json')
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding='utf-8') as f:
                    entry = json.load(f)['mcpServers']['envoy']
                if entry.get('command') and os.path.isfile(entry['command']):
                    return entry['command']
            except Exception:
                pass
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


class TestAgentRunnerMachinery(EmbodyTestCase):

    def _python(self):
        py = _find_venv_python()
        if not py:
            raise SkipTest('.mcp.json venv python not found - run InitEnvoy')
        return py

    def _run_job_to_completion(self, job, max_s=8.0):
        """Drive poll manually (no frames) with a bounded wait.

        On bound expiry the job is killed and finished FIRST - never leave a
        live child or a stale sys-mirror entry behind (a stale entry would be
        reaped much later, after the OS may have recycled the pid)."""
        deadline = time.monotonic() + max_s
        while self.runner._pollAgentJobOnce(job) == 'running':
            if time.monotonic() > deadline:
                self.runner._killPidTree(job['proc'].pid, proc=job['proc'])
                self.runner._finishAgentJob(job)
                raise AssertionError('job did not finish within the bound')
            time.sleep(0.05)
        return self.runner._finishAgentJob(job)

    # ------------------------------------------------------------------
    # discovery gating
    # ------------------------------------------------------------------

    def test_A01_normal_discovery_excludes_tagged_tiers(self):
        suites = self.runner._discoverTestSuites(tier='normal')
        names = {cls.__name__ for cls, _mod in suites}
        self.assertNotIn('TestAgentMCPContract', names)
        self.assertNotIn('TestAgentSmokeClaude', names)
        self.assertNotIn('TestAgentSmokeCodex', names)
        self.assertNotIn('TestCustomParameters', names)  # DESTRUCTIVE
        self.assertIn('TestAgentRunnerMachinery', names)  # this suite is normal

    def test_A02_agent_discovery_only_agent_suites(self):
        suites = self.runner._discoverTestSuites(tier='agent')
        names = {cls.__name__ for cls, _mod in suites}
        self.assertIn('TestAgentMCPContract', names)
        self.assertIn('TestAgentSmokeClaude', names)
        self.assertIn('TestAgentSmokeCodex', names)
        self.assertNotIn('TestCustomParameters', names)
        self.assertNotIn('TestAgentRunnerMachinery', names)

    def test_A03_destructive_discovery_excludes_agent_suites(self):
        suites = self.runner._discoverTestSuites(tier='destructive')
        names = {cls.__name__ for cls, _mod in suites}
        self.assertIn('TestCustomParameters', names)
        self.assertNotIn('TestAgentMCPContract', names)
        self.assertNotIn('TestAgentSmokeClaude', names)
        self.assertNotIn('TestAgentRunnerMachinery', names)

    # ------------------------------------------------------------------
    # job primitives
    # ------------------------------------------------------------------

    def test_B01_job_lifecycle_captures_output(self):
        py = self._python()
        spec = {
            'argv': [py, '-c',
                     'import sys; sys.stdout.write("hello-out"); '
                     'sys.stderr.write("hello-err")'],
            'timeout_s': 20.0, 'env': None, 'cwd': None,
            'verify': None, 'label': 'echo', 'cmdline': None,
        }
        job = self.runner._launchAgentJob(spec)
        pid = job['proc'].pid
        mirror = getattr(sys, '_embody_agent_jobs', {})
        self.assertIn(pid, mirror, 'pid not mirrored on sys during the job')
        result = self._run_job_to_completion(job)
        self.assertEqual(result['returncode'], 0)
        self.assertIn('hello-out', result['stdout'])
        self.assertIn('hello-err', result['stderr'])
        self.assertFalse(result['timed_out'])
        self.assertNotIn(pid, mirror, 'pid still mirrored after finish')
        # Captured temp files are deleted after reading.
        self.assertFalse(os.path.exists(job['stdout_path']))
        self.assertFalse(os.path.exists(job['stderr_path']))

    def test_B02_timeout_kills_process_tree(self):
        py = self._python()
        spec = {
            'argv': [py, '-c', 'import time; time.sleep(30)'],
            'timeout_s': 600.0, 'env': None, 'cwd': None,
            'verify': None, 'label': 'sleeper', 'cmdline': None,
        }
        job = self.runner._launchAgentJob(spec)
        job['deadline'] = time.perf_counter() - 1.0  # force expiry now
        state = self.runner._pollAgentJobOnce(job)
        self.assertEqual(state, 'done')
        self.assertTrue(job['timed_out'])
        result = self.runner._finishAgentJob(job)
        self.assertTrue(result['timed_out'])
        self.assertIsNotNone(job['proc'].poll(),
                             'sleeper survived the timeout kill')

    def test_B03_launch_rejects_bad_spec(self):
        with self.assertRaises(ValueError):
            self.runner._launchAgentJob({'argv': None, 'cmdline': None})
        with self.assertRaises(ValueError):
            self.runner._launchAgentJob('not a dict')

    # ------------------------------------------------------------------
    # verdict classification
    # ------------------------------------------------------------------

    def _result(self, rc=0, timed_out=False):
        return {'returncode': rc, 'stdout': 'out', 'stderr': 'err',
                'duration_s': 1.0, 'timed_out': timed_out}

    def test_C01_classify_no_verify_uses_exit_code(self):
        status, _ = self.runner._classifyVerdict(None, self._result(0))
        self.assertEqual(status, 'PASS')
        status, msg = self.runner._classifyVerdict(None, self._result(3))
        self.assertEqual(status, 'FAIL')
        self.assertIn('exit code 3', msg)

    def test_C02_classify_timeout_is_error(self):
        status, msg = self.runner._classifyVerdict(
            None, self._result(0, timed_out=True))
        self.assertEqual(status, 'ERROR')
        self.assertIn('timed out', msg)

    def test_C03_classify_verify_outcomes(self):
        def ok(result):
            pass

        def fails(result):
            raise AssertionError('nope')

        def skips(result):
            raise SkipTest('later')

        def blows_up(result):
            raise RuntimeError('boom')

        self.assertEqual(
            self.runner._classifyVerdict(ok, self._result())[0], 'PASS')
        status, msg = self.runner._classifyVerdict(fails, self._result())
        self.assertEqual((status, msg), ('FAIL', 'nope'))
        status, msg = self.runner._classifyVerdict(skips, self._result())
        self.assertEqual((status, msg), ('SKIP', 'later'))
        status, msg = self.runner._classifyVerdict(blows_up, self._result())
        self.assertEqual(status, 'ERROR')
        self.assertIn('boom', msg)

    def test_C04_stale_agent_tick_is_inert_during_normal_run(self):
        """A stale agent tick (run-id mismatch) must not touch a live run.

        Regression test for the shared-_running hazard: a tick surviving an
        extension reinit must never finalize a normal run that started after
        the reinit, un-suppress its dialogs, or clear its flags."""
        self.assertTrue(self.runner._running,
                        'expected to be inside a live normal run')
        comp = self.runner.ownerComp
        saved = comp.fetch(self.runner._FC_KEY, None, search=False)
        self.runner._agentTick(run_id=-12345)  # no agent run owns this id
        self.assertTrue(self.runner._running,
                        'stale tick cleared _running of a live run')
        self.assertEqual(self.embody.par.Filecleanup.eval(), 'delete',
                         'stale tick un-suppressed dialogs mid-run')
        self.assertEqual(
            comp.fetch(self.runner._FC_KEY, None, search=False), saved)

    # ------------------------------------------------------------------
    # AgentTestCase helpers
    # ------------------------------------------------------------------

    def test_D01_job_builder_shape_and_validation(self):
        case = AgentTestCase(sandbox=self.sandbox, embody=self.embody,
                             runner=self.runner)
        spec = case.job(argv=['x'], verify=None, label='t')
        self.assertEqual(spec['argv'], ['x'])
        self.assertEqual(spec['timeout_s'], 240.0)
        self.assertEqual(spec['label'], 't')
        self.assertIsNone(spec['cmdline'])
        with self.assertRaises(ValueError):
            case.job()

    def test_D02_agent_base_is_tagged_and_uninstantiable_as_suite(self):
        self.assertTrue(getattr(AgentTestCase, 'AGENT', False))
        # No test_ methods of its own: never picked up as a suite.
        self.assertFalse(
            any(m.startswith('test_') for m in AgentTestCase.__dict__))

    def test_D03_filecleanup_storage_roundtrip(self):
        """Suppress/restore survives instance boundaries via COMP storage.

        A test run is active, so the runner has ALREADY suppressed the
        dialogs; exercise the re-entrancy guard and a restore/re-suppress
        roundtrip, leaving the suppressed state exactly as we found it."""
        comp = self.runner.ownerComp
        saved = comp.fetch(self.runner._FC_KEY, None, search=False)
        self.assertIsNotNone(saved, 'runner did not storage-back the saved '
                                    'Filecleanup value for this run')
        self.assertEqual(self.embody.par.Filecleanup.eval(), 'delete')
        # Re-entrancy: a second suppress must NOT overwrite the original.
        self.runner._suppressFileCleanupDialog()
        self.assertEqual(
            comp.fetch(self.runner._FC_KEY, None, search=False), saved)
        # Roundtrip: restore brings the original back and clears storage...
        self.runner._restoreFileCleanupDialog()
        try:
            self.assertEqual(self.embody.par.Filecleanup.eval(), saved)
            self.assertIsNone(
                comp.fetch(self.runner._FC_KEY, None, search=False))
        finally:
            # ...then re-suppress so the active run stays as it was.
            self.runner._suppressFileCleanupDialog()
        self.assertEqual(self.embody.par.Filecleanup.eval(), 'delete')
        self.assertEqual(
            comp.fetch(self.runner._FC_KEY, None, search=False), saved)
