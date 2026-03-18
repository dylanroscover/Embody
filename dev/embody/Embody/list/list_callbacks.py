"""
List COMP callbacks for Embody Manager UI.

Renders the externalization tree with expand/collapse, rollover
highlights, and clickable Strategy/Delete buttons. All styling
is pure Python — no TOP textures needed.

me   - this callbacks DAT
comp - the List COMP (available in all callbacks)
"""
from datetime import datetime, timezone

# --- Column indices ---
COL_EXPANDO = 0
COL_PATH = 1
COL_TYPE = 2
COL_FILE = 3
COL_STRATEGY = 4
COL_BUILD = 5
COL_TIMESTAMP = 6
COL_DELETE = 7

NUM_COLS = 8

HEADER_LABELS = [
	'', 'Network Path', '', 'External File Path',
	'Strategy', 'Build', 'Timestamp', 'Del',
]
COL_WIDTHS    = [0, 0, 80, 200, 70, 50, 190, 36]
COL_STRETCHES = [False, True, False, True, False, False, False, False]
TEXT_PAD_X = 6  # horizontal padding for left-justified cells

# Row whose Strategy cell is "active" (menu open) — shows "..." while menu visible
_active_strategy_row = -1

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

	# State colors from Embody parameters
	dirty_raw = _par4('Dirtycolor')
	dirty = _composite(dirty_raw, row) if dirty_raw[3] < 1.0 else dirty_raw

	par_change_raw = _par4('Dirtyparcolor')
	par_change = _composite(par_change_raw, row) if par_change_raw[3] < 1.0 else par_change_raw

	tdn_saved_raw = _par4('Tdnsavedcolor')
	tdn_saved = _composite(tdn_saved_raw, row) if tdn_saved_raw[3] < 1.0 else tdn_saved_raw

	# TDN exporting — warm shift from TDN saved blue
	tdn_amber = (tdn_saved[0] + 0.12, max(0, tdn_saved[1] - 0.02),
	             max(0, tdn_saved[2] - 0.04), 1.0)

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
		'tdn_saved': tdn_saved,
		'tdn_saved_roll': _brighten(tdn_saved, 0.08),
		'tdn_amber': tdn_amber,
		'tdn_amber_roll': _brighten(tdn_amber, 0.08),
		'border': border,
	})


def _ensure_theme():
	"""Lazy-load theme if _t was reset by module recompile."""
	if not _t:
		_load_theme()


def clearActiveStrategy():
	"""Clear the active strategy row (called when menu closes)."""
	global _active_strategy_row
	_active_strategy_row = -1


def _source():
	"""Return the inject_parents DAT (data source)."""
	return op('inject_parents')


def _row_bg(row):
	return _t['row'] if row % 2 == 0 else _t['row_alt']


