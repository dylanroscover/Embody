"""
Test suite: the status readout's signals (Embody/startup_progress.py).

Pure module, no TouchDesigner import, so this runs under plain pytest on
the whole CI matrix as well as inside TD -- which is the point. This
readout is the one surface a user watches while everything else is
blocked, and the failures it exists to expose (a wedged catalog scan, a
dependency install that never finishes, a Convoy host that needs repair)
are precisely the ones nobody can reproduce on demand. So the DECISION --
which state each subsystem's status string maps to, and what the row
therefore says -- is tested here rather than by squinting at a running
session.

The assertions worth reading are the ones about lying: a state that means
FINISHED or NEVER STARTED must not read as work in flight, a stopped
Convoy is not progress, a skipped step is not a failure, and an age is
measured against the calendar rather than folded into a 24-hour clock so
a seven-week-old project cannot report "1h ago".
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


# Shared test harness -- module level because the whole suite uses it.
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

    `path` is fixed because the module keys its observed clocks
    (_SINCE) per COMP; a per-instance id would give every
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
            setattr(self.par, k, _Par(v))


class TestEnvoyStep(EmbodyTestCase):
    """Derived from Envoystatus -- about twenty writers, one parameter."""

    def test_the_one_time_dependency_install_is_running_and_can_wedge(self):
        """'Installing deps... (one-time)' is one opaque uv call -- no
        honest denominator. The spinner says working; past the dwell
        is_stuck turns it the failure colour, which is the only honest
        wedge signal a mark can give."""
        got = sp.envoy_step('Installing deps... (one-time)', started=100.0)
        self.assertEqual(got['state'], sp.RUNNING)
        self.assertFalse(sp.is_stuck(got, now=110.0), 'slow is not stuck')
        self.assertTrue(sp.is_stuck(got, now=100.0 + sp.STUCK_DWELL_S))

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
        """These are exactly the states that were invisible all week.

        'Consent required' is deliberately NOT here: STALLED's own
        definition names answering a consent dialog as its example --
        it waits on a person, which is neither progress nor a defect.
        """
        for text in ('Not installed',
                     'Needs repair -- Python not found (reinstall)',
                     'Install failed -- see log'):
            self.assertEqual(sp.convoy_step(text)['state'], sp.FAILED, text)

    def test_waiting_on_the_user_is_STALLED_not_running(self):
        """Blocked is not progress, and must not animate like it."""
        self.assertEqual(sp.convoy_step('Waiting for project save')['state'],
                         sp.STALLED)

    def test_disabled_is_skipped(self):
        self.assertEqual(sp.convoy_step('Disabled')['state'], sp.SKIPPED)


class TestTheSourceStaysASCII(EmbodyTestCase):
    """rules/ascii-punctuation.md, asserted on the bytes.

    This module is imported by a TouchDesigner DAT whose text round-trips
    through the .toe on every save, and a non-ASCII byte read back through
    a legacy codepage is mojibake. The tick, cross and ellipsis glyphs are
    the only non-ASCII characters the module wants, and they are written
    as backslash-u escapes for exactly this reason.
    """

    def test_every_glyph_is_written_as_an_escape_not_a_literal(self):
        import io
        with io.open(_PATH, 'rb') as f:
            raw = f.read()
        self.assertEqual([b for b in bytearray(raw) if b > 127], [])
        self.assertNotEqual(raw[:3], b'\xef\xbb\xbf', 'no BOM')


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
        return _Fake(**base)

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

    def test_update_checks_off_is_skipped_not_a_failure(self):
        got = sp.version_step('6.0.225', '', 'off')
        self.assertEqual(got['state'], sp.SKIPPED)
        self.assertIn('off', got['detail'])

    def test_the_resting_disabled_stamp_claims_nothing(self):
        """execute.py stamps the bare word 'Disabled' on EVERY open (the
        release scrub's resting value, the version row's 'Idle'). To a
        notify-mode user any rendering of it as off/disabled reports a
        setting they did not choose -- the row stays claimless until
        the updater actually says something."""
        got = sp.version_step('6.0.234', 'Disabled', 'notify')
        self.assertEqual(got['state'], sp.IDLE)
        self.assertFalse(got.get('detail'))
        off = sp.version_step('6.0.234', 'Disabled', 'off')
        self.assertEqual(off['detail'], 'updates off',
                         'mode=off is the user choice and still says so')

    def test_an_updater_refusal_shows_its_REASON(self):
        got = sp.version_step(
            '6.0.234', 'Disabled -- dev checkout (update via git)',
            'notify')
        self.assertEqual(got['state'], sp.SKIPPED)
        self.assertEqual(sp.compact_status(got['detail']), 'dev checkout')

    def test_a_completed_update_is_DONE_not_a_refusal(self):
        got = sp.version_step('6.0.229', 'Updated to v6.0.230', 'notify')
        self.assertEqual(got['state'], sp.DONE)
        self.assertIn('6.0.230', got['detail'])

    def test_a_BLANK_update_status_claims_nothing(self):
        """A blank Updatestatus means no check has reported anything --
        rendering 'up to date' over it is false reassurance (a blank
        field is the v6.0.145 regression the smoke gates on)."""
        step = sp.version_step('6.0.234', '', 'notify')
        self.assertEqual(step.get('state'), sp.IDLE)
        self.assertNotIn('up to date',
                         str(step.get('detail') or '').lower())

    def test_a_stated_refusal_is_skipped_not_failed(self):
        """The dev-checkout message is a no-op with a reason, not a
        broken updater."""
        got = sp.version_step(
            '6.0.225', 'This is the Embody dev checkout -- update via git',
            'notify')
        self.assertEqual(got['state'], sp.SKIPPED)


class TestCompactStatus(EmbodyTestCase):
    """One consumer left: the save age on the Saved row.

    The status-phrase vocabulary that used to live here died with the
    wordy layout -- marks carry the states now -- so what this helper
    must still do is small and exact: turn a stamp's detail into the
    shortest honest age text, and clamp visibly when asked.
    """

    def test_a_timestamp_keeps_only_the_time(self):
        self.assertEqual(sp.compact_status('Saved 14:53:05 UTC'),
                         '14:53:05')

    def test_ages_pass_through_untouched(self):
        for text in ('2m ago', '22h ago', '51d ago', 'never'):
            self.assertEqual(sp.compact_status(text), text)

    def test_asides_and_trailing_dots_are_dropped(self):
        self.assertEqual(sp.compact_status('Bypassed (Perform Mode)'),
                         'Bypassed')
        self.assertEqual(sp.compact_status('Saving...'), 'Saving')

    def test_it_still_truncates_visibly_when_asked(self):
        got = sp.compact_status('a' * 100, max_width=20)
        self.assertEqual(len(got), 20)
        self.assertTrue(got.endswith('...'))


class TestLineSpacing(EmbodyTestCase):
    """Height follows the font, not the other way round."""

    def test_the_row_box_is_the_glyph_plus_the_leading(self):
        self.assertAlmostEqual(sp.row_height_for(20),
                               20 * (1.0 + sp.LINE_SPACING))
        self.assertAlmostEqual(sp.row_height_for(27.3),
                               27.3 * (1.0 + sp.LINE_SPACING))

    def test_the_leading_still_separates_the_lines(self):
        """Tightening leading buys glyph size, but rows that touch stop
        reading as rows: the box must stay clearly larger than
        Consolas' ~1.17 em line."""
        self.assertGreaterEqual(sp.LINE_SPACING, 0.2)
        self.assertLessEqual(
            sp.ROW_FONT_RATIO * (1.0 + sp.LINE_SPACING), 1.0,
            'a height-budget font must still FIT its row box')

    def test_spacing_is_adjustable_and_never_collapses(self):
        self.assertAlmostEqual(sp.row_height_for(20, 0.0), 20.0)
        self.assertGreaterEqual(sp.row_height_for(0), 1.0)
        self.assertGreaterEqual(sp.row_height_for(None), 1.0)


