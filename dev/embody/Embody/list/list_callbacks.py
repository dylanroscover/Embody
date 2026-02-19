"""
List COMP callbacks for Embody Manager UI.

Renders the externalization tree with expand/collapse, rollover
highlights, and clickable Tox/TDN/Delete buttons. All styling
is pure Python — no TOP textures needed.

me   - this callbacks DAT
comp - the List COMP (available in all callbacks)
"""

# --- Column indices ---
COL_EXPANDO = 0
COL_PATH = 1
COL_TYPE = 2
COL_FILE = 3
COL_TIMESTAMP = 4
COL_TOX = 5
COL_TDN = 6
COL_BUILD = 7
COL_TOUCH_BUILD = 8
COL_DELETE = 9

NUM_COLS = 10

HEADER_LABELS = [
	'', 'Network Path', '', 'External File Path',
	'Timestamp', 'Tox', 'TDN', 'Build', 'Touch Build', 'Del',
]
COL_WIDTHS    = [0, 0, 80, 200, 190, 42, 42, 50, 82, 36]
COL_STRETCHES = [False, True, False, True, False, False, False, False, False, False]
TEXT_PAD_X = 6  # horizontal padding for left-justified cells


# --- Theme colors (loaded from Embody UI pars in onInitTable) ---
_t = {}


def _par4(name):
	"""Read an RGBA color tuple from Embody's UI parameters."""
	p = parent.Embody.par
	return (
		getattr(p, name + 'r').eval(),
		getattr(p, name + 'g').eval(),
		getattr(p, name + 'b').eval(),
		getattr(p, name + 'a').eval(),
	)


def _composite(fg, bg):
	"""Alpha-composite fg over bg, return opaque RGBA."""
	a = fg[3]
	return (
		a * fg[0] + (1 - a) * bg[0],
		a * fg[1] + (1 - a) * bg[1],
		a * fg[2] + (1 - a) * bg[2],
		1.0,
	)


def _brighten(color, amount=0.06):
	return (
		min(1.0, color[0] + amount),
		min(1.0, color[1] + amount),
		min(1.0, color[2] + amount),
		color[3],
	)


def _load_theme():
	"""Read colors from Embody UI parameters into _t cache."""
	global _t
	text = _par4('Textcolor')
	row = _par4('Listrowcolor')
	header = _par4('Listheadercolor')
	select = _par4('Listrowselectcolor')
	saved_raw = _par4('Savedcolor')
	btn = _par4('Buttonbackgroundcolor')

	# Pre-composite saved color (may have alpha < 1) over row bg
	saved = _composite(saved_raw, row) if saved_raw[3] < 1.0 else saved_raw

	# Amber for exporting — warm shift from saved
	amber = (saved[0] + 0.12, max(0, saved[1] - 0.02),
	         max(0, saved[2] - 0.04), 1.0)

	# Comp state button — slightly brighter than row bg for subtle visibility
	comp_bg = _brighten(row, 0.08)

	# Dirty state — desaturated red, stands out without being garish
	dirty = (0.55, 0.18, 0.18, 1.0)
	# Parameter change — warm orange, distinct from both saved green and dirty red
	par_change = (0.60, 0.38, 0.12, 1.0)

	# Subtle column separator — just visible enough to delineate columns
	border = _brighten(row, 0.04)

	_t.update({
		'text': text,
		'text_dim': (text[0] * 0.55, text[1] * 0.55, text[2] * 0.55, 1.0),
		'row': row,
		'row_alt': _brighten(row, 0.015),
		'header': header,
		'select': select,
		'saved': saved,
		'saved_roll': _brighten(saved, 0.08),
		'comp': comp_bg,
		'comp_roll': _brighten(comp_bg, 0.06),
		'amber': amber,
		'amber_roll': _brighten(amber, 0.08),
		'dirty': dirty,
		'dirty_roll': _brighten(dirty, 0.08),
		'par_change': par_change,
		'par_change_roll': _brighten(par_change, 0.08),
		'border': border,
	})


def _ensure_theme():
	"""Lazy-load theme if _t was reset by module recompile."""
	if not _t:
		_load_theme()


def _source():
	"""Return the inject_parents DAT (data source)."""
	return op('inject_parents')


