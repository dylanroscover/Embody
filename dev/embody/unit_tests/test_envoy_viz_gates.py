"""
Tests for the viz activation gates in envoy_viz (issues #57 and #86).

Background (issue #57): on TD 2025.32460 a first-of-session MCP create_op
wedged TD's main thread permanently (AppHang 1002; dump showed an orphaned /
self-owned critical section inside TD's editor internals, GIL held) --
reproducible with viz ON, 7/7 clean with viz OFF. The common factor was viz
performing EDITOR work (bot template creation, annotateCOMP copyOPs,
selection writes, pane.owner navigation) in the SAME RefreshHook frame that
mutated the network, on the first activation after dormancy. Two gates now
decouple those moments:

  - settle gate (vizSettled): after ANY mutating op, ALL editor-adjacent viz
    work holds for _VIZ_MUTATION_SETTLE_FRAMES, so the MCP response is
    delivered before any viz editor write can run.
  - cold hold (coldHoldElapsed): the FIRST hop after viz dormancy pings the
    node colour only; bot/camera machinery starts once the hold elapses.

Issue #86 added two more gates plus the artifacts that keep them safe.
Embot has no identity across networks: any active op in a DIFFERENT network
tears him down and rebuilds him by copying 9 annotateCOMPs on the MAIN
thread -- measured at 150-230ms in one frame (blockSpawn) or 9 x 61-129ms
(assembleStep), 28% of all wall clock over a 6.3s mutation stream, and
21.3 fps when every hop crosses a network. The per-event cost is
editor-side rather than Python (the same copy costs ~33ms off-screen vs
~65ms into a displayed net, and does not scale with the destination's
size) and is not reducible here, so the fix attacks the RATE and the
waste:

  - relocation gate (netRelocationOK): Embot + camera relocate only after
    the work has SETTLED in a new net for _VIZ_NET_DWELL_S (or a queued
    batch proves it, or a CONTINUOUS _VIZ_NET_STARVE_S refusal streak
    forces it -- continuous because a clock left running by work that
    stopped must not buy a stray hop a free relocation), and
    never more often than _VIZ_RELOCATE_MIN_S -- the hard cost bound, since
    a spawn is reachable only through a commit. The gate is a PREDICATE:
    only commitRelocation stamps _viz_home, and trackActive calls it only
    after the spawn actually landed, so a refused spawn can never leave
    viz "committed" to a network Embot is not in.
  - write-retire suppression (vizRetireForWrite / writeSuppressed): his own
    parts are what mark a COMP dirty, so a .tox write that retires him must
    also bar re-entry briefly -- otherwise respawn re-dirties the COMP and
    the next Update() saves, retires and respawns again.
  - visibility spawn gate (botWouldBeSeen / spawnWouldBeSeen): never build
    9 annotates into a net nobody is looking at or about to look at.
  - botWritesNeeded: the figure's state advances while invisible, the
    editor writes do not.
  - assembleStep's template-position restore + _VIZ_TEMPLATE_PARK: the
    staging trick was leaking a coordinate into the SHIPPED template, and
    from there into Embody.tdn (all nine parts were committed at
    [1860, 251]).
  - purgeVizArtifacts / vizRetireForWrite / TDNExt's annotation filter --
    on BOTH export walks, the sync one and the async one behind
    ExportNetworkAsync: longer residency in one network must not widen any
    path by which a bot part could bake into a saved .toe, .tox or .tdn.

These tests drive the live envoy_viz module functions with a stub ext
(plain SimpleNamespace mirroring EnvoyExt's _viz_* state) plus real
sandbox operators where an actual op is needed. Every timing test runs on
an INJECTED float clock (`now` is already a parameter), never a real-clock
deadline, so it is deterministic and instant on any CI runner. No panes are
navigated, no bot is spawned in a live network, nothing outside the sandbox
is touched. NOT destructive.
"""

from types import SimpleNamespace

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

viz = op.Embody.op('envoy_viz').module


class _FakePar:
    def __init__(self, v):
        self._v = v

    def eval(self):
        return self._v


def _stub_ext(embot=False, follow=True):
    """A SimpleNamespace carrying every _viz_* attr the exercised paths touch,
    mirroring EnvoyExt.__init__ defaults, plus a minimal ownerComp stub."""
    owner = SimpleNamespace(
        ext=SimpleNamespace(Embody=SimpleNamespace(_performMode=False)),
        fetch=lambda *a, **k: False,
        par=SimpleNamespace(Embotenable=_FakePar(embot),
                            Envoyfollow=_FakePar(follow)),
    )
    return SimpleNamespace(
        ownerComp=owner,
        _log=lambda *a, **k: None,
        _crashTrace=lambda *a, **k: None,
        _viz_target_op=None,
        _viz_target_queue=[],
        _viz_hop_until=0.0,
        _viz_last_view=None,
        _viz_takeover_until=0.0,
        _viz_settle_until=0.0,
        _viz_zoom_pending=False,
        _viz_follow_net=None,
        _viz_selected_op=None,
        _viz_last_activity=0.0,
        _viz_action_text='',
        _viz_speech_src='',
        _viz_speech_t0=0.0,
        _viz_last_skin=None,
        _viz_last_paint=0.0,
        _viz_pulse_op=None,
        _viz_pulse_orig=None,
        _viz_pulse_start=0.0,
        _viz_bot_net=None,
        _viz_bot_pos=None,
        _viz_bot_from=None,
        _viz_bot_target=None,
        _viz_bot_jump_t0=0.0,
        _viz_jump_dur=0.52,
        _viz_bot_pending_entrance=False,
        _viz_bot_dest=None,
        _viz_bot_stage=None,
        _viz_bot_build_queue=[],
        _viz_assemble_next_frame=0,
        _viz_bot_pending_cleanup=set(),
        _viz_mutation_frame=-10 ** 6,
        _viz_session_warm=False,
        _viz_cold_since=-1,
        # Issue #86 relocation gate state (EnvoyExt.__init__ defaults).
        _viz_home=None,
        _viz_net_candidate=None,
        _viz_relocate_blocked_since=None,
        _viz_relocate_blocked_last=None,
        _viz_write_suppress={},
    )


def _landAt(ext, netpath, now):
    """Simulate what trackActive does when a spawn ACTUALLY lands: ensureBot
    claims the net, then commitRelocation stamps home. Tests must never stamp
    _viz_home directly -- the whole point of the fix is that the two move
    together."""
    ext._viz_bot_net = netpath
    viz.commitRelocation(ext, netpath, now)


