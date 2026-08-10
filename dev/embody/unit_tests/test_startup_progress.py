"""
Test suite: the startup-progress signals (Embody/startup_progress.py).

Pure module, no TouchDesigner import, so this runs under plain pytest on
the whole CI matrix as well as inside TD -- which is the point. The
startup viewer is the one surface a user watches while everything else is
blocked, and the failures it exists to expose (a wedged catalog scan, a
dependency install that never finishes, a Convoy host that needs repair)
are precisely the ones nobody can reproduce on demand. So the DECISION --
which state each step is in, and whether a bar may be drawn at all -- is
tested here rather than by squinting at a running session.

The assertions worth reading are the ones about lying: total=0 must yield
a None fraction (an elapsed clock, never an invented width), a skipped
step must not read as failed or as 0%, and the halo denominator must not
count a failure as progress.
"""

import importlib.util
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_PATH = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody',
                     'startup_progress.py')

_spec = importlib.util.spec_from_file_location('startup_progress', _PATH)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)
sys.modules[_spec.name] = sp

_EMBODY_TDN = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody.tdn')
_EMBODY_EXT = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody',
                           'EmbodyExt.py')
_CATALOG_EXT = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody',
                            'CatalogManagerExt.py')
_EXECUTE = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody', 'execute.py')

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


def _real_custom_pars():
    """Every custom parameter the Embody COMP actually has.

    Read from the COMP's own .tdn rather than hard-coded, so this cannot
    be the second copy of a name that drifts. The block is the top-level
    `custom_pars:` mapping; it ends at the next column-zero key.
    """
    import io
    import re
    names = set()
    inside = False
    with io.open(_EMBODY_TDN, 'r', encoding='utf-8') as handle:
        for line in handle:
            if not inside:
                inside = line.rstrip('\n') == 'custom_pars:'
                continue
            if line[:1] not in (' ', '\n', '\r', ''):
                break                       # next column-zero key
            match = re.match(r'\s+- name: (\w+)\s*$', line)
            if match:
                names.add(match.group(1))
    return names


REAL_PARS = _real_custom_pars()


class TestCatalogStep(EmbodyTestCase):
    """The ONE step with a real denominator."""

    def test_a_scan_reports_its_real_counters(self):
        got = sp.catalog_step('Scanning defaults (7/344)', 7, 344)
        self.assertEqual(got['state'], sp.RUNNING)
        self.assertEqual((got['done'], got['total']), (7, 344))
        self.assertAlmostEqual(sp.fraction(got), 7 / 344.0)

    def test_the_palette_pass_is_also_measured(self):
        got = sp.catalog_step('Scanning palette (12/40)', 12, 40)
        self.assertEqual(got['state'], sp.RUNNING)
        self.assertAlmostEqual(sp.fraction(got), 0.3)

    def test_disabled_is_SKIPPED_not_zero_percent(self):
        """A bar at 0% for something that legitimately never ran is its
        own small lie."""
        got = sp.catalog_step('Disabled')
        self.assertEqual(got['state'], sp.SKIPPED)
        self.assertEqual(sp.fraction(got), 1.0)

    def test_a_complete_catalog_is_DONE_even_though_it_took_one_frame(self):
        got = sp.catalog_step('Enabled', 0, 0)
        self.assertEqual(got['state'], sp.DONE)
        self.assertEqual(sp.fraction(got), 1.0)

    def test_a_failed_scan_is_FAILED_and_keeps_its_words(self):
        got = sp.catalog_step('Palette scan failed -- see log', 3, 40)
        self.assertEqual(got['state'], sp.FAILED)
        self.assertIn('see log', got['detail'])

    def test_the_counters_win_over_the_string(self):
        """The string is for humans; the bar must follow the scan's own
        numbers so the two can never disagree."""
        got = sp.catalog_step('Scanning defaults (0/0)', 9, 10)
        self.assertEqual((got['done'], got['total']), (9, 10))


class TestEnvoyStep(EmbodyTestCase):
    """Derived from Envoystatus -- about twenty writers, one parameter."""

    def test_the_one_time_dependency_install_is_running_with_NO_bar(self):
        """The real screenshot that started this: 'Installing deps...
        (one-time)'. It is one opaque uv call, so there is no honest
        denominator and the viewer must show elapsed time instead."""
        got = sp.envoy_step('Installing deps... (one-time)', started=100.0)
        self.assertEqual(got['state'], sp.RUNNING)
        self.assertEqual(got['total'], 0)
        self.assertIsNone(sp.fraction(got),
                          'no denominator means NO bar -- never a '
                          'fabricated percentage')
        self.assertEqual(sp.elapsed_text(got, 142.0), '0:42')

    def test_running_on_a_port_is_done(self):
        self.assertEqual(sp.envoy_step('Running on port 9870')['state'],
                         sp.DONE)

    def test_every_in_flight_phrase_reads_as_running(self):
        for text in ('Preparing Python environment...', 'Starting...',
                     'Restarting after save...',
                     'Restarting after reinit...', 'Reviving (watchdog)...',
                     'Convoy wake starting...'):
            self.assertEqual(sp.envoy_step(text)['state'], sp.RUNNING, text)

    def test_disabled_and_perform_mode_are_skipped(self):
        for text in ('Disabled', 'Perform Mode'):
            self.assertEqual(sp.envoy_step(text)['state'], sp.SKIPPED, text)

    def test_errors_are_FAILED_and_never_animate(self):
        for text in ('Error: Python environment not ready',
                     'Error: ports 9870-9879 in use',
                     'Error: bridge (gave up after 5 min)'):
            got = sp.envoy_step(text)
            self.assertEqual(got['state'], sp.FAILED, text)
            self.assertEqual(got['detail'], text)


class TestConvoyStep(EmbodyTestCase):

    def test_connected_is_done(self):
        self.assertEqual(sp.convoy_step('Connected')['state'], sp.DONE)

    def test_the_blocking_readouts_are_FAILED(self):
        """These are exactly the states that were invisible all week."""
        for text in ('Not installed',
                     'Needs repair -- Python not found (reinstall)',
                     'Install failed -- see log',
                     'Consent required -- enable Convoy again'):
            self.assertEqual(sp.convoy_step(text)['state'], sp.FAILED, text)

    def test_waiting_on_the_user_is_STALLED_not_running(self):
        """Blocked is not progress, and must not animate like it."""
        got = sp.convoy_step('Waiting for project save')
        self.assertEqual(got['state'], sp.STALLED)
        self.assertIsNone(sp.fraction(got))

    def test_disabled_is_skipped(self):
        self.assertEqual(sp.convoy_step('Disabled')['state'], sp.SKIPPED)


class TestTheViewerCannotBeLiedTo(EmbodyTestCase):
    """fraction() is the gate between honest and invented width."""

    def test_no_denominator_means_no_bar(self):
        self.assertIsNone(sp.fraction(sp.simple_step(sp.RUNNING)))

    def test_a_zero_total_never_divides(self):
        self.assertIsNone(sp.fraction(
            sp.simple_step(sp.RUNNING, done=5, total=0)))

    def test_overshoot_and_undershoot_are_clamped(self):
        self.assertEqual(
            sp.fraction(sp.simple_step(sp.RUNNING, done=99, total=10)), 1.0)
        self.assertEqual(
            sp.fraction(sp.simple_step(sp.RUNNING, done=-4, total=10)), 0.0)

    def test_idle_is_empty_and_terminal_success_is_full(self):
        self.assertEqual(sp.fraction(sp.simple_step(sp.IDLE)), 0.0)
        self.assertEqual(sp.fraction(sp.simple_step(sp.DONE)), 1.0)
        self.assertEqual(sp.fraction(sp.simple_step(sp.SKIPPED)), 1.0)

    def test_elapsed_needs_a_real_start(self):
        self.assertEqual(sp.elapsed_text(sp.simple_step(sp.RUNNING), 10.0),
                         '')
        self.assertEqual(
            sp.elapsed_text(sp.simple_step(sp.RUNNING, started=0.0), 61.0),
            '1:01')


class TestSnapshot(EmbodyTestCase):

    def test_four_bars_by_default_and_the_fifth_only_when_relevant(self):
        four = sp.snapshot()
        self.assertEqual(four['order'], list(sp.BASE_STEPS))
        self.assertNotIn(sp.STEP_RESTORE, four['steps'])
        five = sp.snapshot(restore=sp.simple_step(sp.RUNNING))
        self.assertEqual(five['order'][-1], sp.STEP_RESTORE)

    def test_the_halo_denominator_excludes_a_failure(self):
        """Counting terminals would show 4/4 with a red wedge and read as
        success. `in_play` is 'steps that could still end well', so a
        failure removes itself from the denominator."""
        snap = sp.snapshot(
            repo=sp.simple_step(sp.DONE),
            catalog=sp.simple_step(sp.DONE),
            envoy=sp.simple_step(sp.FAILED),
            convoy=sp.simple_step(sp.RUNNING))
        s = snap['summary']
        self.assertEqual((s['complete'], s['in_play']), (2, 3))
        self.assertEqual(s['failed'], 1)
        self.assertFalse(s['settled'])

    def test_a_skipped_step_is_not_counted_as_complete(self):
        snap = sp.snapshot(repo=sp.simple_step(sp.DONE),
                           catalog=sp.simple_step(sp.SKIPPED),
                           envoy=sp.simple_step(sp.SKIPPED),
                           convoy=sp.simple_step(sp.SKIPPED))
        s = snap['summary']
        self.assertEqual(s['complete'], 1)
        self.assertEqual(s['skipped'], 3)
        self.assertTrue(s['settled'], 'nothing is still moving')

    def test_a_stall_is_not_settled(self):
        snap = sp.snapshot(convoy=sp.simple_step(sp.STALLED))
        self.assertFalse(snap['summary']['settled'],
                         'waiting on the user is unfinished, not done')

    def test_the_real_startup_this_session_reads_correctly(self):
        """The live values off the running project, end to end."""
        snap = sp.snapshot(
            repo=sp.simple_step(sp.DONE),
            catalog=sp.catalog_step('Enabled'),
            envoy=sp.envoy_step('Running on port 9870'),
            convoy=sp.convoy_step('Connected'))
        s = snap['summary']
        self.assertEqual(s['complete'], 4)
        self.assertEqual(s['in_play'], 4)
        self.assertTrue(s['settled'])
        self.assertEqual(snap['format'], sp.FORMAT)


