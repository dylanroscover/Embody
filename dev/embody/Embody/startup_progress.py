"""What Embody's startup is actually doing, as data.

ONE dict per Embody COMP, published INTO this module by the code that
does the work, so the startup viewer draws from a single honest source
instead of scraping status strings itself.

THE RULE THIS MODULE EXISTS TO ENFORCE: never fake a percentage. Exactly
one startup step has a real denominator -- the catalog scan knows how
many types and .tox files it has to get through. Envoy's dependency
install is one opaque `uv pip install`; Convoy is a handful of discrete
states; the repo/config work is discrete. For those, `total` is 0 and the
viewer shows an ELAPSED CLOCK, not a bar creeping to an invented 80%. A
measured 0:42 proves the step is alive; a fabricated percentage is a lie
that happens to look reassuring.

`skipped` is load-bearing, not a rounding of `done`. The catalog
early-returns in a single frame when a complete catalog for this TD build
already exists, Envoy can be Disabled or in Perform Mode, and Convoy is
off in most projects. A bar sitting at 0% for something that legitimately
never ran is its own small lie -- the whole reason this viewer exists is
that this week's failures were invisible.

WHY IT PARSES STATUS TEXT for envoy and convoy, and PUBLISHES for the
rest. `Envoystatus` is written from about twenty places across EnvoyExt,
EmbodyExt and execute.py; threading a publish call through all of them
would be a wide edit, and every missed writer would be a step that
silently stops updating. The parameter is already the single value the
panel shows, so deriving from it cannot drift from what the user reads.
The catalog, the config pass and the restore phases are the opposite
case: they have real counters or no status string at all (a restore that
restores nothing writes nothing, and a phase that dies writes its last
count forever), so they publish from their own choke points -- which is
the only way a step reaches a TERMINAL state on every path, including
the ones that do no work.

STARTUP IS DECLARED, NOT INFERRED. begin_startup()/end_startup() bracket
the open sequence because the derived view cannot tell "has not begun"
from "is over": at frame one Envoy and Convoy read Disabled and the
catalog reads Enabled -- a perfectly settled snapshot, ten frames before
any of it starts. Inferring from that latched the viewer into its
settled mode before the first phase ran, which is why the bars were
never seen.

Pure and TouchDesigner-free by construction -- it takes strings and
numbers and returns a dict, and the COMP it is handed is only ever read
through getattr -- so the whole mapping is unit-tested on the CI matrix
rather than only in a live session.
"""

# Step keys, in display order. RESTORE is conditional: it only appears
# when the project actually has externalized COMPs to restore.
STEP_REPO = "repo"
STEP_CATALOG = "catalog"
STEP_ENVOY = "envoy"
STEP_CONVOY = "convoy"
STEP_RESTORE = "restore"

STEPS = (STEP_REPO, STEP_CATALOG, STEP_ENVOY, STEP_CONVOY, STEP_RESTORE)

# The four bars the viewer always draws, plus the fifth when relevant.
BASE_STEPS = (STEP_REPO, STEP_CATALOG, STEP_ENVOY, STEP_CONVOY)

LABELS = {
    STEP_REPO: "Project",
    STEP_CATALOG: "Catalog",
    STEP_ENVOY: "Envoy",
    STEP_CONVOY: "Convoy",
    STEP_RESTORE: "Restore",
}

# States. STALLED is distinct from RUNNING on purpose: it means the step
# is waiting on something a user must do (save the project, answer a
# consent dialog), which is not progress and must not read as progress.
IDLE = "idle"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
STALLED = "stalled"

STATES = (IDLE, RUNNING, DONE, FAILED, SKIPPED, STALLED)

# A step is IN PLAY if it can still change. The halo counts
# successfully-complete over in-play, never over all four: counting
# terminals would show 4/4 with a red wedge and read as success.
_TERMINAL = (DONE, FAILED, SKIPPED)

FORMAT = "embody-startup-progress/1"


def _entry(state, done=0, total=0, detail="", started=None):
    return {"state": state, "done": int(done or 0), "total": int(total or 0),
            "detail": str(detail or ""), "started": started}


def _text(value):
    return str(value or "").strip()


def _parse_counts(text):
    """The (N/T) the scan already wrote into its own status line.

    A FALLBACK for callers that cannot pass the counters directly. These
    are still the scan's real numbers -- merely transported through the
    status string instead of handed over -- so using them is not the
    fabrication this module forbids. Returns (0, 0) when absent, which
    correctly yields an indeterminate bar rather than a guess.
    """
    left = text.rfind("(")
    right = text.rfind(")")
    if left < 0 or right < left:
        return 0, 0
    chunk = text[left + 1:right]
    if chunk.count("/") != 1:
        return 0, 0
    a, b = chunk.split("/")
    try:
        return int(a.strip()), int(b.strip())
    except ValueError:
        return 0, 0


def catalog_step(status, done=0, total=0, started=None):
    """The one step with a real denominator.

    `done`/`total` come from CatalogManagerExt's own counters when the
    caller can supply them, so the bar cannot disagree with the scan;
    otherwise they are read back out of the status line the scan wrote.
    """
    text = _text(status)
    low = text.lower()
    if not total:
        done, total = _parse_counts(text)
    if not text or low == "idle":
        return _entry(IDLE)
    if low == "disabled":
        return _entry(SKIPPED, detail="disabled")
    if low.startswith("enabled") or low.startswith("complete"):
        # A catalog already complete for this TD build returns in ONE
        # frame. That is a legitimate skip, not instant work -- but it is
        # still DONE: the catalog is there.
        return _entry(DONE, done=done, total=total)
    if "failed" in low or low.startswith("error"):
        return _entry(FAILED, done=done, total=total, detail=text)
    if low.startswith("scanning"):
        return _entry(RUNNING, done=done, total=total, detail=text,
                      started=started)
    return _entry(RUNNING, done=done, total=total, detail=text,
                  started=started)


def envoy_step(status, started=None):
    """Derived from the Envoystatus parameter -- see the module docstring.

    No denominator exists: the dependency install is a single opaque uv
    call, so a RUNNING envoy step carries total=0 and the viewer shows
    elapsed time.
    """
    text = _text(status)
    low = text.lower()
    if not text:
        return _entry(IDLE)
    if low.startswith("error"):
        return _entry(FAILED, detail=text)
    if low in ("disabled", "perform mode"):
        return _entry(SKIPPED, detail=text)
    if low.startswith("running on port"):
        return _entry(DONE, detail=text)
    # Everything else that is not terminal is work in flight: preparing
    # the environment, the one-time dependency install, starting,
    # restarting after a save or reinit, the watchdog revive.
    return _entry(RUNNING, detail=text, started=started)


def convoy_step(status, started=None):
    """Derived from the Convoystatus parameter.

    Convoy is discrete states, never a fraction. The blocking readouts
    ('Not installed', 'Needs repair ...', 'Install failed ...') are
    FAILED rather than running: this whole viewer exists because those
    were invisible.
    """
    text = _text(status)
    low = text.lower()
    if not text:
        return _entry(IDLE)
    if low == "disabled":
        return _entry(SKIPPED, detail=text)
    if low.startswith(("not installed", "needs repair", "install failed",
                       "consent required")):
        return _entry(FAILED, detail=text)
    if low.startswith(("waiting for", "managed by another supervisor")):
        # Blocked on the user (or on another supervisor), which is not
        # progress and must not animate like it.
        return _entry(STALLED, detail=text)
    if low.startswith(("connected", "running")):
        return _entry(DONE, detail=text)
    return _entry(RUNNING, detail=text, started=started)


def simple_step(state, done=0, total=0, detail="", started=None):
    """A discrete step the caller already knows the state of."""
    state = state if state in STATES else IDLE
    return _entry(state, done=done, total=total, detail=detail,
                  started=started)


