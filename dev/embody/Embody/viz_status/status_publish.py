"""Publish the status readout into status_table, once per change.

WHY A TABLE AT ALL. Every cell parameter used to evaluate the readout
itself -- 90 module calls per cook, each rebuilding the entire grid from
the live parameters, and every one of them time-dependent through
absTime.seconds. A visible panel therefore recomputed the whole readout
60 times a second (measured 6.7 ms per cook, 40% of a 60 fps frame) to
display values that change a handful of times per SESSION. Now the
values are computed here, once, and the cells read finished strings.

WHY IT IS EVENT-DRIVEN, NOT POLLED. Refresh() runs when something
actually changed: a parameter the readout shows (parexec_status), or a
step published by EmbodyExt / CatalogManagerExt. In the steady state
there is no tick at all -- an idle panel cooks ZERO times. The single
exception is a step still RUNNING, which owns an elapsed clock that
advances with no event behind it; that re-arms itself and stops the
moment nothing is running.

WHY ONLY CHANGED CELLS ARE WRITTEN. Writing a DAT cooks it and every
expression reading it, so writing an unchanged value would buy a full
panel redraw for nothing. An unchanged readout writes nothing and
therefore cooks nothing.
"""

# Two cadences, because the panel has two reasons to re-tick.
# SLOW is the elapsed clock ("2m ago" ticking over) -- half a second is
# plenty and costs nothing to keep armed.
# FAST is the busy-mark ANIMATION during an install: 8 frames is ~7.5 Hz
# at 60 fps, which reads as motion. One publish is ~0.65 ms, so the
# animation costs about 5 ms per second WHILE INSTALLING and exactly
# nothing afterwards -- the tick only re-arms while the rows would
# actually differ a tick from now.
TICK_FRAMES = 30
TICK_FRAMES_ANIMATING = 8


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
                return
    except Exception:
        return
    run("me.module.Refresh()", fromOP=me, delayFrames=frames,
        group='embody_status_tick')


def onStart():
    # The .toe may open with the panel already visible, and nothing has
    # published yet -- one publish gives the cells something to read.
    run("me.module.Refresh()", fromOP=me, delayFrames=60,
        group='embody_status_tick')
    return