class TestFontRenderedBars(EmbodyTestCase):
    """The bar is a STRING, so its honesty is directly assertable.

    This is the whole reason the bars are drawn in font rather than in a
    shader: a texture has to be squinted at, a string can be pinned.
    """

    def _bar(self, **kw):
        return sp.bar_text(sp.simple_step(sp.RUNNING, **kw), width=20)

    def test_a_fill_is_proportional_and_sub_character(self):
        """Eighth-blocks mean a 20-cell bar resolves 160 steps, so early
        progress is visible instead of sitting at zero for ages."""
        self.assertEqual(self._bar(done=0, total=344).count(sp.BLOCK_FULL), 0)
        self.assertEqual(self._bar(done=172, total=344),
                         sp.BLOCK_FULL * 10 + sp.BLOCK_TRACK * 10)
        self.assertEqual(self._bar(done=344, total=344), sp.BLOCK_FULL * 20)
        # 3% of 20 cells is 0.6 of one character -- a fifth-eighths block.
        self.assertIn(sp.EIGHTHS[4], self._bar(done=10, total=344))

    def test_a_bar_is_always_exactly_its_width(self):
        """A ragged right edge would break the column alignment that
        makes the value text line up."""
        for done in range(0, 345, 7):
            self.assertEqual(len(self._bar(done=done, total=344)), 20)

    def test_an_indeterminate_bar_NEVER_accumulates(self):
        """The sweep moves but never fills: nothing in the string may
        imply a percentage when no denominator exists."""
        seen = set()
        for phase in (0.0, 0.25, 0.5, 0.75):
            bar = sp.bar_text(sp.simple_step(sp.RUNNING), 20, phase)
            self.assertEqual(len(bar), 20)
            self.assertEqual(bar.count(sp.BLOCK_FULL), 0,
                             'a sweep must never use the FILLED glyph')
            self.assertIn(sp.BLOCK_MED, bar)
            seen.add(bar)
        self.assertGreater(len(seen), 1, 'the sweep must actually move')

    def test_a_failure_is_solid_never_partly_filled(self):
        bar = sp.bar_text(sp.simple_step(sp.FAILED, done=3, total=10), 20)
        self.assertEqual(bar, sp.BLOCK_FULL * 20)

    def test_a_skipped_step_is_all_track_not_all_fill(self):
        bar = sp.bar_text(sp.simple_step(sp.SKIPPED), 20)
        self.assertEqual(bar, sp.BLOCK_TRACK * 20)

    def test_the_value_column_never_shows_a_percentage_without_counters(self):
        self.assertEqual(sp.value_text(sp.simple_step(sp.RUNNING, done=7,
                                                      total=9)), '7/9')
        self.assertEqual(
            sp.value_text(sp.simple_step(sp.RUNNING, started=0.0), now=42.0),
            '0:42')
        self.assertEqual(sp.value_text(sp.simple_step(sp.FAILED)), 'failed')
        self.assertEqual(sp.value_text(sp.simple_step(sp.SKIPPED)), 'skipped')

    def test_every_glyph_is_written_as_an_escape_not_a_literal(self):
        """rules/ascii-punctuation.md: the source file stays ASCII. A raw
        block character read back through a legacy codepage is mojibake,
        and this module round-trips through a TouchDesigner DAT."""
        import io
        with io.open(_PATH, 'rb') as f:
            raw = f.read()
        self.assertEqual([b for b in bytearray(raw) if b > 127], [])

    def test_rows_are_ordered_and_coloured_by_state(self):
        snap = sp.snapshot(repo=sp.simple_step(sp.DONE),
                           catalog=sp.simple_step(sp.FAILED))
        out = sp.rows(snap)
        self.assertEqual([k for k, _t, _c in out], list(sp.BASE_STEPS))
        self.assertEqual(out[0][2], sp.STATE_RGB[sp.DONE])
        self.assertEqual(out[1][2], sp.STATE_RGB[sp.FAILED])


class TestLiveAssembly(EmbodyTestCase):
    """live_row takes the COMP as an argument, so it is testable with a
    stand-in instead of needing a running session."""

    class _Par:
        def __init__(self, value):
            self._value = value

        def eval(self):
            return self._value

    class _Fake:
        """A stand-in COMP that can only carry REAL parameter names.

        The fake used to accept anything, so the suite happily fed it a
        `Catalogstatus` the COMP has never had -- and every catalog
        assertion passed against a parameter that does not exist while
        the live panel read "" and fabricated DONE for the whole life of
        a scan. Rejecting unknown names here is what makes the suite fail
        when a name drifts instead of testing its own invention.

        `path` is fixed because the module keys its observed clocks and
        its published record per COMP; a per-instance id would give every
        `self._comp()` in a test its own history.
        """

        path = '/embody/Embody'

        def __init__(self, **kw):
            unknown = sorted(k for k in kw if k not in REAL_PARS)
            if unknown:
                raise AssertionError(
                    'no such Embody parameter(s): %s -- the fake must '
                    'mirror the real COMP' % ', '.join(unknown))
            self.par = type('P', (), {})()
            for k, v in kw.items():
                setattr(self.par, k, TestLiveAssembly._Par(v))

    def test_it_reads_the_live_parameters(self):
        comp = self._Fake(Version='6.0.224',
                          Envoystatus='Running on port 9870',
                          Convoystatus='Connected',
                          Status='Enabled')
        text, rgb = sp.live_row(comp, sp.STEP_ENVOY)
        self.assertIn('Envoy', text)
        self.assertIn('done', text)
        self.assertEqual(rgb, sp.STATE_RGB[sp.DONE])

    def test_the_catalog_counters_are_read_back_from_the_status_line(self):
        """Real numbers the scan wrote, merely transported through the
        string. The parameter is `Status` -- the scan's own choke point
        writes its line there, and there has never been a Catalogstatus."""
        comp = self._Fake(Version='6.0.224', Envoystatus='Starting...',
                          Convoystatus='Disabled',
                          Status='Scanning defaults (142/344)')
        snap = sp.live_snapshot(comp)
        step = snap['steps'][sp.STEP_CATALOG]
        self.assertEqual((step['done'], step['total']), (142, 344))
        self.assertAlmostEqual(sp.fraction(step), 142 / 344.0)

    def test_a_running_scan_keeps_the_viewer_in_startup_mode(self):
        """The whole cost of the wrong parameter name: the catalog could
        never be seen RUNNING, so the one step with a real denominator
        never once put the panel into its progress view."""
        comp = self._Fake(Version='6.0.224', Status='Scanning palette (3/40)',
                          Envoystatus='Disabled', Convoystatus='Disabled')
        snap = sp.live_snapshot(comp)
        self.assertEqual(snap['steps'][sp.STEP_CATALOG]['state'], sp.RUNNING)
        self.assertTrue(sp.is_installing(snap))

    def test_an_absent_status_is_IDLE_not_a_fabricated_DONE(self):
        """`par("Catalogstatus") or "Enabled"` turned a parameter that
        does not exist into a finished catalog, every frame, forever."""
        comp = self._Fake(Version='6.0.224')
        step = sp.live_snapshot(comp)['steps'][sp.STEP_CATALOG]
        self.assertEqual(step['state'], sp.IDLE)
        self.assertNotEqual(step['state'], sp.DONE)

    def test_a_malformed_count_is_NO_count_not_a_guess(self):
        """Parsing must fail closed. A status line whose (N/T) is absent
        or unreadable yields an indeterminate bar -- never a number the
        scan did not actually report."""
        for text in ('Scanning defaults', 'Scanning (abc/def)',
                     'Scanning (1/2/3)', 'Scanning ()', 'Scanning (7)'):
            step = sp.catalog_step(text)
            self.assertIsNone(sp.fraction(step),
                              '%r must not produce a bar' % (text,))

    def test_a_missing_parameter_never_raises(self):
        snap = sp.live_snapshot(self._Fake())
        self.assertEqual(snap['steps'][sp.STEP_ENVOY]['state'], sp.IDLE)


class TestEveryParameterItReadsExists(EmbodyTestCase):
    """The module reads the COMP through getattr, which cannot fail loud.

    `getattr(embody.par, 'Catalogstatus', None)` returns None for a
    parameter that has never existed, the read falls back to "", and the
    step reports whatever the fallback invents -- silently, on every
    frame, for as long as the name is wrong. Nothing inside the module
    can catch that, so the names it reads are checked against the COMP's
    own .tdn here.
    """

    def _names_read(self):
        import io
        import re
        with io.open(_PATH, 'r', encoding='utf-8') as handle:
            source = handle.read()
        return sorted(set(re.findall(r'\bpar\("(\w+)"\)', source)))

    def test_the_tdn_actually_parsed(self):
        """Sanity: an empty par set would make the guard below vacuous."""
        self.assertGreater(len(REAL_PARS), 50)
        for known in ('Status', 'Envoystatus', 'Convoystatus', 'Version'):
            self.assertIn(known, REAL_PARS)

    def test_it_found_the_reads(self):
        names = self._names_read()
        self.assertGreaterEqual(len(names), 5, names)
        self.assertIn('Status', names)

    def test_every_parameter_read_is_a_real_one(self):
        missing = [n for n in self._names_read() if n not in REAL_PARS]
        self.assertEqual(
            [], missing,
            'startup_progress reads parameter(s) the Embody COMP does not '
            'have: %s -- the read returns "" and the step reports a '
            'fabricated state forever' % (missing,))


