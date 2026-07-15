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
    # --- the character (Embotenable) ---
    if show_bot:
        pulseStart(ext, target, now)    # ping the node colour
        placeBot(ext, net, target, now) # bring the dancing bot to the op
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
    if not ensureBot(ext, net):
        return
    dest = (target.nodeX + target.nodeWidth / 2.0,
            target.nodeY + target.nodeHeight + botFootGap(ext))
    ext._viz_bot_dest = dest           # current op standing point (swoop target)
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
    animated/live bot, so _botUnsafeNet (which forbids a LIVE bot here) is moot."""
    try:
        host = ext.ownerComp
        tmpl = host.op('embot_template')
        if tmpl and tmpl.op(_VIZ_BOT_PREFIX + 'body') and \
                tmpl.op(_VIZ_BOT_PREFIX + 'speech'):
            return tmpl
        if tmpl:
            tmpl.destroy()                  # partial/stale -> rebuild clean
        ext._crashTrace('ensureTemplate BUILD (creating annotateCOMPs)')
        tmpl = host.create(baseCOMP, 'embot_template')
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


def blockSpawn(ext, net: 'COMP') -> None:
    """Copy ALL 9 parts into `net` in ONE copyOPs (~180ms, one frame -- vs the
    ~9-frame, ~464ms spread). ONLY called by _ensureBot when `net` is OFF-SCREEN
    (a sub-COMP we are about to navigate into): copyOPs of many annotateCOMPs into
    a DISPLAYED net hard-crashes TD (the editor redraw -- pinpointed via crash
    trace), and the off-screen owner-swap that once dodged that crash broke the
    pane render, so displayed nets use the safe spread instead. Clears orphans;
    colours on arrival."""
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
    for n in new:
        n.selected = False
        bn = n.name
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
    stage = ext._viz_bot_stage
    if stage:
        try:
            src.nodeX, src.nodeY = stage[0], stage[1]
        except Exception:
            pass
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
    recolor = (skin != ext._viz_last_skin)
    ext._viz_last_skin = skin
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
    if blinking != ext._viz_eyes_closed:
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
    if moving or recolor or squint_changed:
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
    sp = net.op(_VIZ_BOT_PREFIX + 'speech')
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


def vizCleanup(ext) -> None:
    """Retire all live visualization artifacts (restore pulse, destroy bot).
    Idempotent and safe to call from the save path."""
    restorePulse(ext)
    destroyBot(ext)
    # Flush any deferred off-screen teardowns NOW -- the save path must leave no
    # bot parts behind in any network.
    for netpath in list(ext._viz_bot_pending_cleanup):
        net = op(netpath)
        if net and net.valid:
            for c in list(net.children):
                if c.name.startswith(_VIZ_BOT_PREFIX) and c.valid:
                    try:
                        c.destroy()
                    except Exception:
                        pass
    ext._viz_bot_pending_cleanup = set()
    ext._viz_target_queue = []
    ext._viz_hop_until = 0.0
    ext._viz_follow_net = None   # re-establish zoom next time we follow somewhere


def viewTuple(ext, pane) -> tuple:
    """A comparable snapshot of a pane's view state (id, owner, pan, zoom)."""
    owner_path = pane.owner.path if pane.owner else None
    return (pane.id, owner_path, round(pane.x, 2), round(pane.y, 2),
            round(pane.zoom, 4))


def recordView(ext, pane) -> None:
    """Remember what WE last set the pane to (baseline for takeover detect)."""
    ext._viz_last_view = viewTuple(ext, pane)