class TestRowColour(EmbodyTestCase):
    """One colour per row, so the ranking has to be right."""

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


class TestMinimalRows(EmbodyTestCase):
    """Marks carry the states; the only words are information.

    Field-specified (2026-08-11): 'Enabled', 'Connected' and the port
    are GIVENS -- rendering them spent font size on saying what the
    mark already says. What keeps words: the save age and the version.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.225',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return _Fake(**base)

    def _cells(self, comp, now=1.0):
        return dict((k, t) for row in sp.live_grid(comp, now=now)
                    for k, t, _c in row)

    def test_the_autosave_age_is_INLINE_and_always_visible(self):
        """Pins the SHAPE -- mark, label, a live age -- never the value.

        The age is a WALL-time quantity (the panel's `now` is session
        time and deliberately does not reach it; the AGE tick re-renders
        it), so asserting '2h ago' raced the machine's clock: it passed
        at the desk at one time of day and failed on every CI runner at
        another. Exact age arithmetic is pinned where the clock is
        injectable -- autosave_step/ago_text/age_seconds."""
        cells = self._cells(self._comp(), now=1.0)
        text = cells[sp.STATUS_AUTOSAVE]
        self.assertTrue(text.startswith('%s Saved ' % sp.GLYPH_OK), text)
        self.assertTrue(text.endswith(' ago'), text)

    def test_the_givens_render_no_words(self):
        joined = ' '.join(self._cells(self._comp()).values())
        for given in ('Enabled', 'Connected', 'port', '9870',
                      'Running', 'Up to date'):
            self.assertNotIn(given, joined,
                             '%r is a given -- the mark already says it'
                             % given)
        self.assertEqual(self._cells(self._comp())[sp.STATUS_ENVOY],
                         '%s Envoy' % sp.GLYPH_OK)
        self.assertEqual(self._cells(self._comp())[sp.STATUS_CONVOY],
                         '%s Convoy' % sp.GLYPH_OK)

    def test_a_failure_is_a_red_cross_immediately(self):
        grid = sp.live_grid(self._comp(Convoystatus='Error: host crashed'),
                            now=0.0)
        convoy = [c for row in grid for c in row
                  if c[0] == sp.STATUS_CONVOY][0]
        self.assertEqual(convoy[1], '%s Convoy' % sp.GLYPH_BAD)
        self.assertEqual(convoy[2], sp.STATE_RGB[sp.FAILED])

    def test_the_version_rides_the_top_row_in_grey(self):
        grid = sp.live_grid(self._comp(), now=1.0)
        key, text, rgb = grid[0][1]
        self.assertEqual(key, sp.STATUS_VERSION)
        self.assertEqual(text, 'v6.0.225')
        self.assertEqual(rgb, sp.STATE_RGB[sp.SKIPPED])

    def test_an_update_waiting_or_failed_turns_the_version_RED(self):
        for status in ('6.0.230 available', 'Update FAILED; see log'):
            grid = sp.live_grid(self._comp(Updatestatus=status), now=1.0)
            self.assertEqual(grid[0][1][2], sp.STATE_RGB[sp.FAILED],
                             status)

    def test_an_update_in_flight_spins_the_version_cell(self):
        grid = sp.live_grid(self._comp(Updatestatus='Checking...'),
                            now=1.0)
        text = grid[0][1][1]
        self.assertIn(text[0], sp.SPINNER_FRAMES)
        self.assertTrue(text.endswith('v6.0.225'))


class TestPerCellColour(EmbodyTestCase):
    """A row is a layout accident, not a thing."""

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.225',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return _Fake(**base)

    def test_a_disabled_envoy_greys_only_its_OWN_cell(self):
        """Envoy and Convoy share the bottom row; their colours must
        stay independent -- a disabled Envoy recolouring Convoy would
        report a state Convoy is not in."""
        colours = dict((k, c)
                       for row in sp.live_grid(
                           self._comp(Envoystatus='Disabled'), now=1.0)
                       for k, _t, c in row)
        self.assertEqual(colours[sp.STATUS_ENVOY], sp.STATE_RGB[sp.SKIPPED])
        self.assertEqual(colours[sp.STATUS_CONVOY], sp.STATE_RGB[sp.DONE])
        self.assertEqual(colours[sp.STATUS_EMBODY], sp.STATE_RGB[sp.DONE])

    def test_rows_flow_no_padding_air(self):
        """Cells are not padded to a shared column -- the second cell
        starts where the first ends (per-row c0w), so no row rents
        width from a wider neighbour."""
        for row in sp.live_grid(self._comp(), now=1.0):
            for _k, text, _c in row:
                self.assertEqual(text, text.rstrip(),
                                 'padding air spends font size')

    def test_a_project_with_no_parameters_still_lays_out(self):
        """Every step reads IDLE -- the shape is the same anyway."""
        grid = sp.live_grid(_Fake())
        self.assertEqual([len(r) for r in grid], [2, 1, 2])


class TestOneLayoutAlways(EmbodyTestCase):
    """ONE layout. Healthy, broken, installing, bare -- same shape.

    There used to be two (a compact grid while healthy, this list when
    not), and the panel switching shape read as a fault in the panel
    while the compact side hid the autosave age. The contract now is
    brutal and simple: five rows, one cell each, STATUS_ORDER, always.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.225',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return _Fake(**base)

    def _shape(self, grid):
        return [tuple(k for k, _t, _c in row) for row in grid]

    def test_every_state_renders_the_SAME_shape(self):
        reference = self._shape(sp.live_grid(self._comp(), now=1.0))
        self.assertEqual(reference, [
            (sp.STATUS_EMBODY, sp.STATUS_VERSION),
            (sp.STATUS_AUTOSAVE,),
            (sp.STATUS_ENVOY, sp.STATUS_CONVOY),
        ], 'three rows: Embody+version, Saved+age, Envoy+Convoy')
        for name, kw in (
                ('convoy broken', {'Convoystatus': 'Not installed'}),
                ('convoy transient', {'Convoystatus': 'Registering...'}),
                ('envoy installing',
                 {'Envoystatus': 'Installing deps... (one-time)'}),
                ('envoy failed', {'Envoystatus': 'Error: ports in use'}),
                ('update waiting', {'Updatestatus': '6.0.230 available'}),
                ('embody disabled', {'Status': 'Disabled'}),
                ('bare project', None),
        ):
            comp = _Fake() if kw is None else self._comp(**kw)
            self.assertEqual(self._shape(sp.live_grid(comp, now=1.0)),
                             reference, '%s changed the SHAPE' % name)

    def test_the_dual_layout_machinery_is_GONE(self):
        """Tombstones: the second layout, the wordy-cell builder and
        everything that debounced the old switching must not quietly
        return."""
        for name in ('COLUMN_LAYOUT', 'STATUS_AUTOSAVE_AGE',
                     'persistent_problem', 'PROBLEM_DWELL_S',
                     'PROBLEM_GRACE_S', '_SESSION', 'cell_text',
                     'busy_glyph', 'GLYPH_BUSY', 'stuck_clock',
                     'elapsed_text', 'live_col_chars', '_col_chars_of',
                     '_COMPACT_PREFIX', '_COMPACT_WHOLE'):
            self.assertFalse(hasattr(sp, name),
                             '%s is back -- dead machinery returned'
                             % name)

    def test_no_cell_ever_lands_past_the_panels_slots(self):
        for comp in (self._comp(Convoystatus='Not installed'),
                     _Fake(),
                     _Fake(Status='Enabled')):
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
        return _Fake(**base)

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
        self._assert_fits(_Fake(), 'no parameters at all')


