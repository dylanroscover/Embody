"""
Embody Test Framework - Test Runner Extension

Provides test discovery, execution, and reporting for the Embody project.
Lives at /embody/unit_tests as a TD extension.

Usage:
    op.unit_tests.RunTests()                              # Run all (non-blocking, one test per frame)
    op.unit_tests.RunTests(suite_name='test_path_utils')  # Run one suite (non-blocking)
    op.unit_tests.RunTests(delay_frames=5)                # Run all (one test every 5 frames)
    op.unit_tests.RunTestsSync()                          # Run all (synchronous, blocks TD)
    op.unit_tests.RunTestsDeferred()                      # Run all (one suite per frame)
    op.unit_tests.RunTestsDeferredPerTest()                # Run all (one test per frame)
    op.unit_tests.RunAgentTests()                         # AGENT tier: AI-client subprocesses
    op.unit_tests.GetResults()                            # Get results dict
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import deque
from unittest import SkipTest, TestCase


# =============================================================================
# BASE TEST CASE
# =============================================================================

class EmbodyTestCase(TestCase):
    """
    Base class for all Embody test suites.

    Each test file should define a class inheriting from this.
    The test runner injects sandbox, embody, and runner references.
    """

    def __init__(self, sandbox, embody, runner):
        super().__init__()
        self.sandbox = sandbox          # baseCOMP to create temp operators in
        self.embody = embody            # op.Embody reference (the Embody COMP)
        self.runner = runner            # TestRunner instance

    @property
    def embody_ext(self):
        """Resolve EmbodyExt live on every access to avoid stale references.

        Never cache extension references - TD may reinitialize the extension at
        any time (e.g., when an externalized .py file changes on disk), which
        silently invalidates any cached reference. Always resolve inline via
        the component (CLAUDE.md rule #26).
        """
        return self.embody.ext.Embody

    def tearDown(self):
        if self.sandbox is not None:
            for child in list(self.sandbox.children):
                try:
                    child.destroy()
                except Exception:
                    pass


    def assertStartsWith(self, s, prefix, msg=None):
        if not str(s).startswith(prefix):
            raise AssertionError(msg or f'{repr(s)} does not start with {repr(prefix)}')

    def assertEndsWith(self, s, suffix, msg=None):
        if not str(s).endswith(suffix):
            raise AssertionError(msg or f'{repr(s)} does not end with {repr(suffix)}')

    def assertDictHasKey(self, d, key, msg=None):
        if key not in d:
            raise AssertionError(msg or f'Key {repr(key)} not in dict')

    def assertLen(self, container, expected_len, msg=None):
        actual = len(container)
        if actual != expected_len:
            raise AssertionError(
                msg or f'Expected length {expected_len}, got {actual}')


# =============================================================================
# AGENT-TIER TEST CASE
# =============================================================================

class AgentTestCase(EmbodyTestCase):
    """
    Base class for AGENT-tier suites: tests that spawn external AI-client
    subprocesses (claude -p, codex exec, the tier-1 MCP contract client) which
    connect BACK to Envoy over MCP while TD keeps cooking.

    Subclasses inherit ``AGENT = True`` and are therefore excluded from every
    normal run and from RunDestructiveTests; they run only via
    ``op.unit_tests.RunAgentTests()``.

    A test method either asserts synchronously and returns None (reported like
    a normal test), or returns a JOB SPEC built with ``self.job(...)``. The
    runner launches the described subprocess WITHOUT blocking TD's main thread
    (MCP requests drain there - blocking would deadlock the tools under test),
    polls it across frames, and when it exits calls ``verify(result)`` on the
    main thread. ``result`` is ``{'returncode', 'stdout', 'stderr',
    'duration_s', 'timed_out'}``. In ``verify``: raise AssertionError to FAIL,
    SkipTest to SKIP, return cleanly to PASS. ``verify`` may (and should)
    inspect live TD state - the agent's side effects are the primary evidence.
    """

    AGENT = True

    def job(self, argv=None, cmdline=None, timeout_s=240, env=None, cwd=None,
            verify=None, label=None):
        """Build a job spec for the agent runner.

        Args:
            argv:      Argument list for subprocess.Popen (preferred).
            cmdline:   Full command-line string (Windows .cmd shim fallback).
            timeout_s: Wall-clock deadline; on expiry the whole process TREE
                       is killed and the test reports ERROR.
            env:       Full child environment (see launchEnv()).
            cwd:       Working directory (see neutralCwd()).
            verify:    callable(result) -> None, run on the main thread.
            label:     Short human-readable description for the log.
        """
        if not argv and not cmdline:
            raise ValueError('job() needs argv or cmdline')
        return {
            'argv': list(argv) if argv else None,
            'cmdline': cmdline,
            'timeout_s': float(timeout_s),
            'env': env,
            'cwd': cwd,
            'verify': verify,
            'label': label or 'agent job',
        }

    # ------------------------------------------------------------------
    # CLI + environment helpers
    # ------------------------------------------------------------------

    def resolveCli(self, cli):
        """Absolute path of an installed AI CLI (claude/codex/gemini), or None.

        Delegates to embody_launch.resolve_cli_abs - the same filesystem
        probes the Launch AI Client button uses (no subprocess, main-thread
        safe)."""
        try:
            launch = self.embody.op('embody_launch').module
            return launch.resolve_cli_abs(self.embody_ext, cli)
        except Exception:
            return None

    def requireCli(self, cli):
        """Return the CLI's absolute path or SkipTest loudly when missing."""
        path = self.resolveCli(cli)
        if not path:
            raise SkipTest(
                f'{cli} CLI not installed (standard install locations probed) '
                f'- install it and log in to run this agent test')
        return path

    def launchEnv(self, session_label):
        """Cleaned child environment for AI-client subprocesses.

        Starts from embody_launch.launch_env (TD-injected vars stripped - a
        venv python child would otherwise inherit TD's PYTHONHOME/PYTHONPATH
        and fail to import), then removes every API-key variable so headless
        CLIs bill the user's SUBSCRIPTION login, never an API key, and labels
        the session so Envoy peers can identify the test agent."""
        try:
            launch = self.embody.op('embody_launch').module
            env = launch.launch_env(self.embody_ext)
        except Exception:
            env = dict(os.environ)
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                    'OPENAI_API_KEY', 'CODEX_API_KEY'):
            env.pop(key, None)
        env['EMBODY_SESSION_LABEL'] = session_label
        return env

    def neutralCwd(self):
        """A temp working directory outside the repo.

        Running the CLI from here keeps project context (CLAUDE.md, AGENTS.md,
        session rituals) out of the smoke task, so the agent does exactly the
        micro-task and nothing else. Cleaned up in tearDownSuite."""
        import tempfile
        d = tempfile.mkdtemp(prefix='embody_agent_smoke_')
        self._agent_tmpdirs = getattr(self, '_agent_tmpdirs', [])
        self._agent_tmpdirs.append(d)
        return d

    def tearDownSuite(self):
        """Remove temp dirs created via neutralCwd (scratch only, never repo)."""
        import shutil
        for d in getattr(self, '_agent_tmpdirs', []):
            try:
                shutil.rmtree(d)
            except Exception:
                pass
        self._agent_tmpdirs = []

    # ------------------------------------------------------------------
    # Envoy bridge discovery (the SAME command Claude Code spawns)
    # ------------------------------------------------------------------

    def findMcpJson(self):
        """Path of the repo's .mcp.json (walking up from project.folder), or None."""
        import json
        folder = project.folder
        for _ in range(5):
            candidate = os.path.join(folder, '.mcp.json')
            if os.path.isfile(candidate):
                try:
                    with open(candidate, encoding='utf-8') as f:
                        data = json.load(f)
                    if 'envoy' in data.get('mcpServers', {}):
                        return candidate
                except Exception:
                    pass
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
        return None

    def envoyBridgeEntry(self):
        """The envoy stdio server entry from .mcp.json: {'command', 'args'}.

        This is EXACTLY what Claude Code spawns, so agent tests exercise the
        same bridge command a real session uses. SkipTest when unavailable."""
        import json
        mcp_json = self.findMcpJson()
        if mcp_json is None:
            raise SkipTest('.mcp.json with an envoy server not found above '
                           'project.folder - run op.Embody.InitEnvoy() first')
        with open(mcp_json, encoding='utf-8') as f:
            entry = json.load(f)['mcpServers']['envoy']
        if entry.get('type') != 'stdio' or not entry.get('command'):
            raise SkipTest('.mcp.json envoy entry is not a stdio bridge '
                           '(HTTP fallback?) - agent tests need the bridge')
        return entry


