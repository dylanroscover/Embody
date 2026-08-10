"""Embot build-visualization + camera follow for Envoy (module DAT).

Module DAT (mod.envoy_viz) called by EnvoyExt on the MAIN THREAD only. Every
function takes the EnvoyExt instance as `ext`; all mutable viz state
(ext._viz_*) lives on the ext instance so extension reinit semantics are
unchanged. Only the _VIZ_* constants live here at module level. No
module-level TD access -- op()/ui/absTime/project/baseCOMP/annotateCOMP are
TD globals resolved inside function bodies at call time.
"""

from __future__ import annotations

import colorsys
import math
import random


# === Live Build Visualization: smooth follow + navigate to the active op ===
# While Claude builds via MCP, the network editor follows Envoy's work so the
# user can watch in real time:
#   - within the viewed network it smoothly GLIDES to centre on the op just
#     touched (ease-out, one step per frame);
#   - when the work moves to a network NO pane is showing, it NAVIGATES a
#     network-editor pane into that COMP and SNAPS to frame the op (you cannot
#     glide across networks -- different coordinate spaces -- so it cuts).
# Opt-in (Embotenable for the character, Envoyfollow for the camera), main-thread
# only (driven from _onRefresh, which fires
# every frame), and side-effect-free w.r.t. saved files: it only writes
# pane.owner / pane.x / pane.y (view state -- not externalized, and verified to
# add no operator to project.modified). home()/homeSelected() are deliberately
# NOT used -- no-ops on an unfocused pane, which is what an MCP build presents.
#
# No throttle parameter: a move happens at most once per frame (after the
# request drain loop), so a 50-op batch_operations is a single move to its
# last op, never a strobe. The frame rate is the rate limiter, not a knob.
#
# Yield: if the user pans/zooms/navigates the follow pane, _userTookOver adopts
# their view as the new baseline and (re)arms an idle cooldown -- so we resume
# only after they stop, never give up forever, never yank them mid-interaction.

_VIZ_EASE = 0.4         # fraction of the remaining distance covered per frame (snappy -> stays on Embot)
_VIZ_EPS = 1.0          # network units; closer than this -> snap and release
_VIZ_TAKEOVER_S = 6.0   # seconds to yield after the user's last interaction
_VIZ_ZOOM = 0.55        # framing zoom while following -- zoomed out for context
_VIZ_TAKEOVER_PAN = 12.0   # min pan (network units) that counts as a user takeover
_VIZ_TAKEOVER_ZOOM = 0.08  # min zoom change that counts as a user takeover
_VIZ_IDLE_S = 30.0      # seconds of quiet before the bot + pulse retire (survives thinking pauses)
_VIZ_PULSE_S = 0.45     # seconds for a node's colour pulse to fade back
_VIZ_PULSE_COLOR = (0.15, 0.85, 0.70)    # Envoy accent (cyan-green)
# The builder-bot is a little figure of 8 minimal networkbox annotations
# (no text header) -- head, 2 eyes, body, 2 arms, 2 legs. Each part:
# (suffix, centre-offset-x, centre-offset-y, base-w, base-h, is_eye).
# Offsets are network units from the figure's anchor (y up). Body first so
# later parts (head, eyes) draw on top.
_VIZ_BOT_PREFIX = 'envoy_bot_'
_VIZ_BOT_PARTS = (
    ('body',   0.0,    0.0,   30.0, 34.0, False),
    ('arm_l', -22.0,   3.0,    9.0, 26.0, False),
    ('arm_r',  22.0,   3.0,    9.0, 26.0, False),
    ('leg_l',  -8.0,  -29.0,  11.0, 24.0, False),
    ('leg_r',   8.0,  -29.0,  11.0, 24.0, False),
    ('head',   0.0,   31.0,   34.0, 26.0, False),
    ('eye_l',  -8.0,  35.0,   12.0, 13.0, True),
    ('eye_r',   8.0,  35.0,   12.0, 13.0, True),
)
# Robotic motion: the figure JUMPS from node to node (parabolic arc, snappy
# ease) and does a small stepped hover when idle. Squash is subtle and only
# applied on landing.
_VIZ_JUMP_DUR = 0.52      # seconds per hop between nodes
_VIZ_JUMP_ARC = 55.0      # hop arc height (network units)
# Off-view assembly. Copying an annotateCOMP into a net you're VIEWING costs ~280ms
# (the in-viewport redraw); copying it OUTSIDE the viewport costs ~100ms (verified).
# So on an on-screen spawn Embot assembles at a staging point parked just past the
# viewport edge, then swoops in whole -- the per-part copies render off-view (much
# shallower fps sag) and the user sees a clean entrance instead of a stuttering
# build. _VIZ_STAGE_MARGIN is how far past the viewport edge to park; the swoop home
# uses _VIZ_ENTRANCE_DUR (slower than a normal hop, since it covers a big distance).
_VIZ_STAGE_MARGIN = 700.0   # network units past the viewport edge for the staging point
_VIZ_ENTRANCE_DUR = 0.95    # seconds for the swoop-in from staging (vs _VIZ_JUMP_DUR hops)
# Canonical resting coordinates for the TEMPLATE's parts (issue #86). The
# staging trick above parks the SOURCE part off-view before copying it, and the
# template lives inside the Embody COMP -- a TDN-strategy COMP -- so a staging
# coordinate left on the source is written straight into Embody.tdn on the next
# export (verified: all nine template parts were committed at [1860, 251], a
# leaked staging point). (0, 0) is deliberate: TDNExt._exportAnnotations omits
# `position` entirely when both nodeX and nodeY are 0, so the parked coordinate
# cannot drift back into the file at all.
_VIZ_TEMPLATE_PARK = (0.0, 0.0)
# Name of the shipped template COMP (a child of the Embody COMP). This module is
# the SOURCE OF TRUTH for both bot-artifact literals: TDNExt mirrors
# _VIZ_BOT_PREFIX / _VIZ_TEMPLATE_COMP as VIZ_BOT_ANNOTATION_PREFIX /
# VIZ_BOT_TEMPLATE_COMP so its export filter takes no dependency on this module
# DAT. Drift between the two is silent (the filter simply stops matching and
# live bot parts reach .tdn again), so
# test_viz_bot_constants_match_the_tdn_exporter asserts they are equal.
_VIZ_TEMPLATE_COMP = 'embot_template'
# Stepping cadence: how long Embot dwells on each queued op before advancing to
# the next. >= the jump so a hop lands before the next begins. When the queue
# backs up (a fat batch) the dwell shrinks toward _VIZ_HOP_MIN so he races to
# catch the wave -- but every op still gets its own visible hop, never skipped.
_VIZ_HOP_DWELL = 0.8      # base dwell per hop (queue empty)
_VIZ_HOP_MIN = 0.32       # floor dwell when the queue is deep
_VIZ_QUEUE_CAP = 24       # hard cap on pending hops (drop oldest beyond this)
# On-screen spawn pacing. Copying ONE annotateCOMP into a net you are LOOKING AT
# forces a ~70ms annotation-layer redraw -- a single dropped frame that cannot be
# made cheaper (the cost is the editor relayout, not the copy; verified by stripping
# the annotate's internals to no effect). What CAN be fixed is the clustering: the
# old spread copied one part every frame, so 9 hitches landed back-to-back and read
# as a ~1s freeze. Spacing the copies _VIZ_ASSEMBLE_INTERVAL frames apart isolates
# each hitch (smooth motion between them) so assembly reads as "building himself".
# Off-screen spawns use one fast block copy and ignore this entirely -- only the
# on-screen spread is gated. Higher = smoother but slower to finish assembling.
_VIZ_ASSEMBLE_INTERVAL = 32     # frames between on-screen part copies (~0.53s @ 60fps)
# Build order for the on-screen spread: body + head + speech first so he is instantly
# recognizable as "here", then limbs, then eyes -- never a half-built torso sitting
# limbless for seconds. Names match _VIZ_BOT_PARTS suffixes (+ the speech bubble).
_VIZ_ASSEMBLE_ORDER = ('body', 'head', 'speech', 'arm_l', 'arm_r',
                       'leg_l', 'leg_r', 'eye_l', 'eye_r')
_VIZ_HOVER_AMP = 3.0      # idle hover amplitude (network units)
_VIZ_HOVER_FREQ = 3.0     # idle hover frequency
_VIZ_SQUASH = 0.07        # landing squash amount (subtle)
# Occasional happy squint -- eyes briefly flatten + spread, reading as a content
# "^_^". Much rarer than the blink so it stays a gentle accent, not a tic. The
# 10px annotate-size floor means a squint only reads if the eyes are tall enough
# to flatten FROM -- hence the eyes are a bit bigger now (see _VIZ_BOT_PARTS).
_VIZ_SQUINT_GAP_MIN = 9.0    # min seconds between squints
_VIZ_SQUINT_GAP_MAX = 17.0   # max seconds between squints
_VIZ_SQUINT_DUR = 1.1        # how long a squint holds
_VIZ_SQUINT_FLATTEN = 0.74   # eye HEIGHT scale while squinting (toward the 10px floor)
_VIZ_SQUINT_WIDEN = 1.18     # eye WIDTH scale while squinting (the smile spread)
# Embot does an occasional gesture, cycling through several types so it stays
# varied: a wave, an arms-up shrug, an arms-up pump, and -- now and then
# -- a full-body robot dance. Any single gesture (incl. the wave) is therefore
# infrequent.
_VIZ_GESTURE_GAP_MIN = 4.0  # min seconds between gestures (randomized)
_VIZ_GESTURE_GAP_MAX = 11.0 # max seconds between gestures
_VIZ_GESTURE_DUR = 1.6      # how long a hand gesture lasts
_VIZ_DANCE_DUR = 3.0        # the robot dance runs a bit longer
_VIZ_WAVE_LIFT = 28.0       # how high the right arm raises to wave
_VIZ_WAVE_FREQ = 14.0       # wiggle speed of the wave
_VIZ_WAVE_AMP = 9.0         # wiggle amplitude of the wave
# Colour reflects "thinking time" -- how long since the last build op. Cool
# (cyan/blue) when Envoy just acted; warming through green/yellow to red the
# longer it goes between actions (a heavier "thinking" gap). Resets cool on
# each new op.
_VIZ_WARM_S = 14.0        # seconds of thinking to ramp fully cool -> warm
_VIZ_COOL_HUE = 0.58      # short/quick: cool blue-cyan
_VIZ_WARM_HUE = 0.0       # long/thought-heavy: warm red

# Operations that count as "building" and should move the camera. Read-only
# ops (get_*, query_network, read_tdn, capture_top) and batch_operations
# itself (its sub-ops route back through _execute_operation individually) are
# excluded. delete_op is excluded too: the op is gone post-dispatch and a
# deletion has no centre to frame.
_VIZ_MUTATING_OPS = frozenset({
    'create_op', 'import_network', 'connect_ops', 'copy_op',
    'create_annotation', 'create_extension', 'set_parameter',
    'set_op_position', 'set_dat_content', 'edit_dat_content',
    'rename_op', 'set_op_flags',
})