class TestTheFontNeverMovesInsideTheBudget(EmbodyTestCase):
    """The font is pinned to TARGET_ROW_CHARS, so text changes cannot
    resize the panel.

    The old design kept the panel still by HIDING the reasons; with the
    reasons always on, stability comes from the font rule instead: any
    row shorter than the budget renders at the budget size, so
    'Registering...' -> 'Connected' moves nothing. Only a row LONGER
    than the budget may shrink the font -- and never below fitting.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.226',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return _Fake(**base)

    # Real producer strings, deliberately including the LONG ones: the
    # first version of this test sampled only statuses that already fit
    # the budget, and stayed green while an unbounded 'Error: ...'
    # detail moved the font by 2x on every Convoy retry cycle.
    STATUS_BARRAGE = (
        {'Convoystatus': 'Connected'},
        {'Convoystatus': 'Registering...'},
        {'Convoystatus': 'Checking...'},
        {'Convoystatus': 'Not installed'},
        {'Convoystatus': 'Error: host app did not answer /register '
                         'within 5.0s (attempt 3)'},
        {'Convoystatus': 'Error: convoy_client module missing'},
        {'Convoystatus': 'Refused: not_admitted -- this node is not in '
                         'the convoy'},
        {'Convoystatus': 'Installed -- no supervisor (use Repair Convoy '
                         'App)'},
        {'Convoystatus': 'Running 1.4.0 -- installed by a newer Embody'},
        {'Envoystatus': 'Restarting after save...'},
        {'Envoystatus': 'Installing deps... (one-time)'},
        {'Envoystatus': 'Error: Python environment not ready -- '
                        'preparing environment, retry shortly'},
        {'Autosavestatus': 'Saving...'},
        {'Autosavestatus': 'Autosave FAILED; see the log for the '
                           'export error'},
        {'Updatestatus': 'Update FAILED; backup unusable -- reinstall '
                         'from the release page'},
    )

    def test_transient_statuses_never_change_the_font(self):
        fonts = set()
        for kw in self.STATUS_BARRAGE:
            fonts.add(round(sp.live_font(self._comp(**kw), 452, now=1.0), 6))
        self.assertEqual(len(fonts), 1,
                         'the font moved across states: %r' % (fonts,))

    def test_no_state_can_render_a_row_past_the_budget(self):
        """The structural half of the pin: every ROW of every state
        fits TARGET_ROW_CHARS, so the font arithmetic CANNOT move."""
        for kw in self.STATUS_BARRAGE:
            grid = sp.live_grid(self._comp(**kw), now=1.0)
            for total in sp.row_chars_of(grid):
                self.assertLessEqual(
                    total, sp.TARGET_ROW_CHARS,
                    '%r renders a %d-char row past the budget'
                    % (kw, total))

    def test_the_pin_is_the_design_budget(self):
        self.assertAlmostEqual(
            sp.live_font(self._comp(), 452, now=1.0),
            sp.font_for_rows(452, ['x' * sp.TARGET_ROW_CHARS]), places=6)

    def test_a_transient_spins_its_mark(self):
        """'Registering...' is activity: the mark spins, and that IS
        the rendering -- no words."""
        cells = dict((k, t) for row in sp.live_grid(
            self._comp(Convoystatus='Registering...'), now=1.0)
            for k, t, _c in row)
        self.assertIn(cells[sp.STATUS_CONVOY][0], sp.SPINNER_FRAMES)
        self.assertEqual(cells[sp.STATUS_CONVOY][1:], ' Convoy')

    def test_a_real_failure_is_a_red_mark_immediately(self):
        convoy = [c for row in sp.live_grid(
            self._comp(Convoystatus='Error: host crashed'), now=0.0)
            for c in row if c[0] == sp.STATUS_CONVOY][0]
        self.assertEqual(convoy[1][0], sp.GLYPH_BAD)
        self.assertEqual(convoy[2], sp.STATE_RGB[sp.FAILED])


class TestTheFontMatchesWhatIsDrawn(EmbodyTestCase):
    """There must be exactly ONE rule that sets the font size.

    There were two, and they drifted: one latched "startup is over" while
    the other asked "is anything installing?", which is true for any
    RUNNING step. A Convoy heartbeat then left one view on screen while
    the font was sized for the other -- measured on the live panel as 39.1
    -> 24.9 -> 39.1 on a ~30s cycle, with the column widths never moving,
    which is why it read as the layout freaking out rather than as a font
    change. Both views are gone now; the single rule is what must not come
    back apart.
    """

    def _comp(self, **kw):
        base = {'Status': 'Enabled', 'Autosavestatus': 'Saved 14:53:05 UTC',
                'Envoystatus': 'Running on port 9870',
                'Convoystatus': 'Connected', 'Version': '6.0.226',
                'Updatestatus': 'Up to date', 'Autoupdate': 'notify'}
        base.update(kw)
        return _Fake(**base)

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def test_the_font_ALWAYS_matches_the_grid_it_draws(self):
        """There is no second sizing rule to drift from.

        The font is derived from row_chars_of() -- the same numbers
        that set the cell widths -- floored at TARGET_ROW_CHARS so
        narrow content cannot inflate it. No state, transient or
        otherwise, can make the size disagree with the content.
        """
        for kw in ({}, {'Envoystatus': 'Installing deps... (one-time)'},
                   {'Convoystatus': 'Registering...'},
                   {'Convoystatus': 'Not installed'}):
            comp = self._comp(**kw)
            total = max(sp.row_chars_of(sp.live_grid(comp, now=1.0)))
            self.assertAlmostEqual(
                sp.live_font(comp, 452, now=1.0),
                sp.font_for_rows(
                    452, ['x' * max(sp.TARGET_ROW_CHARS, total)]),
                places=6)


class TestAnObservedClockMeasuresTheRightThing(EmbodyTestCase):
    """A row driven by a parameter has no timestamp of its own.

    Envoy's dependency install is the longest step in a cold startup,
    and `Envoystatus` carries no clock -- so the start has to be
    OBSERVED, or the wedge colour could never know when the step began.
    """

    def setUp(self):
        super().setUp()
        sp.reset_session()

    def tearDown(self):
        sp.reset_session()
        super().tearDown()

    def test_a_running_row_measures_its_own_elapsed_time(self):
        comp = _Fake(
            Status='Enabled', Version='6.0.226', Convoystatus='Disabled',
            Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=100.0)                  # first sighting
        rgb = [c[2] for row in sp.live_grid(comp, now=142.0)
               for c in row if c[0] == sp.STATUS_ENVOY][0]
        self.assertEqual(rgb, sp.STATE_RGB[sp.FAILED],
                         '42s past a 20s dwell must read as TOO LONG')

    def test_the_rows_stay_inside_the_row_character_budget(self):
        comp = _Fake(
            Status='Enabled', Version='6.0.226',
            Envoystatus='Installing deps... (one-time)',
            Convoystatus='Installed -- starting...')
        for total in sp.row_chars_of(sp.live_grid(comp, now=0.0)):
            self.assertLessEqual(total, sp.TARGET_ROW_CHARS)


class TestAWedgeTurnsTheMarkRed(EmbodyTestCase):
    """A spinner reads the same at four seconds and at forty minutes.

    That is exactly how a wedged dependency install looked identical to
    a slow one -- the failure class this viewer was built for. The
    minimal layout spends no characters on it: past STUCK_DWELL_S the
    mark keeps spinning (the work may yet finish) but its colour turns
    the failure red -- "working TOO LONG" in zero characters.
    """

    def setUp(self):
        super().setUp()
        sp.reset_session()
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
        return _Fake(**base)

    def _envoy_rgb(self, comp, now):
        return [c[2] for row in sp.live_grid(comp, now=now)
                for c in row if c[0] == sp.STATUS_ENVOY][0]

    def test_a_transient_is_not_red(self):
        comp = self._comp(Envoystatus='Restarting after save...')
        sp.live_grid(comp, now=0.0)                  # observed here
        self.assertEqual(self._envoy_rgb(comp, 3.0),
                         sp.STATE_RGB[sp.RUNNING])

    def test_a_step_that_STAYS_running_turns_red(self):
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=0.0)
        self.assertEqual(self._envoy_rgb(comp, sp.STUCK_DWELL_S + 42),
                         sp.STATE_RGB[sp.FAILED])

    def test_the_dwell_measures_from_the_first_sighting(self):
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=100.0)
        self.assertEqual(self._envoy_rgb(comp, 110.0),
                         sp.STATE_RGB[sp.RUNNING], 'not yet')
        self.assertEqual(self._envoy_rgb(comp, 100.0 + sp.STUCK_DWELL_S),
                         sp.STATE_RGB[sp.FAILED], 'now')

    def test_a_state_change_restarts_the_dwell(self):
        """Two phases of a sequence are two measurements, not one that
        never resets."""
        sp.live_grid(self._comp(Envoystatus='Preparing Python '
                                            'environment...'), now=0.0)
        busy = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(busy, now=30.0)
        self.assertEqual(self._envoy_rgb(busy, 40.0),
                         sp.STATE_RGB[sp.RUNNING],
                         'phase two started at t=30; its own dwell '
                         'has not elapsed')
        self.assertEqual(self._envoy_rgb(busy, 30.0 + sp.STUCK_DWELL_S),
                         sp.STATE_RGB[sp.FAILED])

    def test_recovery_drops_the_red(self):
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=0.0)
        healthy = self._comp()
        self.assertEqual(self._envoy_rgb(healthy, 500.0),
                         sp.STATE_RGB[sp.DONE])
        sp.live_grid(comp, now=600.0)                # a fresh install
        self.assertEqual(self._envoy_rgb(comp, 605.0),
                         sp.STATE_RGB[sp.RUNNING],
                         'a fresh install starts a fresh dwell')

    def test_a_clockless_caller_never_reddens(self):
        """Headless width probes pass now=None; they must not render a
        measurement they cannot make."""
        comp = self._comp(Envoystatus='Installing deps... (one-time)')
        sp.live_grid(comp, now=0.0)
        self.assertEqual(self._envoy_rgb(comp, None),
                         sp.STATE_RGB[sp.RUNNING])


class TestTheReadoutIsFedAndRedrawn(EmbodyTestCase):
    """The readout derives from parameters -- but it still has to REDRAW.

    STATIC coverage on purpose: these paths run inside a live TouchDesigner
    open sequence, so pytest cannot drive them -- but it CAN prove the
    calls exist. Deleting one fails a test here; whether the resulting
    panel reads correctly is owed a live check.

    THIS CLASS IS THE MUTATION GUARD FOR A REMOVAL. The startup phases used
    to publish their counts into the module for a column of progress bars.
    The bars went, the record went, and the seven wrappers that fed it went
    -- but a half-landed removal is the dangerous state, and by the time
    the bars died one wrapper was already calling a function that no longer
    existed, silently, inside its own `except Exception: pass`. So these
    tests assert what must be ABSENT as firmly as what must be present.
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

    def _source(self, path):
        import io
        with io.open(path, 'r', encoding='utf-8') as handle:
            return handle.read()

    # -- the removal must be WHOLE --------------------------------------

    GONE = ('_publishStartupStep', '_finishStartupStep', '_startupProgress',
            '_STARTUP_PHASE_STEPS', '_beginStartupProgress',
            '_completeStartupConfigStep', '_closeStartupPhases')

    def test_no_wrapper_of_the_deleted_record_survives(self):
        """A wrapper left behind calls into a module that no longer has
        the function -- swallowed by its own except, so nothing fails and
        the phase it was closing silently stops closing."""
        source = self._source(_EMBODY_EXT)
        for name in self.GONE:
            self.assertNotIn(name, source,
                             'EmbodyExt still carries %r after the publish '
                             'machinery was removed' % name)

    def test_no_call_site_of_the_deleted_record_survives(self):
        for path, label in ((_EXECUTE, 'execute.py'),
                            (_CATALOG_EXT, 'CatalogManagerExt.py')):
            source = self._source(path)
            for name in self.GONE + ('publish_catalog', 'begin_startup',
                                     'end_startup', 'close_unreported'):
                self.assertNotIn(name, source,
                                 '%s still calls %r' % (label, name))

    def test_the_module_itself_kept_nothing_warm(self):
        """A write-only record is indistinguishable from a working one
        until somebody trusts it, so it is deleted rather than kept."""
        for name in ('publish', 'publish_catalog', 'begin_startup',
                     'end_startup', 'close_unreported', 'clear_published',
                     'catalog_step', 'fraction', 'bar_text', 'row_text',
                     'rows', 'value_text', 'STEPS', 'STATES', 'LABELS'):
            self.assertFalse(hasattr(sp, name),
                             'startup_progress still exports %r, which '
                             'nothing reads' % name)

    # -- what still has to happen ---------------------------------------

    def test_the_open_sequence_seeds_the_autosave_row(self):
        """Both open paths, because a dropped .tox is an open too.

        Without it the auto-save row rests at the value the release scrub
        ships and answers nothing -- on a project that is not being edited,
        for the whole session.
        """
        source = self._source(_EXECUTE)
        self.assertEqual(source.count('SeedAutosaveStatus'), 2,
                         'both onStart and onCreate must seed the row')

    def test_the_seed_exists_and_redraws_what_it_wrote(self):
        """The parameter-execute DAT that normally redraws the readout is
        not watching yet at frame 0, so this write has to announce itself
        or the seeded row is not drawn until the next unrelated event."""
        tree = self._tree(_EMBODY_EXT)
        seed = self._func(tree, 'SeedAutosaveStatus')
        self.assertTrue(self._calls(seed, '_setAutosaveStatus'),
                        'the seed writes nothing')
        self.assertTrue(self._calls(seed, '_republishStatusPanel'),
                        'the seed writes a parameter nothing is watching '
                        'yet -- without the redraw the row it wrote is '
                        'never drawn')

    def test_the_scan_choke_point_redraws(self):
        """_setScanStatus is the ONE line every scan status goes through,
        and on a Disabled Embody it deliberately does NOT write the
        parameter -- so without this call a scan on a disabled project has
        no readout at all."""
        tree = self._tree(_CATALOG_EXT)
        setter = self._func(tree, '_setScanStatus')
        self.assertTrue(self._calls(setter, '_publishScanProgress'),
                        '_setScanStatus no longer tells the readout')
        publisher = self._func(tree, '_publishScanProgress')
        self.assertTrue(self._calls(publisher, 'Refresh'),
                        '_publishScanProgress no longer redraws anything, '
                        'so it does nothing at all')