# =============================================================================
# TEST RUNNER EXTENSION
# =============================================================================

class TestRunnerExt:
    """
    Test runner extension for /embody/unit_tests.

    Discovers test suites by loading .py files from dev/embody/unit_tests/,
    runs them with sandbox isolation, and reports results.
    """

    # COMP-storage keys for the saved continuity-dialog preferences. Storage
    # (not instance attributes) so a mid-run extension reinit cannot lose the
    # originals -- restore works from ANY instance, including the fresh one a
    # reinit creates while an agent-tier run is in flight.
    _FC_KEY = '_test_saved_filecleanup'
    _TX_KEY = '_test_saved_toxdropexpr'
    _AX_KEY = '_test_saved_autoexternalize'

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        self.results_dat = self.ownerComp.op('results')
        self.sandbox_comp = self.ownerComp.op('test_sandbox')
        self._running = False
        self._results = []
        self._deferred_queue = []
        self._deferred_test_filter = None
        self._agent_queue = []
        self._agent_delay_frames = 30
        self._agent_run_id = None

    def _suppressFileCleanupDialog(self):
        """Neutralize continuity-sweep dialogs during tests.

        Sets Filecleanup to 'delete' (its dialog uses a raw ui.messageBox that
        would otherwise open a real modal) and Toxdropexpr to 'ignore' so a
        test-triggered continuity sweep never prompts about -- or mutates -- real
        dropped-.tox COMPs in the project. Both are restored afterward."""
        # Re-entrancy guard: capture the ORIGINAL value only on the first
        # suppress. If suppress runs again before restore (nested/batched runs),
        # do NOT overwrite the saved value with the already-suppressed one --
        # that is how Filecleanup got stuck at 'delete' after an interrupted batch.
        try:
            if self.ownerComp.fetch(self._FC_KEY, None, search=False) is None:
                self.ownerComp.store(self._FC_KEY,
                                     op.Embody.par.Filecleanup.eval())
            op.Embody.par.Filecleanup = 'delete'
        except Exception:
            pass
        try:
            if self.ownerComp.fetch(self._TX_KEY, None, search=False) is None:
                self.ownerComp.store(self._TX_KEY,
                                     op.Embody.par.Toxdropexpr.eval())
            op.Embody.par.Toxdropexpr = 'ignore'
        except Exception:
            pass

    def _restoreFileCleanupDialog(self):
        """Restore the original continuity-dialog preferences after tests."""
        try:
            saved = self.ownerComp.fetch(self._FC_KEY, None, search=False)
            if saved is not None:
                op.Embody.par.Filecleanup = saved
                self.ownerComp.unstore(self._FC_KEY)
        except Exception:
            pass
        try:
            saved = self.ownerComp.fetch(self._TX_KEY, None, search=False)
            if saved is not None:
                op.Embody.par.Toxdropexpr = saved
                self.ownerComp.unstore(self._TX_KEY)
        except Exception:
            pass

    def _suppressAutoexternalize(self):
        """Turn off Envoy auto-externalization for the agent-tier run.

        Agent-driven create_op routes through Envoy's auto-externalize
        chokepoint; with the preference at 'dats'/'both', every probe DAT an
        agent creates would be tagged, written to disk under test_sandbox/,
        and added to the TRACKED externalizations table -- churning repo
        files on every run. Storage-backed like the dialog suppression so a
        mid-run reinit cannot lose the original. The 'off' menu token is
        discovered from the parameter, never hardcoded."""
        try:
            par = op.Embody.par.Autoexternalize
            if self.ownerComp.fetch(self._AX_KEY, None, search=False) is None:
                self.ownerComp.store(self._AX_KEY, par.eval())
            off = next((n for n in par.menuNames
                        if n not in ('dats', 'comps', 'both')), None)
            if off is not None:
                op.Embody.par.Autoexternalize = off
        except Exception:
            pass

    def _restoreAutoexternalize(self):
        """Restore the original Autoexternalize preference after agent runs."""
        try:
            saved = self.ownerComp.fetch(self._AX_KEY, None, search=False)
            if saved is not None:
                op.Embody.par.Autoexternalize = saved
                self.ownerComp.unstore(self._AX_KEY)
        except Exception:
            pass

    # =========================================================================
    # PROMOTED METHODS
    # =========================================================================

    def RunTests(self, suite_name=None, test_name=None, delay_frames=1):
        """
        Run test suites non-blocking (one test per frame).

        This is the default entry point. Tests are spread across frames
        to keep TD's cook cycle responsive. Results are available via
        GetResults() after completion.

        Args:
            suite_name:   Run only this suite (e.g., 'test_path_utils').
            test_name:    Run only this test method within the suite.
            delay_frames: Frames between each test (default 1).
        """
        self.RunTestsDeferredPerTest(
            suite_name=suite_name,
            test_name=test_name,
            delay_frames=delay_frames,
        )

    def RunTestsSync(self, suite_name=None, test_name=None):
        """
        Run test suites synchronously (all in one frame).

        Blocks TD until all tests complete. Use for MCP or when
        you need immediate results.

        Args:
            suite_name: Run only this suite (e.g., 'test_path_utils').
            test_name:  Run only this test method within the suite.

        Returns:
            dict with total, passed, failed, errors, skipped counts + results list.
        """
        if self._running:
            self._log('Tests already running', 'WARNING')
            return {'error': 'Tests already running'}

        self._running = True
        self._suppressFileCleanupDialog()
        self._results = []
        self._initResultsTable()

        try:
            suites = self._discoverTestSuites(suite_name)
            self._log(f'Discovered {len(suites)} test suite(s)')

            for suite_class, module_name in suites:
                self._runSuite(suite_class, module_name, test_name)
        finally:
            self._running = False
            self._restoreFileCleanupDialog()

        self._reportSummary()
        return self._getSummary()

    def RunDestructiveTests(self, suite_name=None, test_name=None,
                            confirm_saved=False):
        """Run the DESTRUCTIVE whole-project suites IN ISOLATION, after a save.

        Suites tagged ``DESTRUCTIVE = True`` mutate the ENTIRE live project
        (Disable / ExternalizeProject / Reset on ext.root). They are excluded
        from every normal run (RunTests / RunTestsSync / *Deferred*) and can
        ONLY be run here. On 2026-07-01 running one of these as part of a full
        suite deleted the crown-jewel specimen .tdn files project-wide, so this
        entry point is hard-gated:

          1. It is opt-in -- caller must pass confirm_saved=True.
          2. It refuses if the project has unsaved changes (project.dirty), so a
             saved .toe always exists as a recovery point.

        After the run the live network is intentionally mutated -- reopen the
        saved .toe to restore it. See .claude/rules/destructive-tests.md.
        """
        if self._running:
            self._log('Tests already running', 'WARNING')
            return {'error': 'Tests already running'}
        if not confirm_saved:
            msg = ('RunDestructiveTests is opt-in: SAVE the project first, then '
                   'call RunDestructiveTests(confirm_saved=True). These suites '
                   'mutate the WHOLE live project (they deleted the crown-jewel '
                   '.tdn files on 2026-07-01); the saved .toe is your recovery '
                   'point. See .claude/rules/destructive-tests.md.')
            self._log(msg, 'ERROR')
            return {'error': msg}
        # project.dirty does NOT exist on TD 2025 (AttributeError) -- the old
        # getattr(project, 'dirty', None) always returned None, silently
        # defeating this save-gate. The real member is project.modified, and
        # it is NOT a plain bool, so compare by truthiness, never `is True`.
        dirty = bool(getattr(project, 'modified', False))
        if dirty:
            msg = ('RunDestructiveTests refused: project has unsaved changes '
                   '(project.modified). Save first so there is a recovery '
                   'point.')
            self._log(msg, 'ERROR')
            return {'error': msg}

        self._running = True
        self._suppressFileCleanupDialog()
        self._results = []
        self._initResultsTable()
        try:
            suites = self._discoverTestSuites(suite_name, tier='destructive')
            self._log(f'Running {len(suites)} DESTRUCTIVE suite(s) in isolation '
                      f'(save-gated)', 'WARNING')
            for suite_class, module_name in suites:
                self._runSuite(suite_class, module_name, test_name)
        finally:
            self._running = False
            self._restoreFileCleanupDialog()
        self._reportSummary()
        self._log('DESTRUCTIVE tests complete -- the LIVE project was mutated by '
                  'design. Reopen the saved .toe to restore the live network '
                  'before continuing.', 'WARNING')
        return self._getSummary()

    def RunAgentTests(self, suite_name=None, test_name=None, delay_frames=30):
        """Run the AGENT-tier suites: external AI clients driving Envoy.

        Suites tagged ``AGENT = True`` (usually by inheriting AgentTestCase)
        spawn real AI-client subprocesses -- ``claude -p``, ``codex exec``, and
        the tier-1 MCP contract client -- that connect back to Envoy over MCP
        while TD keeps cooking. They are EXCLUDED from every normal run and
        from RunDestructiveTests: they take minutes, consume the user's
        subscription usage, and depend on CLIs being installed and logged in
        on this machine (missing CLIs SKIP loudly, they never fail silently).

        The runner never blocks the main thread: Envoy drains MCP requests
        there (5 per frame), so blocking here would deadlock the very tools
        under test. Each subprocess is polled every ``delay_frames`` frames
        and killed (whole process tree) when its per-job timeout expires.

        Fire-and-forget: returns ``{'started': True, ...}`` immediately; poll
        GetResults() (or watch the results DAT / log) for completion. See
        .claude/rules/agent-tests.md.
        """
        if self._running:
            self._log('Tests already running', 'WARNING')
            return {'error': 'Tests already running'}

        self._running = True
        self._suppressFileCleanupDialog()
        self._results = []
        self._agent_delay_frames = max(1, int(delay_frames))
        self._initResultsTable()

        suites = self._discoverTestSuites(suite_name, tier='agent')
        self._log(f'Discovered {len(suites)} AGENT suite(s) '
                  f'[async, polled every {self._agent_delay_frames} frames]')

        self._agent_queue = []
        for suite_class, module_name in suites:
            methods = sorted(
                m for m in dir(suite_class)
                if m.startswith('test_') and callable(getattr(suite_class, m, None))
            )
            if test_name:
                methods = [m for m in methods if m == test_name]
            if methods:
                self._agent_queue.append({
                    'suite_class': suite_class,
                    'module_name': module_name,
                    'methods': methods,
                    'sandbox': None,
                    'instance': None,
                    'method_index': 0,
                    'setup_done': False,
                    'job': None,
                })

        if not self._agent_queue:
            self._running = False
            self._restoreFileCleanupDialog()
            self._reportSummary()
            return self._getSummary()

        # Generation token: persists on sys so it never resets across a
        # reinit -- a stale tick from a dead run can never match a new run.
        seq = int(getattr(sys, '_embody_agent_run_seq', 0)) + 1
        sys._embody_agent_run_seq = seq
        self._agent_run_id = seq
        self._suppressAutoexternalize()
        self._scheduleAgentTick()
        return {'started': True, 'suites': len(self._agent_queue),
                'run_id': seq}

    def RunTestsDeferred(self, suite_name=None, test_name=None, delay_frames=1):
        """
        Run test suites across multiple frames (one suite per frame).

        Uses run() with delayFrames to schedule each suite on a
        separate frame, keeping TD's cook cycle responsive.

        Args:
            suite_name:   Run only this suite (e.g., 'test_path_utils').
            test_name:    Run only this test method within the suite.
            delay_frames: Frames between each suite (default 1).
        """
        if self._running:
            self._log('Tests already running', 'WARNING')
            return

        self._running = True
        self._suppressFileCleanupDialog()
        self._results = []
        self._delay_frames = delay_frames
        self._initResultsTable()

        suites = self._discoverTestSuites(suite_name)
        self._log(f'Discovered {len(suites)} test suite(s) [deferred]')

        if not suites:
            self._running = False
            self._restoreFileCleanupDialog()
            self._reportSummary()
            return

        self._deferred_queue = list(suites)
        self._deferred_test_filter = test_name

        # Schedule the first suite on the next frame
        run('args[0]()', self._runNextDeferredSuite, delayFrames=self._delay_frames)

    def RunTestsDeferredPerTest(self, suite_name=None, test_name=None, delay_frames=1):
        """
        Run tests across multiple frames (one test method per frame).

        Like RunTestsDeferred but more granular - each individual test
        method gets its own frame instead of running all methods in a
        suite synchronously. Useful for heavy test suites.

        Args:
            suite_name:   Run only this suite (e.g., 'test_create_all_tops').
            test_name:    Run only this test method within the suite.
            delay_frames: Frames between each test (default 1).
        """
        if self._running:
            self._log('Tests already running', 'WARNING')
            return

        self._running = True
        self._suppressFileCleanupDialog()
        self._results = []
        self._delay_frames = delay_frames
        self._initResultsTable()

        suites = self._discoverTestSuites(suite_name)
        self._log(f'Discovered {len(suites)} test suite(s) [deferred-per-test]')

        if not suites:
            self._running = False
            self._restoreFileCleanupDialog()
            self._reportSummary()
            return

        # Build a flat queue - no sandbox/instance creation yet (deferred to first test)
        self._deferred_per_test_queue = []
        for suite_class, module_name in suites:
            methods = sorted(
                m for m in dir(suite_class)
                if m.startswith('test_') and callable(getattr(suite_class, m, None))
            )
            if test_name:
                methods = [m for m in methods if m == test_name]
            if methods:
                self._deferred_per_test_queue.append({
                    'suite_class': suite_class,
                    'module_name': module_name,
                    'methods': methods,
                    'sandbox': None,
                    'instance': None,
                    'method_index': 0,
                    'setup_done': False,
                })

        if not self._deferred_per_test_queue:
            self._running = False
            self._reportSummary()
            return

        # Schedule the first test
        run('args[0]()', self._runNextDeferredTest, delayFrames=self._delay_frames)

    def RunSuite(self, suite_name):
        """Run a single test suite by name."""
        return self.RunTests(suite_name=suite_name)

    def GetResults(self):
        """Return last test results as a summary dict."""
        return self._getSummary()

    # =========================================================================
    # DEFERRED EXECUTION
    # =========================================================================

    def _runNextDeferredSuite(self):
        """Run the next suite in the deferred queue, then schedule the next."""
        if not self._deferred_queue:
            self._running = False
            self._restoreFileCleanupDialog()
            self._reportSummary()
            return

        suite_class, module_name = self._deferred_queue.pop(0)
        self._runSuite(suite_class, module_name, self._deferred_test_filter)

        if self._deferred_queue:
            run('args[0]()', self._runNextDeferredSuite, delayFrames=self._delay_frames)
        else:
            run('args[0]()', self._finalizeDeferredRun, delayFrames=self._delay_frames)

    def _finalizeDeferredRun(self):
        """Called after all deferred suites have completed."""
        self._running = False
        self._restoreFileCleanupDialog()
        self._deferred_test_filter = None
        self._reportSummary()

    def _runNextDeferredTest(self):
        """Run the next individual test method in the per-test deferred queue."""
        if not self._deferred_per_test_queue:
            self._finalizeDeferredPerTestRun()
            return

        entry = self._deferred_per_test_queue[0]
        module_name = entry['module_name']

        # Lazy init: create sandbox and instance on first access
        if entry['instance'] is None:
            entry['sandbox'] = self._createSandbox(module_name)
            try:
                entry['instance'] = entry['suite_class'](
                    sandbox=entry['sandbox'],
                    embody=op.Embody,
                    runner=self,
                )
            except Exception as e:
                self._addResult(module_name, 'INIT', 'ERROR', str(e))
                self._destroySandbox(entry['sandbox'])
                self._deferred_per_test_queue.pop(0)
                if self._deferred_per_test_queue:
                    run('args[0]()', self._runNextDeferredTest, delayFrames=self._delay_frames)
                else:
                    run('args[0]()', self._finalizeDeferredPerTestRun, delayFrames=self._delay_frames)
                return

        instance = entry['instance']

        # Run suite-level setup once
        if not entry['setup_done']:
            entry['setup_done'] = True
            if hasattr(instance, 'setUpSuite'):
                try:
                    instance.setUpSuite()
                except Exception as e:
                    self._addResult(module_name, 'setUpSuite', 'ERROR', str(e))
                    # Skip entire suite
                    self._destroySandbox(entry['sandbox'])
                    self._deferred_per_test_queue.pop(0)
                    run('args[0]()', self._runNextDeferredTest, delayFrames=self._delay_frames)
                    return

        # Run the current test method
        idx = entry['method_index']
        if idx < len(entry['methods']):
            method_name = entry['methods'][idx]
            self._runTest(instance, module_name, method_name)
            entry['method_index'] = idx + 1

        # Check if this suite is done
        if entry['method_index'] >= len(entry['methods']):
            # Suite-level teardown
            if hasattr(instance, 'tearDownSuite'):
                try:
                    instance.tearDownSuite()
                except Exception as e:
                    self._addResult(module_name, 'tearDownSuite', 'ERROR', str(e))
            self._destroySandbox(entry['sandbox'])
            self._deferred_per_test_queue.pop(0)

        # Schedule the next test
        if self._deferred_per_test_queue:
            run('args[0]()', self._runNextDeferredTest, delayFrames=self._delay_frames)
        else:
            run('args[0]()', self._finalizeDeferredPerTestRun, delayFrames=self._delay_frames)

    def _finalizeDeferredPerTestRun(self):
        """Called after all per-test deferred tests have completed."""
        self._running = False
        self._restoreFileCleanupDialog()
        self._deferred_per_test_queue = []
        self._reportSummary()

    # =========================================================================
    # AGENT-TIER EXECUTION (frame-driven subprocess polling)
    # =========================================================================

    def _scheduleAgentTick(self):
        """Schedule the next agent-runner tick.

        Uses the STRING-EXPRESSION run() form so the live extension is
        re-resolved at fire time: agent jobs span many seconds, long enough
        for an externalized-file save to reinit this extension mid-run, and a
        bound-method callback would then fire on a stale instance. The
        current run's generation token is embedded in the expression so a
        stale tick can be recognized no matter what else changed."""
        run(f"op.unit_tests.ext.TestRunnerExt._agentTick({self._agent_run_id})",
            fromOP=self.ownerComp, delayFrames=self._agent_delay_frames)

    def _agentTick(self, run_id=None):
        """One firing of the frame-driven agent-runner state machine.

        ``run_id`` is the generation token embedded in the scheduled
        expression. A tick whose token does not match the CURRENT run (fresh
        instance after a reinit, or a superseded chain) must not touch shared
        state -- ``_running`` is shared by ALL run modes, so a stale tick
        keying on it could finalize a normal run that started after the
        reinit. Stale ticks only reap what the dead run left behind."""
        if run_id is None or run_id != self._agent_run_id:
            self._reapOrphanAgentJobs()
            return
        if not self._agent_queue:
            self._finalizeAgentRun()
            return

        entry = self._agent_queue[0]
        module_name = entry['module_name']

        # A job in flight: poll it once; finish it when it exits.
        job = entry['job']
        if job is not None:
            if self._pollAgentJobOnce(job) == 'running':
                self._scheduleAgentTick()
                return
            result = self._finishAgentJob(job)
            entry['job'] = None
            method_name = entry['methods'][entry['method_index']]
            status, message = self._classifyVerdict(
                job['spec'].get('verify'), result)
            self._recordAndAdvance(entry, method_name, status, message,
                                   result['duration_s'] * 1000.0)
            self._scheduleAgentTick()
            return

        # Lazy sandbox/instance init (mirrors the per-test deferred runner).
        if entry['instance'] is None:
            entry['sandbox'] = self._createSandbox(module_name)
            try:
                entry['instance'] = entry['suite_class'](
                    sandbox=entry['sandbox'],
                    embody=op.Embody,
                    runner=self,
                )
            except Exception as e:
                self._addResult(module_name, 'INIT', 'ERROR', str(e))
                self._destroySandbox(entry['sandbox'])
                self._agent_queue.pop(0)
                self._scheduleAgentTick()
                return

        instance = entry['instance']

        # Suite-level setup once
        if not entry['setup_done']:
            entry['setup_done'] = True
            if hasattr(instance, 'setUpSuite'):
                try:
                    instance.setUpSuite()
                except Exception as e:
                    self._addResult(module_name, 'setUpSuite', 'ERROR', str(e))
                    self._destroySandbox(entry['sandbox'])
                    self._agent_queue.pop(0)
                    self._scheduleAgentTick()
                    return

        if entry['method_index'] >= len(entry['methods']):
            self._advanceAgentQueue(entry)
            self._scheduleAgentTick()
            return

        method_name = entry['methods'][entry['method_index']]

        # Per-test setUp
        if hasattr(instance, 'setUp'):
            try:
                instance.setUp()
            except Exception as e:
                self._recordAndAdvance(entry, method_name, 'ERROR',
                                       f'setUp failed: {e}')
                self._scheduleAgentTick()
                return

        # Run the method: None = synchronous test; a dict = job spec.
        t0 = time.perf_counter()
        try:
            spec = getattr(instance, method_name)()
        except AssertionError as e:
            self._recordAndAdvance(entry, method_name, 'FAIL', str(e),
                                   (time.perf_counter() - t0) * 1000)
            self._scheduleAgentTick()
            return
        except SkipTest as e:
            self._recordAndAdvance(entry, method_name, 'SKIP', str(e),
                                   (time.perf_counter() - t0) * 1000)
            self._scheduleAgentTick()
            return
        except Exception as e:
            self._recordAndAdvance(entry, method_name, 'ERROR',
                                   f'{type(e).__name__}: {e}',
                                   (time.perf_counter() - t0) * 1000)
            self._scheduleAgentTick()
            return

        if spec is None:
            self._recordAndAdvance(entry, method_name, 'PASS', '',
                                   (time.perf_counter() - t0) * 1000)
            self._scheduleAgentTick()
            return

        try:
            entry['job'] = self._launchAgentJob(spec)
        except Exception as e:
            self._recordAndAdvance(entry, method_name, 'ERROR',
                                   f'launch failed: {type(e).__name__}: {e}')
            self._scheduleAgentTick()
            return

        self._log(f"agent job started: {module_name}.{method_name} "
                  f"[{spec.get('label')}] timeout {int(spec['timeout_s'])}s "
                  f"pid {entry['job']['proc'].pid}")
        self._scheduleAgentTick()

    def _recordAndAdvance(self, entry, method_name, status, message,
                          duration_ms=0):
        """Record a verdict, run per-test tearDown, advance the queue."""
        self._addResult(entry['module_name'], method_name, status, message,
                        duration_ms)
        instance = entry['instance']
        if instance is not None and hasattr(instance, 'tearDown'):
            try:
                instance.tearDown()
            except Exception as e:
                self._addResult(entry['module_name'],
                                f'{method_name}:tearDown', 'ERROR', str(e))
        entry['method_index'] += 1
        self._advanceAgentQueue(entry)

    def _advanceAgentQueue(self, entry):
        """Pop the current suite once its methods are exhausted (with teardown)."""
        if entry['method_index'] < len(entry['methods']):
            return
        instance = entry['instance']
        if instance is not None and hasattr(instance, 'tearDownSuite'):
            try:
                instance.tearDownSuite()
            except Exception as e:
                self._addResult(entry['module_name'], 'tearDownSuite',
                                'ERROR', str(e))
        self._destroySandbox(entry['sandbox'])
        self._agent_queue.pop(0)

    def _finalizeAgentRun(self):
        """Called when the agent queue is exhausted."""
        self._running = False
        self._agent_run_id = None
        self._restoreFileCleanupDialog()
        self._restoreAutoexternalize()
        self._agent_queue = []
        self._reportSummary()

    # ---- job primitives (unit-testable without frames) ----------------------

    def _launchAgentJob(self, spec):
        """Start the subprocess described by a job spec (never blocks).

        stdout/stderr go to temp FILES, not pipes: nobody reads while the
        process runs, and an unread pipe blocks the child at ~64KB. stdin is
        always DEVNULL (codex exec hangs forever probing a silent stdin pipe
        on Windows - openai/codex#20919; claude -p needs no TTY)."""
        import tempfile
        if not isinstance(spec, dict) or not (spec.get('argv') or
                                              spec.get('cmdline')):
            raise ValueError(
                'agent test method must return None or a job spec dict with '
                'argv/cmdline (build one with AgentTestCase.job())')
        stdout_f = tempfile.NamedTemporaryFile(
            prefix='embody_agent_out_', suffix='.txt', delete=False)
        stderr_f = tempfile.NamedTemporaryFile(
            prefix='embody_agent_err_', suffix='.txt', delete=False)
        popen_kwargs = {
            'stdin': subprocess.DEVNULL,
            'stdout': stdout_f,
            'stderr': stderr_f,
            'cwd': spec.get('cwd') or None,
            'env': spec.get('env') or None,
        }
        if sys.platform == 'win32':
            popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            # Own process group so a timeout can kill the whole tree.
            popen_kwargs['start_new_session'] = True
        try:
            if spec.get('cmdline'):
                proc = subprocess.Popen(spec['cmdline'], **popen_kwargs)
            else:
                proc = subprocess.Popen(list(spec['argv']), **popen_kwargs)
        finally:
            stdout_f.close()
            stderr_f.close()
        job = {
            'proc': proc,
            'spec': spec,
            'stdout_path': stdout_f.name,
            'stderr_path': stderr_f.name,
            'started': time.perf_counter(),
            'deadline': time.perf_counter() + float(spec['timeout_s']),
            'timed_out': False,
        }
        # Mirror the job on sys so a reinitialized instance can still reap
        # it. The Popen OBJECT rides along: its open handle lets the reaper
        # poll() before killing, so a recycled pid is never taskkilled.
        mirror = getattr(sys, '_embody_agent_jobs', None)
        if mirror is None:
            mirror = {}
            sys._embody_agent_jobs = mirror
        mirror[proc.pid] = {'label': spec.get('label') or 'agent job',
                            'proc': proc}
        return job

    def _pollAgentJobOnce(self, job):
        """Return 'running' or 'done'; kill the process tree on deadline expiry."""
        proc = job['proc']
        if proc.poll() is None:
            if time.perf_counter() < job['deadline']:
                return 'running'
            job['timed_out'] = True
            self._killPidTree(proc.pid, proc=proc)
        return 'done'

    def _finishAgentJob(self, job):
        """Collect output + exit state after a job finished or was killed."""
        proc = job['proc']
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        duration = time.perf_counter() - job['started']
        result = {
            'returncode': proc.returncode,
            'stdout': self._readAgentFile(job['stdout_path']),
            'stderr': self._readAgentFile(job['stderr_path']),
            'duration_s': duration,
            'timed_out': job['timed_out'],
        }
        mirror = getattr(sys, '_embody_agent_jobs', None)
        if mirror:
            mirror.pop(proc.pid, None)
        return result

    def _classifyVerdict(self, verify, result):
        """Run a job's verify callback against its result -> (status, message).

        verify runs on the MAIN thread so it may inspect live TD state - the
        primary evidence that the agent's operations actually landed."""
        if result['timed_out']:
            tail = (result['stderr'] or result['stdout'] or '')[-300:]
            return ('ERROR',
                    f"timed out after {result['duration_s']:.0f}s; tail: {tail}")
        if verify is None:
            if result['returncode'] == 0:
                return ('PASS', '')
            tail = (result['stderr'] or result['stdout'] or '')[-300:]
            return ('FAIL', f"exit code {result['returncode']}; tail: {tail}")
        try:
            verify(result)
            return ('PASS', '')
        except AssertionError as e:
            return ('FAIL', str(e))
        except SkipTest as e:
            return ('SKIP', str(e))
        except Exception as e:
            return ('ERROR', f'{type(e).__name__}: {e}')

    def _readAgentFile(self, path, max_bytes=262144):
        """Read + delete a job's captured output file (tail only).

        Seeks to the tail instead of reading the whole file: a wedged CLI in
        a print loop can grow the capture file to gigabytes, and this runs on
        TD's main thread -- only the last max_bytes are ever wanted."""
        try:
            size = os.path.getsize(path)
            with open(path, 'rb') as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read(max_bytes)
            text = data.decode('utf-8', errors='replace')
        except Exception as e:
            return f'<unreadable: {e}>'
        try:
            os.remove(path)
        except Exception:
            pass
        return text

    def _killPidTree(self, pid, proc=None):
        """Kill a job subprocess AND its children.

        claude/codex spawn the Envoy bridge as a child; killing only the
        parent strands it. Windows: taskkill /T. POSIX: kill the process
        group created via start_new_session."""
        try:
            if sys.platform == 'win32':
                subprocess.run(
                    ['taskkill', '/T', '/F', '/PID', str(pid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _reapOrphanAgentJobs(self):
        """Clean up after a reinit-interrupted (or superseded) agent run.

        Kills leftover subprocesses via the sys mirror -- poll-first: an
        entry whose Popen already exited is just dropped, so a recycled pid
        is never taskkilled. Then, ONLY when no run is active on this
        instance (restoring mid-run would un-suppress a live run's dialogs),
        restores the suppressed preferences (storage-backed, so the fresh
        instance still has the originals) and clears sandbox debris the dead
        run left behind."""
        mirror = getattr(sys, '_embody_agent_jobs', None) or {}
        for pid in list(mirror):
            info = mirror.pop(pid, None) or {}
            proc = info.get('proc') if isinstance(info, dict) else None
            label = info.get('label') if isinstance(info, dict) else info
            if proc is not None and proc.poll() is not None:
                continue  # already exited on its own; nothing to kill
            self._log(f'reaping orphaned agent-test subprocess pid {pid} '
                      f'({label}) - an interrupted agent run left it behind',
                      'ERROR')
            self._killPidTree(pid, proc=proc)
        if not self._running:
            self._restoreFileCleanupDialog()
            self._restoreAutoexternalize()
            try:
                for child in list(self.sandbox_comp.children):
                    child.destroy()
            except Exception:
                pass

    # =========================================================================
    # TEST DISCOVERY
    # =========================================================================

    def _discoverTestSuites(self, filter_name=None, tier='normal'):
        """
        Discover test suites by loading .py files from dev/embody/unit_tests/.

        Loads externalized test .py files from disk via importlib, injecting
        TD globals and the test base classes into each module before execution.

        Tiers (selected by class attributes on the suite):

        - ``tier='normal'`` (every standard run): excludes BOTH tagged tiers.
        - ``tier='destructive'``: ONLY suites tagged ``DESTRUCTIVE = True`` --
          whole-project mutators (Disable / ExternalizeProject / Reset on
          ext.root) that can delete/convert every tracked file; they run only
          in the save-gated RunDestructiveTests batch. See
          .claude/rules/destructive-tests.md.
        - ``tier='agent'``: ONLY suites tagged ``AGENT = True`` (usually via
          AgentTestCase) -- they spawn external AI-client subprocesses, take
          minutes, and consume subscription usage; they run only via
          RunAgentTests. See .claude/rules/agent-tests.md.

        A suite tagged BOTH ways never surfaces anywhere (unsupported
        combination -- fail safe by exclusion). This filter is the guard that
        stops a plain full run from ever nuking the live project or silently
        burning agent-CLI usage.
        """
        import os
        import importlib.util
        import sys

        suites = []

        # Get the test directory path
        test_dir = project.folder + '/embody/unit_tests'
        if not os.path.isdir(test_dir):
            self._addResult('DISCOVERY', 'ERROR', 'ERROR',
                          f'Test directory not found: {test_dir}')
            return suites

        # Scan for test_*.py files
        for filename in sorted(os.listdir(test_dir)):
            if not filename.startswith('test_') or not (filename.endswith('.py') or filename.endswith('.txt')):
                continue

            module_name = filename.rsplit('.', 1)[0]  # Remove .py or .txt extension

            if filter_name and module_name != filter_name:
                continue

            try:
                # Load module from file
                module_path = os.path.join(test_dir, filename)
                if filename.endswith('.txt'):
                    from importlib.machinery import SourceFileLoader
                    loader = SourceFileLoader(module_name, module_path)
                    spec = importlib.util.spec_from_file_location(module_name, module_path, loader=loader)
                else:
                    spec = importlib.util.spec_from_file_location(module_name, module_path)
                if spec is None or spec.loader is None:
                    self._addResult(module_name, 'DISCOVERY', 'ERROR',
                                  f'Failed to load module spec: {module_path}')
                    continue

                mod = importlib.util.module_from_spec(spec)

                # Inject TouchDesigner globals into the module namespace
                # (these are available in DATs automatically but not in importlib-loaded modules)

                # Core TD functions
                td_global_names = [
                    'op', 'parent', 'root', 'iop', 'rop', 'ipar',
                    'project', 'ui', 'me', 'panel', 'app', 'args', 'ext',
                ]
                for name in td_global_names:
                    try:
                        mod.__dict__[name] = globals()[name]
                    except KeyError:
                        pass  # Skip globals that don't exist in this context

                # Inject all td module contents (operator types, TD classes, etc.)
                import td
                for name in dir(td):
                    if not name.startswith('_'):
                        mod.__dict__[name] = getattr(td, name)

                # Inject test framework base classes so tests don't need to import them
                mod.__dict__['EmbodyTestCase'] = EmbodyTestCase
                mod.__dict__['AgentTestCase'] = AgentTestCase
                mod.__dict__['SkipTest'] = SkipTest

                # Inject common TD enums
                try:
                    mod.__dict__['ParMode'] = td.ParMode
                except AttributeError:
                    pass  # ParMode not available in this TD version

                # Add to sys.modules so cross-module imports work
                sys.modules[module_name] = mod

                # Execute the module (now it has access to TD globals)
                spec.loader.exec_module(mod)

                # Extract test classes (same logic as before)
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if (isinstance(obj, type) and
                            obj is not EmbodyTestCase and
                            obj is not AgentTestCase and
                            any(m.startswith('test_') for m in dir(obj))):
                        # Tier segregation: DESTRUCTIVE suites only run in the
                        # save-gated RunDestructiveTests batch; AGENT suites
                        # only via RunAgentTests; a normal run gets neither.
                        # This is the single guard that prevents a full run
                        # from nuking the live project or spawning AI clients.
                        is_destructive = bool(getattr(obj, 'DESTRUCTIVE', False))
                        is_agent = bool(getattr(obj, 'AGENT', False))
                        if tier == 'destructive':
                            if not is_destructive or is_agent:
                                continue
                        elif tier == 'agent':
                            if not is_agent or is_destructive:
                                continue
                        else:
                            if is_destructive or is_agent:
                                continue
                        suites.append((obj, module_name))

            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                self._addResult(module_name, 'DISCOVERY', 'ERROR',
                              f'{type(e).__name__}: {e}\n{tb}')

        return suites

    # =========================================================================
    # TEST EXECUTION
    # =========================================================================

    def _runSuite(self, suite_class, module_name, test_filter=None):
        """Run all test methods in a suite class."""
        sandbox = self._createSandbox(module_name)

        try:
            instance = suite_class(
                sandbox=sandbox,
                embody=op.Embody,
                runner=self,
            )

            # Suite-level setup
            if hasattr(instance, 'setUpSuite'):
                try:
                    instance.setUpSuite()
                except Exception as e:
                    self._addResult(module_name, 'setUpSuite', 'ERROR', str(e))
                    return

            # Discover and run test methods (sorted for deterministic order)
            test_methods = sorted(
                m for m in dir(instance)
                if m.startswith('test_') and callable(getattr(instance, m))
            )

            for method_name in test_methods:
                if test_filter and method_name != test_filter:
                    continue
                self._runTest(instance, module_name, method_name)

            # Suite-level teardown
            if hasattr(instance, 'tearDownSuite'):
                try:
                    instance.tearDownSuite()
                except Exception as e:
                    self._addResult(module_name, 'tearDownSuite', 'ERROR', str(e))
        finally:
            self._destroySandbox(sandbox)

    def _runTest(self, instance, suite_name, method_name):
        """Run a single test method with setUp/tearDown."""
        # setUp
        if hasattr(instance, 'setUp'):
            try:
                instance.setUp()
            except Exception as e:
                self._addResult(suite_name, method_name, 'ERROR',
                                f'setUp failed: {e}')
                return

        # Execute test
        t0 = time.perf_counter()
        try:
            method = getattr(instance, method_name)
            method()
            duration = (time.perf_counter() - t0) * 1000
            self._addResult(suite_name, method_name, 'PASS', '', duration)
        except AssertionError as e:
            duration = (time.perf_counter() - t0) * 1000
            self._addResult(suite_name, method_name, 'FAIL', str(e), duration)
        except SkipTest as e:
            duration = (time.perf_counter() - t0) * 1000
            self._addResult(suite_name, method_name, 'SKIP', str(e), duration)
        except Exception as e:
            duration = (time.perf_counter() - t0) * 1000
            self._addResult(suite_name, method_name, 'ERROR',
                            f'{type(e).__name__}: {e}', duration)

        # tearDown
        if hasattr(instance, 'tearDown'):
            try:
                instance.tearDown()
            except Exception as e:
                self._addResult(suite_name, f'{method_name}:tearDown', 'ERROR',
                                str(e))

    # =========================================================================
    # SANDBOX (FIXTURE ISOLATION)
    # =========================================================================

    def _createSandbox(self, suite_name):
        """Create an isolated baseCOMP sandbox for a test suite."""
        safe_name = suite_name.replace('.', '_')
        return self.sandbox_comp.create(baseCOMP, f'sandbox_{safe_name}')

    def _destroySandbox(self, sandbox):
        """Destroy sandbox and all its contents."""
        if sandbox and sandbox.valid:
            sandbox.destroy()

    # =========================================================================
    # RESULTS TRACKING
    # =========================================================================

    def _initResultsTable(self):
        """Initialize the results tableDAT with header row."""
        self.results_dat.clear()
        self.results_dat.appendRow(
            ['suite', 'test', 'status', 'message', 'duration_ms'])

    def _addResult(self, suite, test, status, message, duration_ms=0):
        """Record a single test result."""
        result = {
            'suite': suite,
            'test': test,
            'status': status,
            'message': message,
            'duration_ms': round(duration_ms, 2),
        }
        self._results.append(result)
        self.results_dat.appendRow([
            suite, test, status, message, f'{duration_ms:.2f}',
        ])
        if status in ('FAIL', 'ERROR'):
            self._log(f'{status}: {suite}.{test} - {message}', 'ERROR')
        elif status == 'SKIP':
            self._log(f'SKIP: {suite}.{test} - {message}', 'WARNING')

    def _getSummary(self):
        """Build summary dict from results."""
        total = len(self._results)
        passed = sum(1 for r in self._results if r['status'] == 'PASS')
        failed = sum(1 for r in self._results if r['status'] == 'FAIL')
        errors = sum(1 for r in self._results if r['status'] == 'ERROR')
        skipped = sum(1 for r in self._results if r['status'] == 'SKIP')
        return {
            'total': total,
            'passed': passed,
            'failed': failed,
            'errors': errors,
            'skipped': skipped,
            'results': self._results,
        }

    def _reportSummary(self):
        """Log a one-line summary."""
        s = self._getSummary()
        parts = [f"{s['passed']}/{s['total']} passed"]
        if s['failed']:
            parts.append(f"{s['failed']} failed")
        if s['errors']:
            parts.append(f"{s['errors']} errors")
        if s['skipped']:
            parts.append(f"{s['skipped']} skipped")

        msg = 'Tests complete: ' + ', '.join(parts)
        level = 'SUCCESS' if s['failed'] == 0 and s['errors'] == 0 else 'ERROR'
        self._log(msg, level)

    # =========================================================================
    # LOGGING
    # =========================================================================

    def _log(self, msg, level='INFO'):
        """Log through Embody's logging system."""
        try:
            op.Embody.Log(msg, level)
        except Exception:
            print(f'[TestRunner][{level}] {msg}')
