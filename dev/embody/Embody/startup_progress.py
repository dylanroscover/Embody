"""What the Embody node's status readout shows, as data.

ONE VIEW, ONE QUESTION: what is the state of each subsystem right now?
Embody, the auto-save, Envoy, Convoy and the version each get a row, and
a row is a MARK plus a name. There was a second view -- a column of
progress bars drawn while the project opened -- and it is gone. For the
subsystems it drew, "how far along is this?" had no honest answer (a
dependency install is one opaque `uv pip install`), so the bars swept
without measuring anything, and every state that could pin them on
screen was another way for the panel to get stuck. The startup states
are still reported; they are reported in the same one view as
everything else.

THE RULE THE BARS LEAVE BEHIND: never fake a measurement. Nothing here
renders a percentage, because nothing here has a denominator to render
one from. A busy row shows an ELAPSED CLOCK instead, and only once it
has been busy longer than STUCK_DWELL_S -- a measured 2:14 proves the
step is alive rather than wedged, which is the one thing a mark cannot
do: it reads the same at four seconds and at forty minutes.

IT DERIVES FROM PARAMETERS; NOTHING PUBLISHES INTO IT. Every row comes
from a parameter on the Embody COMP -- Status, Autosavestatus,
Envoystatus, Convoystatus, Version, Updatestatus, Autoupdate -- read at
render time by _live_steps. There was a second source: a module-level
record the startup phases published their counts into. When the bars
went, that record's last reader went with them, and it is DELETED
rather than kept warm, because a write-only record is indistinguishable
from a working one until somebody trusts it. Deriving from the
parameters cannot drift from what the user reads, and every writer of
those parameters is a writer of this readout for free.

THE CLOCK IS OBSERVED, NOT READ. A parameter carries no timestamp, so
_stamp records when a row entered its current non-terminal state, keyed
on state AND detail so two phases of one sequence are two measurements.
That record is the only state this module keeps. Module-level on
purpose: COMP storage recooks on every write and serializes into the
.toe and into Embody's own .tdn, so last session's observations would
come back on disk and be replayed as this session's.

ONE LAYOUT. Five rows, one subsystem each, mark + label + reason,
always -- during an install, settled, and broken alike. The font is
pinned to TARGET_ROW_CHARS so text changes inside the budget move
nothing. The readout must never change SHAPE: shape changes read as
faults in the panel, and hiding the reason (or the autosave age) on
the happy path traded away the information the panel exists to show.

RENDERING IS COMPUTED ONCE PER EVENT. table_rows() turns the readout
into finished strings for viz_status/status_publish to write into a
table; the panel's cells read that table and never call in here. The
other way round cost 90 module calls per cook, all time-dependent, on a
panel showing values that change a handful of times per session.

No `import td` and nothing at import time: the COMP is an argument and
its parameters are read through getattr, so the whole mapping is
unit-tested on the CI matrix rather than only in a live session. The one
exception is _panel_row_budget, which reaches for the panel COMP itself
to read a height -- guarded, and returning None headless.
"""

# States. STALLED is distinct from RUNNING on purpose: it means the step
# is waiting on something a user must do (save the project, answer a
# consent dialog), which is not progress and must not read as progress.
IDLE = "idle"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"
STALLED = "stalled"


def _entry(state, done=0, total=0, detail="", started=None):
    return {"state": state, "done": int(done or 0), "total": int(total or 0),
            "detail": str(detail or ""), "started": started}


def _text(value):
    return str(value or "").strip()


