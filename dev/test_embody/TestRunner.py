"""
Embody Test Framework — Test Runner Extension

Provides test discovery, execution, and reporting for the Embody project.
Lives at /embody/unit_tests as a TD extension.

Usage:
    op('/embody/unit_tests').RunTests()                              # Run all (synchronous)
    op('/embody/unit_tests').RunTests(suite_name='test_path_utils')  # Run one suite
    op('/embody/unit_tests').RunTestsDeferred()                      # Run all (one suite per frame)
    op('/embody/unit_tests').GetResults()                            # Get results dict
"""

from __future__ import annotations

import time
from collections import deque


# =============================================================================
# SKIP TEST EXCEPTION
# =============================================================================

class SkipTest(Exception):
    """Raise inside a test method to skip it."""
    pass


# =============================================================================
# BASE TEST CASE
# =============================================================================

class EmbodyTestCase:
    """
    Base class for all Embody test suites.

    Each test file should define a class inheriting from this.
    The test runner injects sandbox, embody, and runner references.
    """

    def __init__(self, sandbox, embody, runner):
        self.sandbox = sandbox          # baseCOMP to create temp operators in
        self.embody = embody            # op.Embody reference (the Embody COMP)
        self.embody_ext = embody.ext.Embody   # Direct EmbodyExt instance
        self.runner = runner            # TestRunner instance

    def setUp(self):
        """Called before each test method. Override for per-test setup."""
        pass

    def tearDown(self):
        """Called after each test method. Destroys all sandbox children."""
        for child in list(self.sandbox.children):
            try:
                child.destroy()
            except Exception:
                pass

    # -----------------------------------------------------------------
    # Assertion helpers
    # -----------------------------------------------------------------

    def assertEqual(self, a, b, msg=None):
        if a != b:
            raise AssertionError(msg or f'Expected {repr(a)} == {repr(b)}')

    def assertNotEqual(self, a, b, msg=None):
        if a == b:
            raise AssertionError(msg or f'Expected {repr(a)} != {repr(b)}')

    def assertTrue(self, val, msg=None):
        if not val:
            raise AssertionError(msg or f'Expected truthy, got {repr(val)}')

    def assertFalse(self, val, msg=None):
        if val:
            raise AssertionError(msg or f'Expected falsy, got {repr(val)}')

    def assertIsNone(self, val, msg=None):
        if val is not None:
            raise AssertionError(msg or f'Expected None, got {repr(val)}')

    def assertIsNotNone(self, val, msg=None):
        if val is None:
            raise AssertionError(msg or 'Expected not None')

    def assertIs(self, a, b, msg=None):
        if a is not b:
            raise AssertionError(msg or f'Expected {repr(a)} is {repr(b)}')

    def assertIsNot(self, a, b, msg=None):
        if a is b:
            raise AssertionError(msg or f'Expected {repr(a)} is not {repr(b)}')

    def assertIn(self, item, container, msg=None):
        if item not in container:
            raise AssertionError(msg or f'{repr(item)} not in {repr(container)}')

    def assertNotIn(self, item, container, msg=None):
        if item in container:
            raise AssertionError(msg or f'{repr(item)} unexpectedly in {repr(container)}')

    def assertIsInstance(self, obj, cls, msg=None):
        if not isinstance(obj, cls):
            raise AssertionError(
                msg or f'Expected instance of {cls.__name__}, got {type(obj).__name__}')

    def assertGreater(self, a, b, msg=None):
        if not (a > b):
            raise AssertionError(msg or f'Expected {repr(a)} > {repr(b)}')

    def assertGreaterEqual(self, a, b, msg=None):
        if not (a >= b):
            raise AssertionError(msg or f'Expected {repr(a)} >= {repr(b)}')

    def assertLess(self, a, b, msg=None):
        if not (a < b):
            raise AssertionError(msg or f'Expected {repr(a)} < {repr(b)}')

    def assertLessEqual(self, a, b, msg=None):
        if not (a <= b):
            raise AssertionError(msg or f'Expected {repr(a)} <= {repr(b)}')

    def assertStartsWith(self, s, prefix, msg=None):
        if not str(s).startswith(prefix):
            raise AssertionError(msg or f'{repr(s)} does not start with {repr(prefix)}')

    def assertEndsWith(self, s, suffix, msg=None):
        if not str(s).endswith(suffix):
            raise AssertionError(msg or f'{repr(s)} does not end with {repr(suffix)}')

    def assertApproxEqual(self, a, b, tolerance=1e-6, msg=None):
        if abs(a - b) > tolerance:
            raise AssertionError(msg or f'{a} != {b} (tolerance {tolerance})')

    def assertDictHasKey(self, d, key, msg=None):
        if key not in d:
            raise AssertionError(msg or f'Key {repr(key)} not in dict')

    def assertDictEqual(self, a, b, msg=None):
        if a != b:
            raise AssertionError(msg or f'Dicts differ:\n  {repr(a)}\n  vs\n  {repr(b)}')

    def assertListEqual(self, a, b, msg=None):
        if list(a) != list(b):
            raise AssertionError(msg or f'Lists differ:\n  {repr(list(a))}\n  vs\n  {repr(list(b))}')

    def assertRaises(self, exc_type, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except exc_type:
            return
        except Exception as e:
            raise AssertionError(
                f'Expected {exc_type.__name__}, got {type(e).__name__}: {e}')
        raise AssertionError(f'Expected {exc_type.__name__} but no exception raised')

    def assertLen(self, container, expected_len, msg=None):
        actual = len(container)
        if actual != expected_len:
            raise AssertionError(
                msg or f'Expected length {expected_len}, got {actual}')

    def skip(self, reason=''):
        raise SkipTest(reason)


# =============================================================================
# TEST RUNNER EXTENSION
# =============================================================================

class TestRunner:
    """
    Test runner extension for /embody/unit_tests.

    Discovers test suites in child textDATs (names starting with 'test_'),
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

    # =========================================================================
    # PROMOTED METHODS
    # =========================================================================

    def RunTests(self, suite_name=None, test_name=None):
        """
        Run test suites synchronously (all in one frame).

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
        self._results = []
        self._initResultsTable()

        try:
            suites = self._discoverTestSuites(suite_name)
            self._log(f'Discovered {len(suites)} test suite(s)')

            for suite_class, module_name in suites:
                self._runSuite(suite_class, module_name, test_name)
        finally:
            self._running = False

        self._reportSummary()
        return self._getSummary()

    def RunTestsDeferred(self, suite_name=None, test_name=None):
        """
        Run test suites across multiple frames (one suite per frame).

        Uses run() with delayFrames to schedule each suite on a
        separate frame, keeping TD's cook cycle responsive.

        Args:
            suite_name: Run only this suite (e.g., 'test_path_utils').
            test_name:  Run only this test method within the suite.
        """
        if self._running:
            self._log('Tests already running', 'WARNING')
            return

        self._running = True
        self._results = []
        self._initResultsTable()

        suites = self._discoverTestSuites(suite_name)
        self._log(f'Discovered {len(suites)} test suite(s) [deferred]')

        if not suites:
            self._running = False
            self._reportSummary()
            return

        self._deferred_queue = list(suites)
        self._deferred_test_filter = test_name

        # Schedule the first suite on the next frame
        run('args[0]()', self._runNextDeferredSuite, delayFrames=1)

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
            self._reportSummary()
            return

        suite_class, module_name = self._deferred_queue.pop(0)
        self._runSuite(suite_class, module_name, self._deferred_test_filter)

        if self._deferred_queue:
            # Schedule the next suite on the next frame
            run('args[0]()', self._runNextDeferredSuite, delayFrames=1)
        else:
            # All done — finalize on next frame
            run('args[0]()', self._finalizeDeferredRun, delayFrames=1)

    def _finalizeDeferredRun(self):
        """Called after all deferred suites have completed."""
        self._running = False
        self._deferred_test_filter = None
        self._reportSummary()

    # =========================================================================
    # TEST DISCOVERY
    # =========================================================================

    def _discoverTestSuites(self, filter_name=None):
        """Find all test DATs and extract test classes."""
        suites = []

        for child in sorted(self.ownerComp.children, key=lambda c: c.name):
            if child.family != 'DAT':
                continue
            if not child.name.startswith('test_'):
                continue
            if filter_name and child.name != filter_name:
                continue

            try:
                mod = child.module
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if (isinstance(obj, type) and
                            obj is not EmbodyTestCase and
                            any(m.startswith('test_') for m in dir(obj))):
                        suites.append((obj, child.name))
            except Exception as e:
                self._addResult(child.name, 'DISCOVERY', 'ERROR', str(e))

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