class TestResponsiveLayout(EmbodyTestCase):
    """The bar fills the container, at any container size."""

    def test_a_wider_panel_gets_a_longer_bar(self):
        narrow = sp.fit_bar_width(470, 13)
        wide = sp.fit_bar_width(940, 13)
        self.assertGreater(wide, narrow)
        self.assertGreater(narrow, 6)

    def test_a_bigger_font_gets_fewer_cells(self):
        self.assertLess(sp.fit_bar_width(470, 26),
                        sp.fit_bar_width(470, 13))

    def test_it_never_returns_a_zero_or_negative_bar(self):
        """A panel squeezed to nothing must still render a readable stub
        -- a negative width would raise or produce an empty row."""
        for width in (0, 1, 40, -100):
            got = sp.fit_bar_width(width, 13)
            self.assertGreaterEqual(got, 6)
            self.assertEqual(len(sp.bar_text(sp.simple_step(sp.DONE), got)),
                             got)

    def test_garbage_input_degrades_instead_of_raising(self):
        """These come from live panel geometry, which can be None during
        a rebuild."""
        self.assertEqual(sp.fit_bar_width(None, 13), 6)
        self.assertEqual(sp.fit_bar_width(470, None), 6)

    def test_the_rendered_row_actually_fits_the_panel(self):
        """The point of the arithmetic: the composed row must not exceed
        the character budget the panel can show."""
        for width in (300, 470, 700, 940, 1400):
            cells = sp.fit_bar_width(width, 13)
            row = sp.row_text(sp.STEP_CATALOG,
                              sp.simple_step(sp.RUNNING, done=1, total=2),
                              bar_width=cells)
            budget = int((width - 48) / (13 * sp.MONO_ADVANCE))
            self.assertLessEqual(len(row), budget,
                                 'row overflows at width %d' % width)


class TestFontFitting(EmbodyTestCase):

    def test_a_short_wide_panel_is_limited_by_HEIGHT(self):
        self.assertAlmostEqual(sp.fit_font_size(2000, 30),
                               30 * sp.ROW_FONT_RATIO)

    def test_a_tall_narrow_panel_is_limited_by_WIDTH(self):
        """Sizing off height alone gives huge glyphs and a nine-cell bar
        with dead space -- the mistake this function exists to stop."""
        got = sp.fit_font_size(480, 200)
        self.assertLess(got, 200 * 0.43)
        self.assertAlmostEqual(got, 480 / (sp.TARGET_ROW_CHARS
                                           * sp.MONO_ADVANCE))

    def test_the_result_always_leaves_a_readable_bar(self):
        for w, h in ((260, 40), (480, 294), (900, 80), (1400, 300)):
            font = sp.fit_font_size(w, h)
            cells = sp.fit_bar_width(w, font, padding=int(w * 0.03) * 2)
            self.assertGreaterEqual(cells, 12,
                                    'only %d cells at %dx%d' % (cells, w, h))

    def test_it_is_clamped_and_never_raises(self):
        self.assertEqual(sp.fit_font_size(None, None), 7.0)
        self.assertEqual(sp.fit_font_size(0, 0), 7.0)
        self.assertLessEqual(sp.fit_font_size(99999, 99999), 40.0)