class _WriteRecorder:
    """Stands in for an OP where the test needs to count editor WRITES rather
    than trust TouchDesigner to round-trip a value. `selected` / `current` are
    real properties, so a write is observable without a live node (and without
    stealing the current op from whatever network the sandbox lives in)."""

    def __init__(self, path, selected=False, current=False):
        self.path = path
        self.valid = True
        self.writes = []
        self._selected = selected
        self._current = current

    @property
    def selected(self):
        return self._selected

    @selected.setter
    def selected(self, v):
        self.writes.append(('selected', v))
        self._selected = v

    @property
    def current(self):
        return self._current

    @current.setter
    def current(self, v):
        self.writes.append(('current', v))
        self._current = v


def _annotate(parent, name):
    """Create an annotateCOMP that actually CARRIES `name`.

    Passing a name to `COMP.create(annotateCOMP, ...)` is silently ignored and
    the op is auto-named 'annotate1' -- unlike every other operator type. envoy_viz's
    ensureTemplate has always done create-then-rename for exactly this reason
    (see its `p = tmpl.create(annotateCOMP)` / `p.name = ...`). A fixture that
    skips the rename builds parts named annotate1/annotate2, which no
    `envoy_bot_` prefix filter can ever match -- and the failure then reads as
    "the filter is broken" when the filter is fine and the fixture is not.
    """
    a = parent.create(annotateCOMP)
    a.name = name
    return a