class TestPublishedTableRows(EmbodyTestCase):
    """The rows the panel actually draws from.

    The readout used to be evaluated by every cell parameter -- 90 module
    calls per cook, all time-dependent, so a visible panel recomputed the
    whole thing 60 times a second (6.7 ms per cook). It is computed once
    per event now and published as these rows, so this is the shape the
    panel depends on.
    """

    def _comp(self, **kw):
        return _Fake(**kw)

    def test_rows_carry_every_cell_the_panel_can_draw(self):
        rows = sp.table_rows(self._comp(), 600, now=100.0)
        names = [r[0] for r in rows]
        for layout in ('font', 'rowh', 'pad', 'c0w0', 'c0w1', 'c0w2',
                       'c0w3', 'c0w4'):
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
        # places=3, not 6: the published cell is "%.4f"-formatted, so it
        # carries at most 4 decimals -- the old comparison only survived
        # at 6 places because the compact grid's font hit the 40.0 clamp
        # exactly.
        self.assertAlmostEqual(float(rows['font'][1]),
                               sp.live_font(comp, 600 - pad * 2, now=100.0),
                               places=3)
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

    def test_a_live_age_does_arm_the_AGE_tick(self):
        """The one thing that moves with no event behind it: a rendered
        age. will_change's short horizons cannot see a flip that is
        minutes of REAL time away, so the publisher's slow re-check is
        armed off this predicate -- it must fire for any ' ago' row and
        stay silent for a readout with none."""
        ticking = self._comp(Status='Enabled', Envoystatus='Connected',
                             Convoystatus='Off',
                             Autosavestatus='Saved 00:00:05 UTC')
        self.assertTrue(sp.rows_show_a_live_age(
            sp.table_rows(ticking, 600, now=10.0)))
        still = self._comp(Status='Enabled', Envoystatus='Connected',
                           Convoystatus='Off', Autosavestatus='Idle')
        self.assertFalse(sp.rows_show_a_live_age(
            sp.table_rows(still, 600, now=10.0)),
            "'never' is not a moving age; it must not keep a tick alive")