class TestSettledStatusMode(EmbodyTestCase):
    """After startup, bars answer a question nobody is asking.

    Four identical full-width blocks say nothing; what the user wants
    then is the state of each subsystem. Same panel, different question,
    one switch.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.225',
                'Updatestatus': '', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def test_a_settled_project_shows_STATUSES_not_bars(self):
        """Settled mode is the compact grid: marks, no bars."""
        view = sp.live_view(self._comp())
        joined = ''.join(t for _k, t, _c in view)
        self.assertNotIn(sp.BLOCK_FULL, joined)
        self.assertNotIn(sp.BLOCK_TRACK, joined)
        for name in ('Embody', 'Autosaved', 'Envoy', 'Convoy', 'v6.0.225'):
            self.assertIn(name, joined, name)
        self.assertIn(sp.GLYPH_OK, joined)

    def test_a_healthy_project_uses_TWO_columns(self):
        """Fewer, shorter rows is what pays for the larger font."""
        view = sp.live_view(self._comp())
        self.assertEqual(len(view), 3, 'five cells in two columns')

    def test_a_problem_drops_to_ONE_column_and_explains_itself(self):
        """A reason does not fit half a panel, and the layout changing
        is itself a signal that something needs looking at -- once the
        problem has outlasted the dwell."""
        sp.reset_session()
        comp = self._comp(Convoystatus='Not installed')
        sp.live_view(comp, now=0.0)                      # first sighting
        view = sp.live_view(comp, now=sp.PROBLEM_DWELL_S + 1)
        self.assertEqual(len(view), 5, 'one cell per row')
        joined = ''.join(t for _k, t, _c in view)
        self.assertIn(sp.GLYPH_BAD, joined)
        self.assertIn('not installed', joined)

    def test_the_happy_path_says_nothing_it_does_not_need_to(self):
        """A tick beside 'Convoy' already says 'Connected'."""
        view = sp.live_view(self._comp())
        joined = ''.join(t for _k, t, _c in view).lower()
        for noise in ('connected', 'enabled', 'up to date', 'dev checkout'):
            self.assertNotIn(noise, joined, noise)

    def test_an_installing_project_shows_BARS(self):
        sp.reset_session()
        view = sp.live_view(
            self._comp(Envoystatus='Installing deps... (one-time)'),
            now=1.0, phase=0.2)
        joined = ''.join(t for _k, t, _c in view)
        self.assertIn(sp.BLOCK_TRACK, joined)
        self.assertEqual([k for k, _t, _c in view], list(sp.BASE_STEPS))

    def test_the_switch_is_ONE_predicate(self):
        busy = sp.live_snapshot(self._comp(Convoystatus='Checking...'))
        calm = sp.live_snapshot(self._comp())
        self.assertTrue(sp.is_installing(busy))
        self.assertFalse(sp.is_installing(calm))

    # -- the five rows ---------------------------------------------------

    def test_embody_and_autosave_map_their_own_vocabulary(self):
        self.assertEqual(sp.embody_step('Enabled')['state'], sp.DONE)
        self.assertEqual(sp.embody_step('Disabled')['state'], sp.SKIPPED)
        self.assertEqual(sp.autosave_step('Saved 14:53:05 UTC')['state'],
                         sp.DONE)
        self.assertEqual(sp.autosave_step('Saving...')['state'], sp.RUNNING)
        self.assertEqual(sp.autosave_step('Export failed')['state'],
                         sp.FAILED)

    def test_an_available_update_is_STALLED_not_done_and_not_failed(self):
        """Check and Notify: nothing is broken, but something waits on
        the user. Green would hide it; red would misreport it."""
        got = sp.version_step('6.0.225', '6.0.230 available', 'notify')
        self.assertEqual(got['state'], sp.STALLED)
        self.assertIn('6.0.230', got['detail'])

    def test_up_to_date_is_done_and_names_the_version(self):
        """The version rides in the row's own LABEL column, so the word
        'Version' never has to be written and the detail stays short."""
        got = sp.version_step('6.0.225', 'Up to date', 'notify')
        self.assertEqual(got['state'], sp.DONE)
        self.assertEqual(got['label'], 'v6.0.225')
        self.assertEqual(got['detail'], 'up to date')

    def test_the_version_row_renders_its_version_as_the_label(self):
        line = sp.status_row_text(
            sp.STATUS_VERSION,
            sp.version_step('6.0.225', '6.0.230 available', 'notify'))
        self.assertTrue(line.startswith('v6.0.225'))
        self.assertNotIn('Version', line)

    def test_update_checks_off_is_skipped_not_a_failure(self):
        got = sp.version_step('6.0.225', '', 'off')
        self.assertEqual(got['state'], sp.SKIPPED)
        self.assertIn('off', got['detail'])

    def test_a_stated_refusal_is_skipped_not_failed(self):
        """The dev-checkout message is a no-op with a reason, not a
        broken updater."""
        got = sp.version_step(
            '6.0.225', 'This is the Embody dev checkout -- update via git',
            'notify')
        self.assertEqual(got['state'], sp.SKIPPED)

    def test_a_long_status_is_truncated_VISIBLY(self):
        """A silent cut at the panel edge reads as though the message
        ended -- which is how a truncated warning becomes a misread one."""
        step = sp.version_step('6.0.225', 'x' * 200, 'notify')
        line = sp.status_row_text(sp.STATUS_VERSION, step, width=40)
        self.assertEqual(len(line), 40)
        self.assertTrue(line.endswith('...'))

    def test_a_short_status_is_not_padded_or_cut(self):
        line = sp.status_row_text(sp.STATUS_EMBODY,
                                  sp.embody_step('Enabled'), width=40)
        self.assertEqual(
            line, 'Embody'.ljust(9) + ' Enabled',
            'the label column is padded to label_width and no further')
        self.assertFalse(line.endswith('...'))


class TestCompactStatus(EmbodyTestCase):
    """Width IS font size here, so wasted characters cost legibility.

    Two kinds of waste: the value repeating the label ("Envoy" /
    "Running on port 9870"), and the value spelling out a state that
    colour already carries.
    """

    def test_the_value_stops_repeating_the_label(self):
        self.assertEqual(sp.compact_status('Running on port 9870'),
                         'port 9870')

    def test_an_explanatory_clause_is_detail_not_status(self):
        self.assertEqual(
            sp.compact_status('Needs repair -- Python not found (reinstall)'),
            'needs repair')
        self.assertEqual(
            sp.compact_status('This is the Embody dev checkout -- update '
                              'via git, not self-update.'), 'dev checkout')

    def test_parentheticals_and_trailing_dots_are_dropped(self):
        self.assertEqual(sp.compact_status('Installing deps... (one-time)'),
                         'installing deps')
        self.assertEqual(sp.compact_status('Restarting after save...'),
                         'restarting: save')

    def test_a_timestamp_keeps_only_the_time(self):
        self.assertEqual(sp.compact_status('Saved 14:53:05 UTC'), '14:53:05')

    def test_the_redundant_error_word_goes_but_the_reason_stays(self):
        """Colour already says it failed; the ports are the useful part."""
        self.assertEqual(sp.compact_status('Error: ports 9870-9879 in use'),
                         'ports 9870-9879 in use')

    def test_short_values_are_left_alone(self):
        for text in ('Connected', 'Enabled', 'Perform Mode'):
            self.assertEqual(sp.compact_status(text), text)

    def test_every_known_status_fits_the_row_budget(self):
        """The whole point: nothing routine should truncate at the
        smallest panel size."""
        budget = sp.TARGET_ROW_CHARS - 8 - 1
        for text in ('Running on port 9870', 'Saved 14:53:05 UTC',
                     'Connected', 'Enabled', 'Installing deps... (one-time)',
                     'Needs repair -- Python not found (reinstall)',
                     'Not installed', 'Waiting for project save',
                     'This is the Embody dev checkout -- update via git'):
            self.assertLessEqual(len(sp.compact_status(text)), budget, text)

    def test_it_still_truncates_visibly_when_asked(self):
        got = sp.compact_status('a' * 100, max_width=20)
        self.assertEqual(len(got), 20)
        self.assertTrue(got.endswith('...'))


class TestLineSpacing(EmbodyTestCase):
    """Height follows the font, not the other way round."""

    def test_the_row_box_is_the_glyph_plus_half(self):
        self.assertAlmostEqual(sp.row_height_for(20), 30.0)
        self.assertAlmostEqual(sp.row_height_for(27.3), 27.3 * 1.5)

    def test_spacing_is_adjustable_and_never_collapses(self):
        self.assertAlmostEqual(sp.row_height_for(20, 0.0), 20.0)
        self.assertGreaterEqual(sp.row_height_for(0), 1.0)
        self.assertGreaterEqual(sp.row_height_for(None), 1.0)

    def test_font_from_width_alone_ignores_height(self):
        """Vertical space must not cap the glyph -- the panel grows to
        fit the text, so only the width budget binds."""
        self.assertAlmostEqual(
            sp.font_for_width(480),
            480 / (sp.TARGET_ROW_CHARS * sp.MONO_ADVANCE))
        self.assertEqual(sp.font_for_width(None), 7.0)
        self.assertLessEqual(sp.font_for_width(99999), 40.0)

    def test_the_bar_floor_holds_at_every_panel_width(self):
        """The regression this pins: tightening the budget silently
        starved the bar, because font scales with width too."""
        for w in (260, 340, 480, 700, 900, 1400):
            font = sp.font_for_width(w)
            cells = sp.fit_bar_width(w, font, padding=int(w * 0.03) * 2)
            self.assertGreaterEqual(cells, 12,
                                    'only %d cells at width %d' % (cells, w))


class TestRowColour(EmbodyTestCase):
    """One colour per row, so the ranking has to be right."""

    def test_a_failure_colours_the_whole_row(self):
        self.assertEqual(
            sp.worst_state([sp.simple_step(sp.DONE),
                            sp.simple_step(sp.FAILED)]), sp.FAILED)

    def test_a_skipped_cell_does_NOT_grey_out_a_healthy_neighbour(self):
        """Skipped is the least noteworthy state. Ranking it above done
        greyed out 'Autosaved' because the version cell beside it was a
        stated no-op."""
        self.assertEqual(
            sp.worst_state([sp.simple_step(sp.DONE),
                            sp.simple_step(sp.SKIPPED)]), sp.DONE)

    def test_attention_outranks_progress(self):
        self.assertEqual(
            sp.worst_state([sp.simple_step(sp.RUNNING),
                            sp.simple_step(sp.STALLED)]), sp.STALLED)

    def test_the_font_budget_subtracts_the_padding(self):
        """Sizing off the full panel width overflowed the last cell --
        'v6.0.225' rendered as 'v6.0.22' against the edge."""
        texts = ['x' * 23]
        usable = 480 - 28
        font = sp.font_for_rows(usable, texts)
        self.assertLessEqual(len(texts[0]) * font * sp.MONO_ADVANCE, usable)


class TestAutosaveAge(EmbodyTestCase):
    """Freshness beats a timestamp: '3m ago' needs no arithmetic."""

    def test_it_reports_an_age_not_a_clock_time(self):
        got = sp.autosave_step('Saved 14:53:05 UTC',
                               now_seconds=14 * 3600 + 56 * 60 + 5)
        self.assertEqual(got['state'], sp.DONE)
        self.assertEqual(got['detail'], '3m ago')

    def test_the_units_scale(self):
        self.assertEqual(sp.ago_text(12), '12s ago')
        self.assertEqual(sp.ago_text(185), '3m ago')
        self.assertEqual(sp.ago_text(7300), '2h ago')
        self.assertEqual(sp.ago_text(90000), '1d ago')

    def test_crossing_midnight_wraps_instead_of_going_negative(self):
        """A negative age renders as an empty cell -- once a day."""
        got = sp.autosave_step('Saved 23:59:00 UTC',
                               now_seconds=60)          # 00:01:00
        self.assertEqual(got['detail'], '2m ago')

    def test_an_unparseable_stamp_keeps_the_original_words(self):
        got = sp.autosave_step('Saved just now', now_seconds=100)
        self.assertEqual(got['state'], sp.DONE)
        self.assertIn('just now', got['detail'])

    def test_clock_parsing_is_strict(self):
        self.assertEqual(sp.clock_seconds('Saved 14:53:05 UTC'),
                         14 * 3600 + 53 * 60 + 5)
        self.assertIsNone(sp.clock_seconds('Saved 99:99:99'))
        self.assertIsNone(sp.clock_seconds('Saved 14:53'))
        self.assertIsNone(sp.clock_seconds('no time here'))

    def test_ago_text_never_raises(self):
        for bad in (None, 'x', -5):
            self.assertEqual(sp.ago_text(bad), '')


class TestAlwaysShownDetail(EmbodyTestCase):

    def test_the_autosave_age_gets_its_OWN_cell(self):
        """A tick cannot answer 'is my work safe?'. The age can -- and on
        its own line it costs 11 characters of column instead of 18,
        which is what pays for the larger font."""
        comp = TestLiveAssembly._Fake(
            Status='Enabled', Autosavestatus='Saved 14:53:05 UTC',
            Envoystatus='Running on port 9870', Convoystatus='Connected',
            Version='6.0.225', Updatestatus='Up to date',
            Autoupdate='notify')
        grid = sp.live_grid(comp)
        cells = dict((k, t) for row in grid for k, t, _c in row)
        self.assertIn(sp.STATUS_AUTOSAVE_AGE, cells)
        self.assertIn('ago', cells[sp.STATUS_AUTOSAVE_AGE])
        self.assertNotIn('ago', cells[sp.STATUS_AUTOSAVE],
                         'the label cell stays bare')

    def test_other_healthy_rows_stay_bare(self):
        self.assertEqual(
            sp.cell_text(sp.STATUS_CONVOY, sp.convoy_step('Connected')),
            '%s Convoy' % sp.GLYPH_OK)


class TestPerCellColour(EmbodyTestCase):
    """A row is a layout accident, not a thing."""

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.225',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def test_a_disabled_envoy_does_not_grey_its_ROW_MATE(self):
        """They share a row only because that is where they landed. One
        colour per row meant a disabled Envoy reported a state its
        neighbour was not in."""
        grid = sp.live_grid(self._comp(Envoystatus='Disabled'))
        row = [r for r in grid if any(k == sp.STATUS_ENVOY
                                      for k, _t, _c in r)][0]
        colours = dict((k, c) for k, _t, c in row)
        self.assertEqual(colours[sp.STATUS_ENVOY], sp.STATE_RGB[sp.SKIPPED])
        others = [c for k, c in colours.items() if k != sp.STATUS_ENVOY]
        self.assertTrue(others, 'the row has a neighbour to protect')
        for colour in others:
            self.assertNotEqual(colour, sp.STATE_RGB[sp.SKIPPED])

    def test_column_widths_follow_their_content(self):
        widths = sp.live_col_chars(self._comp())
        self.assertEqual(len(widths), 2)
        self.assertGreater(widths[1], widths[0],
                           'the autosave column is the wider one')

    def test_a_project_with_no_parameters_still_lays_out(self):
        """Every step reads IDLE. Nothing is BROKEN, so it takes the
        compact two-column path rather than the explanatory one."""
        widths = sp.live_col_chars(TestLiveAssembly._Fake())
        self.assertEqual(len(widths), 2)
        self.assertGreater(widths[0], 0)


class TestUnhealthyLayout(EmbodyTestCase):
    """The problem view must be ONE column of five rows.

    A tuple of one-key tuples reads almost identically and produces five
    COLUMNS of one row -- rendered side by side, with everything past
    the second cell slot invisible, exactly when something is wrong.
    """

    def _broken(self):
        return TestLiveAssembly._Fake(
            Status='Enabled', Autosavestatus='Saved 14:53:05 UTC',
            Envoystatus='Running on port 9870',
            Convoystatus='Not installed', Version='6.0.225',
            Updatestatus='Up to date', Autoupdate='notify')

    def test_a_problem_renders_one_column_of_five_rows(self):
        sp.reset_session()
        comp = self._broken()
        sp.live_grid(comp, now=0.0)
        grid = sp.live_grid(comp, now=sp.PROBLEM_DWELL_S + 1)
        self.assertEqual(len(grid), 5, 'five rows')
        for row in grid:
            self.assertEqual(len(row), 1, 'one cell per row')

    def test_no_cell_ever_lands_past_the_panels_slots(self):
        """The panel has two cell slots per row; a wider grid would be
        silently cropped."""
        for comp in (self._broken(),
                     TestLiveAssembly._Fake(),
                     TestLiveAssembly._Fake(Status='Enabled')):
            for row in sp.live_grid(comp):
                self.assertLessEqual(len(row), 2)


class TestTheGridFitsThePanel(EmbodyTestCase):
    """The panel has a FIXED number of cell slots; a grid larger than
    them is not clipped or scrolled -- it is silently dropped.

    This shipped: the panel had three row slots while the problem view
    is one column of five rows, so Convoy and the version disappeared
    exactly when something had gone wrong. The compact view is three
    rows, so it looked correct right up until it mattered.
    """

    PANEL_ROWS = 5
    PANEL_COLS = 2

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.225',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def _assert_fits(self, comp, label):
        grid = sp.live_grid(comp)
        self.assertLessEqual(len(grid), self.PANEL_ROWS,
                             '%s needs %d rows; the panel has %d'
                             % (label, len(grid), self.PANEL_ROWS))
        for row in grid:
            self.assertLessEqual(len(row), self.PANEL_COLS, label)

    def test_the_healthy_grid_fits(self):
        self._assert_fits(self._comp(), 'healthy')

    def test_EVERY_unhealthy_state_still_fits(self):
        """One transient subsystem flips the whole layout, so every one
        of them has to fit -- not just the tidy case."""
        for name, kw in (
                ('convoy transient', {'Convoystatus': 'Checking...'}),
                ('convoy broken', {'Convoystatus': 'Not installed'}),
                ('envoy starting', {'Envoystatus': 'Starting...'}),
                ('envoy failed',
                 {'Envoystatus': 'Error: ports 9870-9879 in use'}),
                ('autosave saving', {'Autosavestatus': 'Saving...'}),
                ('update waiting', {'Updatestatus': '6.0.230 available'}),
                ('embody disabled', {'Status': 'Disabled'}),
                ('nothing known', {}),
        ):
            self._assert_fits(self._comp(**kw), name)

    def test_a_bare_project_fits_too(self):
        self._assert_fits(TestLiveAssembly._Fake(), 'no parameters at all')


class TestTheLayoutDoesNotFLAP(EmbodyTestCase):
    """A panel that jumps every few seconds reads as a broken viewer.

    Convoy re-checks its host app, Envoy restarts on a reinit, the
    autosave runs -- all routine, all RUNNING, all of them used to
    reflow the whole panel and (worse) throw it back to progress bars.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.225',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def test_routine_activity_does_not_expand_the_grid(self):
        sp.live_grid(self._comp())          # startup settles first
        for name, kw in (('convoy heartbeat',
                          {'Convoystatus': 'Checking...'}),
                         ('envoy restarting',
                          {'Envoystatus': 'Restarting after save...'}),
                         ('autosave running',
                          {'Autosavestatus': 'Saving...'})):
            grid = sp.live_grid(self._comp(**kw))
            self.assertEqual(len(grid), 3, '%s reflowed the panel' % name)

    def test_a_real_problem_still_expands_it(self):
        for name, kw in (('convoy broken',
                          {'Convoystatus': 'Not installed'}),
                         ('update waiting',
                          {'Updatestatus': '6.0.230 available'}),
                         ('envoy failed',
                          {'Envoystatus': 'Error: ports in use'})):
            sp.reset_session()
            comp = self._comp(**kw)
            sp.live_grid(comp, now=0.0)
            grid = sp.live_grid(comp, now=sp.PROBLEM_DWELL_S + 1)
            self.assertEqual(len(grid), 5, '%s did not explain itself'
                             % name)

    def test_once_startup_settles_a_transient_never_returns_to_bars(self):
        """The exact flap that was visible on screen: Convoy checks in,
        the whole panel drops back to progress bars, then returns."""
        settled = sp.live_view(self._comp())
        self.assertNotIn(sp.BLOCK_TRACK, ''.join(t for _k, t, _c in settled))
        during = sp.live_view(self._comp(Convoystatus='Checking...'))
        joined = ''.join(t for _k, t, _c in during)
        self.assertNotIn(sp.BLOCK_TRACK, joined, 'reverted to bars')
        self.assertIn(sp.GLYPH_BUSY, joined, 'but it still shows activity')

    def test_bars_DO_show_before_startup_has_ever_settled(self):
        view = sp.live_view(
            self._comp(Envoystatus='Installing deps... (one-time)'))
        self.assertIn(sp.BLOCK_TRACK, ''.join(t for _k, t, _c in view))