def _row_bg(row):
	return _t['row'] if row % 2 == 0 else _t['row_alt']


def _apply_cell(attribs, row, col, data, highlight=False):
	"""Style a single data cell. Used by onInitCell and rollover restore."""
	_ensure_theme()
	if row >= data.numRows:
		return
	path = data[row, 'path'].val
	bg = _t['select'] if highlight else _row_bg(row)

	if col == COL_EXPANDO:
		attribs.text = ''
		attribs.bgColor = bg

	elif col == COL_PATH:
		name = path.rsplit('/', 1)[-1] if path else ''
		hc = data[row, 'has_children'].val == '1'
		if hc:
			expanded = parent.Embody.fetch('expanded_paths', set())
			arrow = '\u25BC ' if path in expanded else '\u25B6 '
			attribs.text = arrow + name
		else:
			attribs.text = name
		attribs.textJustify = JustifyType.CENTERLEFT
		attribs.textOffsetX = TEXT_PAD_X
		attribs.bgColor = bg

	elif col == COL_TYPE:
		attribs.text = data[row, 'type'].val
		attribs.textJustify = JustifyType.CENTER
		attribs.bgColor = bg

	elif col == COL_FILE:
		attribs.text = data[row, 'rel_file_path'].val
		attribs.textJustify = JustifyType.CENTERLEFT
		attribs.textOffsetX = TEXT_PAD_X
		attribs.bgColor = bg

	elif col == COL_TIMESTAMP:
		attribs.text = data[row, 'timestamp'].val
		attribs.textJustify = JustifyType.CENTERLEFT
		attribs.textOffsetX = TEXT_PAD_X
		attribs.bgColor = bg

	elif col == COL_TOX:
		st = data[row, 'tox_state'].val
		if st == 'Dirty':
			attribs.text = 'TOX'
			attribs.bgColor = _t['dirty']
			attribs.textColor = (1.0, 1.0, 1.0, 1.0)
		elif st == 'ParChange':
			attribs.text = 'Par'
			attribs.bgColor = _t['par_change']
			attribs.textColor = (1.0, 1.0, 1.0, 1.0)
		elif st == 'Saved':
			attribs.text = 'TOX'
			attribs.bgColor = _t['saved']
		elif st == 'Comp':
			attribs.text = 'TOX'
			attribs.bgColor = _t['comp']
		else:
			attribs.text = ''
			attribs.bgColor = bg
		attribs.textJustify = JustifyType.CENTER

	elif col == COL_TDN:
		st = data[row, 'tdn_state'].val
		if st == 'Saved':
			attribs.text = 'TDN'
			attribs.bgColor = _t['saved']
		elif st == 'Comp':
			attribs.text = 'TDN'
			attribs.bgColor = _t['comp']
		elif st == 'Exporting':
			attribs.text = '...'
			attribs.bgColor = _t['amber']
		else:
			attribs.text = ''
			attribs.bgColor = bg
		attribs.textJustify = JustifyType.CENTER

	elif col == COL_BUILD:
		attribs.text = data[row, 'build'].val
		attribs.textJustify = JustifyType.CENTER
		attribs.bgColor = bg

	elif col == COL_TOUCH_BUILD:
		attribs.text = data[row, 'touch_build'].val
		attribs.textJustify = JustifyType.CENTERLEFT
		attribs.textOffsetX = TEXT_PAD_X
		attribs.bgColor = bg

	elif col == COL_DELETE:
		has_ext = bool(data[row, 'rel_file_path'].val
					   or data[row, 'tox_state'].val == 'Saved'
					   or data[row, 'tdn_state'].val == 'Saved')
		if has_ext:
			attribs.text = 'X'
			attribs.textColor = _t['text_dim']
		else:
			attribs.text = ''
		attribs.textJustify = JustifyType.CENTER
		attribs.bgColor = bg


# -- Init callbacks -----------------------------------------------------------

def onInitTable(comp, attribs):
	_load_theme()
	attribs.bgColor = _t['row']
	attribs.textColor = _t['text']
	attribs.fontSizeX = 9
	attribs.sizeInPoints = True
	attribs.rowHeight = 20
	attribs.textJustify = JustifyType.CENTERLEFT


