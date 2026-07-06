"""Layout geometry for Envoy: network lint, dock hug, auto-position.

Module DAT (mod.envoy_layout) called by EnvoyExt on the MAIN THREAD only.
No module-level TD access; every function takes ops explicitly. Behavior
contract is network-layout.md -- the tool layer enforces it here.
"""


def lint_layout(comp):
    """Return layout-violation strings for a COMP's direct children: ops
    stacked at (0,0), overlapping ops, and docked DATs scattered far from
    their host. Enforces network-layout.md after execute_python, which --
    unlike create_op -- uses raw comp.create()/copy() and never positions."""
    try:
        kids = [c for c in comp.children if c.type != 'annotate']
    except Exception:
        return []
    if len(kids) < 2 or len(kids) > 250:
        return []
    docked = set()
    for c in kids:
        for d in getattr(c, 'docked', ()):
            docked.add(d.path)
    main = [c for c in kids if c.path not in docked]
    issues = []
    zeros = [c for c in main if c.nodeX == 0 and c.nodeY == 0]
    if len(zeros) >= 2:
        issues.append('%d ops stacked at (0,0): %s'
                      % (len(zeros), ', '.join(z.name for z in zeros[:6])))
    n = len(main)
    if n <= 80:
        ov = 0
        for i in range(n):
            a = main[i]
            for j in range(i + 1, n):
                b = main[j]
                if (a.nodeX < b.nodeX + b.nodeWidth and a.nodeX + a.nodeWidth > b.nodeX and
                        a.nodeY < b.nodeY + b.nodeHeight and a.nodeY + a.nodeHeight > b.nodeY):
                    ov += 1
        if ov:
            issues.append('%d overlapping op pair(s)' % ov)
    # Hug-scale threshold: a conforming dock row (network-layout.md) stays
    # within ~350u of its host on both axes even for a 4-dock glslmultiTOP;
    # anything past that is stranded, not docked.
    scattered = sum(1 for c in main for d in getattr(c, 'docked', ())
                    if abs(d.nodeX - c.nodeX) > 350 or abs(d.nodeY - c.nodeY) > 350)
    if scattered:
        issues.append('%d docked DAT(s) scattered far from host' % scattered)
    return issues


def same_network_docks(host):
    """Docked companions of `host` that live in host's OWN network (an
    extension code DAT docked from INSIDE a COMP renders elsewhere and
    must never be repositioned by parent-network coordinates)."""
    try:
        host_parent = host.parent()
        if host_parent is None:
            return []
        parent_path = host_parent.path
        return [d for d in getattr(host, 'docked', ())
                if d.valid and d.parent() is not None
                and d.parent().path == parent_path]
    except Exception:
        return []


def place_docked_ops(host):
    """Snap every docked companion (callback/shader/info DATs) into a
    tight row hugging the host's bottom edge, per network-layout.md:
    row 30 below the host, slots dock-width+20 apart, centered under
    the host. TD spawns docked ops at arbitrary coordinates and leaves
    them behind when their host moves, so the tool layer re-hugs them
    instead of relying on the caller. Returns the number placed."""
    docks = same_network_docks(host)
    if not docks:
        return 0
    try:
        dw = max(d.nodeWidth for d in docks)
        dh = max(d.nodeHeight for d in docks)
        step = dw + 20
        row_y = host.nodeY - dh - 30
        cx = host.nodeX + host.nodeWidth / 2.0
        n = len(docks)
        for i, d in enumerate(docks):
            d.nodeX = int(cx + (i - (n - 1) / 2.0) * step - dw / 2.0)
            d.nodeY = int(row_y)
        return n
    except Exception:
        return 0


def find_non_overlapping_position(parent, new_op):
    """Reposition new_op so it doesn't overlap any sibling in the parent
    COMP. The candidate footprint is widened to include the hug row that
    place_docked_ops hangs below the op, so a host with docked companions
    lands where the docks also fit."""
    MARGIN = 20

    own_docks = same_network_docks(new_op)
    own_dock_paths = {d.path for d in own_docks}
    # Annotations are containers, not obstacles -- ops belong INSIDE them
    # (network-layout.md), so they must not repel the position scan.
    siblings = [child for child in parent.children
                if child.path != new_op.path
                and child.path not in own_dock_paths
                and child.type != 'annotate']
    if not siblings:
        return  # No siblings -- default position is fine

    w = new_op.nodeWidth
    h = new_op.nodeHeight

    # Reserve the dock hug row below (and centered under) the host.
    extra_below = 0
    extra_side = 0
    if own_docks:
        dw = max(d.nodeWidth for d in own_docks)
        dh = max(d.nodeHeight for d in own_docks)
        extra_below = dh + 30
        row_w = len(own_docks) * (dw + 20)
        if row_w > w:
            extra_side = (row_w - w) / 2.0

    # Collect sibling bounding rectangles
    rects = [(s.nodeX, s.nodeY, s.nodeWidth, s.nodeHeight) for s in siblings]

    def has_overlap(x, y):
        x0 = x - extra_side
        y0 = y - extra_below
        fw = w + 2 * extra_side
        fh = h + extra_below
        for (sx, sy, sw, sh) in rects:
            if (x0 < sx + sw + MARGIN and x0 + fw + MARGIN > sx and
                    y0 < sy + sh + MARGIN and y0 + fh + MARGIN > sy):
                return True
        return False

    # If current position is already clear, nothing to do
    if not has_overlap(new_op.nodeX, new_op.nodeY):
        return

    # Grid scan: cell size = op footprint + margin
    step_x = w + 2 * extra_side + MARGIN
    step_y = h + extra_below + MARGIN

    # Start from top-left corner of existing layout
    origin_x = min(r[0] for r in rects)
    origin_y = max(r[1] for r in rects)  # highest Y = top

    for row in range(20):
        for col in range(20):
            test_x = origin_x + col * step_x
            test_y = origin_y - row * step_y  # scan downward
            if not has_overlap(test_x, test_y):
                new_op.nodeX = int(test_x)
                new_op.nodeY = int(test_y)
                return