class TestEnvoyVizGates(EmbodyTestCase):

    # ----- pure predicates ------------------------------------------------

    def test_settle_predicate_boundaries(self):
        s = viz._VIZ_MUTATION_SETTLE_FRAMES
        self.assertTrue(viz.vizSettled(-10 ** 6, 0),
                        'no mutation ever -> settled')
        self.assertFalse(viz.vizSettled(100, 100),
                         'mutation this frame -> NOT settled')
        self.assertFalse(viz.vizSettled(100, 100 + s - 1),
                         'one frame short of the window -> NOT settled')
        self.assertTrue(viz.vizSettled(100, 100 + s),
                        'window elapsed -> settled')

    def test_cold_hold_predicate_boundaries(self):
        h = viz._VIZ_COLD_HOLD_FRAMES
        self.assertFalse(viz.coldHoldElapsed(-1, 10 ** 9),
                         'hold never started (-1) -> not elapsed')
        self.assertFalse(viz.coldHoldElapsed(50, 50 + h - 1),
                         'one frame short of the hold -> not elapsed')
        self.assertTrue(viz.coldHoldElapsed(50, 50 + h),
                        'hold elapsed')

    # ----- noteVizActivity stamps the mutation frame ----------------------

    def test_note_activity_stamps_mutation_frame(self):
        ext = _stub_ext()
        ext._resolveActiveOp = lambda o, p, r: '/probe/x'
        ext._actionText = lambda o, t: 'creating x'
        viz.noteVizActivity(ext, 'create_op', {}, {})
        self.assertEqual(ext._viz_mutation_frame, absTime.frame,
                         'mutating op must stamp the current frame')
        self.assertEqual(len(ext._viz_target_queue), 1)

    def test_note_activity_ignores_read_ops(self):
        ext = _stub_ext()
        ext._resolveActiveOp = lambda o, p, r: '/probe/x'
        ext._actionText = lambda o, t: 'reading'
        ext._viz_mutation_frame = -5
        viz.noteVizActivity(ext, 'query_network', {}, {})
        self.assertEqual(ext._viz_mutation_frame, -5,
                         'read ops must not stamp the mutation frame')
        self.assertEqual(len(ext._viz_target_queue), 0)

    def test_note_activity_stamps_even_when_target_unresolved(self):
        ext = _stub_ext()
        ext._resolveActiveOp = lambda o, p, r: None
        ext._actionText = lambda o, t: ''
        viz.noteVizActivity(ext, 'create_op', {}, {})
        self.assertEqual(ext._viz_mutation_frame, absTime.frame,
                         'the network still mutated -- stamp regardless')
        self.assertEqual(len(ext._viz_target_queue), 0)

    # ----- vizTick settle gate --------------------------------------------

    def test_viztick_holds_pump_when_unsettled(self):
        ext = _stub_ext(embot=False, follow=True)
        ext._viz_last_activity = absTime.seconds
        ext._viz_target_queue = [('/probe/x', 'creating x')]
        ext._viz_mutation_frame = absTime.frame   # mutated THIS frame
        viz.vizTick(ext)
        self.assertEqual(len(ext._viz_target_queue), 1,
                         'unsettled frame must not pump the hop queue')
        self.assertIsNone(ext._viz_target_op,
                          'unsettled frame must not select a follow target')

    def test_viztick_settled_pumps_and_cold_gate_pulses_only(self):
        target = self.sandbox.create(nullTOP, 'viz_gate_target')
        ext = _stub_ext(embot=False, follow=True)
        ext._viz_last_activity = absTime.seconds
        ext._viz_target_queue = [(target.path, 'creating viz_gate_target')]
        ext._viz_mutation_frame = absTime.frame - 10   # long settled
        viz.vizTick(ext)
        self.assertEqual(ext._viz_target_op, target.path,
                         'settled frame pumps the hop')
        self.assertEqual(ext._viz_cold_since, absTime.frame,
                         'cold hold starts on the first tracked frame')
        self.assertFalse(ext._viz_session_warm,
                         'still cold within the hold window')
        self.assertEqual(ext._viz_pulse_op, target.path,
                         'cold activation DOES ping the node colour')
        self.assertIsNone(ext._viz_selected_op,
                          'cold activation must NOT select/highlight')
        self.assertIsNone(ext._viz_bot_net,
                          'cold activation must NOT spawn the bot')

    # ----- cold hold elapse + reset ---------------------------------------

    def test_cold_hold_elapses_to_warm(self):
        target = self.sandbox.create(nullTOP, 'viz_warm_target')
        ext = _stub_ext(embot=False, follow=False)
        ext._viz_target_op = target.path
        ext._viz_cold_since = absTime.frame - viz._VIZ_COLD_HOLD_FRAMES
        viz.trackActive(ext, absTime.seconds, False, False)
        self.assertTrue(ext._viz_session_warm,
                        'hold elapsed -> session goes warm')
        self.assertIsNone(ext._viz_selected_op,
                          'follow off -> still no highlight')

    def test_cleanup_resets_cold_state(self):
        ext = _stub_ext()
        ext._viz_session_warm = True
        ext._viz_cold_since = 123
        viz.vizCleanup(ext)
        self.assertFalse(ext._viz_session_warm,
                         'retire -> next activation is cold again')
        self.assertEqual(ext._viz_cold_since, -1)

    # ----- issue #86: pure predicates (no TD at all) -----------------------

    def test_pending_hops_counts_by_parent_net(self):
        self.assertEqual(viz.pendingHopsIn([], '/foo'), 0,
                         'empty queue -> no evidence')
        q = [('/foo/bar', 'a'), ('/foo/baz', 'b')]
        self.assertEqual(viz.pendingHopsIn(q, '/foo'), 2,
                         'both children counted for their parent net')
        self.assertEqual(viz.pendingHopsIn([('/foo', 'a')], '/'), 1,
                         'a top-level op belongs to the root net')
        mixed = [('/foo/a', ''), ('/bar/b', ''), ('/foo/c', ''),
                 ('/bar/d', ''), ('/bar/e', '')]
        self.assertEqual(viz.pendingHopsIn(mixed, '/foo'), 2)
        self.assertEqual(viz.pendingHopsIn(mixed, '/bar'), 3)
        self.assertEqual(viz.pendingHopsIn(mixed, '/nope'), 0)

    def test_spawn_would_be_seen_truth_table(self):
        for follow in (True, False):
            for takeover in (True, False):
                for editor in (True, False):
                    self.assertTrue(
                        viz.spawnWouldBeSeen(True, follow, takeover, editor),
                        'a DISPLAYED net is always worth spawning into')
        self.assertTrue(viz.spawnWouldBeSeen(False, True, False, True),
                        'follow on + editor present + no takeover -> about to be seen')
        self.assertFalse(viz.spawnWouldBeSeen(False, False, False, True),
                         'follow off -> the camera will never go there')
        self.assertFalse(viz.spawnWouldBeSeen(False, True, True, True),
                         'takeover active -> we are yielding to the user')
        self.assertFalse(viz.spawnWouldBeSeen(False, True, False, False),
                         'no network-editor pane -> nothing can show it')

    def test_path_inside_subtree(self):
        self.assertTrue(viz.pathInsideSubtree('/a', '/a'),
                        'a COMP contains itself')
        self.assertTrue(viz.pathInsideSubtree('/a/b', '/a'))
        self.assertFalse(viz.pathInsideSubtree('/ab', '/a'),
                         'the prefix trap: /ab is NOT inside /a')
        self.assertTrue(viz.pathInsideSubtree('/a/b/c', '/'),
                        'everything is inside the root')
        self.assertFalse(viz.pathInsideSubtree('', '/a'))
        self.assertFalse(viz.pathInsideSubtree('/a', ''))

    # ----- issue #86: the relocation gate (stub ext, injected clock) -------

    def test_first_appearance_allowed_but_not_committed(self):
        """The gate is a PREDICATE. Allowing is not committing: only a landed
        spawn stamps _viz_home, via commitRelocation."""
        ext = _stub_ext()
        self.assertTrue(viz.netRelocationOK(ext, '/netA', [], 100.0),
                        'first appearance is never delayed')
        self.assertIsNone(ext._viz_home,
                          'the gate must NOT stamp home -- the spawn decides')
        viz.commitRelocation(ext, '/netA', 100.0)
        self.assertEqual(ext._viz_home, ('/netA', 100.0))
        self.assertIsNone(ext._viz_net_candidate)

    def test_new_net_under_dwell_refused(self):
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        self.assertFalse(viz.netRelocationOK(ext, '/netB', [], t0 + 0.5),
                         'work merely passing through must not be chased')
        self.assertEqual(ext._viz_net_candidate, ('/netB', t0 + 0.5),
                         'the candidate clock starts on first sight')
        self.assertEqual(ext._viz_home[0], '/netA', 'home is unchanged')

    def test_new_net_commits_after_dwell_and_cooldown(self):
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        viz.netRelocationOK(ext, '/netB', [], t0 + 0.5)   # starts the candidate
        t1 = t0 + max(viz._VIZ_NET_DWELL_S, viz._VIZ_RELOCATE_MIN_S) + 0.1
        self.assertTrue(viz.netRelocationOK(ext, '/netB', [], t1),
                        'settled work IS followed once both clocks allow it')
        viz.commitRelocation(ext, '/netB', t1)
        self.assertEqual(ext._viz_home, ('/netB', t1))
        self.assertIsNone(ext._viz_net_candidate)

    def test_alternating_nets_are_rate_limited_not_frozen(self):
        """The measured worst case: every hop crosses a network (21.3 fps, p90
        182ms). A different candidate net restarts the dwell clock, so the dwell
        ALONE would refuse forever -- Embot standing on a stale node for the
        whole session while every op lights up elsewhere. That is a loss of the
        feature, not a rate limit, so _VIZ_NET_STARVE_S is the escape hatch: he
        moves into the work, then stays there (every second hop is then the net
        he is standing in, which is always free)."""
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netC', t0)                 # work is nowhere near him
        nets = ('/netA', '/netB')
        commits = []
        t = t0
        for i in range(120):                      # 120 x 0.4s = 48 simulated seconds
            n = nets[i % 2]
            if viz.netRelocationOK(ext, n, [], t) and n != ext._viz_bot_net:
                _landAt(ext, n, t)
                commits.append(t)
            t += 0.4
        self.assertTrue(commits, 'the ceiling must break the alternating stall')
        self.assertLessEqual(
            commits[0] - t0, 5.0,
            'and it must fire within the DOCUMENTED 4.0s ceiling (plus one 0.4s '
            'sample of slack). Deliberately a literal: the bound this used to '
            'assert was _VIZ_NET_STARVE_S + _VIZ_RELOCATE_MIN_S, which moves '
            'with the constant and so passes at any value, a doubled one '
            'included')
        self.assertEqual(len(commits), 1,
                         'once he is IN the alternating work he settles: the '
                         'net he stands in is always allowed, which keeps '
                         'clearing the starvation clock')

    def test_relocations_stay_under_the_cost_bound(self):
        """The whole point of the gate: _VIZ_RELOCATE_MIN_S is a HARD floor
        between relocations, and it is what turns a 150-230ms main-thread event
        from 28% of wall clock into single digits.

        Two streams, because they are bound by different constants and only the
        first can prove the FLOOR. Stream A presents a brand-new network every
        0.25s, each carrying queued-batch evidence: evidence bypasses the dwell
        on sight, and a commit clears the starvation clock, so the cooldown is
        the ONLY thing left that can space these commits.

        Stream B is the one this test used to run alone -- 12 nets, 0.5s apart,
        no evidence. There the 4.0s starvation ceiling binds and the floor is
        slack, so deleting the floor entirely did not move a single commit and
        the assertion below it could not fail. It is kept for what it does
        prove: work that never settles anywhere is still followed."""
        step = 0.25
        ext = _stub_ext()
        t0 = 100.0
        commits = []
        t = t0
        for i in range(64):                       # 64 x 0.25s = 16 simulated seconds
            n = '/batch%d' % i                    # a NEW net every single sample
            q = [(n + '/a', ''), (n + '/b', '')]  # ... each with real evidence
            if viz.netRelocationOK(ext, n, q, t) and n != ext._viz_bot_net:
                _landAt(ext, n, t)
                commits.append(t)
            t += step
        self.assertGreater(len(commits), 2,
                           'proven batches must still be followed')
        gaps = [b - a for a, b in zip(commits, commits[1:])]
        self.assertGreaterEqual(
            min(gaps), 2.49,
            'the documented 2.5s cooldown is a HARD floor, not a heuristic. '
            'Literal, not viz._VIZ_RELOCATE_MIN_S: an assertion that reads the '
            'constant still holds when the constant is set to 0, which is the '
            'whole cost bound gone')
        self.assertLessEqual(
            max(gaps), 2.5 + step,
            'and the floor is what is doing the spacing -- each commit lands on '
            'the first sample after it expires, not later')
        self.assertLess(
            max(gaps), 4.0,
            'nothing in this stream waited on the 4.0s starvation ceiling, so '
            'the floor is the only thing that can explain the spacing')

        ext = _stub_ext()
        commits = []
        t = t0
        for i in range(60):                       # 60 x 0.5s = 30 simulated seconds
            n = '/net%d' % (i % 12)
            if viz.netRelocationOK(ext, n, [], t) and n != ext._viz_bot_net:
                _landAt(ext, n, t)
                commits.append(t)
            t += 0.5
        self.assertGreater(len(commits), 1,
                           'he must still follow work that never settles')
        gaps = [b - a for a, b in zip(commits, commits[1:])]
        self.assertGreaterEqual(min(gaps), viz._VIZ_RELOCATE_MIN_S,
                                'the cooldown is a HARD floor, not a heuristic')

    def test_dwell_binds_when_the_cooldown_is_long_satisfied(self):
        """_VIZ_NET_DWELL_S pinned at its documented 2.0s, in the ONLY window
        where it can be the binding constraint: the cooldown (2.5s) is longer
        than the dwell, so anywhere near a fresh commit the cooldown hides it
        and a broken dwell would go unnoticed. Here home was committed 10s ago
        -- the cooldown is long satisfied -- and the only thing between the work
        and Embot is the dwell.

        Literal offsets on purpose. Asserting against viz._VIZ_NET_DWELL_S would
        hold at any value, including 0.0 (the dwell deleted, so every cross-net
        drip is chased again -- the exact regression issue #86 is about)."""
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        viz.netRelocationOK(ext, '/netB', [], t0 + 10.0)   # first sight of /netB
        self.assertFalse(viz.netRelocationOK(ext, '/netB', [], t0 + 11.9),
                         '1.9s of continuous work in /netB is not 2.0s')
        self.assertTrue(viz.netRelocationOK(ext, '/netB', [], t0 + 12.1),
                        '2.1s IS the dwell -- and far too soon for the 4.0s '
                        'starvation ceiling to be what allowed it')

    def test_one_queued_hop_is_not_enough_evidence(self):
        """_VIZ_NET_EVIDENCE_HOPS pinned from BELOW. The bypass exists for a
        proven batch; a SINGLE queued hop is the signature of a stream merely
        passing through, which is precisely what the dwell must keep refusing.
        Drop the constant to 1 and this call is allowed."""
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        one = [('/netB/x', '')]
        self.assertFalse(
            viz.netRelocationOK(ext, '/netB', one,
                                t0 + viz._VIZ_RELOCATE_MIN_S + 0.1),
            'one hop is not a batch: past the cooldown, under the dwell, still '
            'refused')

    def test_starvation_streak_expires_when_the_gate_stops_asking(self):
        """The starvation ceiling measures a CONTINUOUS refusal streak, and both
        halves of that matter.

        Half 1 (the escape hatch, must not regress): work actively alternating
        between two networks restarts the candidate clock on every hop, so
        without the ceiling Embot would stand on a stale node forever. It still
        breaks out.

        Half 2 (the leak this closes): the refused branch releases the follow
        target when the queue is empty, so the gate simply stops being called
        while the clock keeps running against wall time. A single stray hop
        later then arrived ALREADY past the ceiling and skipped the dwell on
        first sight -- the ordinary cross-network drip the dwell exists to
        refuse, waved through by an escape hatch built for a stream that no
        longer existed."""
        t0 = 100.0
        ext = _stub_ext()
        _landAt(ext, '/netA', t0)
        nets = ('/netB', '/netC')
        t = t0 + 0.4
        i = 0
        while t < t0 + 6.0 and not viz.netRelocationOK(ext, nets[i % 2], [], t):
            i += 1
            t += 0.4                     # keep ASKING, every sample
        self.assertLess(t - t0, 5.0,
                        'alternating work that keeps asking must still escape '
                        'within the ceiling')

        ext = _stub_ext()
        _landAt(ext, '/netA', t0)
        self.assertFalse(viz.netRelocationOK(ext, '/netB', [], t0 + 0.5))
        self.assertEqual(ext._viz_relocate_blocked_since, t0 + 0.5,
                         'the streak clock starts on the first refusal')
        stray = t0 + 60.0                # ... and then nothing asks for a minute
        self.assertFalse(
            viz.netRelocationOK(ext, '/netC', [], stray),
            'a stray hop long afterwards must serve the full dwell: the clock '
            'stopped describing a streak when the gate stopped being asked')
        self.assertEqual(ext._viz_relocate_blocked_since, stray,
                         'and the streak restarts from this refusal')

    def test_queue_evidence_bypasses_dwell_not_cooldown(self):
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        qb = [('/netB/x', ''), ('/netB/y', '')]   # a real batch queued for netB
        self.assertFalse(viz.netRelocationOK(ext, '/netB', qb, t0 + 0.3),
                         'evidence must NOT bypass the cooldown -- it is the cost bound')
        t1 = t0 + viz._VIZ_RELOCATE_MIN_S + 0.1
        self.assertTrue(viz.netRelocationOK(ext, '/netB', qb, t1))
        _landAt(ext, '/netB', t1)
        # And with a FRESH candidate (zero dwell elapsed) evidence alone
        # commits, proving it really does bypass the dwell.
        qc = [('/netC/x', ''), ('/netC/y', '')]
        t2 = t1 + viz._VIZ_RELOCATE_MIN_S + 0.1
        self.assertTrue(viz.netRelocationOK(ext, '/netC', qc, t2),
                        'a queued batch is followed immediately past the cooldown')
        _landAt(ext, '/netC', t2)
        self.assertEqual(ext._viz_home, ('/netC', t2))

    def test_same_net_never_restamps_home(self):
        """REGRESSION GUARD. Re-stamping _viz_home on the same-network path
        would restart the cooldown every frame and freeze Embot in place
        FOREVER -- a silent, total loss of the feature. commitRelocation is
        called on EVERY frame he is standing at home, so the guard has to live
        there too, not only in the gate."""
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        t = t0
        for _ in range(100):                     # 100 frames over 10 simulated seconds
            self.assertTrue(viz.netRelocationOK(ext, '/netA', [], t),
                            'already home -> always allowed')
            viz.commitRelocation(ext, '/netA', t)
            self.assertEqual(ext._viz_home[1], t0,
                             'the same-net path must not touch the cooldown clock')
            t += 0.1
        viz.netRelocationOK(ext, '/netB', [], t)                       # candidate
        self.assertTrue(
            viz.netRelocationOK(ext, '/netB', [], t + viz._VIZ_NET_DWELL_S + 0.1),
            'relocation still works after a long stay at home')

    def test_refused_spawn_never_commits_a_phantom_home(self):
        """REGRESSION GUARD for the desync that made Embot freeze on a stale
        node. ensureBot legitimately refuses a spawn -- botUnsafeNet fires on
        EVERY TDN-strategy COMP (the auto-externalization default, so most COMPs
        in an Embody project), and botWouldBeSeen fires whenever the follow is
        off and the user is parked elsewhere. If the gate had already stamped
        _viz_home, viz would be committed to a network he never entered, and
        every later hop to the network he IS in would be charged a full
        dwell+cooldown for a relocation that needs zero copyOPs."""
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        t = t0 + 0.1
        while t < t0 + 10.0 and not viz.netRelocationOK(ext, '/netB', [], t):
            t += 0.1
        self.assertLess(t, t0 + 10.0, 'the gate must eventually allow /netB')
        # ensureBot refuses -> trackActive does NOT commit. _viz_bot_net is
        # untouched, so nothing may have moved.
        self.assertEqual(ext._viz_home[0], '/netA',
                         'a refused spawn must leave home where he actually is')
        self.assertEqual(ext._viz_bot_net, '/netA')
        self.assertTrue(viz.netRelocationOK(ext, '/netA', [], t + 0.1),
                        'a hop to the net he is STANDING IN is always free')

    def test_in_flight_assembly_blocks_relocation(self):
        """A DISPLAYED-net spawn is a 9-part spread ~4.8s long -- LONGER than
        the cooldown. Without this, a commit 2.5s in reassigns the build queue,
        the spread restarts and never completes while the copies keep costing.
        Hops WITHIN the assembling net stay free (he is already there)."""
        ext = _stub_ext()
        t0 = 100.0
        _landAt(ext, '/netA', t0)
        ext._viz_bot_build_queue = ['envoy_bot_body', 'envoy_bot_head']
        qb = [('/netB/x', ''), ('/netB/y', ''), ('/netB/z', '')]
        self.assertFalse(
            viz.netRelocationOK(ext, '/netB', qb, t0 + 10.0),
            'not even queued batch evidence may restart an in-flight assembly')
        self.assertTrue(viz.netRelocationOK(ext, '/netA', [], t0 + 10.0),
                        'the net he is assembling in is still free')
        ext._viz_bot_build_queue = []
        self.assertTrue(viz.netRelocationOK(ext, '/netB', qb, t0 + 10.1),
                        'and the block lifts the moment assembly finishes')

    def test_cleanup_resets_relocation_state(self):
        ext = _stub_ext()
        ext._viz_home = ('/netA', 12.0)
        ext._viz_net_candidate = ('/netB', 13.0)
        ext._viz_relocate_blocked_since = 11.0
        ext._viz_relocate_blocked_last = 14.0
        ext._viz_write_suppress = {'/netA': 99.0}
        ext._viz_selected_op = '/netA/foo'      # an op that no longer resolves
        viz.vizCleanup(ext)
        self.assertIsNone(ext._viz_home,
                          'retire -> the next activation is a first appearance')
        self.assertIsNone(ext._viz_net_candidate)
        self.assertIsNone(ext._viz_relocate_blocked_since)
        self.assertIsNone(ext._viz_relocate_blocked_last)
        self.assertEqual(ext._viz_write_suppress, {})
        self.assertIsNone(ext._viz_selected_op,
                          'a stale selection cache would silently kill the '
                          'focus marker for that op for the rest of the session')

    def test_cleanup_deselects_the_op_it_highlighted(self):
        """The selection is part of the follow, so retiring the follow must
        retire it. highlightOp only ever deselects the op it is REPLACING, so
        dropping the cache without dropping the selection leaves the last
        highlighted operator selected for the rest of the session with nothing
        left that will ever clear it (the next highlightOp deselects `prev`,
        which the retire just set to None) -- Envoy's focus marker sitting on a
        node it is no longer following, after a save, perform mode or an idle
        retire."""
        target = self.sandbox.create(nullTOP, 'viz_cleanup_selected')
        target.selected = True
        self.assertTrue(target.selected,
                        'fixture: TD must round-trip .selected for this test to '
                        'mean anything')
        ext = _stub_ext()
        ext._viz_selected_op = target.path
        viz.vizCleanup(ext)
        self.assertFalse(target.selected,
                         'the marker must not outlive the follow that set it')
        self.assertIsNone(ext._viz_selected_op)

    # ----- issue #86: trackActive itself (the gate's only caller) -----------

    def test_track_active_commits_only_a_spawn_that_landed(self):
        """REGRESSION GUARD, driven through trackActive rather than the gate:
        the commit is downstream of the spawn, and the branch that keeps it
        there is one line (`if ext._viz_bot_net == net.path`). Make it
        unconditional and viz is committed to a network Embot never entered --
        every later hop to the network he IS in is then charged a full
        dwell+cooldown for a relocation that needs zero copyOPs, which is Embot
        frozen on a stale node in front of the user."""
        target = self.sandbox.create(nullTOP, 'viz_track_target')
        net = self.sandbox
        t0 = 100.0
        orig_ensure = viz.ensureBot
        try:
            viz.ensureBot = lambda e, n: False      # botUnsafeNet / botWouldBeSeen
            ext = _stub_ext(embot=True, follow=False)
            ext._viz_session_warm = True            # past the issue-57 cold hold
            ext._viz_target_op = target.path
            ext._viz_bot_net = '/viz_elsewhere'
            ext._viz_home = ('/viz_elsewhere', t0)
            viz.netRelocationOK(ext, net.path, [], t0 + 0.1)   # start the dwell
            viz.trackActive(ext, t0 + 3.0, False, True)   # gate ALLOWS by now
            self.assertEqual(ext._viz_home, ('/viz_elsewhere', t0),
                             'a refused spawn must leave home where he is')
            self.assertEqual(ext._viz_bot_net, '/viz_elsewhere',
                             'and must not claim the net either')

            def _landing(e, n):
                e._viz_bot_net = n.path             # what a real spawn does
                return True

            viz.ensureBot = _landing
            ext = _stub_ext(embot=True, follow=False)
            ext._viz_session_warm = True
            ext._viz_target_op = target.path
            ext._viz_bot_net = '/viz_elsewhere'
            ext._viz_home = ('/viz_elsewhere', t0)
            viz.netRelocationOK(ext, net.path, [], t0 + 0.1)
            viz.trackActive(ext, t0 + 3.0, False, True)
            self.assertEqual(ext._viz_home, (net.path, t0 + 3.0),
                             'a spawn that LANDED is exactly what commits')
        finally:
            viz.ensureBot = orig_ensure

    def test_track_active_refusal_pulses_and_releases_the_target(self):
        """The gated path is not a dead end. It pulses the node colour (so the
        user still sees Envoy working, from wherever Embot stands) and then
        RELEASES the follow target exactly as glideStep does once it has caught
        up. Without the release _viz_target_op stays set, trackActive re-runs
        every frame, and pulseStart re-arms the instant the 0.45s fade clears it
        -- a node in a network nobody is viewing strobing forever, one colour
        write per frame. Mid-batch is the exception: there IS more to visit and
        the next pump replaces the target anyway."""
        target = self.sandbox.create(nullTOP, 'viz_refuse_target')
        t0 = 100.0
        orig_ensure = viz.ensureBot
        try:
            # If the gate ever leaked, this keeps the test from spawning parts.
            viz.ensureBot = lambda e, n: False
            ext = _stub_ext(embot=True, follow=False)
            ext._viz_session_warm = True
            ext._viz_target_op = target.path
            ext._viz_bot_net = '/viz_elsewhere'
            ext._viz_home = ('/viz_elsewhere', t0)
            viz.trackActive(ext, t0 + 0.5, False, True)   # under the dwell
            self.assertEqual(ext._viz_pulse_op, target.path,
                             'a refusal still pings the active node')
            self.assertIsNone(ext._viz_target_op,
                              'queue empty -> release, or the node strobes '
                              'forever at one write per frame')

            ext = _stub_ext(embot=True, follow=False)
            ext._viz_session_warm = True
            ext._viz_target_op = target.path
            ext._viz_target_queue = [('/viz_elsewhere/next', 'building next')]
            ext._viz_bot_net = '/viz_elsewhere'
            ext._viz_home = ('/viz_elsewhere', t0)
            viz.trackActive(ext, t0 + 0.5, False, True)
            self.assertEqual(ext._viz_target_op, target.path,
                             'mid-batch the target is left for the pump')
        finally:
            viz.ensureBot = orig_ensure

    # ----- issue #86: ordering + suppression -------------------------------

    def test_ensure_bot_refuses_unseen_net_before_table_scan(self):
        """botWouldBeSeen MUST run before botUnsafeNet: botUnsafeNet reaches
        EmbodyExt._getTDNPaths() (a full externalizations-table scan with a
        per-row op()), and in the suppressed state ensureBot runs its prefix
        EVERY frame."""
        dest = self.sandbox.create(baseCOMP, 'viz_unseen_dest')
        ext = _stub_ext(embot=True, follow=False)
        calls = []
        orig_seen = viz.botWouldBeSeen
        orig_unsafe = viz.botUnsafeNet
        try:
            viz.botWouldBeSeen = lambda e, n: False
            viz.botUnsafeNet = lambda e, n: calls.append(n.path) or False
            self.assertFalse(viz.ensureBot(ext, dest),
                             'an unseen net must not get a 9-part spawn')
            self.assertIsNone(ext._viz_bot_net,
                              'nothing may be claimed for an unseen net')
            self.assertEqual(len(calls), 0,
                             'the table scan must not run in the suppressed state')
        finally:
            viz.botWouldBeSeen = orig_seen
            viz.botUnsafeNet = orig_unsafe

    def test_bot_writes_skipped_while_staging(self):
        """Mid-assembly he is an invisible pile at the off-view staging point;
        an assembly spans ~4.8s, so the skipped writes are ~145 full-figure
        repaints of 9 annotates.

        netIsDisplayed is pinned True for both legs -- it depends on the user's
        live pane layout, and for a sandbox COMP it answers False anyway, which
        made the assertion below pass no matter what the staging branch did.
        With it pinned, the pending-entrance branch is the ONLY thing that can
        explain the False."""
        ext = _stub_ext(embot=True)
        orig_displayed = viz.netIsDisplayed
        try:
            viz.netIsDisplayed = lambda e, n: True
            ext._viz_bot_pending_entrance = False
            self.assertTrue(viz.botWritesNeeded(ext, self.sandbox),
                            'control leg: a displayed net with nobody staging '
                            'DOES want its writes')
            ext._viz_bot_pending_entrance = True
            self.assertFalse(viz.botWritesNeeded(ext, self.sandbox),
                             'no editor writes while parked off-view, even '
                             'though the destination net is displayed')
        finally:
            viz.netIsDisplayed = orig_displayed

    # ----- issue #86: save-path + data integrity ---------------------------

    def test_assemble_step_restores_template_position(self):
        """assembleStep parks the TEMPLATE source off-view before copying it.
        The template lives in the Embody COMP (TDN strategy), so a coordinate
        left behind is exported into Embody.tdn -- which is exactly how all
        nine parts came to be committed at a leaked [1860, 251]."""
        tmpl = self.sandbox.create(baseCOMP, 'embot_template')
        body = _annotate(tmpl, 'envoy_bot_body')
        _annotate(tmpl, 'envoy_bot_speech')   # ensureTemplate checks both
        body.nodeX, body.nodeY = viz._VIZ_TEMPLATE_PARK
        dest = self.sandbox.create(baseCOMP, 'viz_assemble_dest')
        stage = (5000.0, 5000.0)
        ext = _stub_ext(embot=True)
        ext.ownerComp.op = self.sandbox.op          # ensureTemplate's host lookup
        ext._viz_bot_stage = stage
        ext._viz_bot_pos = (10.0, 10.0)
        ext._viz_bot_build_queue = ['envoy_bot_body']
        viz.assembleStep(ext, dest)
        self.assertIsNotNone(dest.op('envoy_bot_body'),
                             'the part must actually have been copied')
        self.assertNotEqual((body.nodeX, body.nodeY), stage,
                            'the template source must NOT be left at the staging point')
        self.assertEqual((body.nodeX, body.nodeY), viz._VIZ_TEMPLATE_PARK,
                         'the template source position must be restored')

    def test_purge_skips_template(self):
        root = self.sandbox.create(baseCOMP, 'viz_purge_root')
        _annotate(root, 'envoy_bot_body')
        _annotate(root, 'envoy_bot_head')
        nested = root.create(baseCOMP, 'viz_purge_nested')
        _annotate(nested, 'envoy_bot_body')     # orphan, deeper down
        tmpl = root.create(baseCOMP, viz._VIZ_TEMPLATE_COMP)
        _annotate(tmpl, 'envoy_bot_body')
        ext = _stub_ext()
        removed = viz.purgeVizArtifacts(ext, root=root)
        self.assertEqual(removed, 3, 'every LOOSE part at any depth is swept')
        self.assertIsNone(root.op('envoy_bot_body'))
        self.assertIsNone(root.op('envoy_bot_head'))
        self.assertIsNone(nested.op('envoy_bot_body'))
        self.assertIsNotNone(tmpl.op('envoy_bot_body'),
                             'the shipped template asset must survive the sweep')

    def test_project_sweep_spares_a_root_child_that_is_not_an_annotation(self):
        """The project-wide sweep descends per root child, and that branch
        collected root-level ops by NAME alone while every sibling branch
        restricts to annotateCOMP. A bot part is always an annotation, so the
        name alone was enough to destroy a user's operator that merely carried
        the prefix -- a TOP, a COMP, anything -- with no undo and only a count
        in the log."""
        root = self.sandbox.create(baseCOMP, 'viz_sweep_root')
        _annotate(root, 'envoy_bot_body')                  # a real loose part
        decoy = root.create(nullTOP, 'envoy_bot_decoy')    # NOT an annotation
        nested = root.create(baseCOMP, 'viz_sweep_nested')
        _annotate(nested, 'envoy_bot_head')                # loose, deeper down
        ext = _stub_ext()
        ext.ownerComp.ext.Embody.root = root   # drive the PROJECT-WIDE branch
        removed = viz.purgeVizArtifacts(ext)
        self.assertTrue(decoy.valid,
                        'only annotateCOMPs are Embot parts -- a TOP with the '
                        'reserved name is a user operator, not an artifact')
        self.assertIsNone(root.op('envoy_bot_body'),
                          'a root-level part is still swept')
        self.assertIsNone(nested.op('envoy_bot_head'),
                          'and so is one further down')
        self.assertEqual(removed, 2, 'exactly the two annotations')

    def test_viz_bot_constants_match_the_tdn_exporter(self):
        """TDNExt mirrors these two literals rather than importing the viz
        module DAT (Envoy is optional; .tdn export must work without it). Drift
        is SILENT -- the export filter simply stops matching and live bot parts
        reach .tdn again with no error and no other failing test."""
        # Read the LIVE extension's own module namespace (via the function
        # object actually doing the filtering) rather than re-compiling the DAT
        # with .module -- this asserts against the code that is running.
        tdn_globals = type(op.Embody.ext.TDN)._exportAnnotations.__globals__
        self.assertEqual(tdn_globals['VIZ_BOT_ANNOTATION_PREFIX'],
                         viz._VIZ_BOT_PREFIX)
        self.assertEqual(tdn_globals['VIZ_BOT_TEMPLATE_COMP'],
                         viz._VIZ_TEMPLATE_COMP)

    def test_export_annotations_omits_loose_bot_parts(self):
        """The filter must NOT strip the shipped template -- Embody.tdn carries
        its nine parts as a legitimate annotations block, and stripping them
        would force a ~1s nine-annotateCOMP rebuild on every fresh open."""
        tdn = op.Embody.ext.TDN
        loose = self.sandbox.create(baseCOMP, 'viz_export_loose')
        _annotate(loose, 'envoy_bot_body')
        _annotate(loose, 'ordinary_note')
        names = [a['name'] for a in tdn._exportAnnotations(loose)]
        self.assertEqual(names, ['ordinary_note'],
                         'a LIVE bot part must never reach a .tdn')
        tmpl = self.sandbox.create(baseCOMP, 'embot_template')
        _annotate(tmpl, 'envoy_bot_body')
        tnames = [a['name'] for a in tdn._exportAnnotations(tmpl)]
        self.assertEqual(tnames, ['envoy_bot_body'],
                         'the shipped template keeps its parts')

    def test_async_walk_routes_annotations_through_the_bot_filter(self):
        """The filter above only protects a .tdn if EVERY walk reaches
        annotations through _exportAnnotations. The sync walk skips annotate
        ops in _exportChildren; the async one (_collectAllPaths, used by
        ExportNetworkAsync -- the PROJECT-WIDE snapshot, the one export that
        runs over networks Embot is perfectly free to stand in) collected them
        as ordinary operators, so a live bot part went to disk as a full
        operator entry, around the filter entirely.

        Both directions: a live part must not be collected, and the shipped
        template's COMP must still be, or _onExportRefresh never calls
        _exportAnnotations on it and the asset silently leaves the file."""
        tdn = op.Embody.ext.TDN
        host = self.sandbox.create(baseCOMP, 'viz_async_host')
        _annotate(host, 'envoy_bot_body')       # a LIVE bot part
        _annotate(host, 'ordinary_note')        # an ordinary annotation
        keep = host.create(nullTOP, 'viz_async_keep')
        tmpl = host.create(baseCOMP, viz._VIZ_TEMPLATE_COMP)
        _annotate(tmpl, 'envoy_bot_body')       # the shipped asset
        paths = tdn._collectAllPaths(host)
        self.assertIn(keep.path, paths, 'ordinary operators still export')
        self.assertNotIn(host.path + '/envoy_bot_body', paths,
                         'a LIVE bot part must never be collected as an op')
        self.assertNotIn(host.path + '/ordinary_note', paths,
                         'no annotation is: they belong to `annotations:`, '
                         'exactly as the sync walk decides')
        self.assertIn(tmpl.path, paths,
                      'the template COMP itself is still collected -- that is '
                      'what gets its parts exported via _exportAnnotations')
        self.assertNotIn(tmpl.path + '/envoy_bot_body', paths)
        self.assertEqual([a['name'] for a in tdn._exportAnnotations(tmpl)],
                         ['envoy_bot_body'],
                         'and that seam still yields them')

    def test_omitted_bot_annotation_is_reported(self):
        """The filter cannot tell a live Embot part from a USER annotation that
        happens to carry the reserved prefix, and for the second the omission is
        data loss. Announce it, in the same voice as execute.py's save-time
        purge warning -- a filter that drops user content silently is
        indistinguishable from a bug. One aggregated line per COMP: a live bot
        is nine parts, and nine warnings would be noise."""
        tdn = op.Embody.ext.TDN
        host = self.sandbox.create(baseCOMP, 'viz_export_logged')
        _annotate(host, 'envoy_bot_body')
        _annotate(host, 'envoy_bot_head')
        _annotate(host, 'ordinary_note')
        logged = []
        # Shadow the bound method on the instance, then remove the shadow.
        tdn._log = lambda msg, level='INFO', **kw: logged.append((level, msg))
        try:
            names = [a['name'] for a in tdn._exportAnnotations(host)]
        finally:
            del tdn._log
        self.assertEqual(names, ['ordinary_note'])
        warnings = [m for lvl, m in logged if lvl == 'WARNING']
        self.assertEqual(len(warnings), 1,
                         'one line for the COMP, not one per part')
        self.assertIn('envoy_bot_body', warnings[0])
        self.assertIn('envoy_bot_head', warnings[0])

    def test_retire_for_write_only_when_inside(self):
        """A blanket retire here would make Embot flicker on every Save() that
        dirtyHandler issues, and would reset the relocation gate with him."""
        ext = _stub_ext(embot=True)
        ext._viz_bot_net = '/a/b'
        self.assertTrue(viz.vizRetireForWrite(ext, '/a'),
                        'serializing his ancestor must retire him first')
        self.assertIsNone(ext._viz_bot_net)
        ext._viz_bot_net = '/a/b'
        self.assertFalse(viz.vizRetireForWrite(ext, '/c'),
                         'an unrelated COMP write must leave him alone')
        self.assertEqual(ext._viz_bot_net, '/a/b')

    def test_retire_for_write_preserves_the_relocation_clocks(self):
        """REGRESSION GUARD for an unbounded retire/respawn loop. This retire
        used to route through vizCleanup, which clears _viz_home -- and a null
        home takes the 'first appearance is NEVER delayed' branch. Since Embot's
        own nine parts are what mark a COMP dirty, the respawn re-dirtied the
        COMP, the next Update() saved it, and the retire fired again: a
        150-230ms main-thread event at MCP-call cadence, explicitly outside the
        cooldown the whole change exists to enforce."""
        ext = _stub_ext(embot=True)
        ext._viz_bot_net = '/a/b'
        ext._viz_home = ('/a/b', 50.0)
        ext._viz_net_candidate = ('/other', 51.0)
        ext._viz_session_warm = True
        ext._viz_target_queue = [('/a/b/x', 'building x')]
        self.assertTrue(viz.vizRetireForWrite(ext, '/a'))
        self.assertEqual(ext._viz_home, ('/a/b', 50.0),
                         'the cooldown clock must survive a write retire')
        self.assertTrue(ext._viz_session_warm,
                        'a .tox write must not re-arm the issue-57 cold hold')
        self.assertEqual(len(ext._viz_target_queue), 1,
                         'the rest of the batch must still be narrated')

    def test_retire_for_write_bars_immediate_reentry(self):
        """The other half of the loop guard: after a retire he may not walk
        straight back into the COMP that was just serialized, or his parts
        re-dirty it and the next Update() writes, retires and respawns again.
        Two retires of the same net cannot buy two spawns inside the cooldown."""
        ext = _stub_ext(embot=True)
        ext._viz_bot_net = '/a/b'
        ext._viz_home = ('/a/b', 50.0)
        t = absTime.seconds
        self.assertTrue(viz.vizRetireForWrite(ext, '/a'))
        self.assertTrue(viz.writeSuppressed(ext, '/a/b', t),
                        'the net he was retired out of is barred')
        self.assertTrue(viz.writeSuppressed(ext, '/a', t),
                        'and so is the COMP itself')
        self.assertFalse(viz.writeSuppressed(ext, '/c', t),
                         'unrelated networks are untouched')
        self.assertFalse(
            viz.writeSuppressed(ext, '/a/b', t + viz._VIZ_WRITE_SUPPRESS_S + 0.1),
            'the bar lifts after the suppression window')
        self.assertGreaterEqual(viz._VIZ_WRITE_SUPPRESS_S,
                                viz._VIZ_RELOCATE_MIN_S,
                                'a retire must not buy a spawn the ordinary '
                                'gate would have refused')

    def test_retire_for_write_spares_the_live_bot_elsewhere(self):
        """A stale deferred-teardown entry inside the COMP being written must
        not take the live bot down with it -- that is exactly the flicker the
        subtree scoping exists to avoid (cleanupDeadBots drains one net per
        frame, so a stale entry is normal mid-build)."""
        ext = _stub_ext(embot=True)
        ext._viz_bot_net = '/x'
        ext._viz_bot_pending_cleanup = {'/a/b'}
        self.assertTrue(viz.vizRetireForWrite(ext, '/a'))
        self.assertEqual(ext._viz_bot_net, '/x',
                         'the bot in an unrelated net was never at risk')
        self.assertNotIn('/a/b', ext._viz_bot_pending_cleanup,
                         'the pending entry inside the written COMP is drained')

    def test_highlight_op_skips_redundant_write(self):
        """trackActive runs every frame while following, so re-asserting
        selection + current is a per-frame editor write on a displayed node.
        Counted on a recorder rather than a live node: the contract is 'no
        write', which is observable directly, and asserting it through TD would
        depend on TD round-tripping a write to OP.current (it manages exactly
        one current op per network) and would take `current` off a real node."""
        ext = _stub_ext()
        target = _WriteRecorder('/viz/highlight_target')
        viz.highlightOp(ext, target)
        self.assertEqual(ext._viz_selected_op, target.path)
        self.assertEqual(target.writes, [('selected', True), ('current', True)],
                         'the first call marks the op')
        target.writes = []
        viz.highlightOp(ext, target)
        self.assertEqual(target.writes, [],
                         'a repeat call on an already-marked op writes nothing')

    def test_highlight_op_reasserts_a_lost_marker(self):
        """The early return must VERIFY, not assume. The user can drop our
        selection by clicking empty canvas, and delete_op + create_op can put a
        different operator at the same path -- a bare cache check then left
        Envoy's focus marker silently missing for that op for the rest of the
        session. `current` is deliberately NOT re-taken: there is one current op
        per network, and grabbing it back every frame is the fight-the-user
        behaviour the early return exists to stop."""
        ext = _stub_ext()
        target = _WriteRecorder('/viz/highlight_target')
        viz.highlightOp(ext, target)
        target.writes = []
        target._selected = False                 # user clicked elsewhere
        viz.highlightOp(ext, target)
        self.assertEqual(target.writes, [('selected', True)],
                         'the marker is restored -- and only the marker')