def onInitCol(comp, col, attribs):
	_ensure_theme()
	if col < len(COL_WIDTHS):
		attribs.colWidth = COL_WIDTHS[col]
		attribs.colStretch = COL_STRETCHES[col]
	# Subtle 1px right border on every column except the last
	if col < NUM_COLS - 1:
		attribs.rightBorderInColor = _t['border']


def onInitRow(comp, row, attribs):
	_ensure_theme()
	if row == 0:
		attribs.bgColor = _t['header']
		return
	data = _source()
	if data and row < data.numRows:
		depth = int(data[row, 'depth'].val or '0')
		attribs.rowIndent = depth * 18
	attribs.bgColor = _row_bg(row)


def onInitCell(comp, row, col, attribs):
	if row == 0:
		attribs.text = HEADER_LABELS[col] if col < len(HEADER_LABELS) else ''
		attribs.textJustify = JustifyType.CENTER
		return
	data = _source()
	if data and row < data.numRows:
		_apply_cell(attribs, row, col, data)


# -- Interaction callbacks ----------------------------------------------------

def onRollover(comp, row, col, coords, prevRow, prevCol, prevCoords):
	_ensure_theme()
	data = _source()
	if not data:
		return
	ncols = min(NUM_COLS, comp.par.cols.eval())

	# Row changed -> restore old row, highlight new row
	if prevRow != row:
		if prevRow > 0 and prevRow < data.numRows:
			for c in range(ncols):
				_apply_cell(comp.cellAttribs[prevRow, c],
				            prevRow, c, data, highlight=False)
		if row > 0 and row < data.numRows:
			for c in range(ncols):
				_apply_cell(comp.cellAttribs[row, c],
				            row, c, data, highlight=True)

	# Column changed within same row -> restore old cell to highlight state
	if prevRow == row and prevCol != col and row > 0:
		if prevCol >= 0 and prevCol < ncols and row < data.numRows:
			_apply_cell(comp.cellAttribs[row, prevCol],
			            row, prevCol, data, highlight=True)

	if row <= 0 or row >= data.numRows or col < 0:
		return

	# Cell-specific rollover effects
	if col == COL_TOX:
		st = data[row, 'tox_state'].val
		if st == 'Dirty':
			comp.cellAttribs[row, col].text = 'Save'
			comp.cellAttribs[row, col].bgColor = _t['dirty_roll']
			comp.cellAttribs[row, col].textColor = (1.0, 1.0, 1.0, 1.0)
		elif st == 'ParChange':
			comp.cellAttribs[row, col].text = 'Save'
			comp.cellAttribs[row, col].bgColor = _t['par_change_roll']
			comp.cellAttribs[row, col].textColor = (1.0, 1.0, 1.0, 1.0)
		elif st == 'Saved':
			comp.cellAttribs[row, col].text = 'Upd'
			comp.cellAttribs[row, col].bgColor = _t['saved_roll']
		elif st == 'Comp':
			comp.cellAttribs[row, col].text = 'New'
			comp.cellAttribs[row, col].bgColor = _t['comp_roll']
	elif col == COL_TDN:
		st = data[row, 'tdn_state'].val
		if st == 'Saved':
			comp.cellAttribs[row, col].text = 'Upd'
			comp.cellAttribs[row, col].bgColor = _t['saved_roll']
		elif st == 'Comp':
			comp.cellAttribs[row, col].text = 'New'
			comp.cellAttribs[row, col].bgColor = _t['comp_roll']
		elif st == 'Exporting':
			comp.cellAttribs[row, col].bgColor = _t['amber_roll']
	elif col == COL_DELETE:
		has_ext = bool(data[row, 'rel_file_path'].val
					   or data[row, 'tox_state'].val == 'Saved'
					   or data[row, 'tdn_state'].val == 'Saved')
		if has_ext:
			comp.cellAttribs[row, col].textColor = _t['text']
			comp.cellAttribs[row, col].bgColor = _t['select']


