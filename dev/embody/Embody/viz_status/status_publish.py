"""Publish the status readout into status_table, once per change.

WHY A TABLE AT ALL. Every cell parameter used to evaluate the readout
itself -- 90 module calls per cook, each rebuilding the entire grid from
the live parameters, and every one of them time-dependent through
absTime.seconds. A visible panel therefore recomputed the whole readout
60 times a second (measured 6.7 ms per cook, 40% of a 60 fps frame) to
display values that change a handful of times per SESSION. Now the
values are computed here, once, and the cells read finished strings.

WHY IT IS EVENT-DRIVEN, NOT POLLED. Refresh() runs when something
actually changed: a parameter the readout shows fires parexec_status,
and Embody's own code calls _republishStatusPanel after events that
have no parameter behind them (the auto-save seed at startup, the
catalog scan's choke point). In the steady state there is no fast tick
at all. The exceptions re-arm themselves and stop when their reason
does: a spinner while something installs, and a slow half-minute check
while any rendered cell is a wall-clock age -- an "N ago" advances with
no event behind it, so a purely event-driven panel would freeze it at
its first value forever (the seeded auto-save age did exactly that).

WHY ONLY CHANGED CELLS ARE WRITTEN. Writing a DAT cooks it and every
expression reading it, so writing an unchanged value would buy a full
panel redraw for nothing. An unchanged readout writes nothing and
therefore cooks nothing -- which is what makes the slow age check
nearly free: one ~0.6 ms publish per half minute, zero panel cooks
unless the age text actually flipped.
"""

# Three cadences, because the panel has three reasons to re-tick.
# FAST is the busy-mark ANIMATION during an install: 8 frames is ~7.5 Hz
# at 60 fps, which reads as motion; ~5 ms per second WHILE INSTALLING
# and nothing afterwards.
# SLOW is an elapsed clock mid-flight ("0:18" on a running step).
# AGE is the half-minute re-check while any cell shows an "N ago" --
# will_change's short horizons cannot see a flip that is minutes out,
# and after the last event nothing else would ever look again.
TICK_FRAMES = 30
TICK_FRAMES_ANIMATING = 8
TICK_FRAMES_AGE = 1800


def _table():
    return me.parent().op('status_table')


def _module():
    dat = op.Embody.op('startup_progress')
    return dat.module if dat is not None else None


def Refresh():
    """Recompute the readout and publish what changed. Returns cells written."""
    mod = _module()
    table = _table()
    if mod is None or table is None:
        return 0
    viz = me.parent()
    try:
        rows = mod.table_rows(op.Embody, viz.par.w.eval(),
                              now=absTime.seconds)
    except Exception:
        return 0
    written = _write(table, rows, mod)
    _arm(mod, rows)
    return written


def _write(table, rows, mod):
    """Write only the cells whose value actually moved."""
    header = list(mod.TABLE_HEADER)
    if table.numRows != len(rows) + 1 or table.numCols != len(header):
        # Shape changed (or first run): rebuild once, then diff forever.
        table.clear()
        table.appendRow(header)
        for row in rows:
            table.appendRow(list(row))
        return len(rows)
    written = 0
    for row in rows:
        name = row[0]
        for index in range(1, len(header)):
            if table[name, index].val != row[index]:
                table[name, index] = row[index]
                written += 1
    return written


def _arm(mod, rows=None):
    """Re-arm ONLY while something is genuinely about to move.

    Not "is a step running": a healthy session reports Envoy Connected and
    Convoy Off as RUNNING, so that question is true for the whole session
    and would keep a timer alive forever to redraw nothing. The panel asks
    whether its own rendered rows differ a tick from now -- true only while
    an elapsed clock or an "N ago" is really about to tick over, and false
    the moment the readout settles.
    """
    try:
        viz = me.parent()
        rate = max(1.0, project.cookRate)
        # Ask at the FAST horizon: an 8-frame-ahead difference catches the
        # spinner, and anything slower (the elapsed clock) still differs
        # over the slow horizon below.
        frames = TICK_FRAMES_ANIMATING
        width = viz.par.w.eval()
        if not mod.will_change(op.Embody, now=absTime.seconds,
                               ahead=frames / rate, panel_width=width,
                               rows=rows):
            frames = TICK_FRAMES
            if not mod.will_change(op.Embody, now=absTime.seconds,
                                   ahead=frames / rate, panel_width=width,
                                   rows=rows):
                # Nothing moves within seconds -- but a rendered AGE still
                # moves within minutes, and with no tick armed nothing
                # would ever look again. See TICK_FRAMES_AGE.
                if not mod.rows_show_a_live_age(rows):
                    return
                frames = TICK_FRAMES_AGE
    except Exception:
        return
    # ONE tick, ever. run(group=...) does not dedupe: every event's
    # Refresh armed another age tick and they accumulated (observed: five
    # concurrent), each firing its own publish per half minute. Kill the
    # group before arming so events RESCHEDULE the tick rather than
    # multiply it.
    try:
        for pending in runs:
            if getattr(pending, 'group', None) == 'embody_status_tick':
                pending.kill()
    except Exception:
        pass
    run("me.module.Refresh()", fromOP=me, delayFrames=frames,
        group='embody_status_tick')


def onStart():
    # The .toe may open with the panel already visible, and nothing has
    # published yet -- one publish gives the cells something to read.
    run("me.module.Refresh()", fromOP=me, delayFrames=60,
        group='embody_status_tick')
    return