# Issue #57 activation gates. On TD 2025.32460 a first-of-session create_op
# wedged TD's main thread permanently (Windows AppHang 1002; dump: an
# orphaned / self-owned critical section inside TD's editor internals, GIL
# held) -- reproducible with viz ON, 7/7 clean with viz OFF. The common factor
# was viz performing EDITOR work (bot template creation, annotateCOMP copyOPs,
# selection writes, pane.owner navigation) in the SAME RefreshHook frame that
# mutated the network, on the first activation after dormancy. Two gates
# decouple those moments; both are cheap frame arithmetic:
#   - settle gate: after ANY mutating op, hold ALL editor-adjacent viz work
#     for _VIZ_MUTATION_SETTLE_FRAMES. The mutation frame completes, the GIL
#     is released between frames, and the worker delivers the MCP response
#     BEFORE any viz editor write can run.
#   - cold hold: the FIRST hop after viz dormancy does the colour ping only;
#     the bot build / camera work starts after _VIZ_COLD_HOLD_FRAMES, once
#     the editor has finished rendering (and auto-framing) the new op.
_VIZ_MUTATION_SETTLE_FRAMES = 2   # frames a mutation must settle before viz editor work
_VIZ_COLD_HOLD_FRAMES = 30        # ~0.5s @60fps of pulse-only on a cold activation

# Issue #86 relocation gate. Embot has no identity across networks: any active
# op in a DIFFERENT network tears him down and rebuilds him by copying the 9
# template parts on the MAIN THREAD -- 150-230ms in ONE frame into an off-screen
# net (blockSpawn), or 9 x 61-129ms spread over ~4.8s into a displayed one
# (assembleStep). Measured over a 6.3s synthetic mutation stream: 10 blockSpawn
# calls, 178ms average, 28% of ALL wall clock. Frame-rate measurements from the
# same harness: 41.8 fps on mixed hops (worst frame 308.8ms), 21.3 fps when
# every hop crosses a network (p90 182.1ms, worst 367.3ms), and -- the severity
# argument -- 44.1 fps at an ORDINARY 1 op / 1.5s pace (worst 226.9ms). Baseline
# with Embot off is a flat 60 fps / 16.7ms.
#
# The per-event cost is NOT reducible here: it is the editor's annotation-layer
# relayout (verified by stripping the annotate's internals to no effect), the
# work must stay on the main thread, and the block path is a documented TD
# hard-crash path. So these constants attack the RATE instead -- Embot settles
# where the work actually lives instead of chasing every network the work merely
# passes through. Reasoned from the measured pacing rows above, not measured
# directly: re-measure against the same stream when tuning.
_VIZ_NET_DWELL_S = 2.0        # seconds work must sit CONTINUOUSLY in a new net before Embot commits
                              # (deliberately above the 1.5s relaxed-pace row, so an ordinary
                              # cross-network drip is never chased)
_VIZ_RELOCATE_MIN_S = 2.5     # hard floor between relocations -- THE cost bound: at most one
                              # ~180ms event per 2.5s, i.e. ~7% of wall clock vs the measured 28%
_VIZ_NET_EVIDENCE_HOPS = 2    # pending hops already queued for a net that prove a real batch and
                              # bypass the dwell, so a genuine 10-op build in a fresh COMP is not
                              # left behind
# Starvation ceiling. The dwell clock RESTARTS whenever the candidate network
# changes, so work alternating between two networks that are both away from home
# would never accumulate a dwell and the gate would refuse FOREVER -- Embot
# frozen on a stale node while every op lights up elsewhere, with no escape hatch
# (activity keeps refreshing _viz_last_activity, so the 30s idle retire never
# fires either). That is a total loss of the feature, not a rate limit. So a
# refusal streak this long forces the next relocation through the dwell -- still
# subject to the cooldown, which remains THE cost bound. 2 x the dwell: it must
# be long enough that an ordinary cross-network drip is still not chased on every
# hop, short enough that Embot visibly follows a stream that never settles.
_VIZ_NET_STARVE_S = 2.0 * _VIZ_NET_DWELL_S
# ...and the streak has to be able to EXPIRE, or the escape hatch leaks into the
# ordinary case. _viz_relocate_blocked_since outlives the work that set it: the
# refused branch releases the follow target when the queue is empty, so the gate
# simply stops being called and the clock keeps running against wall time. One
# stray cross-network hop later (anything under the 30s idle retire, which is the
# only other thing that clears it) then arrives ALREADY past the ceiling and
# skips the dwell on first sight -- exactly the "ordinary cross-network drip"
# the dwell exists to refuse. So a gap this long between two gate calls ends the
# streak: it means viz was not being held back, it was not asking. Equal to the
# starvation window itself, deliberately -- the streak must survive every stream
# the ceiling exists to rescue (alternating work still asks every frame it has a
# target, and a drip slower than the whole ceiling is one the dwell should be
# refusing anyway).
_VIZ_NET_STREAK_GAP_S = _VIZ_NET_STARVE_S
# After a .tox write retires Embot out of a COMP (vizRetireForWrite), he may not
# re-enter that subtree for this long. Without it the retire is self-feeding:
# his nine parts are what mark the COMP dirty, so respawn -> dirty -> the next
# Update() saves it -> retire -> respawn, at MCP-call cadence and outside every
# cooldown. Equal to the relocation floor on purpose -- a retire must not be able
# to buy a spawn the ordinary gate would have refused.
_VIZ_WRITE_SUPPRESS_S = _VIZ_RELOCATE_MIN_S
# Root-level subtrees a PROJECT-WIDE purge sweep skips. /sys and /ui are
# TouchDesigner's own trees (thousands of operators, loaded from the install and
# never saved with the .toe), and the sweep runs inside the pre-save window where
# cost matters and an exception truncates the file (issue #21). A bot part cannot
# reach them: they are never an active op's parent network. Everything a user
# actually saves -- including /local and /perform -- is still swept.
_VIZ_PURGE_SKIP_ROOTS = ('/sys', '/ui')


def vizSettled(mutation_frame, frame_now) -> bool:
    """True once at least _VIZ_MUTATION_SETTLE_FRAMES have passed since the
    last mutating op. Pure -- unit-tested outside a live viz session."""
    return (frame_now - mutation_frame) >= _VIZ_MUTATION_SETTLE_FRAMES


def coldHoldElapsed(cold_since, frame_now) -> bool:
    """True once a cold activation's pulse-only hold has expired. `cold_since`
    is -1 until the first cold-tracked frame stamps it. Pure -- unit-tested."""
    return cold_since >= 0 and (frame_now - cold_since) >= _VIZ_COLD_HOLD_FRAMES


def pendingHopsIn(queue, netpath) -> int:
    """How many already-queued hops target ops in `netpath` -- the EVIDENCE that
    a real batch of work has landed in a network, as opposed to the stream merely
    touching one op there in passing. Pure: `queue` is the list of (op_path,
    caption) tuples, and the parent network is derived by string split, so this
    needs no TD access and is unit-tested outside a live session.

    Known imprecision, and why it is harmless: trackActive redirects a DOCKED DAT
    to its dock host, so a queued docked-DAT path can name a different network
    than the one Embot will actually stand in. This count is EVIDENCE only, never
    a correctness input -- a miscount can only delay a relocation by the dwell, or
    hasten one that the cooldown still bounds."""
    n = 0
    for entry in (queue or ()):
        try:
            p = entry[0]
            i = p.rfind('/')
            par = p[:i] if i > 0 else '/'
            if par == netpath:
                n += 1
        except Exception:
            continue        # malformed entry -- never raise on the hot path
    return n


def netRelocationOK(ext, netpath, queue, now) -> bool:
    """True when Embot + the camera may RELOCATE to `netpath` (issue #86).

    A PREDICATE, not a commit. It advances the candidate/starvation clocks (they
    have to persist across frames) but it deliberately does NOT stamp
    `_viz_home`: only commitRelocation does that, and trackActive calls it ONLY
    after the bot actually landed in the net. That ordering is load-bearing --
    ensureBot can still refuse a spawn (botUnsafeNet on any TDN-strategy COMP,
    botWouldBeSeen with the follow off, a write suppression), and an eager commit
    would leave `_viz_home` naming a network Embot never entered. Every later hop
    to the net he IS standing in would then be charged the full gate for a
    relocation that needs zero copyOPs -- Embot frozen on a stale node in front
    of the user, which is the failure this feature must never produce.

    Every input is an argument or a plain ext attribute -- no op(), no ui, no
    absTime -- so the whole gate is driveable from a stub and unit-tested on an
    injected clock. Five rules, cheapest first:

      - where he already is (`_viz_bot_net`) is always allowed, and so is the net
        already committed as home: neither needs a rebuild, so no rate limit can
        apply. ensureBot returns True at its first line for the former.
      - in-flight assembly blocks: the DISPLAYED-net spawn is a 9-part spread
        _VIZ_ASSEMBLE_INTERVAL frames apart (~4.8s @60fps), which is LONGER than
        the cooldown. Letting a new commit reassign the build queue mid-spread
        restarts it, so he never finishes assembling while the copies keep
        costing. The cooldown alone does not bound this; refusing while the queue
        is non-empty does (assembleTick always drains it, so the block is
        bounded by the assembly itself).
      - dwell (_VIZ_NET_DWELL_S): the work must sit CONTINUOUSLY in the new net.
        A different candidate net restarts the clock, so a stream merely passing
        through is not chased.
      - two dwell bypasses, neither of which touches the cooldown: a queued batch
        (_VIZ_NET_EVIDENCE_HOPS) proving the work has really landed somewhere,
        and a CONTINUOUS _VIZ_NET_STARVE_S refusal streak (continuous meaning
        the gate kept being asked -- a _VIZ_NET_STREAK_GAP_S gap between calls
        restarts it, so a clock left running by work that stopped cannot buy a
        later stray hop a free relocation). The streak is the escape hatch
        without which alternating work would restart the candidate clock forever
        and Embot would never move again -- see the constant.
      - cooldown (_VIZ_RELOCATE_MIN_S): a hard floor between relocations,
        checked LAST and unconditionally. Nothing bypasses it -- it is THE cost
        bound.

    Candidate bookkeeping runs BEFORE the cooldown check on purpose, so the
    worst-case relocation latency is max(dwell, cooldown), never their sum.

    Stale home: if the home COMP is deleted, _viz_home is deliberately NOT
    validated with op() (that would cost this function its TD-free property).
    `netpath` then simply never matches home, and after max(dwell, cooldown) viz
    re-commits elsewhere -- self-correcting, bounded, never wedging."""
    if netpath == ext._viz_bot_net:
        # He is standing here. ensureBot returns True at its first line without
        # copying anything, so gating this would buy nothing and cost the
        # feature.
        ext._viz_net_candidate = None
        ext._viz_relocate_blocked_since = None
        return True
    home = ext._viz_home
    if home is not None and netpath == home[0]:
        # Committed here already (he may have been retired out of it by a .tox
        # write). This branch MUST NOT touch _viz_home: re-stamping its timestamp
        # every frame would restart the cooldown forever and freeze Embot in
        # place permanently (test_same_net_never_restamps_home).
        ext._viz_net_candidate = None
        ext._viz_relocate_blocked_since = None
        return True
    if home is None:
        # First appearance is NEVER delayed -- Embot shows up immediately.
        ext._viz_net_candidate = None
        ext._viz_relocate_blocked_since = None
        return True
    if ext._viz_bot_build_queue:
        # Still assembling -- see above. Checked BEFORE the starvation clock is
        # stamped on purpose: "the gate has been refusing to follow the work"
        # must not include "he was busy building himself", or a 4.8s spread would
        # arrive at its own finish line already starved and immediately buy
        # another one.
        return False
    # Starvation counts a CONTINUOUS refusal streak, so the clock restarts when
    # the gate has not been asked for _VIZ_NET_STREAK_GAP_S -- see the constant.
    # getattr, not attribute access: envoy_viz is a module DAT that hot-reloads
    # on its own, so it can run for a few frames against an EnvoyExt instance
    # built before this field existed, and an AttributeError here would take the
    # whole viz tick down.
    last_blocked = getattr(ext, '_viz_relocate_blocked_last', None)
    if (ext._viz_relocate_blocked_since is None or last_blocked is None
            or (now - last_blocked) > _VIZ_NET_STREAK_GAP_S):
        ext._viz_relocate_blocked_since = now
    ext._viz_relocate_blocked_last = now      # only read while ..._since is set
    cand = ext._viz_net_candidate
    if cand is None or cand[0] != netpath:
        cand = (netpath, now)
        ext._viz_net_candidate = cand
    dwelled = (now - cand[1]) >= _VIZ_NET_DWELL_S
    batch = pendingHopsIn(queue, netpath) >= _VIZ_NET_EVIDENCE_HOPS
    starved = (now - ext._viz_relocate_blocked_since) >= _VIZ_NET_STARVE_S
    if not dwelled and not batch and not starved:
        return False
    if (now - home[1]) < _VIZ_RELOCATE_MIN_S:
        return False
    ext._viz_relocate_blocked_since = None
    return True