class TestTheCompactViewNeverResizes(EmbodyTestCase):
    """Cell width sets column width sets font size sets the whole panel.

    So any cell whose TEXT changes on a timer relays the entire layout.
    'Convoy Registering' (20 chars) against 'Convoy' (8) made the panel
    visibly jump every time the host app checked in -- which read as the
    viewer being broken rather than as news about Convoy.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.226',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def test_convoy_heartbeats_never_move_a_column(self):
        widths = set()
        for status in ('Connected', 'Registering...', 'Checking...',
                       'Starting host app...', 'Installing...',
                       'Connected'):
            widths.add(tuple(sp.live_col_chars(
                self._comp(Convoystatus=status))))
        self.assertEqual(len(widths), 1,
                         'the columns resized: %r' % (widths,))

    def test_envoy_restarts_never_move_a_column(self):
        widths = set()
        for status in ('Running on port 9870', 'Restarting after save...',
                       'Reviving (watchdog)...', 'Starting...',
                       'Running on port 9870'):
            widths.add(tuple(sp.live_col_chars(
                self._comp(Envoystatus=status))))
        self.assertEqual(len(widths), 1, widths)

    def test_the_autosave_age_cannot_drive_the_column_either(self):
        """It ticks every second; its label is longer than any age it
        can render, so the column is pinned by the label instead."""
        widths = set()
        for stamp, now in (('Saved 14:53:05 UTC', 14 * 3600 + 53 * 60 + 6),
                           ('Saved 14:53:05 UTC', 14 * 3600 + 58 * 60),
                           ('Saved 01:00:00 UTC', 23 * 3600),
                           ('Saved 14:53:05 UTC', 14 * 3600 + 53 * 60 + 5)):
            step = sp.autosave_step(stamp, now_seconds=now)
            age = sp.compact_status(step.get('detail'))
            widths.add(len('  %s' % age) <= len('%s Autosaved'
                                                % sp.GLYPH_OK))
        self.assertEqual(widths, {True})

    def test_a_transient_shows_the_MARK_and_nothing_else(self):
        cells = dict((k, t) for row in sp.live_grid(
            self._comp(Convoystatus='Registering...')) for k, t, _c in row)
        self.assertEqual(cells[sp.STATUS_CONVOY].rstrip(),
                         '%s Convoy' % sp.GLYPH_BUSY)
        self.assertNotIn('Registering', ''.join(cells.values()))

    def test_a_real_failure_STILL_says_why(self):
        """Suppressing reasons must not suppress them where they matter."""
        sp.reset_session()
        comp = self._comp(Convoystatus='Not installed')
        sp.live_grid(comp, now=0.0)
        joined = ''.join(t for row in sp.live_grid(
            comp, now=sp.PROBLEM_DWELL_S + 1) for _k, t, _c in row)
        self.assertIn('not installed', joined)


class TestAProblemMustPERSIST(EmbodyTestCase):
    """Convoy passes through real failure states while it updates.

    Its own vocabulary includes "Not installed" and "Needs repair"
    mid-install -- true at that instant, gone a moment later. Reflowing
    on them made the panel expand to five rows and snap back every time
    the host app cycled, which is what "freaks out" looks like.
    """

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.226',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def test_a_convoy_reinstall_never_expands_the_panel(self):
        """The exact sequence: the host app cycles through Not installed
        and Installing while it updates itself, then reconnects."""
        healthy = self._comp()
        clock = 0.0
        for status in ('Connected', 'Checking...', 'Not installed',
                       'Installing...', 'Installed -- starting...',
                       'Connected'):
            clock += 1.0                       # a second per step
            grid = sp.live_grid(self._comp(Convoystatus=status), now=clock)
            self.assertEqual(len(grid), 3,
                             '%r reflowed the panel at t=%s'
                             % (status, clock))

    def test_a_failure_that_STAYS_does_expand(self):
        comp = self._comp(Convoystatus='Not installed')
        self.assertEqual(len(sp.live_grid(comp, now=0.0)), 3, 'not yet')
        self.assertEqual(len(sp.live_grid(comp, now=5.0)), 3, 'still not')
        self.assertEqual(len(sp.live_grid(comp, now=7.0)), 5, 'now')

    def test_the_MARK_is_immediate_even_though_the_reason_waits(self):
        """A user must see something is wrong at once; only the
        explanatory layout is delayed."""
        grid = sp.live_grid(self._comp(Convoystatus='Not installed'),
                            now=0.0)
        self.assertIn(sp.GLYPH_BAD,
                      ''.join(t for row in grid for _k, t, _c in row))

    def test_a_DIFFERENT_failure_restarts_the_clock(self):
        """Otherwise a brief Convoy blip would hand its dwell to an
        unrelated Envoy failure seconds later."""
        sp.live_grid(self._comp(Convoystatus='Not installed'), now=0.0)
        grid = sp.live_grid(
            self._comp(Envoystatus='Error: ports in use'), now=7.0)
        self.assertEqual(len(grid), 3, 'inherited the previous dwell')

    def test_recovery_clears_the_clock(self):
        comp = self._comp(Convoystatus='Not installed')
        sp.live_grid(comp, now=0.0)
        sp.live_grid(self._comp(), now=1.0)          # recovered
        self.assertEqual(len(sp.live_grid(comp, now=2.0)), 3,
                         'a fresh problem must start its own dwell')


class TestTheFontMatchesTheVIEW(EmbodyTestCase):
    """live_view and live_font must answer "which mode?" identically.

    They drifted: live_view latched "startup is over", live_font kept
    asking is_installing(), which is true for any RUNNING step. A Convoy
    heartbeat then left the grid on screen but sized the font for bars --
    measured on the live panel as 39.1 -> 24.9 -> 39.1 on a ~30s cycle,
    with the column widths never moving, which is why it read as the
    layout freaking out rather than as a font change.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.226',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def test_the_font_does_not_move_across_a_convoy_cycle(self):
        sp.live_view(self._comp(), now=0.0)          # startup settles
        sizes = set()
        for status in ('Connected', 'Checking...', 'Registering...',
                       'Not installed', 'Installing...',
                       'Installed -- starting...', 'Connected'):
            sizes.add(round(sp.live_font(self._comp(Convoystatus=status),
                                         452, now=1.0), 2))
        self.assertEqual(len(sizes), 1, 'font moved: %r' % (sizes,))

    def test_the_font_does_not_move_across_an_envoy_restart(self):
        sp.live_view(self._comp(), now=0.0)
        sizes = set()
        for status in ('Running on port 9870', 'Restarting after save...',
                       'Reviving (watchdog)...', 'Running on port 9870'):
            sizes.add(round(sp.live_font(self._comp(Envoystatus=status),
                                         452, now=1.0), 2))
        self.assertEqual(len(sizes), 1, sizes)

    def test_the_font_ALWAYS_matches_the_grid_it_draws(self):
        """There is no second sizing rule to drift from.

        The font is derived from live_col_chars() -- the same numbers
        that set the cell widths -- so no state, transient or otherwise,
        can make the size disagree with the content.
        """
        for kw in ({}, {'Envoystatus': 'Installing deps... (one-time)'},
                   {'Convoystatus': 'Registering...'},
                   {'Convoystatus': 'Not installed'}):
            comp = self._comp(**kw)
            cols = sp.live_col_chars(comp, now=1.0)
            total = sum(cols) + sp.GRID_GAP * max(0, len(cols) - 1)
            self.assertAlmostEqual(
                sp.live_font(comp, 452, now=1.0),
                sp.font_for_rows(452, ['x' * total]), places=6)

    def test_bar_mode_IS_reachable_from_the_panel(self):
        """The gap this replaces: the panel binds its cells to
        live_grid(), which never produced a bar, so the whole startup
        view was unreachable from the viewer and nothing said so.

        live_grid now answers the startup question in the SAME shape the
        panel already renders -- one cell per row -- which is why no
        panel expression had to change.
        """
        sp.reset_session()
        busy = self._comp(Envoystatus='Installing deps... (one-time)')
        view = sp.live_view(busy, now=0.0)
        self.assertTrue(any(sp.BLOCK_TRACK in text
                            for _k, text, _c in view),
                        'live_view produces bars')
        sp.reset_session()
        grid = sp.live_grid(busy, now=0.0)
        self.assertTrue(any(sp.BLOCK_TRACK in text
                            for row in grid for _k, text, _c in row),
                        'live_grid -- what the panel draws -- must too')
        for row in grid:
            self.assertEqual(len(row), 1,
                             'a bar row uses ONE cell slot, so the second '
                             'slot the panel offers stays empty')

    def test_view_and_font_agree_on_every_transient(self):
        sp.live_view(self._comp(), now=0.0)
        for status in ('Checking...', 'Registering...', 'Installing...'):
            comp = self._comp(Convoystatus=status)
            view = sp.live_view(comp, now=1.0)
            is_bars = any(sp.BLOCK_TRACK in t for _k, t, _c in view)
            uses_bar_budget = abs(sp.live_font(comp, 452, now=1.0)
                                  - sp.font_for_width(452)) < 0.01
            self.assertEqual(is_bars, uses_bar_budget,
                             '%s: view and font disagree' % status)