class TestTheAutoSaveRowAnswersHowLongAgo(EmbodyTestCase):
    """The auto-save row exists to say WHEN the work reached disk.

    'Idle' -- the value the release scrub leaves in the shipped .tox so a
    session timestamp cannot leak into git -- matched no branch in
    autosave_step and fell through to the RUNNING default, so a project
    that had simply not checkpointed yet drew a busy mark forever with no
    age beside it: the one thing the row is for, replaced by a spinner.
    """

    def test_idle_is_not_activity(self):
        for value in ('Idle', 'idle', '', 'none', 'never'):
            step = sp.autosave_step(value)
            self.assertEqual(step['state'], sp.IDLE,
                             '%r must not read as work in progress' % value)
            self.assertEqual(step['detail'], 'never',
                             'a virgin project still answers the row\'s '
                             'question instead of rendering a bare label')

    def test_bypassed_for_a_show_is_a_stated_noop_not_activity(self):
        """'Bypassed (Perform Mode)' fell to the RUNNING default: a busy
        mark and, past the dwell, a CLIMBING CLOCK on an auto-save that
        is switched off for the show -- fabricated activity, the same
        class as 'Idle'."""
        step = sp.autosave_step('Bypassed (Perform Mode)')
        self.assertEqual(step['state'], sp.SKIPPED)
        self.assertFalse(sp.is_stuck(
            {'state': step['state'], 'started': 0.0}, now=600.0),
            'a no-op can never redden as a wedge')

    def test_a_saved_stamp_becomes_an_age(self):
        step = sp.autosave_step('Saved 14:53:05 PDT',
                                now_seconds=15 * 3600 + 5 * 60)
        self.assertEqual(step['state'], sp.DONE)
        self.assertIn('ago', step['detail'])

    def test_a_save_before_midnight_is_not_negative(self):
        step = sp.autosave_step('Saved 23:59:00 PDT', now_seconds=60)
        self.assertEqual(step['state'], sp.DONE)
        self.assertNotIn('-', step['detail'])