def commitRelocation(ext, netpath, now) -> None:
    """Stamp `netpath` as viz's home -- the ONLY writer of `_viz_home`, called by
    trackActive only once Embot has actually landed there (issue #86). Keeping
    the commit downstream of the spawn is what preserves the invariant
    `_viz_home == _viz_bot_net`, and with it blockSpawn's cost bound: a spawn
    happens if and only if a commit does.

    Re-committing the SAME net is a deliberate no-op. Re-stamping the timestamp
    every frame while he stands at home would restart the cooldown forever and
    freeze him in place permanently (test_same_net_never_restamps_home)."""
    home = ext._viz_home
    if home is not None and home[0] == netpath:
        return
    ext._viz_home = (netpath, now)
    ext._viz_net_candidate = None
    ext._viz_relocate_blocked_since = None


def spawnWouldBeSeen(displayed, follow_on, takeover_active, has_neteditor) -> bool:
    """Decision core of botWouldBeSeen (issue #86), split out so its truth table
    is testable without faking ui.panes. A spawn is worth paying for when the
    destination is already displayed, or when the camera is about to navigate
    into it -- which requires the follow to be ON, a network-editor pane to
    exist, and the user's takeover window to be closed. Pure."""
    if displayed:
        return True
    return bool(follow_on and has_neteditor and not takeover_active)


def pathInsideSubtree(netpath, root_path) -> bool:
    """True if `netpath` is `root_path` or lives underneath it. Used by
    vizRetireForWrite to fire ONLY when the COMP about to be serialized actually
    contains Embot. Pure; guards the classic prefix trap ('/ab' is NOT inside
    '/a') by comparing against root_path + '/'."""
    if not netpath or not root_path:
        return False
    if netpath == root_path:
        return True
    if root_path == '/':
        return True
    return netpath.startswith(root_path + '/')


def noteWriteRetire(ext, path, now) -> None:
    """Remember that the COMP at `path` was just serialized with Embot retired
    out of it, so no spawn may re-enter that subtree for _VIZ_WRITE_SUPPRESS_S
    (issue #86). Expired entries are pruned here -- the map only ever holds the
    COMPs written in the last couple of seconds. Pure w.r.t. TD."""
    sup = ext._viz_write_suppress
    for k in [k for k, until in sup.items() if until <= now]:
        sup.pop(k, None)
    sup[path] = now + _VIZ_WRITE_SUPPRESS_S


def writeSuppressed(ext, netpath, now) -> bool:
    """True while `netpath` sits inside a subtree that was just written to a
    .tox with Embot retired out of it (issue #86).

    Without this the retire is SELF-FEEDING: his nine annotateCOMPs are what mark
    the COMP dirty, so respawn -> dirty -> the next Update() calls Save() ->
    retire -> respawn. dirtyHandler saves on every Update(), and Update() fires
    from every auto-externalizing MCP call, so that loop runs at MCP-call cadence
    and -- because the respawn goes to a net `_viz_home` already names -- outside
    the relocation cooldown entirely. It would re-create the exact 150-230ms
    main-thread event this whole gate exists to bound, plus a .tox rewrite and a
    Build increment per cycle."""
    try:
        for root_path, until in ext._viz_write_suppress.items():
            if now < until and pathInsideSubtree(netpath, root_path):
                return True
    except Exception:
        pass
    return False


def noteVizActivity(ext, operation: str, params: dict, result) -> None:
    """Enqueue the op Envoy just acted on as a follow hop and stamp the activity
    time. Hot path -- called for every sub-op of a batch (all in one frame), so
    it must ENQUEUE rather than overwrite: the pump steps Embot through the hops
    one at a time. Consecutive touches of the SAME op (e.g. create_op then
    set_op_position on it) collapse into one hop, refining the caption. Never
    raises."""
    try:
        if operation not in _VIZ_MUTATING_OPS:
            return
        # Issue #57 settle gate: stamp the mutation frame FIRST, even if the
        # target fails to resolve below -- the network still mutated this
        # frame, so viz editor work must hold off either way.
        ext._viz_mutation_frame = absTime.frame
        target = ext._resolveActiveOp(operation, params, result)
        if not target:
            return
        caption = ext._actionText(operation, target)
        ext._viz_last_activity = absTime.seconds
        q = ext._viz_target_queue
        # Collapse against the WHOLE pending queue, not just the last entry: a
        # whole batch enqueues before the pump pops anything, so create_op +
        # set_op_position + the later connect_ops that all touch one node fold
        # into its single pending hop (latest caption wins) -- no backtracking
        # to an op he already stepped past. Once a hop is popped it leaves the
        # queue, so a genuinely later touch correctly re-hops.
        for i, (p, _c) in enumerate(q):
            if p == target:
                q[i] = (target, caption)
                break
        else:
            q.append((target, caption))
            if len(q) > _VIZ_QUEUE_CAP:
                del q[0]                      # bound the backlog; oldest gives way
    except Exception:
        pass


def vizTick(ext) -> None:
    """Once-per-frame visualization driver (after the drain loop): retire
    artifacts when idle/disabled/saving, advance the colour pulse + bot dance,
    and follow the active op. Fully guarded -- never breaks the refresh loop."""
    try:
        # Perform mode or the save window: tear everything down so nothing can
        # bake into the .toe (belt-and-suspenders with onProjectPreSave).
        if getattr(ext.ownerComp.ext.Embody, '_performMode', False):
            vizCleanup(ext)
            return
        if ext.ownerComp.fetch('_suppress_dialogs', False, search=False):
            vizCleanup(ext)
            return
        show_bot = ext.ownerComp.par.Embotenable.eval()   # render the character
        follow = ext.ownerComp.par.Envoyfollow.eval()     # camera tracks the active op
        if not show_bot and not follow:
            vizCleanup(ext)
            return
        now = absTime.seconds
        # Quiet for a while -> retire the bot + restore any pulse.
        if ext._viz_last_activity and (now - ext._viz_last_activity) > _VIZ_IDLE_S:
            vizCleanup(ext)
            ext._viz_target_op = None
            return
        pulseTick(ext, now)
        # Issue #57 settle gate: within the settle window of a mutating op,
        # do NOTHING editor-adjacent -- no hop pump, no bot template build,
        # no copyOPs spawn/assembly, no selection writes, no pane navigation.
        # The pulse fade above is an op-colour write (same class as the build
        # op itself) and stays live so pulses never stall mid-fade.
        if not vizSettled(ext._viz_mutation_frame, absTime.frame):
            return
        vizPumpQueue(ext, now)
        if ext._viz_target_op:
            trackActive(ext, now, follow, show_bot)
        if show_bot:
            cleanupDeadBots(ext)   # tear down a left-behind bot off-screen
            assembleTick(ext)      # copy one template part per frame (no freeze)
            botDance(ext, now)
        elif ext._viz_bot_net:
            destroyBot(ext)        # camera-only: ensure no character lingers
    except Exception as e:
        try:
            ext._log(f'Viz tick skipped: {type(e).__name__}: {e}', 'DEBUG')
        except Exception:
            pass


def vizPumpQueue(ext, now: float) -> None:
    """Advance through queued hops one at a time so Embot visibly STEPS from node
    to node -- a batch enqueues many in a single frame, and without this he would
    only ever appear on the last. Each hop is held for a dwell (>= the jump, so it
    lands before the next begins); the dwell shrinks as the backlog grows so he
    races to catch a fat batch, but never skips an op."""
    q = ext._viz_target_queue
    if not q or now < ext._viz_hop_until:
        return
    path, caption = q.pop(0)
    ext._viz_target_op = path
    ext._viz_action_text = caption
    dwell = _VIZ_HOP_DWELL - 0.05 * len(q)   # deeper backlog -> quicker steps
    ext._viz_hop_until = now + (dwell if dwell > _VIZ_HOP_MIN
                                else _VIZ_HOP_MIN)


def trackActive(ext, now: float, follow: bool, show_bot: bool) -> None:
    """For the active op: stand Embot on it (if show_bot / Embotenable) and pan the
    network editor to it (if follow / Envoyfollow). Independent -- the camera frames
    the OP itself, so it follows Envoy's work whether or not the character renders."""
    target = op(ext._viz_target_op) if ext._viz_target_op else None
    if not target or not target.valid:
        ext._viz_target_op = None
        return
    # A docked DAT (e.g. a callbacks DAT) renders attached to its host even
    # though its own nodeX/nodeY is elsewhere -- stand on the HOST (the op you
    # actually see). The speech bubble still names the real op.
    try:
        if target.dock is not None:
            target = target.dock
    except Exception:
        pass
    net = target.parent()
    if net is None:
        return
    # Issue #57 cold hold: the first hop after viz dormancy pings the node
    # colour ONLY. Bot template creation, copyOPs assembly, selection writes
    # and pane navigation begin once the hold elapses -- after the editor has
    # finished rendering (and auto-framing) the freshly-created op, decoupled
    # from the mutation moment that wedged TD 2025.32460 (issue #57).
    if not ext._viz_session_warm:
        if ext._viz_cold_since < 0:
            ext._viz_cold_since = absTime.frame
        pulseStart(ext, target, now)
        if not coldHoldElapsed(ext._viz_cold_since, absTime.frame):
            return
        ext._viz_session_warm = True
    # Issue #86 relocation gate. Embot + the camera relocate together only once
    # the work has genuinely SETTLED in a new network (or a queued batch proves
    # it), and never more often than _VIZ_RELOCATE_MIN_S. Holding them TOGETHER
    # is load-bearing: placeBot/ensureBot must stay ahead of navigateAndFrame so
    # a to-be-visited net is still OFF-SCREEN at spawn time and takes the cheap,
    # crash-safe blockSpawn path. Gating only the bot would let the pane cut land
    # first and push every eventual spawn onto the displayed-net spread
    # (9 x 61-129ms). Camera-only users (show_bot False) skip the gate entirely
    # -- with no bot there is no copyOPs to bound, and the measured camera-only
    # row is degraded, not frozen.
    #
    # The pending state is deliberately IDENTICAL to the cold hold above: pulse
    # the node colour, return. vizPumpQueue still drains hops meanwhile, so
    # _viz_action_text keeps advancing and Embot keeps narrating the current op
    # from where he stands -- he does not go silent, he just does not chase. And
    # the refusal is never permanent: _VIZ_NET_STARVE_S forces the move through
    # once the gate has been refusing for long enough, so work that never settles
    # anywhere still ends up with Embot standing in it.
    #
    # Note the asymmetry with the block below: the gate REFUSING parks the camera
    # too (they must move together, see above), but a gate that ALLOWS and a
    # spawn that then refuses does NOT -- the camera follows on, because with no
    # rebuild to bound there is nothing to gate.
    if show_bot and not netRelocationOK(ext, net.path, ext._viz_target_queue, now):
        pulseStart(ext, target, now)
        # RELEASE the follow target exactly as glideStep does once it has caught
        # up. Without this the gated path never reaches glideStep, so
        # _viz_target_op stays set, trackActive re-runs every frame, and
        # pulseStart re-arms the moment pulseTick's 0.45s fade clears it -- a
        # node in a network nobody is viewing strobing forever, with a colour
        # write every frame. The queue check matters: mid-batch there IS more to
        # visit, and the next pump replaces the target anyway.
        if not ext._viz_target_queue:
            ext._viz_target_op = None
        return
    # --- the character (Embotenable) ---
    if show_bot:
        pulseStart(ext, target, now)    # ping the node colour
        placeBot(ext, net, target, now) # bring the dancing bot to the op
        # Commit only if he ACTUALLY landed. ensureBot can still refuse
        # (botUnsafeNet on a TDN-strategy COMP, botWouldBeSeen with the follow
        # off, a write suppression); committing anyway would point _viz_home at
        # a net he never entered and charge every later hop to the net he IS in
        # for a relocation that costs nothing. See netRelocationOK.
        if ext._viz_bot_net == net.path:
            commitRelocation(ext, net.path, now)
    # --- the camera (Envoyfollow) -- frames the op, bot-independent ---
    if not follow:
        return
    # First time we follow in this network, establish our wide _VIZ_ZOOM (once,
    # applied by _glideStep). The glide otherwise only PANS, so if the pane sat
    # at a tight zoom the follow would track him at that tight zoom.
    if net.path != ext._viz_follow_net:
        ext._viz_follow_net = net.path
        ext._viz_zoom_pending = True
    highlightOp(ext, target)             # mark Envoy's focus (changes selection ->
                                         # only when actually following)
    pane, navigate = pickFollowPane(ext, net)
    if pane is None:
        return
    if navigate:
        navigateAndFrame(ext, pane, net, target)
    else:
        glideStep(ext, pane, target)