class TestThePublishedRecord(EmbodyTestCase):
    """The steps no parameter can describe.

    A restore that restores nothing writes no status string at all, and a
    phase that dies writes its last count forever -- so the phases hand
    their state over directly. The contract that matters is TERMINAL ON
    EVERY PATH: this viewer exists because a readout that keeps showing
    something plausible is worse than no readout.
    """

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Version': '6.0.226',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def test_a_published_step_reaches_the_snapshot(self):
        comp = self._comp()
        self.assertNotIn(sp.STEP_RESTORE, sp.live_snapshot(comp)['steps'])
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=2, total=7,
                   now=10.0)
        step = sp.live_snapshot(comp)['steps'][sp.STEP_RESTORE]
        self.assertEqual((step['state'], step['done'], step['total']),
                         (sp.RUNNING, 2, 7))
        self.assertAlmostEqual(sp.fraction(step), 2 / 7.0)

    def test_the_fifth_bar_appears_only_once_restore_publishes(self):
        comp = self._comp()
        self.assertEqual(sp.live_snapshot(comp)['order'], list(sp.BASE_STEPS))
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, total=3, now=0.0)
        self.assertEqual(sp.live_snapshot(comp)['order'][-1], sp.STEP_RESTORE)

    def test_two_phases_accumulate_into_ONE_bar(self):
        """TOX restore and TDN reconstruction are separate methods run
        fifteen frames apart; together they answer one question."""
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, total=2, add=True,
                   now=0.0)
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=2, add=True,
                   now=0.0)
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, total=3, add=True,
                   now=0.0)
        step = sp.published_steps(comp)[sp.STEP_RESTORE]
        self.assertEqual((step['done'], step['total']), (2, 5))

    def test_a_published_running_step_carries_its_start(self):
        """Without it the value column shows '...' forever, which reads
        the same after four seconds and after forty minutes."""
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, total=9, now=100.0)
        step = sp.published_steps(comp)[sp.STEP_RESTORE]
        self.assertEqual(step['started'], 100.0)
        self.assertEqual(sp.value_text(step, now=142.0), '0/9')
        bare = sp.simple_step(sp.RUNNING, started=100.0)
        self.assertEqual(sp.value_text(bare, now=142.0), '0:42')

    def test_the_start_survives_progress_updates(self):
        """A step that restamps on every count shows 0:00 forever -- the
        clock has to measure the STEP, not the last update."""
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, total=4, now=10.0)
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=1, total=4,
                   now=55.0)
        self.assertEqual(
            sp.published_steps(comp)[sp.STEP_RESTORE]['started'], 10.0)

    def test_finish_calls_a_step_that_did_work_DONE_and_fills_it(self):
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=3, total=5,
                   now=0.0)
        sp.finish(comp, sp.STEP_RESTORE)
        step = sp.published_steps(comp)[sp.STEP_RESTORE]
        self.assertEqual(step['state'], sp.DONE)
        self.assertEqual(sp.fraction(step), 1.0)

    def test_finish_calls_a_ZERO_WORK_startup_SKIPPED_not_done(self):
        """A bar at 100% for something that never ran is the same class
        of lie as a bar at 0% for it."""
        comp = self._comp()
        sp.finish(comp, sp.STEP_RESTORE)
        self.assertEqual(
            sp.published_steps(comp)[sp.STEP_RESTORE]['state'], sp.SKIPPED)

    def test_finish_can_report_a_failure(self):
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=1, total=4,
                   now=0.0)
        sp.finish(comp, sp.STEP_RESTORE, failed=True, detail='3 errors')
        step = sp.published_steps(comp)[sp.STEP_RESTORE]
        self.assertEqual(step['state'], sp.FAILED)
        self.assertIn('3 errors', step['detail'])

    def test_EVERY_published_step_ends_terminal_on_every_path(self):
        """The contract, stated once. Whatever a phase publishes, one
        close leaves nothing that can still animate."""
        for opening in (None, (sp.RUNNING, 0, 0), (sp.RUNNING, 4, 9),
                        (sp.STALLED, 0, 0), (sp.IDLE, 0, 0)):
            sp.reset_session()
            comp = self._comp()
            if opening is not None:
                state, done, total = opening
                sp.publish(comp, sp.STEP_RESTORE, state, done=done,
                           total=total, now=0.0)
            sp.finish(comp, sp.STEP_RESTORE)
            step = sp.published_steps(comp)[sp.STEP_RESTORE]
            self.assertIn(step['state'], (sp.DONE, sp.SKIPPED, sp.FAILED),
                          '%r left the step at %r' % (opening, step['state']))

    def test_an_unreported_step_is_CLOSED_not_left_animating(self):
        """An exception inside a deferred run() callback kills the rest
        of that chain silently, so a phase can stop existing between its
        RUNNING and its terminal publish."""
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=1, total=8,
                   now=0.0)
        closed = sp.close_unreported(comp, (sp.STEP_REPO, sp.STEP_RESTORE),
                                     now=30.0)
        self.assertEqual(closed, [sp.STEP_RESTORE])
        self.assertEqual(
            sp.published_steps(comp)[sp.STEP_RESTORE]['state'], sp.FAILED)

    def test_closing_leaves_a_finished_step_alone(self):
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, total=2, now=0.0)
        sp.finish(comp, sp.STEP_RESTORE)
        self.assertEqual(sp.close_unreported(comp, sp.STEPS, now=30.0), [])
        self.assertEqual(
            sp.published_steps(comp)[sp.STEP_RESTORE]['state'], sp.DONE)

    def test_a_step_nobody_claimed_is_not_failed(self):
        """close_unreported must not invent a failure for a phase that
        this project legitimately never runs."""
        comp = self._comp()
        self.assertEqual(sp.close_unreported(comp, sp.STEPS, now=30.0), [])

    def test_an_unknown_key_or_state_is_REFUSED_not_swallowed(self):
        """A caller that drifts from the vocabulary must fail a contract
        test, not publish nothing and look fine."""
        comp = self._comp()
        self.assertFalse(sp.publish(comp, 'catalogue', sp.RUNNING))
        self.assertFalse(sp.publish(comp, sp.STEP_RESTORE, 'busy'))
        self.assertFalse(sp.finish(comp, 'catalogue'))
        self.assertTrue(sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING))

    def test_the_catalog_choke_point_publishes_the_scans_own_numbers(self):
        comp = self._comp(Status='Disabled')
        sp.publish_catalog(comp, 'Scanning defaults (12/240)', now=0.0)
        step = sp.live_snapshot(comp)['steps'][sp.STEP_CATALOG]
        self.assertEqual((step['state'], step['done'], step['total']),
                         (sp.RUNNING, 12, 240))
        self.assertNotEqual(
            step['state'], sp.SKIPPED,
            'the Disabled guard blocks the par write, not the scan -- '
            'a scan that is running must not read as skipped')

    def test_the_published_catalog_wins_over_the_parameter(self):
        comp = self._comp(Status='Enabled')
        sp.publish_catalog(comp, 'Scanning palette (2/40)', now=0.0)
        self.assertEqual(
            sp.live_snapshot(comp)['steps'][sp.STEP_CATALOG]['state'],
            sp.RUNNING)

    def test_clearing_drops_the_record(self):
        comp = self._comp()
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, total=4, now=0.0)
        sp.clear_published(comp)
        self.assertEqual(sp.published_steps(comp), {})
        self.assertNotIn(sp.STEP_RESTORE, sp.live_snapshot(comp)['steps'])


