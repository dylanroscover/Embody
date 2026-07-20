"""
Tests for the issue-57 viz activation gates in envoy_viz.

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

These tests drive the live envoy_viz module functions with a stub ext
(plain SimpleNamespace mirroring EnvoyExt's _viz_* state) plus one real
sandbox operator where a target op is needed. No panes are navigated, no
bot is spawned, nothing is selected -- the gates under test return before
any of that. NOT destructive.
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
    )


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