def envoy_step(status, started=None):
    """Derived from the Envoystatus parameter -- see the module docstring.

    No measurement exists to report: the dependency install is a single
    opaque uv call, so a RUNNING envoy step shows a busy mark and, once
    it has been busy past STUCK_DWELL_S, an elapsed clock.
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

    ONE readout, TWO producers, and the classification enumerates BOTH.
    The node line comes from convoy_client.status_text (Connected /
    Registering... / Waiting for project save / Refused: <reason> /
    Error: <detail> / No Convoy host app / Host app stale) plus
    ConvoyExt's own literals (Consent required, Error: convoy_client
    module missing); the host-app line from convoy_client.
    host_status_text (the five 'Installed -- ...' variants, Checking...,
    Installing..., Repairing runtime..., Needs repair, Install failed,
    Managed by another supervisor, installed-by-a-newer-Embody).
    Enumerating only one producer is how an Error state rendered as a
    busy spinner with a climbing clock.

    The vocabulary is CLOSED -- both producers are finite string tables
    -- so the fallback for an unknown string is IDLE-with-text, never
    RUNNING: showing the words without animation cannot fabricate
    activity, which is the failure mode this readout keeps growing back.
    A new status is a deliberate edit here, not something a default
    silently absorbs into "busy".

    Per convoy_client's own doctrine, ABSENCE IS NOT AN ERROR: absent /
    stale / no-host-app are the normal state of a machine without the
    host app running and read as resting, not failure.
    """
    text = _text(status)
    low = text.lower()
    if not text:
        return _entry(IDLE)
    if low == "disabled":
        return _entry(SKIPPED, detail=text)
    if low.startswith(("error", "refused")):
        # A host-side crash, an unreadable result, a policy refusal: all
        # actionable, none of them progress.
        return _entry(FAILED, detail=text)
    if low.startswith(("not installed", "needs repair", "install failed",
                       "installed -- no supervisor")):
        # 'Installed -- no supervisor (use Repair Convoy App)' names a
        # button the user has to press. A defect with a remedy.
        return _entry(FAILED, detail=text)
    if "installed by a newer embody" in low:
        return _entry(STALLED, detail=text)
    if low.startswith(("waiting for", "consent required",
                       "managed by another supervisor",
                       "no convoy host app", "host app stale")):
        # Blocked on the user (or on another supervisor): not progress,
        # must not animate like it. The absent/stale pair belongs HERE,
        # not in the resting branch below: Convoystatus only ever
        # carries them while Convoy is ENABLED (disabled reads
        # 'Disabled'), and an enabled mesh with no live host app is a
        # wait with a remedy -- the supervisor restart, or Start Convoy
        # App. Classified as resting they rendered a blank mark and no
        # words, and the user had to hunt the parameter page to learn
        # why their mesh was dead. STALLED is still not FAILED, which
        # keeps convoy_client's absence-is-not-an-error doctrine intact.
        return _entry(STALLED, detail=text)
    if low.startswith(("checking", "installing", "repairing",
                       "installed -- starting", "registering",
                       "registered --")):
        # Genuinely in flight: an install, a repair, a start that has
        # not finished, a registration waiting on its Envoy port.
        return _entry(RUNNING, detail=text, started=started)
    if low.startswith(("connected", "running", "host app found")):
        return _entry(DONE, detail=text)
    if low.startswith(("installed -- not running", "installed -- stopped",
                       "stopped", "not running", "idle")):
        # Resting, per the absence doctrine above. Falling through to a
        # busy default here is what pinned the old bars view after Stop
        # Convoy App.
        return _entry(IDLE, detail=text)
    return _entry(IDLE, detail=text)