def pickFollowPane(ext, net: 'COMP'):
    """Choose the pane to follow `net` in, and whether it must be navigated.
    Prefers a network-editor pane already showing `net` (-> glide); else the
    current/first network-editor pane (-> navigate into net). Returns
    (pane, navigate_bool), or (None, False) if the user has taken over."""
    try:
        neteditors = [p for p in ui.panes
                      if str(p.type) == 'PaneType.NETWORKEDITOR']
        if not neteditors:
            return None, False
        netpath = net.path
        pane = next((p for p in neteditors
                     if p.owner is not None and p.owner.path == netpath), None)
        navigate = False
        if pane is None:
            cur_id = ui.panes.current.id
            pane = next((p for p in neteditors if p.id == cur_id), neteditors[0])
            navigate = True
        if userTookOver(ext, pane):
            return None, False
        return pane, navigate
    except Exception:
        return None, False


def userTookOver(ext, pane) -> bool:
    """True only while the user has deliberately navigated the pane to a DIFFERENT
    network -- then we briefly yield it to them. Pan/zoom changes are deliberately
    IGNORED: TD auto-frames (pans + zooms into) a freshly-spawned node, a change we
    did NOT make, and treating that as 'the user took over' froze the follow for
    ~6s while Embot raced off -- the camera then snapped to the last node instead
    of ever tracking him. Following him beats honouring a transient auto-frame; a
    real owner change (the user clicking into another network) still yields."""
    now = absTime.seconds
    cur = viewTuple(ext, pane)               # (id, owner, x, y, zoom)
    if now < ext._viz_settle_until:
        ext._viz_last_view = cur             # our navigate is still settling -> adopt
        return False
    lv = ext._viz_last_view
    if lv and lv[0] == cur[0] and lv[1] != cur[1]:   # OWNER changed -> user navigated away
        ext._viz_takeover_until = now + _VIZ_TAKEOVER_S
    ext._viz_last_view = cur                 # always re-baseline (no stale pan/zoom compare)
    return now < ext._viz_takeover_until


def navigateAndFrame(ext, pane, net: 'COMP', target: 'OP') -> None:
    """Cut `pane` into `net` and SNAP to frame `target` (coordinate spaces
    differ across networks, so gliding from the old view is meaningless).
    Releases the target -- subsequent same-network ops glide from here."""
    # Set ONLY the owner here. pane.x/pane.y/zoom set in the same frame as the
    # owner change do NOT stick (the pane is mid-navigation), and the stale
    # values then misfired takeover and froze the follow. Owner alone sticks;
    # we do NOT clear the target, so the glide -- which runs in-network on the
    # following frames, where pan writes DO stick -- pans to the target.
    pane.owner = net
    recordView(ext, pane)
    ext._viz_settle_until = absTime.seconds + 0.4
    # TD auto-frames the new (often near-empty) network on the owner change,
    # which zooms WAY in. Re-apply our wide _VIZ_ZOOM on the next frame -- setting
    # it here (same frame as owner) would not stick.
    ext._viz_zoom_pending = True


def glideStep(ext, pane, target: 'OP') -> None:
    """One frame of an ease toward the active OP's standing point -- the spot where
    Embot stands (op centre-x, top edge), computed from the OP so the camera follows
    whether or not the character is rendered. `target` is the CURRENT pump op (the
    one the bot is on), not a stale queue entry. Pan only; releases the pane once it
    has caught the op and nothing is left queued."""
    if ext._viz_zoom_pending:
        try:
            pane.zoom = _VIZ_ZOOM   # undo TD's auto-frame zoom-in (once, sticks now)
        except Exception:
            pass
        ext._viz_zoom_pending = False
    cx = target.nodeX + target.nodeWidth / 2.0
    cy = target.nodeY + target.nodeHeight + botFootGap(ext)   # Embot's standing centre
    dx = cx - pane.x
    dy = cy - pane.y
    if abs(dx) < _VIZ_EPS and abs(dy) < _VIZ_EPS:
        pane.x = cx
        pane.y = cy
        if not ext._viz_target_queue:   # on him AND nothing left to build/visit
            ext._viz_target_op = None   # -> release the pane to the user
    else:
        pane.x = pane.x + dx * _VIZ_EASE
        pane.y = pane.y + dy * _VIZ_EASE
    # Pan only -- zoom is set once on navigate. Easing zoom per-frame made the
    # read-back jitter trip _userTookOver, freezing the follow.
    recordView(ext, pane)


def highlightOp(ext, target: 'OP') -> None:
    """Select + make-current the op being worked, so Envoy's focus is visibly
    marked. Only deselects the op WE previously highlighted -- the user's own
    selections elsewhere are left alone. Best-effort; never raises."""
    try:
        # Issue #86: already marked -> at most re-assert the marker. trackActive
        # runs EVERY frame while a follow target is set, so unconditionally
        # rewriting .selected + .current hit a node in the DISPLAYED network 60
        # times a second -- the same class of redundant editor write the freeze
        # measurements implicate.
        #
        # But the cache must not be trusted blindly: _viz_selected_op described
        # a selection the user can drop (clicking empty canvas) and a path that
        # can be deleted and recreated, and a bare early return then left
        # Envoy's focus marker silently missing for that op for the rest of the
        # session. So VERIFY .selected and re-assert it when it has genuinely
        # been lost -- a no-op read on the common path.
        #
        # .current is deliberately NOT re-asserted here: there is exactly one
        # current op per network, so re-taking it every frame is precisely the
        # fight-the-user behaviour this early return exists to stop. It is set
        # once, when we first target the op.
        if ext._viz_selected_op == target.path:
            if not target.selected:
                target.selected = True
            return
        prev = ext._viz_selected_op
        if prev and prev != target.path:
            po = op(prev)
            if po and po.valid:
                po.selected = False
        target.selected = True
        target.current = True
        ext._viz_selected_op = target.path
    except Exception:
        pass


# --- colour pulse on the active op ---

def pulseStart(ext, target: 'OP', now: float) -> None:
    """Begin a colour pulse on `target` (snapshot its colour first). No-op if
    we are already pulsing this op."""
    if ext._viz_pulse_op == target.path:
        return
    restorePulse(ext)
    try:
        ext._viz_pulse_orig = tuple(target.color)
        ext._viz_pulse_op = target.path
        ext._viz_pulse_start = now
    except Exception:
        ext._viz_pulse_op = None


def pulseTick(ext, now: float) -> None:
    """Fade the active pulse from the accent colour back to the op's original."""
    if not ext._viz_pulse_op:
        return
    o = op(ext._viz_pulse_op)
    if not o or not o.valid:
        ext._viz_pulse_op = None
        return
    t = (now - ext._viz_pulse_start) / _VIZ_PULSE_S
    if t >= 1.0:
        restorePulse(ext)
        return
    ac = _VIZ_PULSE_COLOR
    og = ext._viz_pulse_orig or (0.67, 0.67, 0.67)
    k = 1.0 - t   # accent weight fades to 0
    try:
        o.color = (og[0] + (ac[0] - og[0]) * k,
                   og[1] + (ac[1] - og[1]) * k,
                   og[2] + (ac[2] - og[2]) * k)
    except Exception:
        restorePulse(ext)


def restorePulse(ext) -> None:
    """Restore the pulsing op's original colour and clear pulse state."""
    p = ext._viz_pulse_op
    if p and ext._viz_pulse_orig is not None:
        o = op(p)
        if o and o.valid:
            try:
                o.color = ext._viz_pulse_orig
            except Exception:
                pass
    ext._viz_pulse_op = None
    ext._viz_pulse_orig = None


# --- the dancing builder-bot (ephemeral annotation) ---

def placeBot(ext, net: 'COMP', target: 'OP', now: float) -> None:
    """Ensure the figure exists in `net` and set its destination so it STANDS
    on top of the active op (feet on the node's top edge). A new node triggers
    a hop; a network change snaps. Motion + colour come from _botDance."""
    prev_net = ext._viz_bot_net
    # Compute + publish the standing point BEFORE ensureBot (issue #86): a
    # blockSpawn needs it to lay the nine copies out as a FIGURE on arrival
    # rather than leaving them piled on the template's parked coordinates and
    # waiting for botDance to arrange them. Publishing early is safe -- the only
    # other reader, startEntrance, is reachable only after a spawn that this same
    # call re-stamps.
    dest = (target.nodeX + target.nodeWidth / 2.0,
            target.nodeY + target.nodeHeight + botFootGap(ext))
    ext._viz_bot_dest = dest           # current op standing point (swoop target)
    if not ensureBot(ext, net):
        return
    if ext._viz_bot_pos is None or prev_net != ext._viz_bot_net:
        ext._viz_jump_dur = _VIZ_JUMP_DUR
        if ext._viz_bot_build_queue:
            # ON-SCREEN spread spawn: assemble at an off-view staging point (just past
            # the viewport edge) so each annotate copy renders OUTSIDE the viewport
            # (~100ms vs ~280ms in-view -> a far shallower fps sag). He swoops in once
            # whole -- the entrance is fired from _assembleTick when the queue drains.
            stage = (dest[0] + stageOffset(ext, net), dest[1])
            ext._viz_bot_stage = stage
            ext._viz_bot_pos = stage
            ext._viz_bot_from = stage
            ext._viz_bot_target = stage
            ext._viz_bot_pending_entrance = True
        else:
            # off-screen (dive) block spawn -- already cheap -> snap onto the op
            ext._viz_bot_pos = dest
            ext._viz_bot_from = dest
            ext._viz_bot_target = dest
            ext._viz_bot_pending_entrance = False
        ext._viz_bot_jump_t0 = now - ext._viz_jump_dur   # already standing
        return
    if ext._viz_bot_build_queue:
        return                          # still assembling off-view -> hold at staging
    if dest != ext._viz_bot_target:
        ext._viz_jump_dur = _VIZ_JUMP_DUR
        ext._viz_bot_from = ext._viz_bot_pos    # hop from where we are now
        ext._viz_bot_target = dest
        ext._viz_bot_jump_t0 = now