def snapshot(repo=None, catalog=None, envoy=None, convoy=None,
             restore=None, now=None):
    """Assemble the stored dict. Never raises.

    `restore` is omitted entirely when None -- the fifth bar appears only
    when the project has externalized COMPs to restore, rather than
    showing an empty bar every startup.
    """
    steps = {
        STEP_REPO: repo or _entry(IDLE),
        STEP_CATALOG: catalog or _entry(IDLE),
        STEP_ENVOY: envoy or _entry(IDLE),
        STEP_CONVOY: convoy or _entry(IDLE),
    }
    if restore is not None:
        steps[STEP_RESTORE] = restore
    order = [k for k in STEPS if k in steps]
    return {"format": FORMAT, "order": order, "steps": steps,
            "updated": now, "summary": summarize(steps)}


def summarize(steps):
    """Counts the halo and the one-line readout are built from.

    `complete` counts steps that SUCCEEDED. `in_play` counts everything
    that is not terminal PLUS the successes -- i.e. the denominator is
    "steps that could still end well", so a failure removes itself from
    the denominator instead of quietly counting as progress.
    """
    values = list(steps.values())
    complete = sum(1 for s in values if s.get("state") == DONE)
    skipped = sum(1 for s in values if s.get("state") == SKIPPED)
    failed = sum(1 for s in values if s.get("state") == FAILED)
    stalled = sum(1 for s in values if s.get("state") == STALLED)
    running = sum(1 for s in values if s.get("state") == RUNNING)
    in_play = complete + running + stalled
    return {"complete": complete, "in_play": in_play, "failed": failed,
            "skipped": skipped, "stalled": stalled, "running": running,
            "settled": (running == 0 and stalled == 0)}


def fraction(step):
    """0.0-1.0 for a bar, or None when there is no honest denominator.

    None is the whole point: the caller must render an elapsed clock
    rather than invent a width. A terminal step is full (or empty, for a
    failure that never started) regardless of counters.
    """
    if not isinstance(step, dict):
        return None
    state = step.get("state")
    if state in (DONE, SKIPPED):
        return 1.0
    if state == IDLE:
        return 0.0
    total = int(step.get("total") or 0)
    if total <= 0:
        return None
    done = int(step.get("done") or 0)
    if done < 0:
        done = 0
    if done > total:
        done = total
    return float(done) / float(total)


