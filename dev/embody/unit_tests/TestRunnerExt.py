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
    op.unit_tests.GetResults()                            # Get results dict
"""

from __future__ import annotations

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
# TEST RUNNER EXTENSION
# =============================================================================

class TestRunnerExt:
    """
    Test runner extension for /embody/unit_tests.

    Discovers test suites by loading .py files from dev/embody/unit_tests/,
    runs them with sandbox isolation, and reports results.
    """

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        self.results_dat = self.ownerComp.op('results')
        self.sandbox_comp = self.ownerComp.op('test_sandbox')
        self._running = False
        self._results = []
        self._deferred_queue = []
        self._deferred_test_filter = None
        self._saved_filecleanup = None
        self._saved_toxdropexpr = None

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
            if self._saved_filecleanup is None:
                self._saved_filecleanup = op.Embody.par.Filecleanup.eval()
            op.Embody.par.Filecleanup = 'delete'
        except Exception:
            pass
        try:
            if self._saved_toxdropexpr is None:
                self._saved_toxdropexpr = op.Embody.par.Toxdropexpr.eval()
            op.Embody.par.Toxdropexpr = 'ignore'
        except Exception:
            pass

    def _restoreFileCleanupDialog(self):
        """Restore the original continuity-dialog preferences after tests."""
        try:
            if self._saved_filecleanup is not None:
                op.Embody.par.Filecleanup = self._saved_filecleanup
                self._saved_filecleanup = None
        except Exception:
            pass
        try:
            if self._saved_toxdropexpr is not None:
                op.Embody.par.Toxdropexpr = self._saved_toxdropexpr
                self._saved_toxdropexpr = None
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
        dirty = getattr(project, 'dirty', None)
        if dirty is True:
            msg = ('RunDestructiveTests refused: project has unsaved changes '
                   '(project.dirty). Save first so there is a recovery point.')
            self._log(msg, 'ERROR')
            return {'error': msg}

        self._running = True
        self._suppressFileCleanupDialog()
        self._results = []
        self._initResultsTable()
        try:
            suites = self._discoverTestSuites(suite_name, only_destructive=True)
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
    # TEST DISCOVERY
    # =========================================================================

    def _discoverTestSuites(self, filter_name=None, only_destructive=False):
        """
        Discover test suites by loading .py files from dev/embody/unit_tests/.

        Loads externalized test .py files from disk via importlib, injecting
        TD globals and EmbodyTestCase into each module before execution.

        Suites that set the class attribute ``DESTRUCTIVE = True`` mutate the
        WHOLE LIVE PROJECT (Disable / ExternalizeProject / Reset on ext.root)
        and can delete/convert every tracked file. They are EXCLUDED from every
        normal run and only surface when ``only_destructive=True`` (the
        save-gated RunDestructiveTests batch). This is the guard that stops a
        plain full run from ever nuking the live project. See
        .claude/rules/destructive-tests.md.
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
                            any(m.startswith('test_') for m in dir(obj))):
                        # Destructive whole-project suites are segregated: they
                        # ONLY run in the save-gated RunDestructiveTests batch,
                        # never in a normal run. This is the single guard that
                        # prevents a full run from nuking the live project.
                        is_destructive = bool(getattr(obj, 'DESTRUCTIVE', False))
                        if is_destructive != only_destructive:
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