def stageOffset(ext, net: 'COMP') -> float:
    """Network-units to the RIGHT of the active op to park Embot while he assembles,
    so his per-part copies render OUTSIDE the viewport (cheap) instead of inside it.
    Derived from the viewing pane's zoom so it always clears the right edge; falls
    back to a generous fixed value if no pane is found."""
    try:
        for p in ui.panes:
            if str(p.type) == 'PaneType.NETWORKEDITOR' and \
                    p.owner is not None and p.owner.path == net.path:
                return (ui.windowWidth / 2.0) / max(p.zoom, 0.05) + _VIZ_STAGE_MARGIN
    except Exception:
        pass
    return 3000.0


def botFootGap(ext) -> float:
    """Distance from the figure centre down to its feet, so it stands with
    feet on the node's top edge."""
    return max(h / 2.0 - oy for (_s, _ox, oy, _w, h, _e) in _VIZ_BOT_PARTS)


def ensureTemplate(ext):
    """Build (once) and return Embot's source template -- a parked container in
    the Embody COMP holding the 9 styled annotation parts. annotateCOMP creation
    is ~90ms each, so the ~1s to build all of them is paid ONCE here (and it bakes
    into Embody on save, so shipped builds never pay it at all). Every COMP switch
    then just copyOPs the parts forward -- far cheaper than recreating them. The
    template lives inside Embody on purpose: it is a saved static asset, never an
    animated/live bot, so _botUnsafeNet (which forbids a LIVE bot here) is moot.

    Issue #86: this is also where the template's staging-position leak heals.
    assembleStep parks the SOURCE part off-view before copying it, and a source
    left parked there is exported into Embody.tdn (all nine parts were committed
    at a leaked [1860, 251]). Both paths below pin every part to
    _VIZ_TEMPLATE_PARK -- on create, and on the reuse path when it has drifted --
    so the committed drift self-heals on the next spawn + export, with no hand
    edit of the .tdn, and any future leak of the same class is absorbed too."""
    try:
        host = ext.ownerComp
        tmpl = host.op(_VIZ_TEMPLATE_COMP)
        if tmpl and tmpl.op(_VIZ_BOT_PREFIX + 'body') and \
                tmpl.op(_VIZ_BOT_PREFIX + 'speech'):
            # Self-heal a drifted park: cheap reads, and only on a spawn. Write
            # ONLY when it differs, so steady state stays write-free.
            names = [s for (s, _ox, _oy, _w, _h, _e) in _VIZ_BOT_PARTS]
            names.append('speech')
            for suffix in names:
                p = tmpl.op(_VIZ_BOT_PREFIX + suffix)
                if p and p.valid and \
                        (p.nodeX, p.nodeY) != _VIZ_TEMPLATE_PARK:
                    try:
                        p.nodeX, p.nodeY = _VIZ_TEMPLATE_PARK
                    except Exception:
                        pass
            return tmpl
        if tmpl:
            tmpl.destroy()                  # partial/stale -> rebuild clean
        ext._crashTrace('ensureTemplate BUILD (creating annotateCOMPs)')
        tmpl = host.create(baseCOMP, _VIZ_TEMPLATE_COMP)
        tmpl.nodeX, tmpl.nodeY = -1400, -1400   # parked out of the way
        skin = colorsys.hsv_to_rgb(_VIZ_COOL_HUE, 0.95, 1.0)  # default cool
        for (suffix, ox, oy, w, h, is_eye) in _VIZ_BOT_PARTS:
            p = tmpl.create(annotateCOMP)
            p.name = _VIZ_BOT_PREFIX + suffix
            p.selected = False
            p.par.Mode = 'networkbox'
            p.par.Titletext = ''
            p.par.Bodytext = ''
            try:
                p.par.Titleheight = 0       # minimal box -- no text header
            except Exception:
                pass
            p.par.Backcoloralpha = 1.0
            if is_eye:
                p.par.Backcolorr, p.par.Backcolorg, p.par.Backcolorb = 0.0, 0.0, 0.0
            else:
                p.par.Backcolorr, p.par.Backcolorg, p.par.Backcolorb = skin
            p.nodeWidth = w
            p.nodeHeight = h
            p.nodeX, p.nodeY = _VIZ_TEMPLATE_PARK   # born canonical (issue #86)
        sp = tmpl.create(annotateCOMP)      # the speech bubble (titled)
        sp.name = _VIZ_BOT_PREFIX + 'speech'
        sp.selected = False
        sp.par.Titletext = 'Embot'
        sp.par.Bodytext = ''
        sp.par.Backcolorr = 0.12
        sp.par.Backcolorg = 0.12
        sp.par.Backcolorb = 0.17
        sp.par.Backcoloralpha = 0.95
        sp.par.Bodyfontsize = 11
        sp.nodeWidth = 185
        sp.nodeHeight = 74
        sp.nodeX, sp.nodeY = _VIZ_TEMPLATE_PARK     # born canonical (issue #86)
        return tmpl
    except Exception:
        return None


def ensureBot(ext, net: 'COMP') -> bool:
    """Ensure Embot is present (or assembling) in `net`. On a network change he is
    COPIED from the template ONE PART PER FRAME (see _assembleTick) rather than in
    a single block copyOPs. This per-frame spread is the version that ran stably
    for hours; the block copy that replaced it was implicated in repeated TD
    crashes and was reverted. Returns False where a bot must not live."""
    netpath = net.path
    if ext._viz_bot_net == netpath:
        return True                         # already here (assembled or assembling)
    # Issue #86: a COMP that was JUST serialized with him retired out of it is
    # off limits briefly -- his own parts are what re-dirty it, so re-entering
    # immediately means the next Update() saves, retires and respawns again, at
    # MCP-call cadence. Checked FIRST: it is a dict lookup, cheaper than either
    # gate below. See writeSuppressed.
    if writeSuppressed(ext, netpath, absTime.seconds):
        return False
    # Issue #86: never build 9 annotateCOMPs into a network nobody is looking at
    # and nobody is about to look at (follow OFF with the user parked elsewhere,
    # or inside the 6s takeover window). It sits AFTER the "already here" return,
    # so a bot that already exists keeps tracking normally when the user
    # navigates away -- only NEW spawns are suppressed. It MUST precede
    # botUnsafeNet, which reaches EmbodyExt._getTDNPaths() ->
    # _getTDNStrategyComps(): a full externalizations-table scan with a per-row
    # op() plus an exclude-tag lookup. In the suppressed state ensureBot runs its
    # prefix EVERY frame, so the wrong order would add a per-frame table scan.
    #
    # Invariant this creates (botWritesNeeded relies on it): a blockSpawn now
    # happens only when the destination is off-screen AND the camera is about to
    # navigate into it in the SAME frame -- ensureBot -> blockSpawn -> placeBot
    # sets pos -> navigateAndFrame sets pane.owner. blockSpawn lays the parts out
    # as a figure on arrival (from _viz_bot_dest, published by placeBot before
    # this call), so nothing depends on botDance getting a writing frame first
    # and there is no "was it arranged yet" flag -- the user never sees a pile.
    if not botWouldBeSeen(ext, net):
        return False
    if botUnsafeNet(ext, net):
        return False
    ext._crashTrace('ensureBot NET-CHANGE %s -> %s' % (ext._viz_bot_net, netpath))
    if ensureTemplate(ext) is None:
        return False
    # Defer teardown of the bot we're LEAVING (destroying ops from an on-screen net
    # forces a redraw per op); tear it down a frame later, off-screen.
    if ext._viz_bot_net and ext._viz_bot_net != netpath:
        ext._viz_bot_pending_cleanup.add(ext._viz_bot_net)
    ext._viz_bot_pending_cleanup.discard(netpath)   # re-entering -> keep its parts
    ext._viz_bot_pos = None
    ext._viz_bot_from = None
    ext._viz_bot_target = None
    ext._viz_bot_net = netpath
    ext._viz_last_skin = None              # force a recolour onto the new parts
    # FAST + SAFE spawn. A single copyOPs of all 9 parts HARD-CRASHES TD when the
    # target net is ON-SCREEN (instantiating many annotateCOMPs concurrent with the
    # editor redraw -- pinpointed via crash trace: TD died inside copyOPs). But it
    # is crash-free AND ~4x faster into an OFF-SCREEN net. _ensureBot runs BEFORE
    # the follow's navigate, so a net we are about to dive into is still off-screen
    # here -> block-copy it. Only when the net is already displayed do we fall back
    # to the per-frame spread (slower, but safe on a live net).
    if netIsDisplayed(ext, net):
        # net ON-SCREEN: spaced spread. A single block copyOPs into a displayed net
        # crashes TD; the owner-swap that dodged the crash broke the pane's render
        # (owning the project root). So we copy ONE part at a time, but spaced
        # _VIZ_ASSEMBLE_INTERVAL frames apart (not every frame) so the per-part redraw
        # hitches stay isolated instead of fusing into a freeze. Order is body/head/
        # speech first (recognizable immediately), then limbs, then eyes.
        valid = {s for (s, _ox, _oy, _w, _h, _e) in _VIZ_BOT_PARTS}
        valid.add('speech')
        ext._viz_bot_build_queue = [_VIZ_BOT_PREFIX + s
                                    for s in _VIZ_ASSEMBLE_ORDER if s in valid]
        # Copy nothing yet -- _placeBot (runs right after this, same frame) computes the
        # off-view staging point, then _assembleTick copies the parts there. Copying
        # part #1 here would land it in-view (staging not set) and pay the full cost.
        ext._viz_assemble_next_frame = absTime.frame
    else:
        # net OFF-SCREEN (about to navigate into it): ONE fast block copyOPs.
        ext._viz_bot_build_queue = []
        blockSpawn(ext, net)
    return True


def netIsDisplayed(ext, net: 'COMP') -> bool:
    """True if any network-editor pane currently shows `net` -- i.e. a block copy
    into it would redraw the editor and crash TD. Called BEFORE the follow's
    navigate, so a net we are about to dive into reads False (still off-screen).
    Any doubt -> True, so we take the safe spread path."""
    try:
        np = net.path
        for p in ui.panes:
            if str(p.type) == 'PaneType.NETWORKEDITOR' and \
                    p.owner is not None and p.owner.path == np:
                return True
    except Exception:
        return True
    return False