class TestAStoppedConvoyIsNotProgress(EmbodyTestCase):
    """Stopping something is not progress.

    'Installed -- not running' matched no branch in convoy_step and fell
    through to RUNNING, so Stop Convoy App left the panel convinced work
    was in flight, with a climbing clock and nothing ever arriving.

    EVERY STRING BELOW IS ONE A PRODUCER ACTUALLY EMITS. The first attempt
    at this class asserted on 'Stopped', 'Not running', 'Idle' and
    'Starting' -- four inventions, none of which convoy_client can ever
    write -- while the five REAL strings that begin with the word
    "Installed" went untested and were all being swept into one answer by
    a bare prefix match. The list is convoy_client.host_status_text and
    nothing else.
    """

    # Resting: the host app exists and is not doing anything.
    RESTING = ('Installed -- not running (restarts within a minute)',
               'Installed -- stopped')
    # In flight: something is genuinely happening right now.
    WORKING = ('Checking...', 'Installing...', 'Repairing runtime...',
               'Installed -- starting...')
    # Broken with a remedy the user has to apply.
    BROKEN = ('Not installed', 'Needs repair -- Python not found (reinstall)',
              'Install failed -- see log',
              'Installed -- no supervisor (use Repair Convoy App)')

    def test_a_stopped_host_app_is_terminal(self):
        for value in self.RESTING:
            self.assertEqual(sp.convoy_step(value)['state'], sp.IDLE,
                             '%r must not read as work in progress' % value)

    def test_the_states_that_ARE_progress_still_are(self):
        for value in self.WORKING:
            self.assertEqual(sp.convoy_step(value)['state'], sp.RUNNING,
                             '%r is work in flight' % value)

    def test_a_broken_convoy_is_still_reported(self):
        for value in self.BROKEN:
            self.assertEqual(sp.convoy_step(value)['state'], sp.FAILED,
                             '%r needs the user' % value)

    def test_starting_is_not_confused_with_stopped(self):
        """THE MUTATION THIS CLASS EXISTS FOR. Both begin 'Installed --',
        and a `startswith("installed")` branch reported the mid-flight
        start as a resting state -- a host app coming up looked exactly
        like one somebody had stopped."""
        self.assertEqual(sp.convoy_step('Installed -- starting...')['state'],
                         sp.RUNNING)
        self.assertEqual(sp.convoy_step('Installed -- stopped')['state'],
                         sp.IDLE)

    def test_a_newer_embody_owns_it_waits_on_a_PERSON(self):
        """Reported with either lead word depending on whether the daemon
        answered. Neither is progress and neither is fine."""
        for value in ('Installed 6.1.0 -- installed by a newer Embody',
                      'Running 6.1.0 -- installed by a newer Embody'):
            self.assertEqual(sp.convoy_step(value)['state'], sp.STALLED,
                             '%r resolves only when a person acts' % value)

    def test_the_healthy_readouts_are_still_healthy(self):
        for value in ('Connected', 'Running 6.0.234 (pid 8123)'):
            self.assertEqual(sp.convoy_step(value)['state'], sp.DONE, value)


