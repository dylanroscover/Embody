"""
Prepare hierarchical data for the Manager List COMP.

Takes the raw externalizations select DAT as input and:
1. Filters out type='tdn' rows (shown via TDN column, not as separate entries)
2. Injects synthetic parent rows for tree hierarchy
3. Computes depth, has_children for each row
4. Filters by expanded_paths for tree expand/collapse
5. Adds tdn_state and tox_state columns for button rendering
6. Sorts hierarchically (parents before children, alphabetical)

Output columns:
  path, type, rel_file_path, timestamp, build, touch_build,
  tdn_state, tox_state, depth, has_children
"""


def onCook(scriptOp):
	scriptOp.clear()
	inp = scriptOp.inputs[0]

	if not inp or inp.numRows == 0:
		return

	in_headers = [c.val for c in inp.row(0)]
	path_idx = in_headers.index('path')
	type_idx = in_headers.index('type')

	# Separate regular entries from TDN-only entries
	data_rows = {}
	tdn_paths = set()
	comp_paths = set()

	for i in range(1, inp.numRows):
		row = {h: inp[i, j].val for j, h in enumerate(in_headers)}
		if row['type'] == 'tdn':
			tdn_paths.add(row['path'])
		else:
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

	# Detect active TDN export
	exporting_path = None
	tdn_ext = parent.Embody.ext.TDN
	export_state = getattr(tdn_ext, '_export_state', None)
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
	out_headers = ['path', 'type', 'rel_file_path', 'timestamp',
	               'build', 'touch_build', 'tdn_state', 'tox_state',
	               'depth', 'has_children']
	scriptOp.appendRow(out_headers)

	for path in visible:
		row = data_rows[path]
		depth = path.count('/') - min_depth
		hc = '1' if path in has_children else '0'

		oper = op(path)
		is_comp = oper and oper.family == 'COMP'

		if is_comp and path == exporting_path:
			tdn_st = 'Exporting'
		elif is_comp and path in tdn_paths:
			tdn_st = 'Saved'
		elif is_comp:
			tdn_st = 'Comp'
		else:
			tdn_st = ''

		if path in comp_paths:
			dirty_val = row.get('dirty', '')
			if dirty_val == 'Par':
				tox_st = 'ParChange'
			elif dirty_val in ('True', 'true', '1'):
				tox_st = 'Dirty'
			else:
				tox_st = 'Saved'
		elif is_comp:
			tox_st = 'Comp'
		else:
			tox_st = ''

		scriptOp.appendRow([
			path,
			row.get('type', ''),
			row.get('rel_file_path', ''),
			row.get('timestamp', ''),
			row.get('build', ''),
			row.get('touch_build', ''),
			tdn_st,
			tox_st,
			str(depth),
			hc,
		])