class TestStartupIsDeclaredNotInferred(EmbodyTestCase):
    """Frame one looks exactly like "finished".

    execute.py's init() sets Envoystatus and Convoystatus to Disabled and
    the catalog reads Enabled, so the very first evaluation of the panel
    sees four terminal steps -- a perfectly settled snapshot, ten frames
    before the first phase runs. Inferring from it latched the viewer
    into its settled mode for the whole session, which is why the bars
    were never seen.
    """

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def _frame_one(self):
        return TestLiveAssembly._Fake(
            Status='Enabled', Version='6.0.226',
            Envoystatus='Disabled', Convoystatus='Disabled')

    def test_frame_one_really_does_look_settled(self):
        """The premise. If this stops being true the guard below is
        guarding nothing."""
        self.assertTrue(
            sp.live_snapshot(self._frame_one())['summary']['settled'])

    def test_without_the_declaration_it_latches_immediately(self):
        comp = self._frame_one()
        sp.live_grid(comp, now=0.0)
        grid = sp.live_grid(
            TestLiveAssembly._Fake(
                Status='Scanning defaults (1/240)', Version='6.0.226',
                Envoystatus='Disabled', Convoystatus='Disabled'), now=1.0)
        self.assertFalse(any(sp.BLOCK_TRACK in t
                             for row in grid for _k, t, _c in row),
                         'the latch is what this declaration exists to '
                         'defeat -- if bars survive it, the test proves '
                         'nothing')

    def test_the_declaration_holds_the_bars_up_through_frame_one(self):
        comp = self._frame_one()
        sp.begin_startup(comp, now=0.0)
        grid = sp.live_grid(comp, now=0.0)
        self.assertTrue(any(sp.BLOCK_TRACK in t
                            for row in grid for _k, t, _c in row),
                        'startup was declared open; the bars must show')

    def test_releasing_the_hold_does_not_latch_a_scan_still_running(self):
        """The catalog scan legitimately outlives every startup callback
        -- hundreds of consecutive main-thread frames."""
        comp = self._frame_one()
        sp.begin_startup(comp, now=0.0)
        sp.publish_catalog(comp, 'Scanning defaults (12/240)', now=1.0)
        sp.end_startup(comp)
        grid = sp.live_grid(comp, now=2.0)
        self.assertTrue(any(sp.BLOCK_TRACK in t
                            for row in grid for _k, t, _c in row))

    def test_once_it_all_settles_the_panel_latches_for_good(self):
        comp = self._frame_one()
        sp.begin_startup(comp, now=0.0)
        sp.publish_catalog(comp, 'Scanning defaults (12/240)', now=1.0)
        sp.end_startup(comp)
        sp.publish_catalog(comp, 'Enabled', now=9.0)
        grid = sp.live_grid(comp, now=10.0)
        self.assertEqual(len(grid), 3, 'settled: the compact grid')
        sp.publish_catalog(comp, 'Scanning palette (1/40)', now=11.0)
        grid = sp.live_grid(comp, now=12.0)
        self.assertEqual(len(grid), 3,
                         'a later scan must not throw the panel back')

    def test_begin_drops_the_previous_opens_record(self):
        comp = self._frame_one()
        sp.publish(comp, sp.STEP_RESTORE, sp.DONE, done=9, total=9)
        sp.begin_startup(comp, now=0.0)
        self.assertEqual(sp.published_steps(comp), {})

    def test_the_startup_grid_never_outgrows_the_panel(self):
        """Five row slots, two cell slots. All five bars, one cell each."""
        comp = self._frame_one()
        sp.begin_startup(comp, now=0.0)
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=1, total=4,
                   now=0.0)
        grid = sp.live_grid(comp, now=0.0)
        self.assertEqual(len(grid), 5)
        for row in grid:
            self.assertEqual(len(row), 1)

    def test_a_derived_bar_measures_its_own_elapsed_time(self):
        """Envoy's dependency install is the longest step in a cold
        startup and the parameter driving it carries no timestamp, so the
        start has to be OBSERVED. Without that the bar shows '...' for
        the entire install -- the one row a user watches, saying nothing.
        """
        comp = TestLiveAssembly._Fake(
            Status='Enabled', Version='6.0.226', Convoystatus='Disabled',
            Envoystatus='Installing deps... (one-time)')
        sp.begin_startup(comp, now=100.0)
        sp.live_grid(comp, now=100.0)                  # first sighting
        row = [r[0][1] for r in sp.live_grid(comp, now=142.0)
               if r[0][0] == sp.STEP_ENVOY][0]
        self.assertIn('0:42', row, row)
        self.assertNotIn('...', row, 'a measurable step must be measured')

    def test_a_bar_row_fits_the_row_character_budget(self):
        """Width is font size in this viewer; a startup row wider than
        the settled budget would shrink the glyphs when the mode flips."""
        comp = self._frame_one()
        sp.begin_startup(comp, now=0.0)
        sp.publish(comp, sp.STEP_RESTORE, sp.RUNNING, done=1, total=4,
                   now=0.0)
        for row in sp.live_grid(comp, now=0.0):
            self.assertLessEqual(len(row[0][1]), sp.TARGET_ROW_CHARS)


class TestAWedgeShowsItsClock(EmbodyTestCase):
    """A busy mark reads the same at four seconds and at forty minutes.

    That is exactly how a wedged dependency install looked identical to a
    slow one -- the failure class this whole viewer was built for.
    """

    def setUp(self):
        super().setUp()
        sp.reset_session()
        # The COMPACT grid is what these assert on: during startup the
        # bars already carry an elapsed clock by design (there is no
        # denominator to draw instead), and no dwell applies. The dwell
        # exists for the SETTLED view, where the clock costs column width.
        sp.live_grid(self._comp(), now=0.0)

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.226',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return TestLiveAssembly._Fake(**base)

    def _envoy_cell(self, comp, now):
        cells = dict((k, t) for row in sp.live_grid(comp, now=now)
                     for k, t, _c in row)
        return cells[sp.STATUS_ENVOY]

    def test_a_transient_shows_no_clock(self):
        """Cell width sets column width sets font, so a clock on every
        heartbeat relays the whole panel once a second."""
        comp = self._comp(Envoystatus='Restarting after save...')
        sp.live_grid(comp, now=0.0)                  # observed here
        self.assertNotIn(':', self._envoy_cell(comp, 3.0))

    def test_a_step_that_STAYS_running_starts_measuring(self):
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=0.0)
        cell = self._envoy_cell(comp, sp.STUCK_DWELL_S + 42)
        self.assertIn('1:02', cell, cell)

    def test_the_clock_measures_from_the_first_sighting(self):
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=100.0)
        for probe in (110.0, 115.0, 119.0):
            sp.live_grid(comp, now=probe)
        self.assertIn('1:00', self._envoy_cell(comp, 160.0))

    def test_a_state_change_restarts_the_clock(self):
        """Two phases of a sequence are two measurements, not one that
        never resets."""
        sp.live_grid(self._comp(Envoystatus='Preparing Python '
                                            'environment...'), now=0.0)
        busy = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(busy, now=30.0)
        self.assertNotIn(':', self._envoy_cell(busy, 40.0))
        self.assertIn('0:40', self._envoy_cell(busy, 70.0))

    def test_recovery_drops_the_clock(self):
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=0.0)
        healthy = self._comp()
        self.assertNotIn(':', self._envoy_cell(healthy, 500.0))
        sp.live_grid(comp, now=600.0)                # a fresh install
        self.assertNotIn(':', self._envoy_cell(comp, 605.0))

    def test_only_a_RUNNING_step_measures(self):
        for state in (sp.DONE, sp.FAILED, sp.SKIPPED, sp.STALLED, sp.IDLE):
            step = sp.simple_step(state, started=0.0)
            self.assertEqual(sp.stuck_clock(step, now=9999.0), '', state)

    def test_a_clockless_caller_gets_no_clock(self):
        """Headless width probes pass now=None; they must not render a
        measurement they cannot make."""
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=0.0)
        self.assertNotIn(':', self._envoy_cell(comp, None))