class TestAProblemNeedsNoDwellAnyMore(EmbodyTestCase):
    """A problem is visible on its FIRST render, per state, per cell.

    The old dwell existed to debounce a layout switch; with a fixed
    shape the mark simply tracks the current state -- so the 2026-08-10
    blink bug (a retry loop restarting the dwell forever, hiding the
    problem) has no mechanism left to happen in.
    """

    def _comp(self, status):
        return _Fake(
            Status='Enabled', Autosavestatus='Saved 14:53:05 UTC',
            Envoystatus='Running on port 9870', Convoystatus=status,
            Version='6.0.226', Updatestatus='Up to date',
            Autoupdate='notify')

    def _convoy(self, status, now):
        return [c for row in sp.live_grid(self._comp(status), now=now)
                for c in row if c[0] == sp.STATUS_CONVOY][0]

    def test_a_problem_needs_no_dwell_to_show(self):
        cell = self._convoy('No Convoy host app', 0.0)
        self.assertEqual(cell[1][0], sp.GLYPH_WAIT)
        self.assertEqual(cell[2], sp.STATE_RGB[sp.STALLED])

    def test_a_retry_blink_cannot_hide_the_problem(self):
        """The exact 2026-08-10 sequence: the mark tracks the CURRENT
        state render by render."""
        for now, status, expect_wait in ((0.0, 'No Convoy host app', True),
                                         (0.5, 'Registering...', False),
                                         (1.0, 'No Convoy host app', True)):
            cell = self._convoy(status, now)
            if expect_wait:
                self.assertEqual(cell[1][0], sp.GLYPH_WAIT, status)
            else:
                self.assertIn(cell[1][0], sp.SPINNER_FRAMES, status)


class TestAnAgeIsMeasuredAgainstTheCALENDAR(EmbodyTestCase):
    """A stamp from last month must not report as a stamp from this hour.

    Two shapes reach this row and only one carries a date. The live
    auto-save writes a wall clock, because the save it reports happened
    seconds ago; the startup seed reads the externalizations table, whose
    stamps are full UTC dates and are routinely WEEKS old. Folding one of
    those into a 24-hour clock is how a seven-week-old project came to
    report '1h ago' -- fabricated recency, in the one readout whose entire
    job is truthful staleness.
    """

    def _dt(self, y, mo, d, h=0, mi=0, sec=0):
        import datetime
        return datetime.datetime(y, mo, d, h, mi, sec)

    def test_a_seven_week_old_stamp_reports_WEEKS(self):
        """The exact case from this repo's own table."""
        step = sp.autosave_step('Saved 2026-06-20 20:36:02 UTC',
                                now_dt=self._dt(2026, 8, 9, 15, 0, 0))
        self.assertEqual(step['state'], sp.DONE)
        self.assertEqual(step['detail'], '49d ago')

    def test_the_same_stamp_WITHOUT_its_date_is_the_lie(self):
        """Same clock time, date thrown away: this is what the row said
        before, and what it must never say again."""
        self.assertEqual(
            sp.autosave_step('Saved 20:36:02 UTC',
                             now_seconds=15 * 3600)['detail'],
            '18h ago')

    def test_a_dated_stamp_from_today_still_reads_in_hours(self):
        step = sp.autosave_step('Saved 2026-08-09 12:00:00 UTC',
                                now_dt=self._dt(2026, 8, 9, 15, 0, 0))
        self.assertEqual(step['detail'], '3h ago')

    def test_a_future_stamp_shows_the_WORDS_not_a_fake_zero(self):
        """Clock skew, or a UTC stamp read against a local clock. '0s ago'
        would be an invention; the raw stamp is at least true."""
        step = sp.autosave_step('Saved 2026-08-09 18:00:00 UTC',
                                now_dt=self._dt(2026, 8, 9, 15, 0, 0))
        self.assertIn('2026-08-09', step['detail'])

    def test_a_bare_clock_still_wraps_at_midnight(self):
        """The live auto-save's shape is unchanged."""
        self.assertEqual(
            sp.autosave_step('Saved 23:59:00 UTC', now_seconds=60)['detail'],
            '2m ago')

    def test_a_stamp_that_is_not_a_date_is_not_forced_into_one(self):
        self.assertIsNone(sp.dated_stamp('Saved 14:53:05 UTC'))
        self.assertIsNone(sp.dated_stamp('Saved 2026-13-40 99:99:99'))
        self.assertIsNotNone(sp.dated_stamp('2026-06-20 20:36:02 UTC'))


class TestResetSessionForgetsEVERYTHING(EmbodyTestCase):
    """A partial reset is worse than none: it is a silent one.

    The fake COMP has a FIXED path, so every suite in this file shares
    one key in the module's observed-clock record. A reset that left
    the clocks running let a dwell started in an earlier test redden a
    row in a later one that never started it -- order dependence,
    reintroduced by the very function that exists to remove it.
    """

    def _comp(self):
        return _Fake(Status='Enabled', Version='6.0.234',
                     Convoystatus='Disabled',
                     Envoystatus='Installing deps... (one-time)')

    def _envoy_rgb(self, comp, now):
        return [c[2] for row in sp.live_grid(comp, now=now)
                for c in row if c[0] == sp.STATUS_ENVOY][0]

    def test_an_observed_clock_does_not_survive_a_reset(self):
        comp = self._comp()
        sp.reset_session()
        sp.live_grid(comp, now=100.0)               # clock starts here
        self.assertEqual(self._envoy_rgb(comp, 160.0),
                         sp.STATE_RGB[sp.FAILED], 'it is wedged')
        sp.reset_session()
        self.assertEqual(self._envoy_rgb(comp, 190.0),
                         sp.STATE_RGB[sp.RUNNING],
                         'the reset left the clock running, so this row '
                         'reports a dwell nothing in this test started')

    def test_the_clock_restarts_cleanly_after_a_reset(self):
        comp = self._comp()
        sp.reset_session()
        sp.live_grid(comp, now=100.0)
        sp.reset_session()
        sp.live_grid(comp, now=1000.0)              # a fresh first sighting
        self.assertEqual(
            self._envoy_rgb(comp, 1000.0 + sp.STUCK_DWELL_S),
            sp.STATE_RGB[sp.FAILED])