def _strategy_style(state):
	"""Return (text, bgColor, textColor) for a strategy_state value."""
	if state == 'TOX_Saved':
		return ('TOX', _t['saved'], None)
	elif state == 'TOX_Dirty':
		return ('TOX', _t['dirty'], None)
	elif state == 'TOX_ParChange':
		return ('TOX Par', _t['par_change'], None)
	elif state == 'TDN_Saved':
		return ('TDN', _t['tdn_saved'], None)
	elif state == 'TDN_Dirty':
		return ('TDN', _t['dirty'], None)
	elif state == 'TDN_ParChange':
		return ('TDN Par', _t['par_change'], None)
	elif state == 'TDN_Exporting':
		return ('...', _t['tdn_amber'], None)
	elif state == 'Comp':
		return ('Tag', _t['comp'], None)
	elif state == 'DAT_Saved':
		return ('', None, None)  # DATs show strategy text in _apply_cell
	return ('', None, None)


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
		name = (path.rsplit('/', 1)[-1] or path) if path else ''
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

	elif col == COL_STRATEGY:
		st = data[row, 'strategy_state'].val
		strategy = data[row, 'strategy'].val
		text, st_bg, st_text = _strategy_style(st)

		if _active_strategy_row == row and st not in ('DAT_Saved', 'TDN_Exporting', ''):
			# Menu is open for this row — show "..." with rollover color
			attribs.text = '...' if st != 'Comp' else 'Tag'
			if st in ('TOX_Dirty', 'TDN_Dirty'):
				attribs.bgColor = _t['dirty_roll']
			elif st in ('TOX_ParChange', 'TDN_ParChange'):
				attribs.bgColor = _t['par_change_roll']
			elif st == 'TOX_Saved':
				attribs.bgColor = _t['saved_roll']
			elif st == 'TDN_Saved':
				attribs.bgColor = _t['tdn_saved_roll']
			elif st == 'Comp':
				attribs.bgColor = _t['comp_roll']
		elif st == 'DAT_Saved':
			# Show the file extension as the strategy label
			attribs.text = strategy
			attribs.bgColor = bg
		elif st_bg:
			attribs.text = text
			attribs.bgColor = st_bg
			if st_text:
				attribs.textColor = st_text
		else:
			attribs.text = text
			attribs.bgColor = bg
		attribs.textJustify = JustifyType.CENTER

	elif col == COL_BUILD:
		attribs.text = data[row, 'build'].val
		attribs.textJustify = JustifyType.CENTER
		attribs.bgColor = bg

	elif col == COL_TIMESTAMP:
		ts = data[row, 'timestamp'].val
		if ts and parent.Embody.par.Localtimestamps.eval():
			try:
				utc_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
				local_dt = utc_dt.astimezone()
				tz_abbr = local_dt.strftime("%Z")
				# macOS returns full names like "Pacific Daylight Time"; shorten to acronym
				if len(tz_abbr) > 5:
					tz_abbr = ''.join(w[0] for w in tz_abbr.split())
				ts = local_dt.strftime("%Y-%m-%d %H:%M:%S") + ' ' + tz_abbr
			except ValueError:
				pass
		attribs.text = ts
		attribs.textJustify = JustifyType.CENTERLEFT
		attribs.textOffsetX = TEXT_PAD_X
		attribs.bgColor = bg

	elif col == COL_DELETE:
		has_ext = bool(data[row, 'rel_file_path'].val
					   or data[row, 'strategy_state'].val.startswith('TOX')
					   or data[row, 'strategy_state'].val.startswith('TDN'))
		if has_ext:
			attribs.text = '×'
			attribs.fontSizeX = 12
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
		if prevRow is not None and prevRow > 0 and prevRow < data.numRows:
			for c in range(ncols):
				_apply_cell(comp.cellAttribs[prevRow, c],
				            prevRow, c, data, highlight=False)
		if row is not None and row > 0 and row < data.numRows:
			for c in range(ncols):
				_apply_cell(comp.cellAttribs[row, c],
				            row, c, data, highlight=True)

	# Column changed within same row -> restore old cell to highlight state
	if prevRow == row and prevCol != col and row is not None and row > 0:
		if prevCol >= 0 and prevCol < ncols and row < data.numRows:
			_apply_cell(comp.cellAttribs[row, prevCol],
			            row, prevCol, data, highlight=True)

	if row is None or col is None or row <= 0 or row >= data.numRows or col < 0:
		return

	# Cell-specific rollover effects on Strategy column
	if col == COL_STRATEGY:
		st = data[row, 'strategy_state'].val
		if st in ('TOX_Dirty', 'TDN_Dirty'):
			comp.cellAttribs[row, col].text = '...'
			comp.cellAttribs[row, col].bgColor = _t['dirty_roll']
		elif st in ('TOX_ParChange', 'TDN_ParChange'):
			comp.cellAttribs[row, col].text = '...'
			comp.cellAttribs[row, col].bgColor = _t['par_change_roll']
		elif st == 'TOX_Saved':
			comp.cellAttribs[row, col].text = '...'
			comp.cellAttribs[row, col].bgColor = _t['saved_roll']
		elif st == 'TDN_Saved':
			comp.cellAttribs[row, col].text = '...'
			comp.cellAttribs[row, col].bgColor = _t['tdn_saved_roll']
		elif st == 'Comp':
			comp.cellAttribs[row, col].text = 'Tag'
			comp.cellAttribs[row, col].bgColor = _t['comp_roll']
		elif st == 'TDN_Exporting':
			comp.cellAttribs[row, col].bgColor = _t['tdn_amber_roll']
	elif col == COL_TYPE:
		# Brighten to hint that clicking opens the network editor
		comp.cellAttribs[row, col].bgColor = _brighten(_t['select'], 0.04)
	elif col == COL_FILE:
		# Brighten to hint that clicking reveals the file
		if data[row, 'rel_file_path'].val:
			comp.cellAttribs[row, col].bgColor = _brighten(_t['select'], 0.04)
	elif col == COL_DELETE:
		has_ext = bool(data[row, 'rel_file_path'].val
					   or data[row, 'strategy_state'].val.startswith('TOX')
					   or data[row, 'strategy_state'].val.startswith('TDN'))
		if has_ext:
			comp.cellAttribs[row, col].textColor = _t['text']
			comp.cellAttribs[row, col].bgColor = _t['select']


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
			expand_order = parent.Embody.fetch('expand_order', [])
			if path in expanded:
				expanded.discard(path)
				if path in expand_order:
					expand_order.remove(path)
			else:
				expanded.add(path)
				if path in expand_order:
					expand_order.remove(path)
				expand_order.append(path)
			parent.Embody.store('expanded_paths', expanded)
			parent.Embody.store('expand_order', expand_order)
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

	elif col == COL_STRATEGY:
		global _active_strategy_row
		st = data[row, 'strategy_state'].val
		oper = op(path)

		if st == 'TDN_Exporting':
			return

		if st == 'Comp' and oper:
			# Unexternalized COMP — open tagger for strategy choice
			_active_strategy_row = row
			parent.Embody.ext.Embody.rolloverOp = oper
			parent.Embody.op('tagger/switch_family').par.index = 2
			run(lambda: parent.Embody.ext.Embody.SetupTaggerTagMode(oper), delayFrames=1)
			run(f"op('{parent.Embody.op('window_tagging_menu')}').par.winopen.pulse()",
				delayFrames=2)
		elif (st.startswith('TOX_') or st.startswith('TDN_')) and oper:
			# Already tagged COMP — open manage menu with Switch/Save
			_active_strategy_row = row
			parent.Embody.ext.Embody.rolloverOp = oper
			parent.Embody.op('tagger/switch_family').par.index = 2
			run(lambda s=st: parent.Embody.ext.Embody.SetupTaggerManageMode(oper, s),
				delayFrames=1)
			run(f"op('{parent.Embody.op('window_tagging_menu')}').par.winopen.pulse()",
				delayFrames=2)

	elif col == COL_DELETE:
		rel_fp = data[row, 'rel_file_path'].val
		st = data[row, 'strategy_state'].val
		if not rel_fp and not st.startswith('TOX') and not st.startswith('TDN'):
			return
		oper = op(path)
		result = ui.messageBox(
			'Remove',
			'Remove this externalization?\n\n'
			'This will delete the external file from disk, clear the\n'
			"operator's externalization tags, and remove the tracking\n"
			'entry. This cannot be undone.\n\n'
			'Operator: ' + path,
			buttons=['Cancel', 'Remove'])
		if result == 1:
			if st.startswith('TDN'):
				parent.Embody.RemoveTDNEntry(path)
			else:
				parent.Embody.RemoveListerRow(path, rel_fp)


def onRadio(comp, row, col, prevRow, prevCol):
	return


def onFocus(comp, row, col, prevRow, prevCol):
	return


def onEdit(comp, row, col, val):
	return