def botWouldBeSeen(ext, net: 'COMP') -> bool:
    """True if spawning Embot into `net` would actually be VISIBLE to the user --
    either the net is displayed now, or the camera follow is live and about to
    navigate into it (issue #86). Gathers the four live readings and hands them
    to the pure spawnWouldBeSeen.

    Reads _viz_takeover_until DIRECTLY and deliberately does NOT call
    pickFollowPane: that would re-baseline _viz_last_view and re-arm takeover
    detection as a side effect of a read.

    Any exception returns True (fail-open, mirroring netIsDisplayed) so a bug
    here can only ever cost performance -- never Embot's visibility, which is the
    whole point of the feature."""
    try:
        displayed = netIsDisplayed(ext, net)
        follow_on = bool(ext.ownerComp.par.Envoyfollow.eval())
        takeover_active = absTime.seconds < ext._viz_takeover_until
        has_neteditor = any(str(p.type) == 'PaneType.NETWORKEDITOR'
                            for p in ui.panes)
        return spawnWouldBeSeen(displayed, follow_on, takeover_active,
                                has_neteditor)
    except Exception:
        return True


def botWritesNeeded(ext, net: 'COMP') -> bool:
    """True if botDance should perform its TD writes this frame (issue #86). The
    figure's STATE always advances; only the editor writes are skipped. Off-view
    during the spread assembly he is a pile at the staging point that nobody can
    see, and an assembly runs _VIZ_ASSEMBLE_INTERVAL x 9 frames (~4.8s @60fps) --
    that is ~145 full-figure repaints of 9 annotates delivering zero delight."""
    if ext._viz_bot_pending_entrance:
        return False          # parked off-view at the staging point mid-assembly
    return netIsDisplayed(ext, net)


def blockSpawn(ext, net: 'COMP') -> None:
    """Copy ALL 9 parts into `net` in ONE copyOPs (~180ms, one frame -- vs the
    ~9-frame, ~464ms spread). ONLY called by _ensureBot when `net` is OFF-SCREEN
    (a sub-COMP we are about to navigate into): copyOPs of many annotateCOMPs into
    a DISPLAYED net hard-crashes TD (the editor redraw -- pinpointed via crash
    trace), and the off-screen owner-swap that once dodged that crash broke the
    pane render, so displayed nets use the safe spread instead. Clears orphans;
    colours on arrival.

    Issue #86 cost bound -- DO NOT BREAK: a spawn (this, or a spread queue) is
    reachable ONLY from ensureBot's net-change branch, which is reachable only
    from placeBot, which is reachable only from trackActive AFTER
    netRelocationOK allowed the move -- and trackActive commits _viz_home
    immediately after, if and only if the spawn actually happened. When
    netpath == _viz_bot_net ensureBot returns without copying. So spawn <-> net
    change <-> commit, and every commit is stamped under a
    >= _VIZ_RELOCATE_MIN_S test.

    The honest bound is once per max(_VIZ_RELOCATE_MIN_S, assembly time), not
    flatly once per _VIZ_RELOCATE_MIN_S: the DISPLAYED-net path is a 9-part
    spread _VIZ_ASSEMBLE_INTERVAL frames apart (~4.8s @60fps), longer than the
    cooldown, which is why netRelocationOK also refuses while
    _viz_bot_build_queue is non-empty. Off-screen (this function) the whole
    spawn is one frame, so there the cooldown alone is the bound. Any future
    edit that adds a spawn path not gated by a commit breaks it."""
    tmpl = ensureTemplate(ext)
    if tmpl is None:
        return
    for c in list(net.children):            # clear orphans
        if c.name.startswith(_VIZ_BOT_PREFIX) and c.valid:
            try:
                c.destroy()
            except Exception:
                pass
    srcs = [tmpl.op(_VIZ_BOT_PREFIX + s)
            for (s, _ox, _oy, _w, _h, _e) in _VIZ_BOT_PARTS]
    srcs.append(tmpl.op(_VIZ_BOT_PREFIX + 'speech'))
    srcs = [s for s in srcs if s]
    try:
        ext._crashTrace('blockSpawn COPY %d -> %s (off-screen)' % (len(srcs), net.path))
        new = net.copyOPs(srcs)
        ext._crashTrace('blockSpawn COPIED %s' % net.path)
    except Exception:
        return
    idle = absTime.seconds - ext._viz_last_activity
    f = min(1.0, max(0.0, idle / _VIZ_WARM_S))
    hue = round((_VIZ_COOL_HUE +
                 (_VIZ_WARM_HUE - _VIZ_COOL_HUE) * f) * 36.0) / 36.0
    skin = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
    # Arrange the copies into the FIGURE right here (issue #86). copyOPs lands
    # every part on the template's own coordinates -- now the canonical
    # _VIZ_TEMPLATE_PARK origin -- so without this the nine parts sit stacked at
    # the destination network's (0, 0) until botDance's next writing frame. That
    # frame is not guaranteed: botDance is throttled to ~30fps and its writes are
    # gated on netIsDisplayed, so a pane.owner read-back that lags by a frame, or
    # a takeover armed in the same frame as the spawn, showed the user a pile of
    # annotation boxes at the origin of the network they were just cut into.
    # Same formula as botDance's resting layout (sx = sy = 1, no gesture).
    dest = ext._viz_bot_dest
    offsets = {_VIZ_BOT_PREFIX + s: (ox, oy, w, h)
               for (s, ox, oy, w, h, _e) in _VIZ_BOT_PARTS}
    for n in new:
        n.selected = False
        bn = n.name
        if dest is not None:
            try:
                if bn.endswith('speech'):
                    n.nodeX = dest[0] - n.nodeWidth / 2.0
                    n.nodeY = dest[1] + 58.0
                else:
                    ox, oy, w, h = offsets[bn]
                    n.nodeX = (dest[0] + ox) - w / 2.0
                    n.nodeY = (dest[1] + oy) - h / 2.0
            except Exception:
                pass
        if bn.endswith('speech'):
            continue
        if bn.endswith('eye_l') or bn.endswith('eye_r'):
            n.par.Backcolorr, n.par.Backcolorg, n.par.Backcolorb = 0.0, 0.0, 0.0
        else:
            n.par.Backcolorr, n.par.Backcolorg, n.par.Backcolorb = skin


def assembleStep(ext, net: 'COMP') -> None:
    """Copy ONE queued template part into `net` -- the per-frame unit of Embot's
    spread assembly. Colours each part on arrival (skin for the body, black for
    eyes) so it looks right immediately, independent of _botDance's recolour
    throttle. The speech bubble keeps its own template styling."""
    q = ext._viz_bot_build_queue
    if not q:
        return
    tmpl = ensureTemplate(ext)
    if tmpl is None:
        ext._viz_bot_build_queue = []
        return
    name = q.pop(0)
    src = tmpl.op(name)
    if not src or net.op(name):             # missing source / already present
        return
    # copyOPs lands the copy at the SOURCE's coords, and the copy's cost is set by
    # whether THAT landing spot is in the viewport. So park the source at the off-view
    # staging point first -> the copy lands off-view and pays ~100ms, not ~280ms.
    # (_botDance then arranges the copies into the figure wherever the bot stands.)
    #
    # Issue #86: the source is the TEMPLATE part, which lives inside the Embody
    # COMP -- a TDN-strategy COMP. A staging coordinate left on it is exported
    # into Embody.tdn (all nine parts were committed at a leaked [1860, 251]), so
    # every on-screen assembly silently dirtied a tracked file. Snapshot the
    # source position and restore it in a finally that survives the except below.
    stage = ext._viz_bot_stage
    orig = None
    if stage:
        try:
            orig = (src.nodeX, src.nodeY)
            src.nodeX, src.nodeY = stage[0], stage[1]
        except Exception:
            orig = None
    try:
        ext._crashTrace('assembleStep COPY %s -> %s' % (name, net.path))
        new = net.copyOPs([src])
        ext._crashTrace('assembleStep COPIED %s' % name)
        idle = absTime.seconds - ext._viz_last_activity
        f = min(1.0, max(0.0, idle / _VIZ_WARM_S))
        hue = round((_VIZ_COOL_HUE +
                     (_VIZ_WARM_HUE - _VIZ_COOL_HUE) * f) * 36.0) / 36.0
        skin = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
        pos = ext._viz_bot_pos
        for n in new:
            n.selected = False
            bn = n.name
            if bn.endswith('speech'):
                # Place the bubble at the head on arrival so it never flashes at
                # its copied (0,0) spot before _botDance catches it.
                if pos:
                    n.nodeX = pos[0] - n.nodeWidth / 2.0
                    n.nodeY = pos[1] + 58.0
                continue
            if bn.endswith('eye_l') or bn.endswith('eye_r'):
                n.par.Backcolorr, n.par.Backcolorg, n.par.Backcolorb = 0.0, 0.0, 0.0
            else:
                n.par.Backcolorr, n.par.Backcolorg, n.par.Backcolorb = skin
    except Exception:
        pass
    finally:
        if orig is not None:
            try:
                src.nodeX, src.nodeY = orig     # never leave the template parked
            except Exception:
                pass


def assembleTick(ext) -> None:
    """Drive Embot's spread assembly: one template part copied every
    _VIZ_ASSEMBLE_INTERVAL frames until he is whole. He assembles at an off-view
    staging point (see _placeBot) so each copy renders outside the viewport; once the
    queue drains he swoops in via _startEntrance. Runs each frame so assembly completes
    even after the follow target clears (idle mid-build)."""
    q = ext._viz_bot_build_queue
    if q and absTime.frame >= ext._viz_assemble_next_frame:
        netpath = ext._viz_bot_net
        net = op(netpath) if netpath else None
        if not net or not net.valid:
            ext._viz_bot_build_queue = []
        else:
            assembleStep(ext, net)
            ext._viz_assemble_next_frame = absTime.frame + _VIZ_ASSEMBLE_INTERVAL
    # Assembly finished -> swoop in from the off-view staging point.
    if not ext._viz_bot_build_queue and ext._viz_bot_pending_entrance:
        startEntrance(ext)


def startEntrance(ext) -> None:
    """Fire Embot's swoop from the off-view staging point onto his destination op,
    once off-view assembly has completed. Uses the slower entrance duration so the
    long travel reads as a deliberate fly-in, not a teleport."""
    ext._viz_bot_pending_entrance = False
    dest = ext._viz_bot_dest
    if dest is None or ext._viz_bot_pos is None:
        return
    ext._viz_bot_from = ext._viz_bot_pos
    ext._viz_bot_target = dest
    ext._viz_jump_dur = _VIZ_ENTRANCE_DUR
    ext._viz_bot_jump_t0 = absTime.seconds


def cleanupDeadBots(ext) -> None:
    """Tear down a bot left behind by a switch -- ONE network per frame, now that
    the navigate has moved it off-screen so destroying its parts no longer redraws
    the editor. Never touches the live bot's net or the Embody template."""
    pend = ext._viz_bot_pending_cleanup
    if not pend:
        return
    netpath = pend.pop()
    if netpath == ext._viz_bot_net:
        return
    net = op(netpath)
    if net and net.valid:
        ext._crashTrace('cleanupDead ENTER %s' % netpath)
        for c in list(net.children):
            if c.name.startswith(_VIZ_BOT_PREFIX) and c.valid:
                try:
                    c.destroy()
                except Exception:
                    pass
        ext._crashTrace('cleanupDead DONE %s' % netpath)