class TestTheMarkSpinsWhileRunning(EmbodyTestCase):
    """The mark IS the activity signal (field spec 2026-08-11)."""

    def test_every_spinner_frame_is_one_character(self):
        self.assertTrue(all(len(f) == 1 for f in sp.SPINNER_FRAMES),
                        'a wider frame resizes the row, font and panel')

    def test_the_spinner_cycles(self):
        seen = [sp.spinner_frame(t / sp.SPINNER_FPS)
                for t in range(len(sp.SPINNER_FRAMES))]
        self.assertEqual(len(set(seen)), len(sp.SPINNER_FRAMES))

    def test_running_spins_and_settled_states_hold_still(self):
        self.assertIn(sp._mark({'state': sp.RUNNING}, now=1.0),
                      sp.SPINNER_FRAMES)
        self.assertNotEqual(sp._mark({'state': sp.RUNNING}, now=0.0),
                            sp._mark({'state': sp.RUNNING},
                                     now=1.0 / sp.SPINNER_FPS),
                            'the mark must MOVE while work is in flight')
        self.assertEqual(sp._mark({'state': sp.DONE}), sp.GLYPH_OK)
        self.assertEqual(sp._mark({'state': sp.FAILED}), sp.GLYPH_BAD)
        self.assertEqual(sp._mark({'state': sp.STALLED}), sp.GLYPH_WAIT)
        self.assertEqual(sp._mark({'state': sp.SKIPPED}), sp.GLYPH_SKIP)


class TestConvoyStepSpeaksBothVocabularies(EmbodyTestCase):
    """One readout, two producers -- and the classifier must know both.

    THE PANEL FINDING THIS PINS: convoy_step enumerated only the host-app
    strings (convoy_client.host_status_text), so the node line's
    'Error: ...' and 'Refused: ...' fell through to the busy default and
    rendered as a spinner with a climbing clock -- a crash displayed as
    progress. The node vocabulary comes from convoy_client.status_text
    plus ConvoyExt's own literals.
    """

    def test_errors_and_refusals_are_failures_not_activity(self):
        for value in ('Error: host crashed', 'Error: unreadable result',
                      'Error: convoy_client module missing',
                      'Refused: not_admitted'):
            self.assertEqual(sp.convoy_step(value)['state'], sp.FAILED,
                             '%r must be visible as a failure' % value)

    def test_the_node_lines_in_flight_states_animate(self):
        for value in ('Registering...', 'Registered -- Envoy port pending',
                      'Installed -- starting...', 'Checking...',
                      'Repairing runtime...'):
            self.assertEqual(sp.convoy_step(value)['state'], sp.RUNNING,
                             '%r is genuinely in flight' % value)

    def test_enabled_but_absent_is_a_wait_with_its_words_shown(self):
        """Convoystatus only carries these while Convoy is ENABLED (a
        disabled Convoy reads 'Disabled'), so an absent host app means an
        opted-in mesh that is not delivering: a wait with a remedy, not a
        resting state. Classified as resting it rendered a blank mark and
        no words, and the user hunted the parameter page to learn why
        (field report, 2026-08-10). STALLED, not FAILED -- the client's
        absence-is-not-an-error doctrine still holds.
        """
        for value in ('No Convoy host app', 'Host app stale'):
            step = sp.convoy_step(value)
            self.assertEqual(step['state'], sp.STALLED, value)
            self.assertEqual(step['detail'], value,
                             'the words must reach the panel')

    def test_waiting_on_a_person_is_stalled(self):
        for value in ('Waiting for project save',
                      'Consent required -- enable Convoy again',
                      'Managed by another supervisor'):
            self.assertEqual(sp.convoy_step(value)['state'], sp.STALLED)

    def test_an_unknown_status_cannot_fabricate_activity(self):
        """The vocabulary is closed; a string this classifier has never
        seen means someone added a state without updating it. Showing the
        words without animation is honest; a busy default is the failure
        mode this readout keeps growing back."""
        step = sp.convoy_step('Some Brand New State')
        self.assertEqual(step['state'], sp.IDLE)
        self.assertEqual(step['detail'], 'Some Brand New State',
                         'the unknown text must still be SHOWN')


class TestARenderedAgeKeepsMoving(EmbodyTestCase):
    """An "N ago" advances with no event behind it.

    THE PANEL FINDING THIS PINS: the publisher re-armed only when the
    rows would differ within seconds, so after the last event the seeded
    auto-save age froze at its first value forever -- '1m ago' on screen
    at the end of an eight-hour session. rows_show_a_live_age is what
    the publisher asks before letting the tick die.
    """

    def test_an_age_cell_demands_a_recheck(self):
        rows = [('r0c1', '  8h ago  ', '', '', '', '1')]
        self.assertTrue(sp.rows_show_a_live_age(rows))

    def test_an_elapsed_clock_demands_a_recheck(self):
        rows = [('r1c1', '| Envoy 0:18', '', '', '', '1')]
        self.assertTrue(sp.rows_show_a_live_age(rows))

    def test_static_rows_let_the_tick_die(self):
        rows = [('r0c0', 'Connected', '', '', '', '1'),
                ('r2c1', '- v6.0.233 updates off', '', '', '', '1')]
        self.assertFalse(sp.rows_show_a_live_age(rows),
                         'no moving cell means no timer -- zero cooks idle')

    def test_a_port_number_is_not_a_clock(self):
        """'port 9870' and a version string carry digits; neither moves."""
        rows = [('r1c1', 'port 9870', '', '', '', '1')]
        self.assertFalse(sp.rows_show_a_live_age(rows))