class TestTheStartupPhasesActuallyPublish(EmbodyTestCase):
    """Nothing fed this module during startup, and nothing said so.

    STATIC coverage on purpose: the phases run inside a live TouchDesigner
    open sequence, so pytest cannot drive them -- but it CAN prove the
    calls exist and that every exit from the phase that owns the restore
    bar closes it. Deleting a publisher call fails a test here; whether
    the resulting panel reads correctly is owed a live check.
    """

    def _tree(self, path):
        import ast
        import io
        with io.open(path, 'r', encoding='utf-8') as handle:
            return ast.parse(handle.read())

    def _func(self, tree, name):
        import ast
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail('no such function: %s' % name)

    def _calls(self, node, attr):
        import ast
        found = []
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr == attr:
                found.append(sub)
        return found

    def _first_string_arg(self, call):
        import ast
        for arg in call.args:
            if isinstance(arg, ast.Str if hasattr(ast, 'Str') else ast.Constant):
                value = getattr(arg, 's', getattr(arg, 'value', None))
                if isinstance(value, str):
                    return value
        return None

    # -- the vocabulary cannot drift ------------------------------------

    def test_every_published_key_and_state_is_one_this_module_defines(self):
        import ast
        tree = self._tree(_EMBODY_EXT)
        checked = 0
        for attr, valid in (('_publishStartupStep', (sp.STEPS, sp.STATES)),
                            ('_finishStartupStep', (sp.STEPS, None))):
            for call in self._calls(tree, attr):
                literals = [a.value for a in call.args
                            if isinstance(a, ast.Constant)
                            and isinstance(a.value, str)]
                if not literals:
                    continue
                self.assertIn(literals[0], valid[0],
                              '%s publishes unknown step %r' % (attr,
                                                                literals[0]))
                if valid[1] is not None and len(literals) > 1:
                    self.assertIn(literals[1], valid[1],
                                  '%s publishes unknown state %r'
                                  % (attr, literals[1]))
                checked += 1
        self.assertGreaterEqual(checked, 6,
                                'found almost no publish calls (%d), so the '
                                'assertions above prove nothing' % checked)

    def test_the_closed_phase_list_names_real_steps(self):
        import io
        import re
        with io.open(_EMBODY_EXT, 'r', encoding='utf-8') as handle:
            match = re.search(r'_STARTUP_PHASE_STEPS = \(([^)]*)\)',
                              handle.read())
        self.assertIsNotNone(match, 'the closed-phase list is gone')
        names = re.findall(r"'(\w+)'", match.group(1))
        self.assertTrue(names)
        for name in names:
            self.assertIn(name, sp.STEPS)

    # -- the three phases -----------------------------------------------

    def test_the_config_pass_reports_a_terminal_state(self):
        import ast
        fn = self._func(self._tree(_EMBODY_EXT), '_upgradeEnvoy')
        states = set()
        for call in self._calls(fn, '_publishStartupStep'):
            literals = [a.value for a in call.args
                        if isinstance(a, ast.Constant)
                        and isinstance(a.value, str)]
            if len(literals) > 1 and literals[0] == sp.STEP_REPO:
                states.add(literals[1])
        self.assertIn(sp.DONE, states, '_upgradeEnvoy never reports success')
        self.assertIn(sp.FAILED, states,
                      '_upgradeEnvoy never reports failure, so a broken '
                      'config pass animates forever')

    def test_the_tox_restore_publishes_a_total_and_per_item_progress(self):
        import ast
        fn = self._func(self._tree(_EMBODY_EXT), 'RestoreTOXComps')
        calls = [c for c in self._calls(fn, '_publishStartupStep')]
        self.assertGreaterEqual(len(calls), 2,
                                'the TOX restore feeds no progress at all')
        kwargs = [set(k.arg for k in c.keywords) for c in calls]
        self.assertTrue(any('total' in k for k in kwargs),
                        'no denominator published -- the bar cannot fill')
        self.assertTrue(any('done' in k for k in kwargs),
                        'no per-item progress published')
        for call in calls:
            names = set(k.arg for k in call.keywords)
            self.assertIn('add', names,
                          'the restore bar is SHARED with the TDN phase; '
                          'a non-accumulating publish erases its counts')
        # and it must not close the shared bar -- the TDN phase owns that
        self.assertEqual(self._calls(fn, '_finishStartupStep'), [])

    def test_EVERY_exit_from_the_tdn_phase_closes_the_restore_bar(self):
        """The contract that keeps a zero-work startup from animating.

        Four early returns (mode off, create-on-start off, export mode, no
        rows) plus the fall-through: five exits, five closes. A new return
        added without a close fails here rather than in a session nobody
        can reproduce.
        """
        import ast
        fn = self._func(self._tree(_EMBODY_EXT), 'ReconstructTDNComps')
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        closes = self._calls(fn, '_finishStartupStep')
        self.assertGreaterEqual(len(returns), 4, 'lost the early returns')
        self.assertEqual(
            len(closes), len(returns) + 1,
            'ReconstructTDNComps has %d returns plus a fall-through but %d '
            'closes -- an uncovered exit leaves the restore bar running for '
            'the rest of the session' % (len(returns), len(closes)))
        for call in closes:
            self.assertEqual(self._first_string_arg(call), sp.STEP_RESTORE)

    # -- the catalog choke point ----------------------------------------

    def test_the_scan_choke_point_publishes(self):
        tree = self._tree(_CATALOG_EXT)
        setter = self._func(tree, '_setScanStatus')
        self.assertTrue(self._calls(setter, '_publishScanProgress'),
                        '_setScanStatus is the ONE choke point every scan '
                        'line goes through; if it does not publish, the '
                        'catalog bar has no source')
        publisher = self._func(tree, '_publishScanProgress')
        self.assertTrue(self._calls(publisher, 'publish_catalog'))

    # -- the brackets ---------------------------------------------------

    def test_the_open_sequence_declares_and_closes_itself(self):
        import io
        with io.open(_EXECUTE, 'r', encoding='utf-8') as handle:
            source = handle.read()
        for needed in ('_beginStartupProgress', '_closeStartupPhases'):
            self.assertIn(needed, source,
                          'execute.py no longer %s -- without both brackets '
                          'the viewer either never shows the bars or never '
                          'stops showing them' % needed)
        self.assertEqual(source.count('_beginStartupProgress'), 2,
                         'both onStart and onCreate must declare it')
        self.assertEqual(source.count('_closeStartupPhases'), 2,
                         'both onStart and onCreate must close it')


class TestAProblemSeenWithoutAClock(EmbodyTestCase):
    """The dwell must survive an untimed first sighting.

    problem_since pinned to None on the first call that had no clock, and
    every later call read `since is None` and returned False -- so that
    failure was frozen out of the explanatory view for the rest of the
    session, which is the one view that says WHY.
    """

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def _steps(self):
        return [(sp.STATUS_CONVOY, sp.convoy_step('Not installed'))]

    def test_an_untimed_first_sighting_still_expands_later(self):
        steps = self._steps()
        self.assertFalse(sp.persistent_problem(steps),
                         'no clock: cannot have dwelled yet')
        self.assertFalse(sp.persistent_problem(steps, now=0.0),
                         'the first TIMED sighting starts the dwell')
        self.assertTrue(
            sp.persistent_problem(steps, now=sp.PROBLEM_DWELL_S),
            'the dwell was pinned to None and never elapsed')

    def test_the_panel_itself_recovers_from_it(self):
        comp = TestLiveAssembly._Fake(
            Status='Enabled', Autosavestatus='Saved 14:53:05 UTC',
            Envoystatus='Running on port 9870',
            Convoystatus='Not installed', Version='6.0.226',
            Updatestatus='Up to date', Autoupdate='notify')
        sp.live_grid(comp)                       # untimed probe first
        sp.live_grid(comp, now=0.0)
        grid = sp.live_grid(comp, now=sp.PROBLEM_DWELL_S + 1)
        self.assertEqual(len(grid), 5, 'the reason never became visible')

    def test_a_timed_first_sighting_is_unchanged(self):
        steps = self._steps()
        self.assertFalse(sp.persistent_problem(steps, now=0.0))
        self.assertFalse(sp.persistent_problem(steps, now=1.0))
        self.assertTrue(sp.persistent_problem(steps,
                                              now=sp.PROBLEM_DWELL_S))


class TestPublishedTableRows(EmbodyTestCase):
    """The rows the panel actually draws from.

    The readout used to be evaluated by every cell parameter -- 90 module
    calls per cook, all time-dependent, so a visible panel recomputed the
    whole thing 60 times a second (6.7 ms per cook). It is computed once
    per event now and published as these rows, so this is the shape the
    panel depends on.
    """

    def _comp(self, **kw):
        return TestLiveAssembly._Fake(**kw)

    def test_rows_carry_every_cell_the_panel_can_draw(self):
        rows = sp.table_rows(self._comp(), 600, now=100.0)
        names = [r[0] for r in rows]
        for layout in ('font', 'rowh', 'col0w', 'pad'):
            self.assertIn(layout, names, 'the panel reads %r' % layout)
        for r in range(sp.PANEL_ROWS):
            self.assertIn('row%d' % r, names)
            for c in range(sp.PANEL_COLS):
                self.assertIn('r%dc%d' % (r, c), names,
                              'every cell must have a row, shown or not')

    def test_every_row_matches_the_header_width(self):
        """The publisher writes by index, so a short row would write a
        value into the wrong column rather than fail."""
        rows = sp.table_rows(self._comp(), 600, now=100.0)
        for row in rows:
            self.assertEqual(len(row), len(sp.TABLE_HEADER),
                             'row %r does not match the header' % (row,))

    def test_absent_cells_are_hidden_not_blank_looking(self):
        """A cell with no content must be hidden outright -- an empty but
        DISPLAYED cell still takes its column width and pushes the layout."""
        rows = dict((r[0], r) for r in sp.table_rows(self._comp(), 600,
                                                      now=100.0))
        last = rows['r%dc1' % (sp.PANEL_ROWS - 1)]
        self.assertEqual(last[5], '0', 'an unused cell must not be shown')

    def test_the_published_text_is_what_the_grid_says(self):
        """The rows must not re-derive the readout -- one source only."""
        comp = self._comp()
        grid = sp.live_grid(comp, now=100.0)
        rows = dict((r[0], r) for r in sp.table_rows(comp, 600, now=100.0))
        for r, row in enumerate(grid[:sp.PANEL_ROWS]):
            for c, cell in enumerate(row[:sp.PANEL_COLS]):
                self.assertEqual(rows['r%dc%d' % (r, c)][1], cell[1])

    def test_layout_matches_the_helpers_it_replaced(self):
        """table_rows derives font/columns from ONE grid instead of
        rebuilding it four times; the arithmetic must not drift from the
        live_* helpers the panel used before."""
        comp = self._comp()
        pad = int(600 * 0.03)
        rows = dict((r[0], r) for r in sp.table_rows(comp, 600, now=100.0))
        self.assertAlmostEqual(float(rows['font'][1]),
                               sp.live_font(comp, 600 - pad * 2, now=100.0),
                               places=6)
        self.assertEqual(int(rows['rowh'][1]),
                         int(sp.row_height_for(
                             sp.live_font(comp, 600 - pad * 2, now=100.0))))

    def test_the_tick_is_armed_only_when_something_will_actually_move(self):
        """The publisher must go silent when the readout is settled.

        Asking "is a step RUNNING?" was measurably the wrong question: a
        healthy session reports Envoy `Connected` and Convoy `Off` as
        RUNNING, so that predicate stays true for the whole session and
        would keep a timer alive forever to redraw nothing.
        """
        settled = self._comp(Status='Enabled', Envoystatus='Connected',
                             Convoystatus='Off', Autosavestatus='',
                             Version='6.0.229', Updatestatus='Disabled')
        self.assertFalse(sp.will_change(settled, now=100.0, ahead=1.0),
                         'a settled readout must not keep a tick alive')

    def test_a_live_age_does_arm_the_tick(self):
        """The one thing that moves with no event behind it: a row showing
        an elapsed time. It must keep the tick alive while it ticks over."""
        ticking = self._comp(Status='Enabled', Envoystatus='Connected',
                             Convoystatus='Off',
                             Autosavestatus='Saved 00:00:05 UTC')
        moved = any(sp.will_change(ticking, now=float(t), ahead=2.0)
                    for t in range(0, 120, 5))
        self.assertTrue(moved,
                        'an elapsed-time row must arm the tick at some point')