def _hasTDNExport(op_path):
	"""Check if a TDN export exists in the externalizations table."""
	if not op_path:
		return False
	table = parent.Embody.ext.Embody.Externalizations
	if not table:
		return False
	for i in range(1, table.numRows):
		if table[i, 'path'].val == op_path and table[i, 'type'].val == 'tdn':
			return True
	return False


def onSelect(comp, startRow, startCol, startCoords,
             endRow, endCol, endCoords, start, end):
	if not end:
		return
	if startRow != endRow or startCol != endCol:
		return
	if startRow <= 0:
		return

	row, col = startRow, startCol
	data = _source()
	if not data or row >= data.numRows:
		return

	path = data[row, 'path'].val

	if col in (COL_EXPANDO, COL_PATH):
		hc = data[row, 'has_children'].val == '1'
		if hc:
			expanded = parent.Embody.fetch('expanded_paths', set())
			if path in expanded:
				expanded.discard(path)
			else:
				expanded.add(path)
			parent.Embody.store('expanded_paths', expanded)
			parent.Embody.Refresh()

	elif col == COL_TYPE:
		oper = op(path)
		if oper:
			for sibling in oper.parent().findChildren(depth=1):
				sibling.selected = False
			oper.selected = True
			pane = ui.panes.createFloating(
				type=PaneType.NETWORKEDITOR, name=oper.name,
				maxWidth=1920, maxHeight=1080,
				monitorSpanWidth=0.9, monitorSpanHeight=0.9)
			pane.owner = oper.parent()
			pane.home(zoom=True, op=oper)
			pane.zoom = 2
			pane.x = oper.nodeCenterX
			pane.y = oper.nodeCenterY

	elif col == COL_FILE:
		rel_fp = data[row, 'rel_file_path'].val
		if rel_fp:
			parent.Embody.OpenSaveFile(rel_fp)

	elif col == COL_TOX:
		st = data[row, 'tox_state'].val
		oper = op(path)
		if st == 'Comp' and oper:
			parent.Embody.ext.Embody.applyTagToOperator(oper, 'tox')
			parent.Embody.Update()
		elif st in ('Saved', 'Dirty', 'ParChange'):
			parent.Embody.Save(path)
			parent.Embody.Refresh()

	elif col == COL_TDN:
		st = data[row, 'tdn_state'].val
		if st == 'Exporting':
			return
		oper = op(path)
		if oper and oper.family == 'COMP':
			parent.Embody.ext.TDN.ExportNetworkAsync(
				root_path=path, output_file='auto')
			# Recook data source to pick up Exporting state, then reset list
			run("op('{}').cook(force=True); op('{}').reset()".format(
				op('inject_parents').path, comp.path), delayFrames=1)

	elif col == COL_DELETE:
		rel_fp = data[row, 'rel_file_path'].val
		if not rel_fp and data[row, 'tox_state'].val != 'Saved' \
				and data[row, 'tdn_state'].val != 'Saved':
			return
		oper = op(path)
		is_comp = oper is not None and oper.family == 'COMP'
		has_tdn = _hasTDNExport(path) if is_comp else False
		if has_tdn:
			result = ui.messageBox(
				'Remove',
				'This COMP has both an externalization (.tox) and a TDN '
				'export (.tdn).\nWhat would you like to remove?\n\n'
				'Operator: ' + path,
				buttons=['Cancel', 'Remove Tox', 'Remove TDN', 'Remove Both'])
			if result == 1:
				parent.Embody.RemoveListerRow(path, rel_fp)
			elif result == 2:
				parent.Embody.RemoveTDNEntry(path)
			elif result == 3:
				parent.Embody.RemoveListerRow(path, rel_fp)
				parent.Embody.RemoveTDNEntry(path)
		else:
			result = ui.messageBox(
				'Remove',
				'Remove this externalization?\n\n'
				'This will delete the external file from disk, clear the\n'
				"operator's externalization tags, and remove the tracking\n"
				'entry. This cannot be undone.\n\n'
				'Operator: ' + path,
				buttons=['Cancel', 'Remove'])
			if result == 1:
				parent.Embody.RemoveListerRow(path, rel_fp)


def onRadio(comp, row, col, prevRow, prevCol):
	return


def onFocus(comp, row, col, prevRow, prevCol):
	return


def onEdit(comp, row, col, val):
	return