def elapsed_text(step, now):
    """'0:42' since the step started, or '' when it has not.

    Real measurement, no implied denominator: it proves the step is alive
    rather than hung, which is exactly what a fake bar cannot do.
    """
    started = (step or {}).get("started")
    if started is None or now is None:
        return ""
    seconds = int(max(0.0, float(now) - float(started)))
    return "%d:%02d" % (seconds // 60, seconds % 60)


# -- the bar, rendered in FONT --------------------------------------------
#
# The bar IS text. Block glyphs give eighth-of-a-character precision, stay
# crisp at any size (a Text COMP is resolution independent -- a fixed
# texture is not, and a node tile gets scaled), and, the part that earns
# its keep here, make the bar a pure string this suite can assert on
# instead of a rendered image somebody has to squint at.
#
# WRITTEN AS \u ESCAPES, NEVER LITERAL GLYPHS. Every file in this repo
# stays ASCII (rules/ascii-punctuation.md): a raw block character read
# back through a legacy codepage is mojibake, and this module is imported
# by a TouchDesigner DAT whose text round-trips on every save.
BLOCK_FULL = "\u2588"       # full block -- filled
BLOCK_MED = "\u2592"        # medium shade -- the indeterminate sweep
BLOCK_TRACK = "\u2591"      # light shade -- the unfilled track
# Left one-eighth .. seven-eighths, for sub-character precision, so a
# 20-cell bar actually resolves 160 steps rather than 20.
EIGHTHS = ("", "\u258f", "\u258e", "\u258d", "\u258c", "\u258b", "\u258a",
           "\u2589")

# Per-state colour for the row, as TD 0..1 floats. Colour carries the
# state; the glyphs carry the quantity. Neither is asked to do both,
# which is what keeps a red bar from also reading as "80% done".
STATE_RGB = {
    IDLE: (0.30, 0.32, 0.36),
    RUNNING: (0.30, 0.68, 0.95),
    DONE: (0.35, 0.78, 0.45),
    FAILED: (0.90, 0.30, 0.28),
    SKIPPED: (0.40, 0.44, 0.50),
    STALLED: (0.95, 0.70, 0.25),
}


def bar_text(step, width=20, phase=0.0):
    """The bar as characters. Cannot express a fraction it does not have.

    A step with no denominator returns a SWEEP, not a fill: the glyphs
    move but never accumulate, so nothing in the string implies a
    percentage. That is the whole contract, and it is asserted directly
    because the string is the artifact.
    """
    width = max(1, int(width))
    state = (step or {}).get("state")
    if state == FAILED:
        # Solid, full width. A failure is not partial success and must
        # never render as a part-filled bar.
        return BLOCK_FULL * width
    if state == SKIPPED:
        # The track alone: the row exists and legitimately did not run.
        return BLOCK_TRACK * width
    value = fraction(step)
    if value is None:
        span = max(1, width // 5)
        head = int((phase % 1.0) * width)
        return "".join(
            BLOCK_MED if ((i - head) % width) < span else BLOCK_TRACK
            for i in range(width))
    filled = value * width
    full = int(filled)
    if full >= width:
        return BLOCK_FULL * width
    head = EIGHTHS[int((filled - full) * 8)]
    body = BLOCK_FULL * full + head
    return body + BLOCK_TRACK * (width - len(body))


def value_text(step, now=None):
    """The right-hand column: a real measurement or a real word.

    Never a percentage for a step that has no denominator -- that column
    shows the elapsed clock instead, which proves the step is alive
    rather than implying how far along it is.
    """
    state = (step or {}).get("state")
    if state == DONE:
        return "done"
    if state == FAILED:
        return "failed"
    if state == SKIPPED:
        return "skipped"
    if state == STALLED:
        return "waiting"
    if state == IDLE:
        return ""
    total = int((step or {}).get("total") or 0)
    if total > 0:
        return "%d/%d" % (int((step or {}).get("done") or 0), total)
    return elapsed_text(step, now) or "..."


def row_text(key, step, now=None, phase=0.0, label_width=9, bar_width=20):
    """One monospace line: label, bar, value."""
    label = LABELS.get(key, key)[:label_width].ljust(label_width)
    return "%s %s  %s" % (label, bar_text(step, bar_width, phase),
                          value_text(step, now))


def rows(snap, now=None, phase=0.0, label_width=9, bar_width=20):
    """Every row of a snapshot, ready to hand to Text COMPs.

    Returns a list of (key, text, rgb) -- one entry per bar, in display
    order, so the panel builder never re-derives ordering or colour.
    """
    out = []
    for key in (snap or {}).get("order", ()):
        step = (snap or {}).get("steps", {}).get(key) or {}
        out.append((key, row_text(key, step, now, phase, label_width,
                                  bar_width),
                    STATE_RGB.get(step.get("state"), STATE_RGB[IDLE])))
    return out


# -- what the startup phases publish --------------------------------------
#
# Module-level rather than COMP storage on purpose. store() recooks on
# every write (the catalog scan writes hundreds of times), and storage is
# serialized into the .toe and into Embody's own .tdn -- so a startup
# that finished LAST session would come back on disk and be replayed as
# this session's progress, which is precisely the frozen readout this
# module exists to stop. The record is per-COMP-path so two Embody COMPs
# in one process cannot overwrite each other's steps.
_PUBLISHED = {}

# When a DERIVED step entered its current non-terminal state. The
# parameter carries no timestamp, so the clock has to be observed rather
# than read: without it a wedged step renders "..." forever, and "..."
# looks identical after four seconds and after forty minutes.
_SINCE = {}


def _comp_key(embody):
    """A stable id for the COMP. Reads nothing TouchDesigner-specific."""
    try:
        return str(getattr(embody, "path", "") or id(embody))
    except Exception:
        return ""


def published_steps(embody):
    """Everything published for this COMP, as {step key: entry}."""
    return dict(_PUBLISHED.get(_comp_key(embody), {}))


def clear_published(embody):
    """Forget this COMP's published startup (a genuine re-open, or tests)."""
    _PUBLISHED.pop(_comp_key(embody), None)
    for slot in [s for s in _SINCE if s[0] == _comp_key(embody)]:
        _SINCE.pop(slot, None)


def publish(embody, key, state, done=0, total=0, detail="", now=None,
            add=False):
    """Record one startup step's REAL state. Never raises.

    `add` accumulates the counters instead of replacing them, because
    ONE bar can be fed by more than one phase -- the TOX restore and the
    TDN reconstruction are separate methods, run fifteen frames apart,
    that together answer "how much of my project came back?".

    Returns False for a key or state this module does not define, so a
    caller that drifts from the vocabulary fails a contract test instead
    of silently publishing nothing.
    """
    if key not in STEPS or state not in STATES:
        return False
    try:
        slot = _PUBLISHED.setdefault(_comp_key(embody), {})
        old = slot.get(key) or {}
        if add:
            done = int(done or 0) + int(old.get("done") or 0)
            total = int(total or 0) + int(old.get("total") or 0)
        entry = _entry(state, done=done, total=total, detail=detail)
        # The start time is stamped HERE, on the transition into a
        # non-terminal state, so no caller can forget it -- a RUNNING
        # step with no start renders no clock, which is the wedge going
        # invisible again.
        if state in (RUNNING, STALLED):
            started = old.get("started")
            if started is None or old.get("state") not in (RUNNING, STALLED):
                started = now
            entry["started"] = started
        slot[key] = entry
        return True
    except Exception:
        return False


def finish(embody, key, failed=False, detail="", now=None):
    """Close a step: DONE when it did work, SKIPPED when there was none.

    One rule in one place rather than a decision at each of the return
    points that reach it. SKIPPED is not a rounding of DONE here: a
    startup with nothing to restore must say so, and a phase that ran
    nothing must not erase the counts a sibling phase really earned.
    """
    if key not in STEPS:
        return False
    try:
        slot = _PUBLISHED.setdefault(_comp_key(embody), {})
        old = slot.get(key) or {}
        done = int(old.get("done") or 0)
        total = int(old.get("total") or 0)
        if failed:
            state = FAILED
        elif done or total:
            state = DONE
            done = total if total else done
        else:
            state = SKIPPED
        slot[key] = _entry(state, done=done, total=total,
                           detail=detail or old.get("detail", ""))
        return True
    except Exception:
        return False


def close_unreported(embody, keys, detail="did not report", now=None):
    """Fail every named step still non-terminal. The last-resort net.

    An exception inside a deferred run() callback kills the rest of that
    chain silently, so a phase can stop existing between its RUNNING and
    its terminal publish. A step left animating forever is the exact lie
    this module was written against -- if the phase cannot say how it
    ended, the viewer says it did not end.
    """
    closed = []
    slot = _PUBLISHED.get(_comp_key(embody), {})
    for key in keys:
        entry = slot.get(key)
        if entry and entry.get("state") in (RUNNING, STALLED, IDLE):
            publish(embody, key, FAILED, done=entry.get("done", 0),
                    total=entry.get("total", 0), detail=detail, now=now)
            closed.append(key)
    return closed


def publish_catalog(embody, status, now=None):
    """The scan's own choke point, with the counters it is counting with.

    Routed through catalog_step so the (N/T) the scan wrote is read back
    by the same parser the fallback uses -- these are the scan's real
    numbers, merely transported through its status line, which is what
    keeps the bar from ever disagreeing with the sweep.
    """
    step = catalog_step(status)
    return publish(embody, STEP_CATALOG, step["state"], done=step["done"],
                   total=step["total"], detail=step["detail"], now=now)


# -- live assembly --------------------------------------------------------
#
# Takes the Embody COMP as an ARGUMENT rather than reaching for a global,
# so it stays testable with a stand-in object and this module keeps its
# no-TouchDesigner-import property: nothing here imports td, and none of
# it runs at import time.
def live_snapshot(embody, now=None, catalog=None):
    """Read the live parameters and build the snapshot. Never raises.

    `catalog` lets CatalogManagerExt hand in its REAL counters from
    inside the scan -- the one step with a true denominator, and the one
    moment when nothing else on the main thread is running.

    THE PARAMETER THE FALLBACK READS IS `Status`, not a catalog-specific
    one: there has never been a Catalogstatus par on this COMP, and
    reading a name that does not exist returned "" and then fabricated
    "Enabled", so the catalog bar reported DONE for the entire life of a
    scan. `_setScanStatus` writes the scan's line into par.Status, which
    is why the same string also drives the Embody row.
    """
    def par(name):
        try:
            value = getattr(embody.par, name, None)
            return value.eval() if value is not None else ""
        except Exception:
            return ""

    pub = published_steps(embody)
    if catalog is None:
        catalog = pub.get(STEP_CATALOG) or catalog_step(par("Status"))
    return snapshot(
        repo=pub.get(STEP_REPO)
        or simple_step(DONE if par("Version") else IDLE),
        catalog=catalog,
        envoy=_stamp(embody, STEP_ENVOY,
                     envoy_step(par("Envoystatus")), now),
        convoy=_stamp(embody, STEP_CONVOY,
                      convoy_step(par("Convoystatus")), now),
        restore=pub.get(STEP_RESTORE),
        now=now)


def _stamp(embody, key, step, now=None):
    """Give a DERIVED step the clock its parameter cannot carry.

    Keyed on (state, detail) so each phase of a long sequence times
    itself -- "Preparing Python environment" finishing and "Installing
    deps" starting is two measurements, not one that never resets. A
    step that is already terminal drops its record, so a later restart
    starts a fresh clock rather than inheriting a stale one.
    """
    state = (step or {}).get("state")
    slot = (_comp_key(embody), key)
    if state not in (RUNNING, STALLED):
        _SINCE.pop(slot, None)
        return step
    if step.get("started") is not None:
        return step
    mark = (state, step.get("detail", ""))
    seen = _SINCE.get(slot)
    if seen is None or seen[0] != mark:
        if now is None:
            return step         # no clock to start it with; try again later
        _SINCE[slot] = (mark, float(now))
        seen = _SINCE[slot]
    step["started"] = seen[1]
    return step


def live_row(embody, key, now=None, phase=0.0, catalog=None):
    """(text, rgb) for one row, straight from the live parameters."""
    snap = live_snapshot(embody, now=now, catalog=catalog)
    step = snap.get("steps", {}).get(key) or _entry(IDLE)
    return (row_text(key, step, now, phase),
            STATE_RGB.get(step.get("state"), STATE_RGB[IDLE]))


# -- responsive layout ----------------------------------------------------
#
# The row is monospace, so how many bar cells fit is arithmetic rather
# than guesswork: character advance is a fixed fraction of the font size.
# Consolas measures ~0.55; the constant is named so a font change is a
# one-line correction instead of a hunt through expressions.
MONO_ADVANCE = 0.55

# Separator spaces in row_text: one after the label, two before the value.
_SEPARATORS = 3

# The value column is sized to the LONGEST thing it renders, measured
# rather than rounded up: "skipped" and "142/344" are both 7. Reserving 8
# cost a character of bar at the smallest panel size for nothing. A
# four-digit catalog total ("1420/3440") would exceed it, which costs two
# cells of bar on that one row and nothing else.
VALUE_WIDTH = 7


def fit_bar_width(panel_width, fontsize, label_width=9, value_width=VALUE_WIDTH,
                  padding=48, minimum=6, maximum=120):
    """Bar cells that fit `panel_width`, so the row fills the container.

    Clamped at both ends: a panel squeezed to nothing still renders a
    readable stub rather than a zero-length bar (or a negative one), and
    a very wide panel stops growing the bar long before the string cost
    matters.
    """
    try:
        usable = float(panel_width) - float(padding)
        cell = max(1.0, float(fontsize) * MONO_ADVANCE)
    except (TypeError, ValueError):
        return minimum
    chars = int(usable / cell) - int(label_width) - int(value_width) \
        - _SEPARATORS
    return max(minimum, min(maximum, chars))


# The row we want to fit at minimum: label + a bar worth reading + value
# + separators. Below this the bar stops being a bar and becomes a few
# fat cells, which looks broken even though it technically fills.
#
# THIS NUMBER IS THE FONT SIZE. It is the width constraint that binds at
# node-tile sizes, so every character the layout stops spending buys
# legibility directly: 46 -> 33 is a ~39% larger glyph at the same panel
# width. 32 is DERIVED, not guessed. When width is the binding
# constraint the font scales with the panel too, so the cell count lands
# at a constant (1 - 2*pad) * TARGET - 18 regardless of panel size --
# 32 is the smallest budget that still yields the 12-cell bar floor.
# It is set by BAR mode, the tighter of the two:
# label(9) + bar(12) + value(7) + separators(3).
# Settled mode fits comfortably inside it because compact_status stops
# the value repeating the label -- "Running on port 9870" becomes "port
# 9870", which is what bought the last two font increases.
TARGET_ROW_CHARS = 33

# Font size as a fraction of row height, when height is the binding
# constraint. Consolas sits well below its em box, so 0.52 fills the row
# without clipping ascenders or descenders.
ROW_FONT_RATIO = 0.52

# Leading, as a fraction of the font size. The row box is the glyph plus
# this much breathing room -- 0.5 keeps the lines clearly separate while
# spending no vertical space on nothing, which is what lets the font be
# as large as it is at node-tile sizes.
LINE_SPACING = 0.5


def fit_font_size(panel_width, row_height, target_chars=TARGET_ROW_CHARS,
                  minimum=7.0, maximum=40.0):
    """Font size that satisfies BOTH the row height and the width.

    Sizing off height alone is the obvious mistake and it looks wrong
    immediately: a short wide panel yields huge glyphs and a bar of about
    nine fat cells with dead space beside it. The width constraint keeps
    enough characters on the line for the bar to still read as a bar.
    """
    try:
        by_height = float(row_height) * ROW_FONT_RATIO
        by_width = float(panel_width) / (float(target_chars) * MONO_ADVANCE)
    except (TypeError, ValueError, ZeroDivisionError):
        return minimum
    return max(minimum, min(maximum, by_height, by_width))


# == mode two: the settled status list ====================================
#
# Bars answer "how far along is this?", which is only a real question
# while something is installing. Once startup settles, a bar is four
# identical full-width blocks saying nothing -- so the viewer switches to
# what the user actually wants at that point: WHAT IS THE STATE OF EACH
# SUBSYSTEM RIGHT NOW. Same panel, same rows, different question.
STATUS_EMBODY = "embody"
STATUS_AUTOSAVE = "autosave"
STATUS_ENVOY = "envoy"
STATUS_CONVOY = "convoy"
STATUS_VERSION = "version"

STATUS_ORDER = (STATUS_EMBODY, STATUS_AUTOSAVE, STATUS_ENVOY,
                STATUS_CONVOY, STATUS_VERSION)

# The compact grid, laid out BY MEANING rather than by dividing the list
# in half: the three subsystems that can be up or down read down one
# column, and the two housekeeping rows sit in the other. A naive split
# put Convoy in a different column from Embody and Envoy, which reads as
# though it belongs to something else.
# The autosave AGE gets its own cell under its label rather than sitting
# beside it. "Autosaved 1h ago" was the widest cell in the grid at 18
# characters, and the widest cell sets the font for everything; split
# across two lines the column needs 11, which is most of a 30% font
# increase. It also balances the grid -- column two had two entries
# against column one's three, so the age lands in a slot that was empty.
STATUS_AUTOSAVE_AGE = "autosave_age"

COLUMN_LAYOUT = (
    (STATUS_EMBODY, STATUS_ENVOY, STATUS_CONVOY),
    (STATUS_AUTOSAVE, STATUS_AUTOSAVE_AGE, STATUS_VERSION),
)

STATUS_LABELS = {
    STATUS_EMBODY: "Embody",
    STATUS_AUTOSAVE: "Autosaved",
    STATUS_ENVOY: "Envoy",
    STATUS_CONVOY: "Convoy",
    STATUS_VERSION: "Version",
}


def _labelled(entry, label):
    """Give a row its own label instead of the fixed column heading."""
    entry["label"] = label
    return entry


def embody_step(status):
    """The Embody COMP's own Status parameter."""
    text = _text(status)
    low = text.lower()
    if not text:
        return _entry(IDLE)
    if low.startswith("disabled"):
        return _entry(SKIPPED, detail=text)
    if "failed" in low or low.startswith("error"):
        return _entry(FAILED, detail=text)
    if low.startswith("enabled"):
        return _entry(DONE, detail=text)
    return _entry(RUNNING, detail=text)


def clock_seconds(text):
    """Seconds-since-midnight from an HH:MM:SS inside `text`, or None."""
    for chunk in _text(text).replace(",", " ").split():
        parts = chunk.split(":")
        if len(parts) != 3:
            continue
        try:
            h, m, sec = (int(x) for x in parts)
        except ValueError:
            continue
        if 0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60:
            return h * 3600 + m * 60 + sec
    return None


def ago_text(seconds):
    """'12s ago' / '3m ago' / '2h ago'. Freshness, not a timestamp.

    A wall-clock time makes the reader do the arithmetic; the thing they
    actually want to know is whether the save is recent.
    """
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def autosave_step(status, now_seconds=None):
    """TDN auto-save. 'Saved <time>' is the healthy resting state.

    The saved TIME is converted to an AGE. Crossing midnight is handled
    by wrapping rather than reporting a negative age, which would render
    as an empty cell exactly once a day.
    """
    text = _text(status)
    low = text.lower()
    if not text:
        return _entry(IDLE)
    if low.startswith(("off", "disabled")):
        return _entry(SKIPPED, detail=text)
    if "failed" in low or low.startswith("error"):
        return _entry(FAILED, detail=text)
    if low.startswith(("saving", "exporting", "writing")):
        return _entry(RUNNING, detail=text)
    if low.startswith("saved"):
        stamp = clock_seconds(text)
        if stamp is not None:
            if now_seconds is None:
                # Compare against a clock in the SAME zone as the stamp.
                # Embody writes local time when Localtimestamps is on
                # (the default) and UTC when it is off, and a session
                # that started before this change still holds a UTC
                # string -- so the marker in the text decides, not an
                # assumption about which one we are looking at.
                import datetime
                now = (datetime.datetime.utcnow() if "utc" in low
                       else datetime.datetime.now())
                now_seconds = (now.hour * 3600 + now.minute * 60
                               + now.second)
            delta = int(now_seconds) - stamp
            if delta < 0:
                delta += 86400          # the save was before midnight
            return _entry(DONE, detail=ago_text(delta) or text)
        return _entry(DONE, detail=text)
    return _entry(RUNNING, detail=text)


def version_step(version, update_status, autoupdate=None):
    """Version, and whether an update is waiting.

    An AVAILABLE update is STALLED, not FAILED and not DONE: nothing is
    broken, but something is waiting on the user. That is exactly the
    'Check and Notify' contract -- Autoupdate=notify checks once at
    startup and reports availability rather than installing behind you.
    """
    version = _text(version) or "unknown"
    # The LABEL column carries "v6.0.225"; the word "Version" would spend
    # seven characters saying what the number already says.
    label = "v%s" % version
    text = _text(update_status)
    low = text.lower()
    mode = _text(autoupdate).lower()
    if mode == "off" or low.startswith("disabled"):
        return _labelled(_entry(SKIPPED, detail="updates off"), label)
    if "available" in low or "newer" in low:
        return _labelled(_entry(STALLED, detail=text), label)
    if "failed" in low or low.startswith("error"):
        return _labelled(_entry(FAILED, detail=text), label)
    if low.startswith(("checking", "download", "installing")):
        return _labelled(_entry(RUNNING, detail=text), label)
    if not text or "up to date" in low:
        return _labelled(_entry(DONE, detail="up to date"), label)
    # A refusal with a reason (a git checkout, an unwritable install) is
    # not a failure of Embody -- it is a stated no-op, said plainly.
    return _labelled(_entry(SKIPPED, detail=text), label)


def status_row_text(key, step, label_width=9, width=None):
    """One settled-status line: label, then what it is doing. No bar.

    Truncation is MARKED. Some of these strings are long (the updater's
    dev-checkout refusal is a full sentence), and a silent cut at the
    panel edge reads as though the message simply ended -- which is how
    a truncated warning becomes a misread one.
    """
    label = ((step or {}).get("label")
             or STATUS_LABELS.get(key, key))[:label_width].ljust(label_width)
    detail = (compact_status((step or {}).get("detail"))
              or value_text(step))
    line = "%s %s" % (label, detail)
    if width:
        width = int(width)
        if width > 3 and len(line) > width:
            line = line[:width - 3].rstrip() + "..."
        else:
            line = line[:width]
    return line


def live_status_rows(embody, label_width=9, width=None):
    """(key, text, rgb) per settled-status row, from live parameters."""
    def par(name):
        try:
            value = getattr(embody.par, name, None)
            return value.eval() if value is not None else ""
        except Exception:
            return ""

    steps = {
        STATUS_EMBODY: embody_step(par("Status")),
        STATUS_AUTOSAVE: autosave_step(par("Autosavestatus")),
        STATUS_ENVOY: envoy_step(par("Envoystatus")),
        STATUS_CONVOY: convoy_step(par("Convoystatus")),
        STATUS_VERSION: version_step(par("Version"), par("Updatestatus"),
                                     par("Autoupdate")),
    }
    return [(key, status_row_text(key, steps[key], label_width, width),
             STATE_RGB.get(steps[key].get("state"), STATE_RGB[IDLE]))
            for key in STATUS_ORDER]


def is_installing(snap):
    """True while any startup step is still moving or blocked.

    The ONE switch between the two modes, so the panel and any test
    agree on which question is being asked.
    """
    return not (snap or {}).get("summary", {}).get("settled", True)


# Set once, the first time this session is seen fully settled. Bars
# answer "how far along is startup?", which stops being a question the
# moment startup finishes -- after that a Convoy heartbeat must not throw
# the panel back to a progress view. Module-level because it is a
# per-session observation, not project state; reset_session() keeps tests
# from depending on order.
_SESSION = {"settled_once": False, "problem_key": None,
            "problem_since": None, "startup": False}

# How long a problem must PERSIST before it is allowed to change the
# layout. Convoy's own vocabulary passes through "Not installed" and
# "Needs repair" while it installs or updates itself -- real states, but
# momentary ones -- so treating them as failures made the panel expand
# to five rows and snap back every time the host app cycled. A genuine
# failure is still there a few seconds later; an update is not.
PROBLEM_DWELL_S = 6.0

# How long a step must stay RUNNING before its row spends width on an
# elapsed clock. NOT zero: cell width sets column width sets font size,
# so a clock ticking through every routine heartbeat relays the whole
# panel once a second -- the exact flap the compact view exists to stop.
# Past this the step is no longer routine, and a measured 2:14 is the
# only thing on the panel that can tell a wedge from a slow machine.
STUCK_DWELL_S = 20.0


def stuck_clock(step, now=None, dwell=STUCK_DWELL_S):
    """'2:14' for a step that has been RUNNING too long, else ''."""
    if (step or {}).get("state") != RUNNING:
        return ""
    started = (step or {}).get("started")
    if started is None or now is None:
        return ""
    try:
        if (float(now) - float(started)) < float(dwell):
            return ""
    except (TypeError, ValueError):
        return ""
    return elapsed_text(step, now)


def reset_session():
    """Forget everything observed this session (tests, a genuine re-init).

    The published record and the observed clocks go with it: a re-init
    that kept them would replay the previous open's counts and date a new
    RUNNING state to a start that happened before the reset.
    """
    _SESSION["settled_once"] = False
    _SESSION["problem_key"] = None
    _SESSION["problem_since"] = None
    _SESSION["startup"] = False
    _PUBLISHED.clear()
    _SINCE.clear()


def begin_startup(embody=None, now=None):
    """The open sequence has STARTED. Called from the startup chain.

    Declared rather than inferred: see the module docstring. Also drops
    any record from a previous open, so a re-init cannot replay the last
    session's counts.
    """
    if embody is not None:
        clear_published(embody)
    _SESSION["startup"] = True
    _SESSION["settled_once"] = False


def end_startup(embody=None):
    """The open sequence has finished SCHEDULING; not necessarily working.

    This releases the hold, it does not latch: the catalog scan legitimately
    runs for hundreds of frames past the last startup callback, so the
    viewer stays on bars until the snapshot itself settles.
    """
    _SESSION["startup"] = False


def persistent_problem(steps, now=None, dwell=PROBLEM_DWELL_S):
    """Has a problem been continuously present long enough to matter?

    Keyed on WHICH subsystems are unhappy, so a different failure
    restarts the clock rather than inheriting the previous one's dwell.
    With no clock (`now` is None) it answers False: a caller that cannot
    time the state does not get to reflow the panel on it.
    """
    key = tuple(sorted(k for k, step in steps
                       if (step or {}).get("state") in (FAILED, STALLED)))
    if not key:
        _SESSION["problem_key"] = None
        _SESSION["problem_since"] = None
        return False
    if key != _SESSION["problem_key"]:
        _SESSION["problem_key"] = key
        _SESSION["problem_since"] = now
        return False
    since = _SESSION["problem_since"]
    if since is None:
        # First seen by a caller with no clock, so the dwell starts at the
        # first sighting that CAN time it. Pinning problem_since to None
        # instead meant one untimed call -- a headless width probe, a
        # panel expression evaluated before absTime is meaningful -- froze
        # that failure out of the explanatory view for the whole session.
        _SESSION["problem_since"] = now
        return False
    if now is None:
        return False
    return (float(now) - float(since)) >= float(dwell)


def startup_snapshot(embody, now=None):
    """The startup snapshot while startup is in flight, else None.

    THE ONE LATCH, so live_view and live_grid can never disagree about
    which question the panel is answering -- they drifted once already
    and it read on screen as the layout freaking out.

    Two ways to be in flight, and both are needed. `startup` is declared
    by the open sequence (frame one looks perfectly settled, which is why
    inference alone latched the viewer before anything ran); after that
    hold is released, any step still moving keeps the bars up, which is
    what covers a catalog scan that outlives the startup callbacks.
    """
    if _SESSION["settled_once"]:
        return None
    snap = live_snapshot(embody, now=now)
    if _SESSION["startup"] or is_installing(snap):
        return snap
    _SESSION["settled_once"] = True
    return None


def live_view(embody, now=None, phase=0.0, bar_width=20, label_width=9,
              width=None):
    """The rows to draw, whichever mode we are in.

    Positional by design: the two modes do not share a row SET (bars
    describe startup steps, statuses describe subsystems), so the panel
    binds row i to element i rather than to a fixed key.
    """
    snap = startup_snapshot(embody, now=now)
    if snap is not None:
        return rows(snap, now=now, phase=phase,
                    label_width=label_width, bar_width=bar_width)
    return live_minimal_rows(embody, now=now)


# -- compact status text --------------------------------------------------
#
# WIDTH IS FONT SIZE in this viewer, so every character the value column
# stops spending makes the glyphs bigger. Two things are pure waste:
#
#   1. Repeating the label. The row already says "Envoy", so "Running on
#      port 9870" only needs to say "port 9870".
#   2. Spelling out the state. Colour already carries running / failed /
#      skipped, so the words do not have to.
#
# The FULL text stays in step['detail'] for logs and the parameter
# itself; this is only what the row renders.
_COMPACT_PREFIX = (
    ("running on ", ""),
    ("installing deps", "installing deps"),
    ("preparing python environment", "preparing env"),
    ("restarting after ", "restarting: "),
    ("reviving (watchdog)", "reviving"),
    ("needs repair", "needs repair"),
    ("not installed", "not installed"),
    ("install failed", "install failed"),
    ("consent required", "consent needed"),
    ("waiting for", "waiting:"),
    ("managed by another supervisor", "external supervisor"),
    ("this is the embody dev checkout", "dev checkout"),
    ("saved ", ""),
    ("error: ", ""),
)


def compact_status(text, max_width=None):
    """The shortest phrasing that still answers the row's question."""
    raw = _text(text)
    if not raw:
        return ""
    low = raw.lower()
    out = raw
    for prefix, replacement in _COMPACT_PREFIX:
        if low.startswith(prefix):
            out = replacement + raw[len(prefix):]
            break
    # An explanatory clause after ' -- ', and a parenthetical aside, are
    # DETAIL rather than status. Both are kept in step['detail'].
    for marker in (" -- ", " ("):
        cut = out.find(marker)
        if cut > 0:
            out = out[:cut]
    out = out.replace(" UTC", "").strip().rstrip(".").strip()
    if max_width and len(out) > int(max_width) > 3:
        out = out[:int(max_width) - 3].rstrip() + "..."
    return out


def row_height_for(fontsize, spacing=LINE_SPACING):
    """The row box for a given glyph size.

    Height FOLLOWS the font here rather than the other way round: the
    font is set by the width budget, so letting rows divide a fixed
    panel height instead would leave dead space between the lines and
    buy nothing.
    """
    try:
        return max(1.0, float(fontsize) * (1.0 + float(spacing)))
    except (TypeError, ValueError):
        return 1.0


def font_for_width(panel_width, target_chars=TARGET_ROW_CHARS,
                   minimum=7.0, maximum=40.0):
    """Font size from the WIDTH budget alone."""
    try:
        by_width = float(panel_width) / (float(target_chars) * MONO_ADVANCE)
    except (TypeError, ValueError, ZeroDivisionError):
        return minimum
    return max(minimum, min(maximum, by_width))


# == minimal mode: a glyph per subsystem, two columns =====================
#
# The state is carried by a MARK, not a sentence: a tick when it is fine,
# a cross when it is not. That does two things at once -- it is read at a
# glance, and it makes the happy-path words ("Enabled", "Connected", "up
# to date") redundant, which is what pays for the bigger font.
#
# U+2713/U+2717 are deliberate. U+2714 (heavy check) falls back to an
# emoji font in Consolas and renders in the wrong colour and weight --
# verified by capture, not assumed.
# The busy mark ANIMATES while work is actually in flight, because a
# static mark cannot distinguish "installing" from "hung" -- which is the
# whole reason this readout exists. ASCII on purpose: the cells render in
# Consolas and these four are guaranteed present in it. A braille or block
# spinner looks smoother but falls back to another font when a glyph is
# missing, and a fallback glyph is not the same advance width -- which
# would resize the column, the font and the whole panel on every frame of
# the animation. Every frame here is exactly ONE character, so the layout
# cannot move: widths come from len(text) (see MONO_ADVANCE).
SPINNER_FRAMES = ("|", "/", "-", "\\")

# Slow enough to be cheap, fast enough to read as motion. One publish per
# step (~0.65 ms measured) is the animation's entire cost, and it is paid
# ONLY while a step is genuinely running: the panel re-arms its tick only
# while its own rows would differ a tick from now, so the animation stops
# when the work does and a settled project cooks nothing at all.
SPINNER_FPS = 8.0


def spinner_frame(now=None, fps=SPINNER_FPS):
    """The busy mark for this instant. Pure; always ONE character."""
    try:
        return SPINNER_FRAMES[int(float(now or 0.0) * float(fps))
                              % len(SPINNER_FRAMES)]
    except (TypeError, ValueError):
        return SPINNER_FRAMES[0]


def busy_glyph(now=None, animate=False):
    """The RUNNING mark: animated while installing, static otherwise.

    `animate` is the caller's answer to "is this an installation, or a
    settled project merely reporting state?". Settled, the ellipsis is
    both correct and free -- an unchanging cell publishes nothing, so the
    panel returns to zero cooks the moment startup ends.
    """
    return spinner_frame(now) if animate else GLYPH_BUSY



GLYPH_OK = "\u2713"
GLYPH_BAD = "\u2717"
GLYPH_WAIT = "!"
GLYPH_BUSY = "\u2026"
GLYPH_SKIP = "-"

STATE_GLYPH = {
    DONE: GLYPH_OK,
    FAILED: GLYPH_BAD,
    STALLED: GLYPH_WAIT,
    RUNNING: GLYPH_BUSY,
    SKIPPED: GLYPH_SKIP,
    IDLE: " ",
}

# Values that say nothing the tick has not already said.
_REDUNDANT_OK = ("enabled", "connected", "up to date", "running", "ok",
                 "saved", "online")

# Rows whose detail is worth the width even when everything is fine. The
# autosave AGE is the one genuinely useful happy-path fact here -- "3m
# ago" answers a question a tick cannot ("is my work safe?"), unlike
# "Connected", which only repeats the tick.
_ALWAYS_SHOW = ()


def cell_text(key, step, verbose=False, now=None, animate=False):
    """One cell: mark, name, and a reason ONLY when there is one.

    On the happy path the detail is suppressed -- a tick beside "Convoy"
    already says "Connected", and spending 10 characters to repeat it
    costs font size everywhere else.

    The one thing that outranks the suppression is an elapsed clock on a
    step that has been RUNNING past STUCK_DWELL_S. A busy mark alone
    reads the same at four seconds and at forty minutes, which is how a
    wedged dependency install looked exactly like a slow one.
    """
    step = step or {}
    state = step.get("state")
    glyph = STATE_GLYPH.get(state, " ")
    if state == RUNNING:
        # Animated ONLY while something is installing. In a settled
        # project the mark is static, which is what keeps the panel at
        # zero cooks once startup is over.
        glyph = busy_glyph(now, animate=animate)
    label = (step.get("label") or STATUS_LABELS.get(key, key))
    clock = stuck_clock(step, now)
    if clock:
        return "%s %s %s" % (glyph, label, clock)
    detail = compact_status(step.get("detail"))
    # THE COMPACT VIEW CARRIES NO REASONS AT ALL -- only the mark.
    #
    # This is what stops the panel resizing on a timer. Cell width sets
    # the column width, which sets the font, which relays the whole
    # panel; so a transient reason ("Convoy Registering", 20 chars,
    # against "Convoy" at 8) made the layout visibly jump every time a
    # subsystem checked in. The glyph already says busy / fine / broken,
    # and a reason nobody can read before it changes is not information.
    # Reasons live in the expanded view, which only a real problem
    # triggers and which therefore does not flicker.
    show = detail and (verbose or key in _ALWAYS_SHOW)
    return ("%s %s %s" % (glyph, label, detail)).rstrip() if show \
        else "%s %s" % (glyph, label)


def worst_state(steps):
    """The state a row should be COLOURED by: the most alarming in it."""
    order = (FAILED, STALLED, RUNNING, DONE, SKIPPED, IDLE)
    present = [s.get("state") for s in steps if s]
    for state in order:
        if state in present:
            return state
    return IDLE


def grid_rows(cells, columns=2, gap=2, col_width=None):
    """Lay cells out in columns, COLUMN-MAJOR, as (text, rgb) rows.

    Column-major so each column still reads top-to-bottom; row-major
    would interleave unrelated subsystems across the fold.
    """
    cells = list(cells)
    columns = max(1, int(columns))
    per = (len(cells) + columns - 1) // columns
    if col_width is None:
        col_width = max([len(t) for _k, t, _s in cells] or [1])
    col_width = int(col_width)
    out = []
    for r in range(per):
        picked = []
        for cnum in range(columns):
            index = cnum * per + r
            if index < len(cells):
                picked.append(cells[index])
        if not picked:
            continue
        text = (" " * gap).join(
            t.ljust(col_width) if i < len(picked) - 1 else t
            for i, (_k, t, _s) in enumerate(picked)).rstrip()
        rgb = STATE_RGB.get(worst_state([s for _k, _t, s in picked]),
                            STATE_RGB[IDLE])
        out.append(("row%d" % r, text, rgb))
    return out


GRID_GAP = 2


def layout_rows(by_key, layout, gap=GRID_GAP, now=None):
    """Compose rows from an EXPLICIT column layout.

    Columns may be different lengths (three subsystems beside two
    housekeeping rows), so this pads by column rather than assuming an
    even split.
    """
    columns = [[(key, cell_text(key, by_key[key], now=now), by_key[key])
                for key in col if key in by_key] for col in layout]
    widths = [max([len(t) for _k, t, _s in col] or [0]) for col in columns]
    depth = max([len(col) for col in columns] or [0])
    out = []
    for r in range(depth):
        picked, parts = [], []
        for cnum, col in enumerate(columns):
            if r < len(col):
                picked.append(col[r][2])
                parts.append(col[r][1].ljust(widths[cnum]))
            elif any(r < len(c) for c in columns[cnum + 1:]):
                parts.append(" " * widths[cnum])
        text = (" " * gap).join(parts).rstrip()
        out.append(("row%d" % r, text,
                    STATE_RGB.get(worst_state(picked), STATE_RGB[IDLE])))
    return out


# Bar cells in the startup grid. The row is label(9) + bar + value(7)
# plus three separators, so 14 keeps it inside TARGET_ROW_CHARS -- the
# same budget the settled rows are sized against, which is what stops the
# font jumping when the panel changes mode.
GRID_BAR_WIDTH = 14

# Sweep cycles per second for an indeterminate bar. Slow enough to read
# as deliberate motion rather than strobing at whatever frame rate the
# project happens to be running.
SWEEP_RATE = 0.5


def startup_grid(snap, now=None, bar_width=GRID_BAR_WIDTH, label_width=9):
    """The startup bars, shaped like the grid the panel already draws.

    ONE cell per row. The viewer binds row i / cell j positionally, so a
    one-cell row simply leaves the second slot empty -- five bars fit the
    five row slots with no change to a single panel expression, which is
    the whole reason the bars are returned in grid shape rather than as
    their own row list.
    """
    phase = ((now or 0.0) * SWEEP_RATE) % 1.0
    return [[(key, text, rgb)]
            for key, text, rgb in rows(snap, now=now, phase=phase,
                                       label_width=label_width,
                                       bar_width=bar_width)]


def live_grid(embody, verbose=False, now=None):
    """Rows of CELLS, each carrying its OWN colour.

    A row is a layout accident, not a thing: Envoy and the version
    number share row two only because that is where they landed. Giving
    the row one colour meant a disabled Envoy greyed out the version
    beside it, reporting a state that subsystem is not in.
    """
    snap = startup_snapshot(embody, now=now)
    if snap is not None:
        return startup_grid(snap, now=now)
    steps = _live_steps(embody, now=now)
    # ONLY A PROBLEM EXPANDS THE GRID. The first rule was "anything not
    # done or skipped", which made every routine heartbeat reflow the
    # whole panel: Convoy re-checks its host app, Envoy restarts on a
    # reinit, the autosave runs. Those are RUNNING, not broken. A layout
    # that jumps every few seconds reads as a fault in the viewer rather
    # than news about the project -- and a real failure persists, while
    # activity does not.
    healthy = not persistent_problem(steps, now)
    # Animate the busy mark ONLY while an install is genuinely in flight.
    # `startup_snapshot` returning None above means startup is over, so the
    # only animating case left here is a subsystem still installing (a
    # Convoy host app coming up after the open sequence). A settled project
    # gets the static ellipsis and therefore publishes nothing.
    animate = any((step or {}).get("state") == RUNNING
                  and (step or {}).get("detail", "").lower().startswith(
                      ("install", "building", "preparing", "starting"))
                  for _k, step in steps)
    by_key = dict(steps)
    # ONE column containing every key -- not one column PER key. The
    # latter reads the same in a tuple literal and is catastrophically
    # different: five columns of one row each, rendered side by side,
    # with three of them past the panel's two cell slots and therefore
    # invisible exactly when something has gone wrong.
    layout = COLUMN_LAYOUT if healthy else (tuple(STATUS_ORDER),)
    show_detail = verbose or not healthy
    def cell(key):
        if key == STATUS_AUTOSAVE_AGE:
            # A continuation line: indented under its label, no mark of
            # its own, and it inherits the autosave colour.
            step = by_key[STATUS_AUTOSAVE]
            age = compact_status(step.get("detail"))
            return (key, "  %s" % age if age else "", step)
        return (key, cell_text(key, by_key[key], show_detail, now=now,
                               animate=animate),
                by_key[key])

    columns = [[cell(key) for key in col
                if key in by_key or key == STATUS_AUTOSAVE_AGE]
               for col in layout]
    widths = [max([len(t) for _k, t, _s in col] or [0]) for col in columns]
    depth = max([len(col) for col in columns] or [0])
    grid = []
    for r in range(depth):
        row = []
        for cnum, col in enumerate(columns):
            if r < len(col):
                key, text, step = col[r]
                row.append((key, text.ljust(widths[cnum]),
                            STATE_RGB.get(step.get("state"),
                                          STATE_RGB[IDLE])))
        if row:
            grid.append(row)
    return grid


def _live_steps(embody, now=None):
    """(key, step) for every settled-status row, from live parameters.

    Every row is stamped, so a subsystem that stops moving carries the
    clock that proves it: the settled view is where a wedge hides
    longest, because a busy mark reads the same forever.
    """
    def par(name):
        try:
            value = getattr(embody.par, name, None)
            return value.eval() if value is not None else ""
        except Exception:
            return ""

    return [
        (key, _stamp(embody, key, step, now)) for key, step in (
            (STATUS_EMBODY, embody_step(par("Status"))),
            (STATUS_AUTOSAVE, autosave_step(par("Autosavestatus"))),
            (STATUS_ENVOY, envoy_step(par("Envoystatus"))),
            (STATUS_CONVOY, convoy_step(par("Convoystatus"))),
            (STATUS_VERSION, version_step(par("Version"),
                                          par("Updatestatus"),
                                          par("Autoupdate"))),
        )
    ]


def live_minimal_rows(embody, columns=2, verbose=False, now=None):
    """The settled view, as marks in a grid."""
    steps = _live_steps(embody, now=now)
    # ONLY A PROBLEM EXPANDS THE GRID. The first rule was "anything not
    # done or skipped", which made every routine heartbeat reflow the
    # whole panel: Convoy re-checks its host app, Envoy restarts on a
    # reinit, the autosave runs. Those are RUNNING, not broken. A layout
    # that jumps every few seconds reads as a fault in the viewer rather
    # than news about the project -- and a real failure persists, while
    # activity does not.
    healthy = not persistent_problem(steps, now)
    by_key = dict(steps)
    if not healthy:
        # Something needs a REASON, and a reason does not fit half a
        # panel. Drop to one column and let the rows say what is wrong --
        # the layout changing is itself a signal.
        cells = [(key, cell_text(key, by_key[key], True, now=now),
                  by_key[key]) for key in STATUS_ORDER]
        return grid_rows(cells, columns=1)
    return layout_rows(by_key, COLUMN_LAYOUT, now=now)


def font_for_rows(panel_width, texts, minimum=7.0, maximum=40.0):
    """Font sized to the CONTENT, not to a fixed character budget.

    Bars are fixed-length by construction so they use the budget; the
    settled view is not, and sizing it to the longest row it actually
    renders is what makes the minimal mode as large as it can be.
    """
    try:
        longest = max([len(t) for t in texts] or [1])
        return max(minimum, min(maximum, float(panel_width)
                                / (max(1, longest) * MONO_ADVANCE)))
    except (TypeError, ValueError, ZeroDivisionError):
        return minimum


def _panel_row_budget(embody, now=None):
    """Vertical room ONE row may occupy, or None when it cannot be read.

    The viewer's height is locked to the Embody node's aspect so the node
    viewer draws it edge to edge with no letterbox band. That makes the
    height a FIXED budget the content must live inside, rather than
    something the content grows. Sizing the font from the width alone
    then overflows: at the full five-row startup grid the rows need more
    height than a panel proportioned for the node can give, and the last
    row is clipped exactly when the most is happening.

    Safe against recursion by construction: the panel's own height
    expression reads the node dimensions, never the font, so asking for
    it here cannot re-enter live_font. Returns None headless (no panel,
    no TD) so width-only sizing still applies in tests.
    """
    try:
        # PANEL_COMP, not a literal spelled twice: this lookup kept the old
        # name after the COMP was renamed viz_startup -> viz_status, so it
        # silently returned None and the row budget quietly fell back to
        # width-only sizing -- no error, just a font sized for the wrong
        # box. One constant means the next rename cannot half-land.
        panel = embody.op(PANEL_COMP)
        if panel is None:
            return None
        usable = (float(panel.par.h.eval())
                  - float(panel.par.margint.eval())
                  - float(panel.par.marginb.eval()))
        rows = len(live_grid(embody, now=now))
        if rows <= 0 or usable <= 0:
            return None
        return usable / rows
    except Exception:
        return None


def live_font(embody, panel_width, row_height=None, now=None):
    """Font sized from EXACTLY what the panel draws.

    THE BUG THIS REPLACES, measured on the live panel every ~30s: this
    used to ask is_installing() and fall back to the bar budget, while
    the panel itself renders live_grid(). A Convoy heartbeat then left
    the grid on screen but sized the font for bars -- 39.1 to 24.9 and
    back, with the column widths never moving, which is why it read as
    the layout freaking out rather than as a font change.

    The cure is not a better guess about the mode: it is deriving the
    size from live_col_chars(), the same numbers that set the cell
    widths. Content and font cannot disagree if they come from one
    source.
    """
    cols = live_col_chars(embody, now=now)
    total = sum(cols) + GRID_GAP * max(0, len(cols) - 1)
    size = font_for_rows(panel_width, ["x" * max(1, total)])
    if row_height is None:
        # No explicit budget -> take the panel's own, so the grid always
        # fits the aspect-locked height instead of clipping its last row.
        row_height = _panel_row_budget(embody, now=now)
    if row_height:
        try:
            size = min(size, float(row_height) * ROW_FONT_RATIO)
        except (TypeError, ValueError):
            pass
    return max(7.0, size)


def live_col_chars(embody, now=None):
    """Character width of each grid column.

    The panel packs columns to their CONTENT, not into equal halves --
    an even split sized the font for one budget and then gave each cell
    a different one, which clipped the wider column ("Autosaved 1h ago"
    lost its "ago").
    """
    grid = live_grid(embody, now=now)
    if not grid:
        return [1]
    columns = max(len(row) for row in grid)
    out = []
    for i in range(columns):
        out.append(max([len(row[i][1]) for row in grid if i < len(row)]
                       or [0]))
    return out or [1]


# --- Publishing: the readout as finished rows, computed ONCE per change ---
#
# The panel used to evaluate this module from every cell parameter: 90
# calls per cook, each rebuilding the whole grid, and every one of them
# time-dependent through absTime.seconds -- so a visible panel recomputed
# the entire readout 60 times a second (measured 6.7 ms per cook, 40% of
# a 60 fps frame) to show values that change a handful of times per
# session. These two functions are what the publisher writes into a table
# instead; the cells then read finished strings and never call in here.

# The COMP that draws this readout. Named ONCE: the lookup below used to
# spell it inline, so renaming the COMP left a dangling op() that returned
# None without an error.
PANEL_COMP = "viz_status"

PANEL_ROWS = 5          # rowboxes the panel ships
PANEL_COLS = 2          # cells per rowbox
TABLE_HEADER = ("name", "value", "r", "g", "b", "show")


def will_change(embody, now=None, ahead=1.0, panel_width=600,
                rows=None):
    """Would the readout LOOK different `ahead` seconds from now?

    The publisher is event-driven and must go completely silent when
    nothing is moving, so it needs to know whether the panel owns a
    value that changes on its own -- an elapsed clock on a step that is
    still working, or an "N ago" that is about to tick over. Asking
    "is anything RUNNING?" is the wrong question and was measurably
    wrong: a healthy session reports Envoy `Connected` and Convoy `Off`
    as RUNNING, so that predicate is true forever and would keep a timer
    alive for the life of the session to redraw nothing.

    Comparing the rendered rows against themselves one tick later is the
    exact question instead: it is true only while some string is really
    about to move, and it needs no table of which states own clocks.
    """
    try:
        # `rows` lets the caller hand in what it JUST computed. Without it
        # every arm check rebuilt the readout twice, and the two-horizon
        # check did it four times -- five full rebuilds per publish to
        # decide whether to publish again, which is the same
        # recompute-per-frame waste this whole design removed from the
        # cells.
        current = rows if rows is not None else table_rows(
            embody, panel_width, now=now)
        return current != table_rows(embody, panel_width,
                                     now=(now or 0.0) + float(ahead))
    except Exception:
        return False


def _col_chars_of(grid):
    """live_col_chars, but from a grid already in hand."""
    if not grid:
        return [1]
    columns = max(len(row) for row in grid)
    out = []
    for i in range(columns):
        out.append(max([len(row[i][1]) for row in grid if i < len(row)]
                       or [0]))
    return out or [1]


def _font_of(embody, grid, chars, panel_width, now=None):
    """live_font, but from a grid already in hand (same arithmetic)."""
    total = sum(chars) + GRID_GAP * max(0, len(chars) - 1)
    size = font_for_rows(panel_width, ["x" * max(1, total)])
    row_height = _panel_row_budget(embody, now=now)
    if row_height:
        try:
            size = min(size, float(row_height) * ROW_FONT_RATIO)
        except (TypeError, ValueError):
            pass
    return max(7.0, size)


def table_rows(embody, panel_width, now=None, verbose=False):
    """The whole readout as table rows: layout numbers, then cell strings.

    Returns rows of (name, value, r, g, b, show) -- `value` carries the
    number for a layout row and the finished text for a cell row. Pure
    apart from the parameter reads every other live_* helper already
    does, so the shape is unit-testable without TouchDesigner.
    """
    try:
        width = float(panel_width)
    except (TypeError, ValueError):
        width = 0.0
    pad = int(width * 0.03)
    # ONE grid per publish. live_font -> live_col_chars -> live_grid means
    # the naive call sequence rebuilds it four times; the readout is the
    # same object every time, so derive the rest from a single build.
    grid = live_grid(embody, verbose=verbose, now=now)
    chars = _col_chars_of(grid)
    font = _font_of(embody, grid, chars, width - pad * 2, now=now)
    rows = [
        ("font", "%.4f" % font, "", "", "", ""),
        ("rowh", str(int(row_height_for(font))), "", "", "", ""),
        ("col0w", str(pad + int(((chars + [1])[0] + 2)
                                * font * MONO_ADVANCE)), "", "", "", ""),
        ("pad", str(pad), "", "", "", ""),
    ]
    for r in range(PANEL_ROWS):
        row = grid[r] if r < len(grid) else []
        rows.append(("row%d" % r, "", "", "", "", "1" if row else "0"))
        for c in range(PANEL_COLS):
            name = "r%dc%d" % (r, c)
            if c < len(row):
                cell = row[c]
                text = cell[1]
                rgb = cell[2]
                rows.append((name, text, "%.4f" % rgb[0], "%.4f" % rgb[1],
                             "%.4f" % rgb[2], "1"))
            else:
                rows.append((name, "", "0", "0", "0", "0"))
    return rows