def elapsed_text(step, now):
    """'0:42' since the step started, or '' when it has not.

    A real measurement with no implied denominator: it proves the step
    is alive rather than hung, which is exactly what a mark cannot do.
    """
    started = (step or {}).get("started")
    if started is None or now is None:
        return ""
    seconds = int(max(0.0, float(now) - float(started)))
    return "%d:%02d" % (seconds // 60, seconds % 60)


# Per-state colour for the row, as TD 0..1 floats. Colour carries the
# state; the glyph carries whether anything is moving. Neither is asked
# to do both.
STATE_RGB = {
    IDLE: (0.30, 0.32, 0.36),
    RUNNING: (0.30, 0.68, 0.95),
    DONE: (0.35, 0.78, 0.45),
    FAILED: (0.90, 0.30, 0.28),
    SKIPPED: (0.40, 0.44, 0.50),
    STALLED: (0.95, 0.70, 0.25),
}


# When a step entered its current non-terminal state. The parameter
# carries no timestamp, so the clock has to be OBSERVED rather than read:
# without it a wedged step shows a busy mark forever, and a busy mark
# looks identical after four seconds and after forty minutes.
#
# Module-level rather than COMP storage on purpose. store() recooks on
# every write, and storage is serialized into the .toe and into Embody's
# own .tdn -- so LAST session's observations would come back on disk and
# be replayed as this session's, which is precisely the frozen readout
# this module exists to stop. Keyed per COMP path so two Embody COMPs in
# one process cannot overwrite each other's clocks.
_SINCE = {}


def _comp_key(embody):
    """A stable id for the COMP. Reads nothing TouchDesigner-specific."""
    try:
        return str(getattr(embody, "path", "") or id(embody))
    except Exception:
        return ""


# -- live assembly --------------------------------------------------------
#
# Takes the Embody COMP as an ARGUMENT rather than reaching for a global,
# so it stays testable with a stand-in object and this module keeps its
# no-TouchDesigner-import property: nothing here imports td, and none of
# it runs at import time.
def _stamp(embody, key, step, now=None):
    """Give a step the clock its parameter cannot carry.

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


# -- responsive layout ----------------------------------------------------
#
# The row is monospace, so how many characters fit is arithmetic rather
# than guesswork: character advance is a fixed fraction of the font size.
# Consolas measures ~0.55; the constant is named so a font change is a
# one-line correction instead of a hunt through expressions.
MONO_ADVANCE = 0.55

# The widest row the readout is allowed to want, and THEREFORE the font
# size: width is the constraint that binds at node-tile sizes, so every
# character the layout stops spending buys legibility directly (46 -> 33
# was ~39% more glyph; 33 -> 23 is another ~43%, because the 33 was
# sized for the deleted two-column grid while the single column's
# longest DESIGNED row is 22 characters). This must be readable at tiny
# node-tile resolutions -- the field requirement is "as readable as
# humanly possible" -- so the budget is exactly the designed content
# and not a character more. Every compact vocabulary entry and every
# label is sized INTO it (see cell_text's per-row clamp); widening any
# string past its row budget shrinks the font everywhere, which is why
# the suite bounds every producer state structurally.
TARGET_ROW_CHARS = 23

# Font size as a fraction of row height, when height is the binding
# constraint. The row box is font * (1 + LINE_SPACING); Consolas' line
# occupies ~1.17 em, so 0.68 of the budget fills a 1.35-box without
# clipping ascenders or descenders (0.68 * 1.35 = 0.92 of the budget,
# 8% slack).
ROW_FONT_RATIO = 0.68

# Leading, as a fraction of the font size. 0.35 keeps the lines clearly
# separate while spending the least vertical space that still reads as
# separate rows -- at node-tile sizes every point of leading is a point
# of glyph given away.
LINE_SPACING = 0.35


# == the rows: one per subsystem ==========================================
#
# These keys ARE the readout. There is no second set and no second view:
# the one question the panel answers, during an install and after it, is
# WHAT IS THE STATE OF EACH SUBSYSTEM RIGHT NOW.
STATUS_EMBODY = "embody"
STATUS_AUTOSAVE = "autosave"
STATUS_ENVOY = "envoy"
STATUS_CONVOY = "convoy"
STATUS_VERSION = "version"

STATUS_ORDER = (STATUS_EMBODY, STATUS_AUTOSAVE, STATUS_ENVOY,
                STATUS_CONVOY, STATUS_VERSION)

# ONE layout. There used to be two -- a two-column compact grid while
# healthy and this list expanded while not -- and the panel switching
# shape read as a fault in the panel, while the compact side hid the
# autosave age entirely. A status readout that renders differently
# depending on how it feels is two layouts for one feature; the field
# verdict (2026-08-11) was blunt and correct. The single shape is the
# informative one: five rows, mark + label + reason, always.
STATUS_LABELS = {
    STATUS_EMBODY: "Embody",
    # 'Saved', not 'Autosaved': the row answers "how long ago did the
    # work reach disk" whoever wrote it, and the four characters go
    # straight into glyph size (the longest healthy row sets the font).
    STATUS_AUTOSAVE: "Saved",
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


def dated_stamp(text):
    """A 'YYYY-MM-DD HH:MM:SS' inside `text` as a naive datetime, or None.

    TWO SHAPES REACH THE AUTO-SAVE ROW and only one carries a date. The
    live auto-save writes a wall clock ('Saved 14:53:05 PDT') because
    the save it reports happened seconds ago. The startup seed reads the
    externalizations table, whose stamps are full UTC dates and are
    routinely WEEKS old -- and folding one of those into a 24-hour clock
    is how a seven-week-old project came to report '1h ago'. So a dated
    stamp is recognised as a date, not truncated to its time.
    """
    import datetime
    import re
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
                      _text(text))
    if not match:
        return None
    try:
        return datetime.datetime(*(int(g) for g in match.groups()))
    except ValueError:
        return None                 # 2026-13-40: a stamp, but not a date


def age_seconds(text, now_seconds=None, now_dt=None):
    """How long ago the timestamp in `text` was, or None if it has none.

    `now_dt` times a DATED stamp; `now_seconds` (seconds since midnight)
    times a bare clock. Both are injectable so the suite never races the
    machine's own clock. The timezone marker in the text picks the
    reference when neither is supplied -- Embody writes local time when
    Localtimestamps is on (the default) and UTC when it is off, so which
    one this is has to be read, not assumed.
    """
    import datetime
    utc = "utc" in _text(text).lower()
    stamp_dt = dated_stamp(text)
    if stamp_dt is not None:
        if now_dt is None:
            now_dt = (datetime.datetime.utcnow() if utc
                      else datetime.datetime.now())
        return int((now_dt - stamp_dt).total_seconds())
    stamp = clock_seconds(text)
    if stamp is None:
        return None
    if now_seconds is None:
        now = (datetime.datetime.utcnow() if utc else datetime.datetime.now())
        now_seconds = now.hour * 3600 + now.minute * 60 + now.second
    delta = int(now_seconds) - stamp
    if delta < 0:
        delta += 86400              # the save was before midnight
    return delta


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


def autosave_step(status, now_seconds=None, now_dt=None):
    """TDN auto-save. 'Saved <time>' is the healthy resting state.

    The saved TIME is converted to an AGE, because the age is the whole
    question -- how long ago did the work reach disk. A bare clock wraps
    at midnight and is wrapped rather than reported negative; a DATED
    stamp (what SeedAutosaveStatus reads out of the externalizations
    table) is measured against the calendar, so a project last written
    seven weeks ago says '51d ago' and not '1h ago'.
    """
    text = _text(status)
    low = text.lower()
    if not text or low in ("idle", "none", "never"):
        # "Idle" is the value the release scrub leaves in the shipped .tox
        # so a session's timestamp cannot leak into git (see EmbodyExt's
        # _TRANSIENT_STATUS_PARS). It is NOT a running state -- and with
        # no branch for it this fell through to the RUNNING default, so a
        # project that had simply not checkpointed yet rendered a busy
        # mark on the auto-save row forever, with no age beside it. The
        # detail says "never" rather than nothing: this row's one job is
        # how long ago the work was written, and a bare label is the
        # ageless "Autosaved" the field verdict called out. (On any
        # project with externalized files SeedAutosaveStatus replaces
        # this within seconds of startup; "never" shows only on a truly
        # virgin project before its first checkpoint.)
        return _entry(IDLE, detail="never")
    if low.startswith(("off", "disabled", "bypassed")):
        # 'Bypassed (Perform Mode)' is a stated no-op, not activity --
        # unhandled it fell to the RUNNING default and rendered a busy
        # mark with a climbing clock on an auto-save that is switched
        # off for the show. Same defect class as 'Idle' above.
        return _entry(SKIPPED, detail=text)
    if "failed" in low or low.startswith("error"):
        return _entry(FAILED, detail=text)
    if low.startswith(("saving", "exporting", "writing")):
        return _entry(RUNNING, detail=text)
    if low.startswith("saved"):
        delta = age_seconds(text, now_seconds=now_seconds, now_dt=now_dt)
        if delta is None:
            return _entry(DONE, detail=text)
        # A negative age means the stamp is in the FUTURE (a clock skew,
        # or a UTC stamp read against a local clock). Showing the words
        # is honest; inventing '0s ago' is not -- ago_text returns ''
        # for a negative, and the fallback is the text itself.
        return _entry(DONE, detail=ago_text(delta) or text)
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
    if mode == "off":
        return _labelled(_entry(SKIPPED, detail="updates off"), label)
    if low == "disabled":
        # The bare word is the release scrub's RESTING value -- stamped
        # on every project open (execute.py), exactly like the auto-save
        # row's 'Idle'. The updater has not said anything yet this
        # session, so the row claims nothing: rendering it as "updates
        # off"/"disabled" told a notify-mode user their setting was off.
        return _labelled(_entry(IDLE), label)
    if low.startswith("disabled"):
        # A stated refusal WITH its reason ('Disabled -- dev checkout
        # (update via git)'): show the reason, not a fabricated user
        # choice. The clause after ' -- ' is the whole message here.
        reason = text.split(" -- ", 1)
        return _labelled(_entry(SKIPPED,
                                detail=reason[1] if len(reason) > 1
                                else text), label)
    if low.startswith("updated to"):
        # The one state where the updater ACTED. A catch-all no-op mark
        # here graded the successful update as a refusal.
        return _labelled(_entry(DONE, detail=text), label)
    if "available" in low or "newer" in low:
        return _labelled(_entry(STALLED, detail=text), label)
    if "failed" in low or low.startswith("error"):
        return _labelled(_entry(FAILED, detail=text), label)
    if low.startswith(("checking", "download", "installing")):
        return _labelled(_entry(RUNNING, detail=text), label)
    if "up to date" in low:
        return _labelled(_entry(DONE, detail="up to date"), label)
    if not text:
        # A blank Updatestatus means no check has reported ANYTHING.
        # Claiming "up to date" over it is exactly the kind of false
        # reassurance this readout exists to prevent (a blank field is
        # the v6.0.145 regression the smoke gates on).
        return _labelled(_entry(IDLE), label)
    # A refusal with a reason (a git checkout, an unwritable install) is
    # not a failure of Embody -- it is a stated no-op, said plainly.
    return _labelled(_entry(SKIPPED, detail=text), label)


def reset_session():
    """Forget EVERYTHING this session has observed.

    TEST SUPPORT, and the reason it is public: the per-row state clocks
    in _SINCE are observations that outlive any one call, so without a
    reset the suites depend on execution order -- the fake COMP the
    suites use has a FIXED path, so every suite shares one key, and a
    clock started in an earlier test then renders as an elapsed time in
    a later one that never started it.

    (The problem-dwell records that used to live here went with the
    dual-layout view they gated: a fixed layout has nothing to reflow,
    so nothing needs to debounce it.)
    """
    _SINCE.clear()


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
# Every replacement is sized into its row's budget (TARGET_ROW_CHARS
# minus mark, label and two spaces -- 14 for Convoy, 15 for Envoy and
# Saved, ~12 for the version row). One character past the budget
# shrinks the font on EVERY row, so terseness here is glyph size there.
_COMPACT_PREFIX = (
    ("running on ", ""),
    ("installing deps", "installing deps"),
    ("preparing python environment", "preparing env"),
    ("restarting after ", "restarting: "),
    ("reviving (watchdog)", "reviving"),
    ("needs repair", "needs repair"),
    ("not installed", "not installed"),
    # The 'Installed -- X' family keeps X: the clause after ' -- ' IS
    # the status there, and the generic clause-cut below was throwing it
    # away -- 'Installed -- no supervisor (use Repair Convoy App)'
    # rendered as a bare 'Installed' under a failure mark, words
    # contradicting the mark with the remedy deleted.
    ("installed -- starting", "starting"),
    ("installed -- not running", "restarts soon"),
    ("installed -- stopped", "stopped"),
    ("installed -- no supervisor", "repair needed"),
    ("install failed", "install failed"),
    ("consent required", "consent needed"),
    ("waiting for", "waiting:"),
    ("managed by another supervisor", "other manager"),
    ("this is the embody dev checkout", "dev checkout"),
    ("saved ", ""),
    ("error: ", ""),
    ("refused: ", "refused: "),
)

# Full replacements: the tail carries nothing the row needs, so the
# whole phrase maps to a fixed short form (a prefix rule would append
# the tail back).
_COMPACT_WHOLE = (
    ("restarting after save", "restarting"),
    ("updated to ", "updated"),
    ("no convoy host app", "no host app"),
    # The remedy said as the imperative it is -- 'waiting: project save'
    # buries the verb and busts the row budget.
    ("waiting for project save", "save project"),
)


def compact_status(text, max_width=None):
    """The shortest phrasing that still answers the row's question."""
    raw = _text(text)
    if not raw:
        return ""
    low = raw.lower()
    out = raw
    # 'Running X -- installed by a newer Embody' has its status in the
    # CLAUSE, with a version in front of it -- no fixed prefix can catch
    # it, and the clause-cut below would leave 'Running X' with a
    # waiting mark beside it.
    if " -- installed by a newer embody" in low:
        return "newer Embody"
    # '6.0.230 available' has its status at the END, behind a version
    # that varies -- same shape, same treatment.
    if low.rstrip(".").endswith(" available"):
        return "update ready"
    for prefix, replacement in _COMPACT_WHOLE:
        if low.startswith(prefix):
            out = replacement
            break
    else:
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


# == the marks: a glyph per subsystem, two columns ========================
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


def cell_text(key, step, now=None, animate=False):
    """One cell: mark, name, and the reason. ALWAYS all three.

    The reason is never suppressed. There used to be a compact mode that
    rendered only mark + label while healthy, which meant "Autosaved"
    with no age and a second layout for the panel to snap between; the
    row now always answers its own question ("22h ago", "port 9870",
    "Connected"). What keeps the panel from resizing on transient text
    is the FONT rule, not hiding words: the font is pinned to
    TARGET_ROW_CHARS (see _font_of), so a row growing or shrinking
    inside the budget moves nothing.

    An elapsed clock on a step RUNNING past STUCK_DWELL_S outranks the
    detail: a busy mark alone reads the same at four seconds and at
    forty minutes, which is how a wedged dependency install looked
    exactly like a slow one.
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
    # The detail is CLAMPED to what fits this row inside the design
    # budget -- mark, space, label, space, then whatever remains of
    # TARGET_ROW_CHARS. This is the other half of the font pin: with no
    # bound, a long producer string ('Error: host app did not answer
    # /register within 5.0s') widened the column past the budget and
    # resized the whole panel on every retry cycle -- the 2026-08-10
    # flap re-entering through the font instead of the shape.
    budget = TARGET_ROW_CHARS - len(label) - 3
    detail = compact_status(step.get("detail"), max_width=budget)
    return ("%s %s %s" % (glyph, label, detail)).rstrip() if detail \
        else "%s %s" % (glyph, label)


GRID_GAP = 2


def live_grid(embody, now=None):
    """Rows of CELLS, each carrying its OWN colour.

    ONE SHAPE, ALWAYS: five rows, one subsystem each, mark + label +
    reason, in STATUS_ORDER. The healthy/expanded split this replaces
    rendered the same feature two different ways and hid the autosave
    age on the happy path -- and with a fixed shape there is nothing to
    reflow, so the dwell/grace machinery that debounced the switching
    went with it.
    """
    steps = _live_steps(embody, now=now)
    # Animate the busy mark ONLY while an install is genuinely in
    # flight, read off the DETAIL rather than off a startup flag: there
    # is no startup flag any more, and an install can begin long after
    # the open sequence (a Convoy host app coming up on demand). A
    # settled project gets the static ellipsis -- an unchanging cell,
    # which publishes nothing and cooks nothing.
    animate = any((step or {}).get("state") == RUNNING
                  and (step or {}).get("detail", "").lower().startswith(
                      ("install", "building", "preparing", "starting"))
                  for _k, step in steps)
    by_key = dict(steps)
    cells = [(key, cell_text(key, by_key[key], now=now, animate=animate),
              by_key[key])
             for key in STATUS_ORDER if key in by_key]
    width = max([len(t) for _k, t, _s in cells] or [0])
    return [[(key, text.ljust(width),
              STATE_RGB.get(step.get("state"), STATE_RGB[IDLE]))]
            for key, text, step in cells]


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


def font_for_rows(panel_width, texts, minimum=7.0, maximum=40.0):
    """Font sized to the CONTENT, not to a fixed character budget.

    TARGET_ROW_CHARS is what the rows are DESIGNED to fit; this is what
    they actually came out as. Sizing to the longest row really being
    rendered is what makes the readout as large as it can be on any
    given panel, rather than as large as the worst case allows.
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

    THE BUG THIS REPLACES, measured on the live panel every ~30s: the
    font used to be chosen by guessing which view was up, while the
    panel rendered live_grid() regardless. A Convoy heartbeat then left
    the grid on screen but sized the font for the other view -- 39.1 to
    24.9 and back, with the column widths never moving, which is why it
    read as the layout freaking out rather than as a font change.

    The cure is not a better guess: it is deriving the size from
    live_col_chars(), the same numbers that set the cell widths.
    Content and font cannot disagree if they come from one source.

    KEPT DESPITE HAVING NO PRODUCTION CALLER. table_rows() builds one
    grid and derives everything from it (_font_of/_col_chars_of); this
    is the reference implementation those two are pinned against by
    test_layout_matches_the_helpers_it_replaced, which is the only
    thing standing between that optimisation and a silent drift.
    """
    cols = live_col_chars(embody, now=now)
    total = sum(cols) + GRID_GAP * max(0, len(cols) - 1)
    # Pinned to the design budget: content NARROWER than
    # TARGET_ROW_CHARS must not inflate the font, or every transient
    # detail change ("Registering..." -> "Connected") resizes the whole
    # panel. Content wider than the budget still shrinks to fit -- the
    # rows are never clipped, the font is simply never bigger than the
    # budget size.
    size = font_for_rows(panel_width,
                         ["x" * max(TARGET_ROW_CHARS, total)])
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

    Reached only through live_font now; both survive as the reference
    the table_rows fast path is tested against. See live_font.
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

# The published table's columns. PUBLIC: the panel's publisher writes by
# index against this, so the order here IS the table's contract.
TABLE_HEADER = ("name", "value", "r", "g", "b", "show")

PANEL_ROWS = 5          # rowboxes the panel ships
PANEL_COLS = 2          # cells per rowbox


def rows_show_a_live_age(rows):
    """True when some rendered cell is a clock that moves on its own.

    An "N ago" age or an elapsed M:SS advances with wall time and no
    event behind it, so an event-driven panel that renders one owes
    itself a slow re-check -- will_change's short horizons cannot see a
    flip that happens minutes out, and after the last event nothing else
    would ever look again. The seeded auto-save age froze at its first
    value for exactly this reason.
    """
    try:
        for row in rows or ():
            value = str(row[1] if len(row) > 1 else "")
            if " ago" in value or _CLOCK_RE.search(value):
                return True
    except Exception:
        pass
    return False


# Word boundaries spelt as explicit character classes: a \b escape
# travelled through one quoting layer too many on the way here and
# arrived as a literal backspace byte, which matches nothing.
_CLOCK_RE = __import__("re").compile(r"(?:^|[^0-9])[0-9]+:[0-9]{2}(?![0-9])")


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
    # Same TARGET_ROW_CHARS pin as live_font -- the two are tested to
    # agree, and the pin is what keeps transient detail-length changes
    # from resizing the panel.
    size = font_for_rows(panel_width,
                         ["x" * max(TARGET_ROW_CHARS, total)])
    row_height = _panel_row_budget(embody, now=now)
    if row_height:
        try:
            size = min(size, float(row_height) * ROW_FONT_RATIO)
        except (TypeError, ValueError):
            pass
    return max(7.0, size)


def table_rows(embody, panel_width, now=None):
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
    grid = live_grid(embody, now=now)
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
