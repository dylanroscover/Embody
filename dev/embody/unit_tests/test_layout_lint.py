"""
Test suite: Envoy layout lint (EnvoyExt._lintLayout / _lintNewOps / _execute_python).

network-layout.md is enforced at the tool layer: execute_python uses raw
comp.create()/copy() (no auto-position), so it keeps dropping new ops at (0,0)
or overlapping. _lintLayout inspects a COMP's DIRECT children and returns a list
of human-readable issue strings; _lintNewOps diffs the pre/post op set of an
execute_python call and emits a 'LAYOUT WARNING' via self._log; _execute_python
wires those together.

Source contracts (EnvoyExt.py, verified):
  _lintLayout(comp) -> list[str]
    - kids = [c for c in comp.children if c.type != 'annotate']
    - guard: len(kids) < 2  -> []           (too few to compare)
    - guard: len(kids) > 250 -> []          (too big to lint cheaply)
    - docked DATs are excluded from 'main'
    - >= 2 main ops at (0,0)        -> '<N> ops stacked at (0,0): <names>'
    - AABB-overlapping main pairs   -> '<N> overlapping op pair(s)'  (only when n <= 80)
    - docked DAT with abs(dX-hostX) > 350 OR abs(dY-hostY) > 350
                                    -> '<N> docked DAT(s) scattered far from host'
  _lintNewOps(pre_paths): auto-hugs scattered docks of NEW ops via
    _placeDockedOps (logs a WARNING naming the fix), then finds new parents,
    lints each, _log(...) per issue set.
  _placeDockedOps(host) -> int: snaps same-network docks into a row 30 below
    the host's bottom edge, slots dock-width+20 apart, centered under the host.
  _execute_python(code): snapshots pre_paths, exec(code), _lintNewOps(pre_paths).

_log signature: _log(self, message, level='INFO') -> op.Embody.Log(message, level, _depth=2)
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestLayoutLint(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        # Per-test log capture: wrap the INSTANCE _log so _lintNewOps' WARNING
        # calls are recorded without touching the real logger output. Restored
        # in tearDown so no leakage between tests or into the live server.
        self._log_calls = []
        self._orig_log = None

    def tearDown(self):
        # Restore _log before sandbox teardown in case it was patched.
        if self._orig_log is not None:
            self.envoy._log = self._orig_log
            self._orig_log = None
        super().tearDown()

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _place(self, o, x, y, w=120, h=120):
        """Set an op's node box deterministically."""
        o.nodeX = x
        o.nodeY = y
        o.nodeWidth = w
        o.nodeHeight = h

    def _patch_log(self):
        """Capture envoy._log(message, level) calls on the instance."""
        self._orig_log = self.envoy._log

        def recorder(message, level='INFO'):
            self._log_calls.append((str(message), str(level)))
        self.envoy._log = recorder

    def _warnings(self):
        return [m for (m, lvl) in self._log_calls if lvl == 'WARNING']

    # -----------------------------------------------------------------
    # _lintLayout: stacked (0,0)
    # -----------------------------------------------------------------

    def test_two_ops_stacked_at_origin_reports_stacked(self):
        """>= 2 main ops both at (0,0) -> a 'stacked at (0,0)' issue naming count."""
        a = self.sandbox.create(textDAT, 'stack_a')
        b = self.sandbox.create(textDAT, 'stack_b')
        self._place(a, 0, 0)
        self._place(b, 0, 0)

        issues = self.envoy._lintLayout(self.sandbox)

        stacked = [s for s in issues if 'stacked at (0,0)' in s]
        self.assertEqual(len(stacked), 1,
                         f'Expected exactly one stacked-(0,0) issue, got {issues!r}')
        self.assertIn('2 ops stacked at (0,0)', stacked[0])

    def test_single_op_at_origin_no_issue(self):
        """Guard: a single child (len(kids) < 2) -> [] (nothing to compare)."""
        a = self.sandbox.create(textDAT, 'lonely')
        self._place(a, 0, 0)

        issues = self.envoy._lintLayout(self.sandbox)
        self.assertEqual(issues, [],
                         f'Single op at (0,0) must not lint, got {issues!r}')

    # -----------------------------------------------------------------
    # _lintLayout: AABB overlap
    # -----------------------------------------------------------------

    def test_overlapping_pair_reports_one_pair(self):
        """Two AABB-overlapping ops (NOT both at origin) -> exactly 1 overlapping pair."""
        a = self.sandbox.create(textDAT, 'ov_a')
        b = self.sandbox.create(textDAT, 'ov_b')
        # a spans x[100,220] y[100,220]; b spans x[150,270] y[150,270] -> overlap,
        # and neither sits at (0,0) so this isolates the overlap issue.
        self._place(a, 100, 100, 120, 120)
        self._place(b, 150, 150, 120, 120)

        issues = self.envoy._lintLayout(self.sandbox)

        overlap = [s for s in issues if 'overlapping op pair(s)' in s]
        self.assertEqual(len(overlap), 1,
                         f'Expected one overlap issue, got {issues!r}')
        self.assertIn('1 overlapping op pair(s)', overlap[0])
        # Not at origin -> no stacked issue should be present.
        self.assertEqual([s for s in issues if 'stacked at (0,0)' in s], [])

    def test_clean_spaced_layout_no_issues(self):
        """A clean, well-spaced, non-overlapping layout -> [] (no false positives)."""
        a = self.sandbox.create(textDAT, 'clean_a')
        b = self.sandbox.create(textDAT, 'clean_b')
        c = self.sandbox.create(textDAT, 'clean_c')
        # Wide horizontal spacing, distinct positions, no overlap, none at (0,0).
        self._place(a, 100, 100, 120, 120)
        self._place(b, 600, 100, 120, 120)
        self._place(c, 1100, 100, 120, 120)

        issues = self.envoy._lintLayout(self.sandbox)
        self.assertEqual(issues, [],
                         f'Clean layout should produce no issues, got {issues!r}')

    # -----------------------------------------------------------------
    # _lintLayout: scattered docked DAT (> 350u boundary)
    # -----------------------------------------------------------------

    def _dock(self, host, dat):
        """Dock `dat` to `host`; skip the test if docking is unsupported here."""
        try:
            dat.dock = host
        except Exception as e:
            self.skip(f'cannot set .dock in this TD build: {e}')
        if dat.path not in [d.path for d in host.docked]:
            self.skip('docking did not register dat in host.docked')

    def test_scattered_docked_dat_reports_scattered(self):
        """A docked DAT forced > 350u from its host -> a 'scattered' issue."""
        host = self.sandbox.create(textDAT, 'host_scatter')
        dat = self.sandbox.create(textDAT, 'dock_scatter')
        self._place(host, 0, 0, 120, 120)
        self._dock(host, dat)
        # Push the docked DAT 800u to the right of the host -> scattered.
        self._place(dat, 800, 0, 120, 120)

        issues = self.envoy._lintLayout(self.sandbox)
        scattered = [s for s in issues if 'scattered far from host' in s]
        self.assertEqual(len(scattered), 1,
                         f'Expected one scattered issue, got {issues!r}')
        self.assertIn('1 docked DAT(s) scattered far from host', scattered[0])

    def test_scatter_boundary_350_clean(self):
        """Boundary: dX == 350 is NOT scattered (the check is strictly > 350)."""
        host = self.sandbox.create(textDAT, 'host_b350')
        dat = self.sandbox.create(textDAT, 'dock_b350')
        self._place(host, 0, 0, 120, 120)
        self._dock(host, dat)
        # Exactly 350u offset -> abs(dX-hostX) == 350, 350 > 350 is False -> clean.
        self._place(dat, 350, 0, 120, 120)

        issues = self.envoy._lintLayout(self.sandbox)
        self.assertEqual([s for s in issues if 'scattered far from host' in s], [],
                         f'dX==350 must be clean, got {issues!r}')

    def test_scatter_boundary_351_trips(self):
        """Boundary: dX == 351 trips (351 > 350 is True -> scattered)."""
        host = self.sandbox.create(textDAT, 'host_b351')
        dat = self.sandbox.create(textDAT, 'dock_b351')
        self._place(host, 0, 0, 120, 120)
        self._dock(host, dat)
        self._place(dat, 351, 0, 120, 120)

        issues = self.envoy._lintLayout(self.sandbox)
        scattered = [s for s in issues if 'scattered far from host' in s]
        self.assertEqual(len(scattered), 1,
                         f'dX==351 must trip scattered, got {issues!r}')

    # -----------------------------------------------------------------
    # _placeDockedOps: the hug formula
    # -----------------------------------------------------------------

    def test_place_docked_ops_hugs_single_dock_below_host(self):
        """One dock -> centered directly below the host, 30u below its bottom edge."""
        host = self.sandbox.create(textDAT, 'hug_host')
        dat = self.sandbox.create(textDAT, 'hug_dock')
        self._place(host, 400, 600, 120, 120)
        self._dock(host, dat)
        self._place(dat, 2000, -2000, 130, 130)   # stranded far away

        n = self.envoy._placeDockedOps(host)

        self.assertEqual(n, 1, 'one dock should be placed')
        # row_y = hostY - dh - 30 = 600 - 130 - 30 = 440
        self.assertEqual(dat.nodeY, 440,
                         f'dock must sit 30u below host bottom, got nodeY={dat.nodeY}')
        # centered: cx = 400 + 60 = 460; nodeX = cx - dw/2 = 460 - 65 = 395
        self.assertEqual(dat.nodeX, 395,
                         f'dock must be centered under host, got nodeX={dat.nodeX}')

    def test_place_docked_ops_rows_two_docks_tight(self):
        """Two docks -> one tight row, slots dw+20 apart, centered under host."""
        host = self.sandbox.create(textDAT, 'hug2_host')
        d1 = self.sandbox.create(textDAT, 'hug2_a')
        d2 = self.sandbox.create(textDAT, 'hug2_b')
        self._place(host, 0, 0, 120, 120)
        self._dock(host, d1)
        self._dock(host, d2)
        self._place(d1, 900, 900, 130, 130)
        self._place(d2, -900, -900, 130, 130)

        n = self.envoy._placeDockedOps(host)

        self.assertEqual(n, 2)
        # Both in the hug row (row_y = 0 - 130 - 30 = -160), step = 150 apart.
        self.assertEqual(d1.nodeY, -160)
        self.assertEqual(d2.nodeY, -160)
        self.assertEqual(abs(d2.nodeX - d1.nodeX), 150,
                         'slots must be dock-width + 20 apart')
        # And the row passes the scatter lint (it hugs).
        issues = self.envoy._lintLayout(self.sandbox)
        self.assertEqual([s for s in issues if 'scattered' in s], [],
                         f'hugged row must not lint as scattered, got {issues!r}')

    def test_place_docked_ops_no_docks_returns_zero(self):
        """An op with no docked companions -> 0, nothing raises."""
        host = self.sandbox.create(textDAT, 'hug0_host')
        self._place(host, 0, 0, 120, 120)
        self.assertEqual(self.envoy._placeDockedOps(host), 0)

    # -----------------------------------------------------------------
    # Tool paths: create_op result + set_op_position dock-follow
    # -----------------------------------------------------------------

    def test_set_op_position_carries_docks_along(self):
        """Moving a host via _set_op_position re-hugs its docks at the new spot."""
        host = self.sandbox.create(textDAT, 'move_host')
        dat = self.sandbox.create(textDAT, 'move_dock')
        self._place(host, 0, 0, 120, 120)
        self._dock(host, dat)
        self.envoy._placeDockedOps(host)   # hugged at the origin position

        result = self.envoy._set_op_position(host.path, x=1000, y=800)

        self.assertNotIn('error', result, f'set_op_position failed: {result!r}')
        self.assertEqual(result.get('docks_moved'), 1,
                         f'result must report docks_moved, got {result!r}')
        self.assertEqual(dat.nodeY, 800 - dat.nodeHeight - 30,
                         'dock must re-hug below the NEW host position')
        self.assertLessEqual(abs(dat.nodeX - host.nodeX), 350,
                             'dock must travel with the host horizontally')

    def test_set_op_position_dock_itself_untouched(self):
        """Moving a DOCK explicitly must not trigger any follow logic on it."""
        host = self.sandbox.create(textDAT, 'still_host')
        dat = self.sandbox.create(textDAT, 'still_dock')
        self._place(host, 0, 0, 120, 120)
        self._dock(host, dat)

        result = self.envoy._set_op_position(dat.path, x=777, y=333)

        self.assertNotIn('error', result)
        self.assertNotIn('docks_moved', result,
                         'a dock has no docks of its own; no follow expected')
        self.assertEqual((dat.nodeX, dat.nodeY), (777, 333),
                         'explicit dock placement must be honored')

    def test_execute_python_scattered_new_dock_auto_hugged(self):
        """execute_python creating a host + scattered dock -> dock auto-hugged
        below the host and a WARNING names the auto-hug fix."""
        self._patch_log()
        sandbox_path = self.sandbox.path
        code = (
            "host = op('%s')\n"
            "h = host.create(textDAT, 'ep_hug_host')\n"
            "d = host.create(textDAT, 'ep_hug_dock')\n"
            "h.nodeX = 200; h.nodeY = 200\n"
            "d.dock = h\n"
            "d.nodeX = 1400; d.nodeY = -1400\n"
        ) % sandbox_path

        result = self.envoy._execute_python(code)
        self.assertTrue(result.get('success'),
                        f'execute_python should succeed, got {result!r}')

        h = self.sandbox.op('ep_hug_host')
        d = self.sandbox.op('ep_hug_dock')
        if d.path not in [x.path for x in h.docked]:
            self.skip('docking did not register in this TD build')
        self.assertEqual(d.nodeY, h.nodeY - d.nodeHeight - 30,
                         f'scattered new dock must be auto-hugged, got nodeY={d.nodeY}')
        hug_msgs = [m for m in self._warnings() if 'auto-hugged' in m]
        self.assertGreaterEqual(len(hug_msgs), 1,
                                f'expected an auto-hug WARNING, got {self._log_calls!r}')

    # -----------------------------------------------------------------
    # _lintLayout: guards
    # -----------------------------------------------------------------

    def test_empty_comp_no_issues(self):
        """Guard: an empty COMP (0 kids, < 2) -> []."""
        sub = self.sandbox.create(baseCOMP, 'empty_sub')
        issues = self.envoy._lintLayout(sub)
        self.assertEqual(issues, [])

    # -----------------------------------------------------------------
    # _execute_python end-to-end: WARNING emitted / not emitted
    # -----------------------------------------------------------------

    def test_execute_python_stacked_creation_logs_warning(self):
        """execute_python that creates 2 ops at (0,0) -> a 'LAYOUT WARNING' is logged."""
        self._patch_log()
        sandbox_path = self.sandbox.path
        code = (
            "host = op('%s')\n"
            "a = host.create(textDAT, 'ep_stack_a')\n"
            "b = host.create(textDAT, 'ep_stack_b')\n"
            "a.nodeX = 0; a.nodeY = 0\n"
            "b.nodeX = 0; b.nodeY = 0\n"
        ) % sandbox_path

        result = self.envoy._execute_python(code)
        self.assertTrue(result.get('success'),
                        f'execute_python should succeed, got {result!r}')

        warnings = [m for m in self._warnings() if 'LAYOUT WARNING' in m]
        self.assertGreaterEqual(len(warnings), 1,
                                f'Expected a LAYOUT WARNING, got log calls {self._log_calls!r}')
        # The warning should reference the parent COMP path it found a problem in.
        self.assertTrue(any(sandbox_path in m for m in warnings),
                        f'Warning should name the sandbox parent {sandbox_path!r}: {warnings!r}')

    def test_execute_python_clean_creation_logs_no_warning(self):
        """execute_python that places new ops cleanly -> NO 'LAYOUT WARNING'."""
        self._patch_log()
        sandbox_path = self.sandbox.path
        code = (
            "host = op('%s')\n"
            "a = host.create(textDAT, 'ep_clean_a')\n"
            "b = host.create(textDAT, 'ep_clean_b')\n"
            "a.nodeX = 100; a.nodeY = 100; a.nodeWidth = 120; a.nodeHeight = 120\n"
            "b.nodeX = 600; b.nodeY = 100; b.nodeWidth = 120; b.nodeHeight = 120\n"
        ) % sandbox_path

        result = self.envoy._execute_python(code)
        self.assertTrue(result.get('success'),
                        f'execute_python should succeed, got {result!r}')

        warnings = [m for m in self._warnings() if 'LAYOUT WARNING' in m]
        self.assertEqual(warnings, [],
                         f'Clean creation must not warn, got {warnings!r}')