def botDance(ext, now: float) -> None:
    """Animate the figure: a robotic HOP from node to node (parabolic arc,
    snappy ease, subtle landing squash) and a small stepped idle hover, with a
    vibrant colour cycle. Pure UI-attr + annotation colour writes (cook-free)."""
    np = ext._viz_bot_net
    if not np or ext._viz_bot_target is None:
        return
    net = op(np)
    if not net:
        ext._viz_bot_net = None
        return
    if (now - ext._viz_last_paint) < 0.033:    # cap figure repaint at ~30fps
        return
    ext._viz_last_paint = now
    # Issue #86: do not animate what nobody can see. Computed AFTER the throttle
    # so a throttled frame never pays for a pane scan. All the STATE below still
    # advances (gesture / blink / squint schedules are plain floats), so he does
    # not resume with a burst of queued gestures -- only the editor writes stop.
    writes = botWritesNeeded(ext, net)
    t = (now - ext._viz_bot_jump_t0) / ext._viz_jump_dur
    sx = sy = 1.0
    if t < 1.0:                                   # mid-hop
        e = 1.0 - (1.0 - t) * (1.0 - t)           # easeOutQuad (snappy)
        fx, fy = ext._viz_bot_from
        tx, ty = ext._viz_bot_target
        px = fx + (tx - fx) * e
        py = fy + (ty - fy) * e + _VIZ_JUMP_ARC * math.sin(math.pi * t)
        if t > 0.82:                              # subtle squash on landing
            k = (t - 0.82) / 0.18
            sx = 1.0 + _VIZ_SQUASH * k
            sy = 1.0 - _VIZ_SQUASH * k
    else:                                         # standing still (robotic; no idle churn)
        tx, ty = ext._viz_bot_target
        px, py = tx, ty
    ext._viz_bot_pos = (px, py)
    # --- random gestures at random intervals (not a fixed loop) ---
    if t >= 1.0 and now >= ext._viz_gesture_end and now >= ext._viz_next_gesture:
        if random.random() < 0.18:
            gtype = 3                               # robot dance, now and then
        else:
            gtype = int(random.random() * 3)        # 0 wave / 1 reach / 2 pump
            if gtype == ext._viz_gesture_type:      # avoid an immediate repeat
                gtype = (gtype + 1) % 3
        ext._viz_gesture_type = gtype
        ext._viz_gesture_start = now
        ext._viz_gesture_end = now + (_VIZ_DANCE_DUR if gtype == 3 else _VIZ_GESTURE_DUR)
        ext._viz_next_gesture = ext._viz_gesture_end + _VIZ_GESTURE_GAP_MIN + \
            random.random() * (_VIZ_GESTURE_GAP_MAX - _VIZ_GESTURE_GAP_MIN)
    active = (t >= 1.0) and (now < ext._viz_gesture_end)
    gi = ext._viz_gesture_type
    gdur = ext._viz_gesture_end - ext._viz_gesture_start
    gp = now - ext._viz_gesture_start
    genv = math.sin(math.pi * (gp / gdur)) if (active and gdur > 0.0) else 0.0
    if active and gi == 3:                          # robot dance: full-body sway + bob
        px = px + round(math.sin(gp * 6.0)) * 11.0 * genv
        py = py + abs(math.sin(gp * 9.0)) * 7.0 * genv
    # Quantized "thinking" colour -- changes a few times/sec, not 60. Writing
    # colour + positions on every part every frame forced a continuous
    # network-editor redraw and halved the FPS; quantize + the moving check
    # below keep idle frames write-free.
    idle = now - ext._viz_last_activity
    f = min(1.0, max(0.0, idle / _VIZ_WARM_S))
    hue = round((_VIZ_COOL_HUE + (_VIZ_WARM_HUE - _VIZ_COOL_HUE) * f) * 36.0) / 36.0
    skin = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
    recolor = writes and (skin != ext._viz_last_skin)
    # Clearing the remembered skin while invisible is what FORCES a recolor -- and
    # therefore a full position + size + colour rewrite of all nine parts -- on
    # the first frame he becomes visible again. Without it he would render stale.
    ext._viz_last_skin = skin if writes else None
    # Only repaint when actually animating (a jump or a gesture) or when the
    # quantized colour ticks -- otherwise leave the parts untouched so idle
    # frames cost nothing.
    # Periodic eye blink. TD clamps annotation node size to a 10px MINIMUM, so a
    # Y-squash of the 9px eyes cannot render -- instead the eyes briefly take the
    # face/skin colour (closed -> invisible) then return to black. Written only on
    # the open<->closed TRANSITION (2 colour writes per blink), so it costs almost
    # nothing and does NOT force a full-figure repaint.
    if now >= ext._viz_next_blink:
        ext._viz_blink_end = now + 0.13                          # blink lasts ~0.13s
        ext._viz_next_blink = now + 2.0 + random.random() * 3.5  # next blink in 2-5.5s
    blinking = now < ext._viz_blink_end
    # Issue #86: while invisible _viz_eyes_closed can go stale (the transition is
    # skipped). Harmless and self-correcting -- the forced recolor above rewrites
    # eye colour per the CURRENT `blinking` value on the first visible frame.
    if writes and blinking != ext._viz_eyes_closed:
        if blinking:
            # match the body's ACTUAL current colour (recolor lags the computed
            # skin) so the eyes truly vanish into the face.
            _bp = net.op(_VIZ_BOT_PREFIX + 'body')
            eye_col = ((_bp.par.Backcolorr.eval(), _bp.par.Backcolorg.eval(),
                        _bp.par.Backcolorb.eval()) if (_bp and _bp.valid) else skin)
        else:
            eye_col = (0.0, 0.0, 0.0)
        for _es in ('eye_l', 'eye_r'):
            _ep = net.op(_VIZ_BOT_PREFIX + _es)
            if _ep and _ep.valid:
                _ep.par.Backcolorr, _ep.par.Backcolorg, _ep.par.Backcolorb = eye_col
        ext._viz_eyes_closed = blinking
    # Occasional happy squint -- far rarer than the blink. The eyes flatten toward
    # the 10px floor and spread a little wider for ~1s, reading as a content "^_^".
    # Applied via the parts loop below (eye gw/gh when squinting), so it costs only
    # the 2 transition frames it forces, not a per-frame repaint.
    if ext._viz_next_squint == 0.0:
        ext._viz_next_squint = now + _VIZ_SQUINT_GAP_MIN   # never squint on spawn
    if now >= ext._viz_next_squint:
        ext._viz_squint_end = now + _VIZ_SQUINT_DUR
        ext._viz_next_squint = now + _VIZ_SQUINT_GAP_MIN + \
            random.random() * (_VIZ_SQUINT_GAP_MAX - _VIZ_SQUINT_GAP_MIN)
    squinting = now < ext._viz_squint_end
    squint_changed = (squinting != ext._viz_squinting)
    ext._viz_squinting = squinting
    moving = (t < 1.0) or active or bool(ext._viz_bot_build_queue)
    if writes and (moving or recolor or squint_changed):
        ext._crashTrace('botDance PARTS moving=%d recolor=%d t=%.2f %s' %
                        (int(moving), int(recolor), t, np))
        for (suffix, ox, oy, w, h, is_eye) in _VIZ_BOT_PARTS:
            p = net.op(_VIZ_BOT_PREFIX + suffix)
            if not p or not p.valid:
                continue
            gw = gh = 1.0
            if active:
                if gi == 0 and suffix == 'arm_r':                  # wave
                    oy = oy + _VIZ_WAVE_LIFT * genv
                    ox = ox + math.sin(gp * _VIZ_WAVE_FREQ) * _VIZ_WAVE_AMP * genv
                elif gi == 1 and suffix in ('arm_l', 'arm_r'):     # shrug: lift arms straight up (no scaling)
                    oy = oy + 16.0 * genv
                elif gi == 2 and suffix in ('arm_l', 'arm_r'):     # both arms pump up
                    oy = oy + _VIZ_WAVE_LIFT * 0.75 * genv
                elif gi == 3:                                      # robot dance: limbs + head
                    if suffix == 'arm_l':
                        oy = oy + 20.0 * genv * (0.5 + 0.5 * math.sin(gp * 7.0))
                    elif suffix == 'arm_r':
                        oy = oy + 20.0 * genv * (0.5 + 0.5 * math.sin(gp * 7.0 + math.pi))
                    elif suffix in ('head', 'eye_l', 'eye_r'):
                        ox = ox + round(math.sin(gp * 6.0)) * 4.0 * genv
            if is_eye and squinting:                    # happy squint: flatten + spread
                gw *= _VIZ_SQUINT_WIDEN
                gh *= _VIZ_SQUINT_FLATTEN
            pw, ph = w * sx * gw, h * sy * gh
            p.nodeWidth = pw
            p.nodeHeight = ph
            p.nodeX = (px + ox * sx) - pw / 2.0
            p.nodeY = (py + oy * sy) - ph / 2.0
            if recolor:
                if is_eye:
                    # open -> black; mid-blink -> track the body's NEW skin so the
                    # eyes stay vanished even if the thinking-colour ticks.
                    p.par.Backcolorr, p.par.Backcolorg, p.par.Backcolorb = \
                        (skin if blinking else (0.0, 0.0, 0.0))
                else:
                    p.par.Backcolorr, p.par.Backcolorg, p.par.Backcolorb = skin
        ext._crashTrace('botDance PARTS-DONE')
    # Speech bubble: follow + a Claude-Code-style typewriter -> spinner + dots.
    # The spinner only runs while actively building (idle < a few sec) so an
    # idle Embot does not churn redraws.
    sp = net.op(_VIZ_BOT_PREFIX + 'speech') if writes else None
    if sp and sp.valid:
        # Anchor the bubble to Embot's BASE position (_viz_bot_pos, captured before
        # the dance sway is added to px/py), NOT the animated px/py. So it follows
        # only while he HOPS to a new node, and stays put while he dances/gestures
        # in place -- saving a per-frame bubble redraw during every dance. The
        # changed-guard still (re)places it once after a hop and skips otherwise.
        bp = ext._viz_bot_pos or (px, py)
        sx_sp = bp[0] - sp.nodeWidth / 2.0
        sy_sp = bp[1] + 58.0
        if abs(sp.nodeX - sx_sp) > 0.5 or abs(sp.nodeY - sy_sp) > 0.5:
            sp.nodeX = sx_sp
            sp.nodeY = sy_sp
        act = ext._viz_action_text
        if act != ext._viz_speech_src:
            ext._viz_speech_src = act
            ext._viz_speech_t0 = now
        if ext._viz_target_queue:         # actively stepping: show the CURRENT
            ext._viz_speech_t0 = now      # caption instantly. The typewriter could
            line = act                    # not keep up with fast hops, so it lagged
                                          # a step behind; reset it for when we settle.
        else:
            shown = act[:int((now - ext._viz_speech_t0) * 45.0)]
            if len(shown) < len(act):
                line = shown + '_'                        # typing (settled, faster)
            elif idle < 4.0:                              # working -> spinner + dots
                line = '%s %s%s' % ('|/-\\'[int(now * 4.0) % 4], act, '.' * (int(now * 2.0) % 4))
            else:
                line = act                                # idle -> static (no churn)
        if sp.par.Bodytext.eval() != line:
            ext._crashTrace('botDance SPEECH-WRITE')
            sp.par.Bodytext = line
            ext._crashTrace('botDance SPEECH-DONE')


def botUnsafeNet(ext, net: 'COMP') -> bool:
    """True if a bot must NOT be created in `net` -- it would risk being saved.
    Unsafe: under /local, under the Embody COMP (ExportPortableTox captures
    Embody's descendants), or inside any TDN-strategy COMP (captured by .tdn
    export)."""
    try:
        if net.path.startswith('/local'):
            return True
        embody_path = ext.ownerComp.path
        tdn = ext.ownerComp.ext.Embody._getTDNPaths()
        p = net
        while p is not None and p.path != '/':
            if p.path == embody_path or p.path in tdn:
                return True
            p = p.parent()
    except Exception:
        return True   # any doubt -> do not create
    return False


