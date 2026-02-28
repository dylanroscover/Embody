"""
Prepare hierarchical data for the Manager List COMP.

Takes the raw externalizations select DAT as input and:
1. Injects synthetic parent rows for tree hierarchy
2. Computes depth, has_children for each row
3. Filters by expanded_paths for tree expand/collapse
4. Adds strategy_state column for the unified Strategy column
5. Sorts hierarchically (parents before children, alphabetical)

Output columns:
  path, type, strategy, rel_file_path, timestamp, build, touch_build,
  strategy_state, depth, has_children
"""


def onCook(scriptOp):
	scriptOp.clear()
	inp = scriptOp.inputs[0]

	if not inp or inp.numRows == 0:
		return

	in_headers = [c.val for c in inp.row(0)]
	path_idx = in_headers.index('path')
	type_idx = in_headers.index('type')
	has_strategy = 'strategy' in in_headers

	# Build data rows dict keyed by path
	data_rows = {}
	comp_paths = set()

	for i in range(1, inp.numRows):
		row = {h: inp[i, j].val for j, h in enumerate(in_headers)}
		path = row['path']
		data_rows[path] = row
		oper = op(path)
		if oper and oper.family == 'COMP':
			comp_paths.add(path)

	# Inject synthetic parent rows for tree hierarchy
	all_paths = set(data_rows.keys())
	for path in list(all_paths):
		parts = path.strip('/').split('/')
		for j in range(1, len(parts)):
			prefix = '/' + '/'.join(parts[:j])
			if prefix not in data_rows:
				data_rows[prefix] = {h: '' for h in in_headers}
				data_rows[prefix]['path'] = prefix
				all_paths.add(prefix)

	if not all_paths:
		return

	# Read filter text from toolbar widget
	filter_widget = parent.Embody.op('toolbar/container_right/filter')
	filter_text = ''
	if filter_widget:
		filter_text = filter_widget.par.Value0.eval().strip().lower()

	# Apply text filter (case-insensitive substring match against path and file path)
	if filter_text:
		matched_paths = set()
		for path in all_paths:
			row = data_rows[path]
			searchable = (path + ' ' + row.get('rel_file_path', '')).lower()
			if filter_text in searchable:
				matched_paths.add(path)

		# Include ancestor paths to maintain tree structure
		paths_to_keep = set()
		for path in matched_paths:
			paths_to_keep.add(path)
			parts = path.strip('/').split('/')
			for j in range(1, len(parts)):
				ancestor = '/' + '/'.join(parts[:j])
				if ancestor in all_paths:
					paths_to_keep.add(ancestor)

		all_paths = paths_to_keep
		if not all_paths:
			return

	# Detect active TDN export
	exporting_path = None
	export_state = getattr(parent.Embody.ext.TDN, '_export_state', None)
	if export_state and not export_state.get('done'):
		exporting_path = export_state.get('root_path')

	# Compute depth relative to shallowest path
	min_depth = min(p.count('/') for p in all_paths)

	# Compute has_children by marking each path's parent
	has_children = set()
	for path in all_paths:
		parts = path.strip('/').split('/')
		if len(parts) > 1:
			parent_p = '/' + '/'.join(parts[:-1])
			if parent_p in all_paths:
				has_children.add(parent_p)

	# Get expand/collapse state
	expanded = parent.Embody.fetch('expanded_paths', None)
	if expanded is None:
		expanded = set(all_paths)
		parent.Embody.store('expanded_paths', expanded)

	# Sort and filter by visibility
	sorted_paths = sorted(all_paths)
	visible_expanded = set()
	visible = []

	for path in sorted_paths:
		parts = path.strip('/').split('/')
		parent_path = '/' + '/'.join(parts[:-1]) if len(parts) > 1 else None
		if (parent_path is None
				or parent_path not in all_paths
				or parent_path in visible_expanded):
			visible.append(path)
			if path in has_children and path in expanded:
				visible_expanded.add(path)

	# Write output
	out_headers = ['path', 'type', 'strategy', 'rel_file_path', 'timestamp',
	               'build', 'touch_build', 'strategy_state',
	               'depth', 'has_children']
	scriptOp.appendRow(out_headers)

	for path in visible:
		row = data_rows[path]
		depth = path.count('/') - min_depth
		hc = '1' if path in has_children else '0'

		oper = op(path)
		is_comp = oper and oper.family == 'COMP'

		# Get strategy — derive from old schema if column missing
		if has_strategy:
			strategy = row.get('strategy', '')
		else:
			row_type = row.get('type', '')
			if row_type == 'tdn':
				strategy = 'tdn'
			elif is_comp and row.get('rel_file_path', ''):
				strategy = 'tox'
			elif row.get('rel_file_path', ''):
				ext = row['rel_file_path'].rsplit('.', 1)[-1] if '.' in row['rel_file_path'] else ''
				strategy = ext
			else:
				strategy = ''

		# Compute unified strategy_state for the Strategy column
		if not strategy and not is_comp:
			# Synthetic parent or DAT without strategy
			strategy_state = ''
		elif strategy == 'tdn':
			if path == exporting_path:
				strategy_state = 'TDN_Exporting'
			else:
				dirty_val = row.get('dirty', '')
				if dirty_val == 'Par':
					strategy_state = 'TDN_ParChange'
				elif dirty_val in ('True', 'true', '1'):
					strategy_state = 'TDN_Dirty'
				else:
					strategy_state = 'TDN_Saved'
		elif strategy == 'tox':
			dirty_val = row.get('dirty', '')
			if dirty_val == 'Par':
				strategy_state = 'TOX_ParChange'
			elif dirty_val in ('True', 'true', '1'):
				strategy_state = 'TOX_Dirty'
			else:
				strategy_state = 'TOX_Saved'
		elif is_comp and not strategy:
			# Unexternalized COMP (synthetic parent row)
			strategy_state = 'Comp'
		elif strategy:
			# DAT with a strategy (py, json, md, etc.)
			strategy_state = 'DAT_Saved'
		else:
			strategy_state = ''

		scriptOp.appendRow([
			path,
			row.get('type', ''),
			strategy,
			row.get('rel_file_path', ''),
			row.get('timestamp', ''),
			row.get('build', ''),
			row.get('touch_build', ''),
			strategy_state,
			str(depth),
			hc,
		])