def destroyBot(ext) -> None:
    """Remove all figure parts if present."""
    np = ext._viz_bot_net
    if np:
        net = op(np)
        if net:
            ext._crashTrace('destroyBot ENTER %s' % np)
            for c in list(net.children):
                if c.name.startswith(_VIZ_BOT_PREFIX) and c.valid:
                    try:
                        ext._crashTrace('destroyBot DESTROY %s' % c.name)
                        c.destroy()
                    except Exception:
                        pass
            ext._crashTrace('destroyBot DONE %s' % np)
    ext._viz_bot_net = None
    ext._viz_bot_pos = None
    ext._viz_bot_from = None
    ext._viz_bot_target = None
    ext._viz_bot_build_queue = []


def destroyPartsIn(ext, netpath) -> int:
    """Destroy every Embot part sitting directly in the network at `netpath` and
    return how many went. The one-network unit shared by the deferred-teardown
    flush and the .tox write retire, so neither has to reach for the blanket
    vizCleanup to remove parts from ONE net."""
    removed = 0
    try:
        net = op(netpath)
        if not net or not net.valid:
            return 0
        for c in list(net.children):
            if c.name.startswith(_VIZ_BOT_PREFIX) and c.valid:
                try:
                    c.destroy()
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


def vizCleanup(ext) -> None:
    """Retire all live visualization artifacts (restore pulse, destroy bot).
    Idempotent and safe to call from the save path."""
    restorePulse(ext)
    destroyBot(ext)
    # Flush any deferred off-screen teardowns NOW -- the save path must leave no
    # bot parts behind in any network.
    for netpath in list(ext._viz_bot_pending_cleanup):
        destroyPartsIn(ext, netpath)
    ext._viz_bot_pending_cleanup = set()
    ext._viz_target_queue = []
    ext._viz_hop_until = 0.0
    ext._viz_follow_net = None   # re-establish zoom next time we follow somewhere
    # The highlight is a follow artifact, not a bot part, but it is still OURS:
    # highlightOp only ever deselects the op it is replacing, so dropping the
    # cache without dropping the selection retires viz while leaving the last
    # op it touched selected for the rest of the session -- a marker for a
    # follow that is no longer running, and one nothing will ever clear (the
    # next highlightOp deselects `prev`, which is now None). Best-effort: a
    # deleted or renamed op is exactly what a retire sweep expects to find.
    # On the pre-save path this is restorePulse's reasoning applied to the other
    # marker viz leaves on a node -- what gets written to the file should not be
    # our highlight -- and it is the same class of write restorePulse already
    # performs there.
    #
    # _viz_selected_op is also load-bearing for the cache itself: highlightOp
    # early-returns on it, so a value that outlives the selection it describes
    # would leave Envoy's focus marker silently missing for that op.
    if ext._viz_selected_op:
        try:
            prev = op(ext._viz_selected_op)
            if prev is not None and prev.valid and prev.selected:
                prev.selected = False
        except Exception:
            pass
    ext._viz_selected_op = None
    # Issue #57: retiring makes the NEXT activation cold again -- its first hop
    # pulses only, and the bot/camera machinery re-engages after the hold.
    ext._viz_session_warm = False
    ext._viz_cold_since = -1
    # Issue #86: retiring makes the NEXT activation a first appearance again, so
    # stale hysteresis can never make Embot late after an idle retire, a save,
    # perform mode, or the _suppress_dialogs window. NOTE: vizRetireForWrite
    # deliberately does NOT come through here -- resetting the clocks on a .tox
    # write is what made the retire self-feeding (see writeSuppressed).
    ext._viz_home = None
    ext._viz_net_candidate = None
    ext._viz_relocate_blocked_since = None
    ext._viz_relocate_blocked_last = None
    ext._viz_write_suppress = {}


def purgeVizArtifacts(ext, root=None) -> int:
    """Destroy every LOOSE Embot part under `root` (default: the whole project)
    and return how many were removed. The structural backstop for constraint "no
    bot part may ever bake into a saved file", run from onProjectPreSave right
    after vizCleanup and, scoped to one COMP, before every .tox write.

    vizCleanup / vizRetireForWrite are the fast bookkeeping flushes and handle
    the normal case; this catches the orphans no bookkeeping CAN know about,
    because every _viz_* field is a plain instance attribute that
    EnvoyExt.__init__ resets:

      - an extension reinit (every .py edit in this repo hot-syncs and reinits)
        while Embot stands somewhere -- onDestroyTD deliberately only sets the
        shutdown event, so nine annotateCOMPs are orphaned with nothing pointing
        at them;
      - a COMP rename, which invalidates _viz_bot_pending_cleanup's path key and
        makes cleanupDeadBots' op(netpath) return None;
      - a Ctrl+Z resurrection, or a crash-recovered session.

    A registry of "nets we spawned into" would only know about orphans it
    recorded; a structural sweep is one function and strictly more complete.

    Two deliberate costs, stated rather than hidden:
      - a USER annotation literally named envoy_bot_* outside the template is
        DESTROYED, not merely skipped (TDNExt's export filter only omits it).
        The caller logs any non-zero count, so a deletion is never silent.
      - the project-wide sweep walks the tree. It skips _VIZ_PURGE_SKIP_ROOTS
        (TD's own /sys and /ui) precisely because it runs inside the pre-save
        window; everything the user actually saves is still swept.

    The template's parts are SKIPPED -- they are the shipped source asset, not a
    live bot. The carve-out is by NAME, not by path, on purpose: it must also
    shelter template parts in a copy of the Embody COMP (a second install, a
    staged copy, an older release .tox restored elsewhere), and it must shelter
    them when this function is called SCOPED to the Embody COMP itself during a
    portable export. Sheltering an extra static asset is harmless; deleting the
    shipped one forces a nine-annotateCOMP rebuild on every open.

    Fully guarded: returns 0 on any failure, because it runs while TD already has
    the .toe open for writing (issue #21)."""
    scoped = root is not None
    found = []
    try:
        if root is None:
            root = ext.ownerComp.ext.Embody.root
        if scoped:
            found = root.findChildren(name=_VIZ_BOT_PREFIX + '*',
                                      type=annotateCOMP, includeUtility=True)
        else:
            # Project-wide: descend per root child so TD's own trees can be
            # skipped. A root-level part is a direct child, so it is collected
            # here rather than by the findChildren below it.
            for c in root.children:
                try:
                    if c.path in _VIZ_PURGE_SKIP_ROOTS:
                        continue
                    # Type-filtered exactly like the findChildren calls around
                    # it: a bot part is always an annotateCOMP, so the name
                    # alone must not condemn (say) a user's TOP named
                    # envoy_bot_*. Anything else keeps falling through to the
                    # descent below, so a COMP with that name is still swept
                    # INSIDE rather than deleted.
                    if (c.name.startswith(_VIZ_BOT_PREFIX)
                            and c.type == 'annotate'):
                        found.append(c)
                        continue
                    if not c.isCOMP:
                        continue
                    found.extend(c.findChildren(name=_VIZ_BOT_PREFIX + '*',
                                                type=annotateCOMP,
                                                includeUtility=True))
                except Exception:
                    continue
    except Exception as e:
        try:
            ext._log('purgeVizArtifacts scan failed: %s: %s'
                     % (type(e).__name__, e), 'DEBUG')
        except Exception:
            pass
        return 0
    removed = 0
    for c in found:
        try:
            if not c.valid:
                continue
            host = c.parent()
            if host is not None and host.name == _VIZ_TEMPLATE_COMP:
                continue                    # the shipped template asset -- keep
            c.destroy()
            removed += 1
        except Exception:
            pass
    return removed


def vizRetireForWrite(ext, path: str) -> bool:
    """Retire Embot out of the COMP at `path`, which is about to be serialized to
    a .tox / a staged copy. Returns True if he was standing inside it or anything
    was swept out of it.

    The hole this closes: EmbodyExt.dirtyHandler calls Save(oper.path) for dirty
    TOX COMPs, and Save calls oper.saveExternalTox(). Adding children marks a
    COMP dirty and Update() fires on init, from the manager, and from every
    externalize_op MCP call -- so Embot standing in a TOX COMP can be written
    into that COMP's .tox with no user action at all. Issue #86 lengthens his
    residency in one network, which makes that more likely, so this is in scope
    because of that design choice.

    Four properties, each of them load-bearing:

    1. SUBTREE-SCOPED. dirtyHandler can call Save for many COMPs on every
       Update(); a blanket retire would make Embot flicker on writes that never
       touch him. A stale deferred-teardown entry inside `path` removes only THAT
       net's parts -- the live bot elsewhere is not collateral.

    2. NOT a vizCleanup. Routing through it reset _viz_home, _viz_session_warm
       and the hop queue. Clearing _viz_home put the very next relocation on the
       "first appearance is never delayed" branch, so the respawn -- into the
       same COMP, whose dirtiness his parts caused -- was exempt from the
       cooldown, and the whole thing looped at MCP-call cadence. It also
       re-armed the issue-57 cold hold and threw away the rest of a batch's
       narration hops for a write he was not even in.

    3. STRUCTURAL FALLBACK. Bookkeeping alone cannot see orphans (an extension
       reinit wipes every _viz_* field while nine annotateCOMPs stay in the
       network), and this is the one save path that fires with no project save
       behind it. So the subtree is also swept structurally; it is a scan of one
       COMP, on a path that is already writing a whole .tox.

    4. SUPPRESSES RE-ENTRY. Removing him is only half the loop: his own parts are
       what mark the COMP dirty, so walking straight back in re-dirties it and
       the next Update() writes, retires and respawns again. noteWriteRetire bars
       the subtree for _VIZ_WRITE_SUPPRESS_S -- see writeSuppressed."""
    now = absTime.seconds
    matched = False
    removed = 0
    try:
        # Deferred teardowns inside the subtree: remove those nets' parts only.
        for netpath in list(ext._viz_bot_pending_cleanup):
            if pathInsideSubtree(netpath, path):
                matched = True
                removed += destroyPartsIn(ext, netpath)
                ext._viz_bot_pending_cleanup.discard(netpath)
        # The live bot, only when he is genuinely inside.
        if ext._viz_bot_net and pathInsideSubtree(ext._viz_bot_net, path):
            matched = True
            if ext._viz_pulse_op and pathInsideSubtree(ext._viz_pulse_op, path):
                restorePulse(ext)   # a mid-fade accent colour would serialize
            removed += destroyPartsIn(ext, ext._viz_bot_net)
            destroyBot(ext)
    except Exception:
        pass
    # Structural backstop for orphans no bookkeeping knows about (see above).
    try:
        root = op(path)
        if root is not None and root.valid and root.isCOMP:
            removed += purgeVizArtifacts(ext, root=root)
    except Exception:
        pass
    if matched or removed:
        try:
            noteWriteRetire(ext, path, now)
        except Exception:
            pass
    if removed:
        try:
            ext._log('Retired %d Embot part(s) out of %s before serializing it'
                     % (removed, path), 'DEBUG')
        except Exception:
            pass
    return matched or removed > 0


def viewTuple(ext, pane) -> tuple:
    """A comparable snapshot of a pane's view state (id, owner, pan, zoom)."""
    owner_path = pane.owner.path if pane.owner else None
    return (pane.id, owner_path, round(pane.x, 2), round(pane.y, 2),
            round(pane.zoom, 4))


def recordView(ext, pane) -> None:
    """Remember what WE last set the pane to (baseline for takeover detect)."""
    ext._viz_last_view = viewTuple(ext, pane)
