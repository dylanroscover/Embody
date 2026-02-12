"""
TDN — TouchDesigner Network open format (.tdn)

Exports and imports TouchDesigner networks as human-readable JSON files.
Only non-default properties are stored, keeping the output minimal.

This extension lives on the Embody COMP and is callable via:
  - MCP tools (export_network / import_network) through Claudius
  - TD UI (keyboard shortcut Ctrl+Shift+N, pulse parameters)
  - Direct Python: op.Embody.ext.TDN.ExportNetwork(...)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event
from typing import Any, Optional, Union

TDN_VERSION = '1.0'

# Parameters to always skip (Embody-managed or internal)
SKIP_PARAMS = {
	'externaltox', 'enableexternaltox', 'reloadtox',
	'file', 'syncfile',
	'reinitextensions', 'savebackup',
	'savecustom', 'reloadcustom',
	'pageindex',  # UI state (visible parameter page tab), not config
}

# Built-in parameter styles to skip (actions, not state)
SKIP_BUILTIN_STYLES = {'Pulse', 'Momentary', 'Header'}

# Suffix patterns for multi-component parameter groups
STYLE_SUFFIXES = {
	'XY': ['x', 'y'],
	'XYZ': ['x', 'y', 'z'],
	'XYZW': ['x', 'y', 'z', 'w'],
	'WH': ['w', 'h'],
	'UV': ['u', 'v'],
	'UVW': ['u', 'v', 'w'],
	'RGB': ['r', 'g', 'b'],
	'RGBA': ['r', 'g', 'b', 'a'],
}

# Map parameter style to Page.append* method name
STYLE_APPEND_MAP = {
	'Float': 'appendFloat',
	'Int': 'appendInt',
	'Str': 'appendStr',
	'Menu': 'appendMenu',
	'StrMenu': 'appendStrMenu',
	'Toggle': 'appendToggle',
	'Pulse': 'appendPulse',
	'Momentary': 'appendMomentary',
	'Header': 'appendHeader',
	'OP': 'appendOP',
	'COMP': 'appendCOMP',
	'TOP': 'appendTOP',
	'CHOP': 'appendCHOP',
	'SOP': 'appendSOP',
	'DAT': 'appendDAT',
	'MAT': 'appendMAT',
	'POP': 'appendPOP',
	'Object': 'appendObject',
	'PanelCOMP': 'appendPanelCOMP',
	'XY': 'appendXY',
	'XYZ': 'appendXYZ',
	'XYZW': 'appendXYZW',
	'WH': 'appendWH',
	'UV': 'appendUV',
	'UVW': 'appendUVW',
	'RGB': 'appendRGB',
	'RGBA': 'appendRGBA',
	'File': 'appendFile',
	'FileSave': 'appendFileSave',
	'Folder': 'appendFolder',
	'Python': 'appendPython',
	'Sequence': 'appendSequence',
}

# Default flag values — only export flags that differ
DEFAULT_FLAGS = {
	'bypass': False,
	'lock': False,
	'display': False,
	'render': False,
	'viewer': False,
	'expose': True,
	'allowCooking': True,
}

DEFAULT_NODE_SIZE = (200, 100)
DEFAULT_COLOR = (0.545, 0.545, 0.545)
COLOR_TOLERANCE = 0.01

# System/internal paths to exclude from export
SYSTEM_PATHS = ('/local', '/sys', '/perform', '/ui')
_SYSTEM_PATH_PREFIXES = tuple(p + '/' for p in SYSTEM_PATHS)


class TDNExt:
	"""Extension for exporting/importing TouchDesigner networks as .tdn JSON."""

	def __init__(self, ownerComp: 'COMP') -> None:
		self.ownerComp: 'COMP' = ownerComp
		self._export_state: Optional[dict[str, Any]] = None

	# =========================================================================
	# PROMOTED METHODS (uppercase — callable directly on op.Embody)
	# =========================================================================

	def ExportNetwork(self, root_path: str = '/', include_dat_content: Optional[bool] = None,
					  output_file: Optional[str] = None, max_depth: Optional[int] = None) -> dict[str, Any]:
		"""
		Export a TouchDesigner network to .tdn JSON format.

		Args:
			root_path: COMP path to export from (default '/')
			include_dat_content: Include text/table content of DATs
			output_file: File path to write JSON to. 'auto' generates a name.
						 None returns the dict without writing to disk.
			max_depth: Maximum recursion depth (None = unlimited)

		Returns:
			dict with 'success' and 'tdn' keys, or 'error' key on failure
		"""
		root_op = op(root_path)
		if not root_op:
			return {'error': f'Operator not found: {root_path}'}
		if not hasattr(root_op, 'children'):
			return {'error': f'{root_path} is not a COMP'}

		# Resolve from toggle if not explicitly set
		if include_dat_content is None:
			include_dat_content = self.ownerComp.par.Embeddatsintdns.eval()

		options = {
			'include_dat_content': include_dat_content,
			'max_depth': max_depth,
		}

		try:
			operators = self._exportChildren(root_op, options, depth=0)

			tdn = {
				'format': 'tdn',
				'version': TDN_VERSION,
				'generator': f'Claudius/{self._getClaudiusVersion()}',
				'td_build': f'{app.version}.{app.build}',
				'exported_at': datetime.now(timezone.utc).strftime(
					'%Y-%m-%dT%H:%M:%SZ'),
				'root': root_path,
				'options': {
					'include_dat_content': include_dat_content,
				},
				'operators': operators,
			}

			result = {'success': True, 'tdn': tdn}

			# Write to file if requested
			if output_file:
				from pathlib import Path
				export_mode = self.ownerComp.par.Tdnexportmode.eval()

				if export_mode == 'percomp':
					ext_folder = self.ownerComp.ext.Embody.ExternalizationsFolder
					if ext_folder:
						base_folder = str(Path(project.folder) / ext_folder)
						Path(base_folder).mkdir(parents=True, exist_ok=True)
					else:
						base_folder = str(project.folder)

					before_tdn = TDNExt._collectExistingTDNFiles(
						base_folder, root_path)

					per_comp_files = TDNExt._splitPerComp(
						operators, root_path,
						project.name.removesuffix('.toe'), base_folder)

					written_files = []
					for fpath, comp_ops in per_comp_files.items():
						comp_tdn = dict(tdn)
						comp_tdn['operators'] = comp_ops
						comp_tdn['export_mode'] = 'percomp'
						Path(fpath).parent.mkdir(parents=True, exist_ok=True)
						Path(fpath).write_text(
							json.dumps(comp_tdn, indent='\t',
									   ensure_ascii=False) + '\n',
							encoding='utf-8')
						written_files.append(fpath)

					stale = TDNExt._cleanupStaleTDNFiles(
						before_tdn, written_files, base_folder)
					if stale:
						self._log(
							f'Cleaned up {len(stale)} stale .tdn file(s)',
							'INFO')

					if root_path == '/':
						root_rel = project.name.removesuffix('.toe') + '.tdn'
					else:
						root_rel = root_path.lstrip('/') + '.tdn'
					root_file = str(Path(base_folder) / root_rel)
					result['file'] = root_file
					result['files'] = written_files
					self._trackTDNExport(root_path, root_file)
					self._log(
						f'Exported network to {len(written_files)} '
						f'.tdn files', 'SUCCESS')
				else:
					ext_folder = self.ownerComp.ext.Embody.ExternalizationsFolder
					if ext_folder:
						scan_folder = str(
							Path(project.folder) / ext_folder)
					else:
						scan_folder = str(project.folder)
					before_tdn = TDNExt._collectExistingTDNFiles(
						scan_folder, root_path)

					filepath = self._resolveOutputPath(output_file, root_op)
					Path(filepath).write_text(
						json.dumps(tdn, indent='\t',
								   ensure_ascii=False) + '\n',
						encoding='utf-8')

					stale = TDNExt._cleanupStaleTDNFiles(
						before_tdn, [filepath], scan_folder)
					if stale:
						self._log(
							f'Cleaned up {len(stale)} stale .tdn file(s)',
							'INFO')

					result['file'] = filepath
					self._trackTDNExport(root_path, filepath)
					self._log(
						f'Exported network to {filepath}', 'SUCCESS')

			return result

		except Exception as e:
			self._log(f'Export failed: {e}', 'ERROR')
			return {'error': f'Export failed: {e}'}

	def ExportNetworkAsync(self, root_path: str = '/', include_dat_content: Optional[bool] = None,
						   output_file: Optional[str] = None, max_depth: Optional[int] = None) -> None:
		"""
		Non-blocking export using Thread Manager. Processes operators in
		batches across frames so TouchDesigner stays responsive.

		Use this for keyboard shortcuts and UI buttons. For MCP (where the
		caller is already waiting), use ExportNetwork() instead.

		Args:
			root_path: COMP path to export from (default '/')
			include_dat_content: Include text/table content of DATs
			output_file: File path to write JSON to. 'auto' generates a name.
			max_depth: Maximum recursion depth (None = unlimited)
		"""
		# Reject if export already running
		if (self._export_state is not None
				and not self._export_state.get('done')):
			self._log('Export already in progress', 'WARNING')
			return

		root_op = op(root_path)
		if not root_op:
			self._log(f'Operator not found: {root_path}', 'ERROR')
			return
		if not hasattr(root_op, 'children'):
			self._log(f'{root_path} is not a COMP', 'ERROR')
			return

		# Phase 1: Collect all operator paths (fast tree walk, single frame)
		op_paths = self._collectAllPaths(root_op, max_depth)
		if not op_paths:
			self._log(f'No operators to export in {root_path}', 'WARNING')
			return

		# Resolve output path now (needs TD access)
		resolved_path = None
		if output_file:
			resolved_path = self._resolveOutputPath(output_file, root_op)

		# Collect metadata now (needs TD access)
		metadata = {
			'generator': f'Claudius/{self._getClaudiusVersion()}',
			'td_build': f'{app.version}.{app.build}',
			'project_name': project.name.removesuffix('.toe'),
			'project_folder': str(project.folder),
			'ext_folder': self.ownerComp.ext.Embody.ExternalizationsFolder,
		}

		# Resolve from toggle if not explicitly set
		if include_dat_content is None:
			include_dat_content = self.ownerComp.par.Embeddatsintdns.eval()

		# Resolve export mode now (needs TD access)
		export_mode = self.ownerComp.par.Tdnexportmode.eval()

		done_event = Event()

		self._export_state = {
			'paths': op_paths,
			'index': 0,
			'batch_size': 50,
			'results': {},
			'options': {
				'include_dat_content': include_dat_content,
				'max_depth': max_depth,
			},
			'root_path': root_path,
			'output_file': resolved_path,
			'export_mode': export_mode,
			'metadata': metadata,
			'done_event': done_event,
			'done': False,
			'error': None,
			'result': None,
		}

		# Capture state ref for worker closure
		state = self._export_state

		def worker():
			"""Worker thread: wait for batches to finish, then write file."""
			done_event.wait()

			if state['error']:
				raise RuntimeError(state['error'])

			# Assemble hierarchy from flat results (pure Python, no TD)
			operators = TDNExt._assembleHierarchy(
				state['results'], state['root_path'])

			tdn = {
				'format': 'tdn',
				'version': TDN_VERSION,
				'generator': state['metadata']['generator'],
				'td_build': state['metadata']['td_build'],
				'exported_at': datetime.now(timezone.utc).strftime(
					'%Y-%m-%dT%H:%M:%SZ'),
				'root': state['root_path'],
				'options': {
					'include_dat_content':
						state['options']['include_dat_content'],
				},
				'operators': operators,
			}

			# Count total operators
			def count_ops(ops):
				n = len(ops)
				for o in ops:
					n += count_ops(o.get('children', []))
				return n

			op_count = count_ops(operators)

			# Write to file (file I/O is fine in worker thread)
			if state['output_file']:
				from pathlib import Path
				export_mode = state.get('export_mode', 'perproject')

				if export_mode == 'percomp':
					ext_folder = state['metadata'].get(
						'ext_folder', '')
					proj_folder = state['metadata']['project_folder']
					if ext_folder:
						base_folder = str(
							Path(proj_folder) / ext_folder)
						Path(base_folder).mkdir(
							parents=True, exist_ok=True)
					else:
						base_folder = proj_folder

					before_tdn = TDNExt._collectExistingTDNFiles(
						base_folder, state['root_path'])

					proj_name = state['metadata']['project_name']
					per_comp_files = TDNExt._splitPerComp(
						operators, state['root_path'],
						proj_name, base_folder)

					written_files = []
					for fpath, comp_ops in per_comp_files.items():
						comp_tdn = dict(tdn)
						comp_tdn['operators'] = comp_ops
						comp_tdn['export_mode'] = 'percomp'
						Path(fpath).parent.mkdir(
							parents=True, exist_ok=True)
						Path(fpath).write_text(
							json.dumps(comp_tdn, indent='\t',
									   ensure_ascii=False) + '\n',
							encoding='utf-8')
						written_files.append(fpath)

					stale = TDNExt._cleanupStaleTDNFiles(
						before_tdn, written_files, base_folder)

					root_path = state['root_path']
					if root_path == '/':
						root_rel = proj_name + '.tdn'
					else:
						root_rel = root_path.lstrip('/') + '.tdn'
					root_file = str(
						Path(base_folder) / root_rel)
					state['result'] = {
						'success': True,
						'op_count': op_count,
						'file': root_file,
						'files': written_files,
						'cleaned_up': len(stale) if stale else 0,
					}
				else:
					ext_folder = state['metadata'].get(
						'ext_folder', '')
					proj_folder = state['metadata']['project_folder']
					if ext_folder:
						scan_folder = str(
							Path(proj_folder) / ext_folder)
					else:
						scan_folder = proj_folder
					before_tdn = TDNExt._collectExistingTDNFiles(
						scan_folder, state['root_path'])

					json_str = json.dumps(
						tdn, indent='\t', ensure_ascii=False) + '\n'
					Path(state['output_file']).write_text(
						json_str, encoding='utf-8')

					stale = TDNExt._cleanupStaleTDNFiles(
						before_tdn, [state['output_file']],
						scan_folder)

					state['result'] = {
						'success': True,
						'op_count': op_count,
						'file': state['output_file'],
						'cleaned_up': len(stale) if stale else 0,
					}
			else:
				state['result'] = {
					'success': True,
					'op_count': op_count,
					'file': None,
				}

		# Create and enqueue TDTask
		thread_manager = op.TDResources.ThreadManager
		task = thread_manager.TDTask(
			target=worker,
			SuccessHook=self._onExportSuccess,
			ExceptHook=self._onExportError,
			RefreshHook=self._onExportRefresh,
		)
		thread_manager.EnqueueTask(task, standalone=True)

		self._log(
			f'Exporting {len(op_paths)} operators from {root_path}...',
			'INFO')

	def _onExportRefresh(self):
		"""RefreshHook: Process a batch of operators per frame (main thread)."""
		state = self._export_state
		if state is None or state['done']:
			return

		paths = state['paths']
		idx = state['index']
		batch_end = min(idx + state['batch_size'], len(paths))

		for i in range(idx, batch_end):
			try:
				target_op = op(paths[i])
				if target_op:
					op_data = self._exportSingleOp(
						target_op, state['options'], depth=0, recurse=False)
					if op_data:
						state['results'][paths[i]] = op_data
			except Exception as e:
				self._log(f'Error exporting {paths[i]}: {e}', 'WARNING')

		state['index'] = batch_end

		if batch_end >= len(paths):
			state['done'] = True
			state['done_event'].set()

	def _onExportSuccess(self):
		"""SuccessHook: Log completion (main thread)."""
		state = self._export_state
		if state and state.get('result'):
			result = state['result']
			msg = f"Exported {result.get('op_count', 0)} operators"
			if result.get('files'):
				msg += f" to {len(result['files'])} .tdn files"
			elif result.get('file'):
				msg += f" to {result['file']}"
			if result.get('file'):
				self._trackTDNExport(state['root_path'], result['file'])
			self._log(msg, 'SUCCESS')
			if result.get('cleaned_up'):
				self._log(
					f"Cleaned up {result['cleaned_up']} stale .tdn file(s)",
					'INFO')

		self._export_state = None

		# Chain next re-export if queue active
		if getattr(self, '_reexport_queue', None):
			run("args[0]._processNextReexport()", self, delayFrames=1)

	def _onExportError(self, e):
		"""ExceptHook: Log error (main thread)."""
		self._log(f'Export failed: {e}', 'ERROR')
		self._export_state = None
		self._reexport_queue = None

	def ReexportAllTDNs(self) -> None:
		"""Re-export all tracked TDN files with current toggle setting."""
		try:
			embody_ext = self.ownerComp.ext.Embody
			table = embody_ext.Externalizations
			if not table:
				return

			tdn_entries = []
			for i in range(1, table.numRows):
				if table[i, 'type'].val == 'tdn':
					root_path = table[i, 'path'].val
					if op(root_path):
						tdn_entries.append(root_path)

			if not tdn_entries:
				self._log('No TDN exports to update', 'INFO')
				return

			self._reexport_queue = list(tdn_entries)
			self._log(
				f'Re-exporting {len(tdn_entries)} TDN file(s)...', 'INFO')
			self._processNextReexport()
		except Exception as e:
			self._log(f'Failed to re-export TDNs: {e}', 'ERROR')

	def _processNextReexport(self):
		"""Pop next TDN from queue and start async export."""
		if not getattr(self, '_reexport_queue', None):
			self._reexport_queue = None
			return

		root_path = self._reexport_queue.pop(0)
		self.ExportNetworkAsync(root_path=root_path, output_file='auto')

	def ImportNetwork(self, target_path: str, tdn: Union[dict[str, Any], list[dict[str, Any]]], clear_first: bool = False) -> dict[str, Any]:
		"""
		Import a .tdn network into a COMP, recreating all operators.

		Args:
			target_path: Destination COMP path to import into
			tdn: The .tdn dict (full document or just the 'operators' list)
			clear_first: Delete all existing children before importing

		Returns:
			dict with 'success', 'created_count', 'created_paths' or 'error'
		"""
		dest = op(target_path)
		if not dest:
			return {'error': f'Destination not found: {target_path}'}
		if not hasattr(dest, 'create'):
			return {'error': f'{target_path} is not a COMP'}

		# Accept full .tdn document or just the operators array
		if isinstance(tdn, dict) and 'operators' in tdn:
			op_defs = tdn['operators']
		elif isinstance(tdn, list):
			op_defs = tdn
		else:
			return {'error': 'Invalid .tdn format'}

		if clear_first:
			for child in list(dest.children):
				try:
					child.destroy()
				except Exception as e:
					self._log(f'Failed to destroy {child.path}: {e}', 'WARNING')

		try:
			created = []

			# Phase 1: Create all operators (depth-first)
			self._createOps(dest, op_defs, created)

			# Phase 2: Create custom parameters
			self._createCustomPars(dest, op_defs)

			# Phase 3: Set parameter values
			self._setParameters(dest, op_defs)

			# Phase 4: Set flags
			self._setFlags(dest, op_defs)

			# Phase 5: Wire connections
			self._wireConnections(dest, op_defs)

			# Phase 6: Set DAT content
			self._setDATContent(dest, op_defs)

			# Phase 7: Set positions (last)
			self._setPositions(dest, op_defs)

			self._log(
				f'Imported {len(created)} operators into {target_path}',
				'SUCCESS')
			return {
				'success': True,
				'destination': target_path,
				'created_count': len(created),
				'created_paths': created,
			}

		except Exception as e:
			self._log(f'Import failed: {e}', 'ERROR')
			return {'error': f'Import failed: {e}'}

	def ImportNetworkFromFile(self, file_path: str, target_path: str = '/') -> Optional[dict[str, Any]]:
		"""
		Load a .tdn JSON file from disk and import it into a COMP.

		Args:
			file_path: Path to the .tdn file on disk
			target_path: Destination COMP path (default '/')
		"""
		if not file_path:
			self._log('No TDN file specified', 'WARNING')
			return

		import os
		if not os.path.isfile(file_path):
			self._log(f'TDN file not found: {file_path}', 'ERROR')
			return

		try:
			with open(file_path, 'r', encoding='utf-8') as f:
				tdn_data = json.load(f)
		except json.JSONDecodeError as e:
			self._log(f'Invalid JSON in TDN file: {e}', 'ERROR')
			return
		except Exception as e:
			self._log(f'Failed to read TDN file: {e}', 'ERROR')
			return

		self._log(f'Importing from {file_path} into {target_path}...', 'INFO')
		return self.ImportNetwork(target_path, tdn_data)

	# =========================================================================
	# EXPORT INTERNALS
	# =========================================================================

	def _exportChildren(self, parent_op, options, depth):
		"""Recursively export children of a COMP."""
		max_depth = options.get('max_depth')
		if max_depth is not None and depth > max_depth:
			return []

		result = []
		for child in parent_op.children:
			# Skip system/internal paths (exact match or children)
			if child.path in SYSTEM_PATHS or child.path.startswith(
					_SYSTEM_PATH_PREFIXES):
				continue

			op_data = self._exportSingleOp(child, options, depth)
			if op_data is not None:
				result.append(op_data)

		return result

	def _exportSingleOp(self, target, options, depth, recurse=True):
		"""Export a single operator to a dict."""
		data = {
			'name': target.name,
			'type': target.OPType,
		}

		# Parameters (built-in, non-default only)
		params = self._exportBuiltinParams(target)
		if params:
			data['parameters'] = params

		# Custom parameters (always all of them)
		custom_pars = self._exportCustomPars(target)
		if custom_pars:
			data['custom_pars'] = custom_pars

		# Flags (non-default only)
		flags = self._exportFlags(target)
		if flags:
			data['flags'] = flags

		# Position
		data['position'] = [target.nodeX, target.nodeY]

		# Size (only if non-default)
		if (target.nodeWidth, target.nodeHeight) != DEFAULT_NODE_SIZE:
			data['size'] = [target.nodeWidth, target.nodeHeight]

		# Color (only if non-default)
		color = tuple(target.color)
		if not self._colorsDiffer(color, DEFAULT_COLOR):
			pass  # skip default color
		else:
			data['color'] = [round(c, 4) for c in color]

		# Comment
		if target.comment:
			data['comment'] = target.comment

		# Tags
		tags = list(target.tags)
		if tags:
			data['tags'] = tags

		# Operator connections (left/right wires)
		connections = self._exportConnections(target)
		if connections:
			data['inputs'] = connections

		# COMP connections (top/bottom wires)
		if hasattr(target, 'inputCOMPConnectors'):
			comp_conns = self._exportCompConnections(target)
			if comp_conns:
				data['comp_inputs'] = comp_conns

		# DAT content (optional)
		if (target.family == 'DAT'
				and options.get('include_dat_content', True)):
			content_data = self._exportDATContent(target)
			if content_data:
				data.update(content_data)

		# Recurse into COMP children (sync mode only)
		# Skip children of palette clones — they come from /sys/ and
		# don't need to be stored (TD recreates them from the clone source)
		if recurse and hasattr(target, 'children'):
			if self._isPaletteClone(target):
				data['palette_clone'] = True
			else:
				max_depth = options.get('max_depth')
				if max_depth is None or depth < max_depth:
					children = self._exportChildren(
						target, options, depth + 1)
					if children:
						data['children'] = children

		return data

	def _exportBuiltinParams(self, target):
		"""Export non-default built-in parameter values."""
		params = {}

		for p in target.pars():
			if p.name in SKIP_PARAMS:
				continue
			# Only built-in params here; custom pars handled separately
			if p.isCustom:
				continue
			if p.readOnly:
				continue
			if p.style in SKIP_BUILTIN_STYLES:
				continue

			try:
				if p.mode == ParMode.EXPRESSION:
					params[p.name] = {'expr': p.expr}
				elif p.mode == ParMode.BIND:
					params[p.name] = {'bind': p.bindExpr}
				elif p.mode == ParMode.CONSTANT:
					current = p.eval()
					default = p.default
					if self._valuesDiffer(current, default):
						params[p.name] = self._serializeValue(current)
				# Skip EXPORT mode (set by the exporter op, not importable)
			except Exception as e:
				self._log(f'Error reading param {p.name} on {target.path}: {e}', 'DEBUG')

		return params

	def _exportCustomPars(self, target):
		"""Export ALL custom parameters with full definitions."""
		if not hasattr(target, 'customPages'):
			return []

		custom_pars = []
		seen_names = set()

		for page in target.customPages:
			for p in page.pars:
				if p.name in seen_names:
					continue

				# Get the tuplet (group of related pars)
				try:
					group = p.tuplet
				except Exception as e:
					self._log(f'Could not get tuplet for {p.name}: {e}', 'DEBUG')
					group = (p,)

				# Mark all pars in this group as seen
				for gp in group:
					seen_names.add(gp.name)

				# Export the group as a single definition
				par_def = self._exportCustomParGroup(page, group)
				if par_def:
					custom_pars.append(par_def)

		return custom_pars

	# Standard defaults TD assigns to newly created custom parameters
	_STANDARD_DEFAULTS = {0, 0.0, '', False}

	def _exportCustomParGroup(self, page, group):
		"""Export a custom parameter group (tuplet) definition."""
		first_par = group[0]
		style = first_par.style
		base_name = self._getGroupBaseName(first_par, group)

		par_def = {
			'name': base_name,
			'page': page.name,
			'style': style,
		}

		# Label — only if different from name
		if first_par.label != base_name:
			par_def['label'] = first_par.label

		# Size for multi-component parameters (Float/Int with size > 1)
		if len(group) > 1 and style in ('Float', 'Int'):
			par_def['size'] = len(group)

		# Section break
		if first_par.startSection:
			par_def['startSection'] = True

		# Numeric range (only non-standard values)
		if first_par.isNumber:
			default_val = self._serializeValue(first_par.default)
			if default_val not in self._STANDARD_DEFAULTS:
				par_def['default'] = default_val
			if first_par.min != 0:
				par_def['min'] = first_par.min
			if first_par.max != 1:
				par_def['max'] = first_par.max
			if first_par.clampMin:
				par_def['clampMin'] = True
			if first_par.clampMax:
				par_def['clampMax'] = True
			if first_par.normMin != 0:
				par_def['normMin'] = first_par.normMin
			if first_par.normMax != 1:
				par_def['normMax'] = first_par.normMax
		else:
			default_val = self._serializeValue(first_par.default)
			if default_val not in self._STANDARD_DEFAULTS:
				par_def['default'] = default_val

		# Menu entries
		if first_par.isMenu:
			if first_par.menuSource:
				# Dynamically populated — store the source, not the entries
				par_def['menuSource'] = first_par.menuSource
			else:
				# Manually defined — store entries
				names = list(first_par.menuNames)
				labels = list(first_par.menuLabels)
				par_def['menuNames'] = names
				if labels != names:
					par_def['menuLabels'] = labels

		# Read-only
		if first_par.readOnly:
			par_def['readOnly'] = True

		# Current values — only if different from default
		if len(group) == 1:
			val = self._getParValue(first_par)
			if val is not None:
				default_val = self._serializeValue(first_par.default)
				if self._valuesDiffer(val, default_val):
					par_def['value'] = val
		else:
			values = []
			has_non_default = False
			for i, gp in enumerate(group):
				v = self._getParValue(gp)
				values.append(v)
				if v is not None:
					d = self._serializeValue(gp.default)
					if self._valuesDiffer(v, d):
						has_non_default = True
			if has_non_default:
				par_def['values'] = values

		return par_def

	def _getParValue(self, p):
		"""Get current value/expr/bind for a parameter. Returns serialized form."""
		try:
			if p.mode == ParMode.EXPRESSION:
				return {'expr': p.expr}
			elif p.mode == ParMode.BIND:
				return {'bind': p.bindExpr}
			elif p.mode == ParMode.CONSTANT:
				return self._serializeValue(p.eval())
			return None
		except Exception as e:
			self._log(f'Error reading value for param {p.name}: {e}', 'DEBUG')
			return None

	def _exportFlags(self, target):
		"""Export flags that differ from defaults."""
		flags = {}
		for flag_name, default_val in DEFAULT_FLAGS.items():
			if flag_name == 'allowCooking' and not target.isCOMP:
				continue
			try:
				actual = getattr(target, flag_name)
				if actual != default_val:
					flags[flag_name] = actual
			except Exception as e:
				self._log(f'Error reading flag {flag_name} on {target.path}: {e}', 'DEBUG')
		return flags

	def _exportConnections(self, target):
		"""Export operator (left/right) input connections as sibling refs."""
		inputs = []
		for i, inp in enumerate(target.inputs):
			if inp is not None:
				# Use sibling name if same parent, otherwise full path
				if inp.parent() == target.parent():
					inputs.append({'index': i, 'source': inp.name})
				else:
					inputs.append({'index': i, 'source': inp.path})
		return inputs

	def _exportCompConnections(self, target):
		"""Export COMP (top/bottom) input connections."""
		inputs = []
		try:
			for i, connector in enumerate(target.inputCOMPConnectors):
				for conn in connector.connections:
					source = conn.owner
					if source.parent() == target.parent():
						inputs.append({'index': i, 'source': source.name})
					else:
						inputs.append({'index': i, 'source': source.path})
		except Exception as e:
			self._log(f'Error exporting COMP connections on {target.path}: {e}', 'DEBUG')
		return inputs

	def _exportDATContent(self, target):
		"""Export DAT text or table content."""
		try:
			if target.isTable:
				rows = []
				for r in range(target.numRows):
					row = []
					for c in range(target.numCols):
						row.append(target[r, c].val)
					rows.append(row)
				return {
					'dat_content': rows,
					'dat_content_format': 'table',
				}
			else:
				text = target.text
				if text:
					return {
						'dat_content': text,
						'dat_content_format': 'text',
					}
		except Exception as e:
			self._log(f'Error reading DAT content from {target.path}: {e}', 'DEBUG')
		return None

	# =========================================================================
	# IMPORT INTERNALS
	# =========================================================================

	def _createOps(self, parent, op_defs, created):
		"""Phase 1: Create all operators depth-first."""
		for op_def in op_defs:
			name = op_def.get('name')
			op_type = op_def.get('type')
			if not name or not op_type:
				continue

			try:
				new_op = parent.create(op_type, name)
				created.append(new_op.path)
			except Exception as e:
				self._log(
					f'Failed to create {op_type} "{name}": {e}', 'WARNING')
				continue

			# Recurse into children for COMPs
			children = op_def.get('children', [])
			if children and new_op.isCOMP:
				self._createOps(new_op, children, created)

	def _createCustomPars(self, parent, op_defs):
		"""Phase 2: Create custom parameters on all operators."""
		for op_def in op_defs:
			target = parent.op(op_def.get('name', ''))
			if not target:
				continue

			custom_pars = op_def.get('custom_pars', [])
			if custom_pars and target.isCOMP:
				self._createCustomParsOnOp(target, custom_pars)

			# Recurse
			children = op_def.get('children', [])
			if children:
				child_comp = parent.op(op_def['name'])
				if child_comp and child_comp.isCOMP:
					self._createCustomPars(child_comp, children)

	def _createCustomParsOnOp(self, target, custom_par_defs):
		"""Create custom parameters on a single operator."""
		pages = {}  # Cache pages by name

		for par_def in custom_par_defs:
			style = par_def.get('style', 'Float')
			par_name = par_def.get('name', '')
			label = par_def.get('label', par_name)
			page_name = par_def.get('page', 'Custom')

			# Get or create page
			if page_name not in pages:
				page = None
				for p in target.customPages:
					if p.name == page_name:
						page = p
						break
				if page is None:
					page = target.appendCustomPage(page_name)
				pages[page_name] = page

			page = pages[page_name]

			# Find append method
			method_name = STYLE_APPEND_MAP.get(style)
			if not method_name:
				self._log(
					f'Unknown par style "{style}" for {par_name}', 'WARNING')
				continue

			append_method = getattr(page, method_name, None)
			if not append_method:
				self._log(
					f'Method {method_name} not found on Page', 'WARNING')
				continue

			try:
				# Build kwargs for append
				kwargs = {'label': label, 'replace': True}

				# Size for Float/Int multi-component
				size = par_def.get('size')
				if size and style in ('Float', 'Int'):
					kwargs['size'] = size

				append_method(par_name, **kwargs)

				# Set properties on the created parameter(s)
				par = getattr(target.par, par_name, None)
				if not par:
					# Try with first suffix (e.g., Posx for XYZ)
					suffixes = STYLE_SUFFIXES.get(style, [])
					if suffixes:
						par = getattr(
							target.par, par_name + suffixes[0], None)
				if not par:
					continue

				# Numeric range
				if par_def.get('min') is not None and par.isNumber:
					par.min = par_def['min']
				if par_def.get('max') is not None and par.isNumber:
					par.max = par_def['max']
				if par_def.get('clampMin') is not None and par.isNumber:
					par.clampMin = par_def['clampMin']
				if par_def.get('clampMax') is not None and par.isNumber:
					par.clampMax = par_def['clampMax']
				if par_def.get('normMin') is not None and par.isNumber:
					par.normMin = par_def['normMin']
				if par_def.get('normMax') is not None and par.isNumber:
					par.normMax = par_def['normMax']

				# Default value
				if 'default' in par_def and not par.isPulse:
					try:
						par.default = par_def['default']
					except Exception as e:
						self._log(f'Could not set default for {par_name}: {e}', 'DEBUG')

				# Menu entries
				if par.isMenu:
					if 'menuSource' in par_def:
						par.menuSource = par_def['menuSource']
					elif 'menuNames' in par_def:
						par.menuNames = par_def['menuNames']
						# Labels default to names if omitted
						par.menuLabels = par_def.get(
							'menuLabels', par_def['menuNames'])

				# Section break
				if par_def.get('startSection'):
					par.startSection = True

				# Read-only
				if par_def.get('readOnly'):
					par.readOnly = True

			except Exception as e:
				self._log(
					f'Failed to create custom par "{par_name}": {e}',
					'WARNING')

	def _setParameters(self, parent, op_defs):
		"""Phase 3: Set parameter values on all operators."""
		for op_def in op_defs:
			target = parent.op(op_def.get('name', ''))
			if not target:
				continue

			# Built-in parameters
			for par_name, value in op_def.get('parameters', {}).items():
				self._setParValue(target, par_name, value)

			# Custom parameter values
			for par_def in op_def.get('custom_pars', []):
				par_name = par_def.get('name', '')
				style = par_def.get('style', '')

				# Single value
				if 'value' in par_def:
					value = par_def['value']
					if value is not None:
						self._setParValue(target, par_name, value)

				# Multi-component values
				if 'values' in par_def:
					suffixes = STYLE_SUFFIXES.get(style, [])
					values = par_def['values']
					if suffixes and len(values) == len(suffixes):
						for suffix, val in zip(suffixes, values):
							if val is not None:
								self._setParValue(
									target, par_name + suffix, val)
					elif style in ('Float', 'Int') and len(values) > 1:
						# Numeric multi-component: suffix is 1, 2, 3...
						for i, val in enumerate(values):
							if val is not None:
								self._setParValue(
									target, f'{par_name}{i+1}', val)

			# Recurse
			children = op_def.get('children', [])
			if children:
				child_comp = parent.op(op_def['name'])
				if child_comp and child_comp.isCOMP:
					self._setParameters(child_comp, children)

	def _setParValue(self, target, par_name, value):
		"""Set a single parameter value (constant, expression, or bind)."""
		par = getattr(target.par, par_name, None)
		if not par:
			return

		try:
			if isinstance(value, dict):
				if 'expr' in value:
					par.expr = value['expr']
					par.mode = ParMode.EXPRESSION
				elif 'bind' in value:
					par.bindExpr = value['bind']
					par.mode = ParMode.BIND
			else:
				par.val = value
		except Exception as e:
			self._log(
				f'Failed to set {par_name} on {target.path}: {e}', 'WARNING')

	def _setFlags(self, parent, op_defs):
		"""Phase 4: Set operator flags."""
		for op_def in op_defs:
			target = parent.op(op_def.get('name', ''))
			if not target:
				continue

			for flag_name, value in op_def.get('flags', {}).items():
				try:
					setattr(target, flag_name, value)
				except Exception as e:
					self._log(f'Failed to set flag {flag_name} on {target.path}: {e}', 'DEBUG')

			# Recurse
			children = op_def.get('children', [])
			if children:
				child_comp = parent.op(op_def['name'])
				if child_comp and child_comp.isCOMP:
					self._setFlags(child_comp, children)

	def _wireConnections(self, parent, op_defs):
		"""Phase 5: Wire all connections."""
		for op_def in op_defs:
			target = parent.op(op_def.get('name', ''))
			if not target:
				continue

			# Operator connections (left/right)
			for conn in op_def.get('inputs', []):
				source_ref = conn.get('source')
				dest_index = conn.get('index', 0)
				if not source_ref:
					continue

				# Resolve source (sibling name or full path)
				source = parent.op(source_ref)
				if not source:
					source = op(source_ref)  # Try full path
				if source:
					try:
						source.outputConnectors[0].connect(
							target.inputConnectors[dest_index])
					except Exception as e:
						self._log(
							f'Failed to connect {source_ref} -> '
							f'{target.name}[{dest_index}]: {e}', 'WARNING')

			# COMP connections (top/bottom)
			for conn in op_def.get('comp_inputs', []):
				source_ref = conn.get('source')
				dest_index = conn.get('index', 0)
				if not source_ref:
					continue

				source = parent.op(source_ref)
				if not source:
					source = op(source_ref)
				if source and hasattr(source, 'outputCOMPConnectors'):
					try:
						source.outputCOMPConnectors[0].connect(
							target.inputCOMPConnectors[dest_index])
					except Exception as e:
						self._log(
							f'Failed to connect COMP {source_ref} -> '
							f'{target.name}[{dest_index}]: {e}', 'WARNING')

			# Recurse
			children = op_def.get('children', [])
			if children:
				child_comp = parent.op(op_def['name'])
				if child_comp and child_comp.isCOMP:
					self._wireConnections(child_comp, children)

	def _setDATContent(self, parent, op_defs):
		"""Phase 6: Set DAT text/table content."""
		for op_def in op_defs:
			target = parent.op(op_def.get('name', ''))
			if not target:
				continue

			if 'dat_content' in op_def and target.family == 'DAT':
				try:
					fmt = op_def.get('dat_content_format', 'text')
					content = op_def['dat_content']
					if fmt == 'table':
						target.clear()
						for row in content:
							target.appendRow(row)
					else:
						target.text = content
				except Exception as e:
					self._log(
						f'Failed to set DAT content on {target.path}: {e}',
						'WARNING')

			# Recurse
			children = op_def.get('children', [])
			if children:
				child_comp = parent.op(op_def['name'])
				if child_comp and child_comp.isCOMP:
					self._setDATContent(child_comp, children)

	def _setPositions(self, parent, op_defs):
		"""Phase 7: Set positions (last, since creation can shift things)."""
		for op_def in op_defs:
			target = parent.op(op_def.get('name', ''))
			if not target:
				continue

			if 'position' in op_def:
				pos = op_def['position']
				target.nodeX = pos[0]
				target.nodeY = pos[1]

			if 'size' in op_def:
				size = op_def['size']
				target.nodeWidth = size[0]
				target.nodeHeight = size[1]

			if 'color' in op_def:
				target.color = tuple(op_def['color'])

			if 'comment' in op_def:
				target.comment = op_def['comment']

			# Recurse
			children = op_def.get('children', [])
			if children:
				child_comp = parent.op(op_def['name'])
				if child_comp and child_comp.isCOMP:
					self._setPositions(child_comp, children)

	# =========================================================================
	# ASYNC EXPORT HELPERS
	# =========================================================================

	def _collectAllPaths(self, parent_op, max_depth=None, depth=0):
		"""Recursively collect all exportable operator paths."""
		paths = []
		for child in parent_op.children:
			# Skip system/internal paths (exact match or children)
			if child.path in SYSTEM_PATHS or child.path.startswith(
					_SYSTEM_PATH_PREFIXES):
				continue
			paths.append(child.path)

			# Recurse into COMPs (but skip palette clone children)
			if hasattr(child, 'children'):
				if self._isPaletteClone(child):
					continue
				if max_depth is None or depth < max_depth:
					paths.extend(
						self._collectAllPaths(child, max_depth, depth + 1))

		return paths

	@staticmethod
	def _assembleHierarchy(flat_results, root_path):
		"""Reassemble flat export results into nested hierarchy.

		Takes a dict of {op_path: op_data} and rebuilds the parent-child
		tree structure based on path relationships.
		"""
		# Group ops by their parent path
		children_by_parent = {}
		for path, data in flat_results.items():
			parent_path = path.rsplit('/', 1)[0] or '/'
			if parent_path not in children_by_parent:
				children_by_parent[parent_path] = []
			children_by_parent[parent_path].append((path, data))

		# Recursively attach children
		def attach_children(op_path, op_data):
			child_entries = children_by_parent.get(op_path, [])
			if child_entries:
				op_data['children'] = [d for _, d in child_entries]
				for child_path, child_data in child_entries:
					attach_children(child_path, child_data)

		# Build root-level list
		root_entries = children_by_parent.get(root_path, [])
		operators = []
		for path, data in root_entries:
			attach_children(path, data)
			operators.append(data)

		return operators

	# =========================================================================
	# PER-COMP SPLIT
	# =========================================================================

	@staticmethod
	def _splitPerComp(operators, root_path, project_name, project_folder):
		"""Split a nested hierarchy into per-COMP .tdn files.

		Each COMP with children gets its own .tdn file. The parent's entry
		replaces 'children' with a 'tdn_ref' pointing to the child file.

		Args:
			operators: Assembled hierarchy (list of operator dicts)
			root_path: The export root path (e.g., '/')
			project_name: The .toe project name (for root file naming)
			project_folder: Absolute path to project directory

		Returns:
			dict of {abs_filepath: operators_list}
		"""
		from pathlib import Path

		base_dir = Path(project_folder)
		files = {}  # {abs_filepath: operators_list}

		def comp_rel_path(td_path):
			"""Convert a TD COMP path to a .tdn relative path."""
			if td_path == '/':
				return project_name + '.tdn'
			return td_path.lstrip('/') + '.tdn'

		def process(ops, current_td_path):
			"""Process operators, extracting COMP children into separate files."""
			processed = []
			for op_data in ops:
				if 'children' in op_data:
					# Build this COMP's TD path
					comp_td_path = current_td_path.rstrip('/') + '/' + op_data['name']
					child_rel = comp_rel_path(comp_td_path)

					# Recursively process the children
					child_ops = process(op_data['children'], comp_td_path)

					# Store child file
					child_abs = str(base_dir / child_rel)
					files[child_abs] = child_ops

					# Replace children with tdn_ref in parent
					op_copy = dict(op_data)
					del op_copy['children']
					op_copy['tdn_ref'] = child_rel
					processed.append(op_copy)
				else:
					processed.append(op_data)
			return processed

		# Process root level operators
		root_ops = process(operators, root_path)

		# Store root file
		root_rel = comp_rel_path(root_path)
		root_abs = str(base_dir / root_rel)
		files[root_abs] = root_ops

		return files

	# =========================================================================
	# STALE FILE CLEANUP
	# =========================================================================

	@staticmethod
	def _collectExistingTDNFiles(base_folder, root_path='/'):
		"""Collect existing .tdn files under base_folder for a given export root.

		For root='/': collects ALL .tdn files under base_folder.
		For sub-COMP root: only collects files matching that COMP's path prefix.

		Args:
			base_folder: Absolute path to the base directory to scan
			root_path: TD root path of the export (e.g., '/' or '/controller')

		Returns:
			Set of absolute file path strings for all matching .tdn files.
		"""
		from pathlib import Path
		base = Path(base_folder)
		if not base.is_dir():
			return set()

		all_tdn = {str(p) for p in base.rglob('*.tdn')}

		if root_path == '/':
			return all_tdn

		# Scope to files belonging to this root
		prefix = root_path.lstrip('/')
		scoped = set()
		for f in all_tdn:
			rel = str(Path(f).relative_to(base)).replace('\\', '/')
			stem = rel.removesuffix('.tdn')
			if stem == prefix or stem.startswith(prefix + '/'):
				scoped.add(f)
		return scoped

	@staticmethod
	def _cleanupStaleTDNFiles(before_files, written_files, base_folder):
		"""Delete .tdn files that existed before export but weren't written.

		Safety:
		- Only deletes files with .tdn extension
		- Only deletes files under base_folder
		- Uses Path.rmdir() for empty directory cleanup (fails on non-empty)

		Args:
			before_files: Set of absolute .tdn file paths from before export
			written_files: List of absolute .tdn file paths just written
			base_folder: Absolute path to base directory (safety boundary)

		Returns:
			List of deleted file paths.
		"""
		from pathlib import Path

		base_root = Path(base_folder).resolve()
		written_set = {str(Path(f).resolve()) for f in written_files}
		deleted = []

		for fpath_str in before_files:
			fpath = Path(fpath_str).resolve()

			# Safety: only delete .tdn files
			if fpath.suffix.lower() != '.tdn':
				continue

			# Safety: only delete files under base_folder
			try:
				fpath.relative_to(base_root)
			except ValueError:
				continue

			# Skip files that were just written
			if str(fpath) in written_set:
				continue

			# Delete the stale file
			try:
				if fpath.is_file():
					fpath.unlink()
					deleted.append(fpath_str)
			except Exception:
				pass

		# Clean up empty directories (bottom-up)
		dirs_to_check = set()
		for d in deleted:
			parent = Path(d).parent
			while parent.resolve() != base_root and parent != parent.parent:
				dirs_to_check.add(parent)
				parent = parent.parent

		for d in sorted(dirs_to_check,
						key=lambda p: len(p.parts), reverse=True):
			try:
				if d.is_dir():
					d.rmdir()  # Only succeeds if empty
			except OSError:
				pass

		return deleted

	# =========================================================================
	# HELPERS
	# =========================================================================

	@staticmethod
	def _isPaletteClone(target):
		"""Check if a COMP is a palette clone (cloned from /sys/)."""
		if not target.isCOMP:
			return False
		clone_par = getattr(target.par, 'clone', None)
		if not clone_par:
			return False
		try:
			# Check evaluated value (operator path)
			clone_op = clone_par.eval()
			if clone_op and hasattr(clone_op, 'path'):
				if clone_op.path.startswith('/sys/'):
					return True
			# Check expression for /sys/ references
			if clone_par.mode == ParMode.EXPRESSION:
				expr = clone_par.expr
				if 'TDTox' in expr or 'TDResources' in expr:
					return True
		except Exception as e:
			self._log(f'Error checking palette clone status for {target.path}: {e}', 'DEBUG')
		return False

	def _serializeValue(self, val):
		"""Convert a parameter value to a JSON-safe type."""
		if val is None:
			return ''
		if isinstance(val, bool):
			return val
		if isinstance(val, int):
			return val
		if isinstance(val, float):
			# Round to avoid floating point noise
			rounded = round(val, 10)
			# Convert to int if it's a whole number
			if rounded == int(rounded) and abs(rounded) < 2**53:
				return int(rounded)
			return rounded
		if isinstance(val, str):
			return val
		if isinstance(val, (list, tuple)):
			return [self._serializeValue(v) for v in val]
		return str(val)

	def _valuesDiffer(self, current, default):
		"""Compare parameter values, handling float precision and None."""
		# OP-reference params: None (no op connected) == '' (empty default)
		if current is None and default == '':
			return False
		if current == '' and default is None:
			return False
		if isinstance(current, float) and isinstance(default, (float, int)):
			return abs(current - float(default)) > 1e-9
		return current != default

	def _colorsDiffer(self, c1, c2):
		"""Check if two RGB tuples differ beyond tolerance."""
		if len(c1) != len(c2):
			return True
		return any(abs(a - b) > COLOR_TOLERANCE for a, b in zip(c1, c2))

	def _getGroupBaseName(self, first_par, group):
		"""Determine the base name of a parameter group."""
		if len(group) == 1:
			return first_par.name

		style = first_par.style
		suffixes = STYLE_SUFFIXES.get(style)

		if suffixes and len(group) == len(suffixes):
			# Strip the known suffix (e.g., 'x' from 'Posx')
			suffix = suffixes[0]
			name = first_par.name
			if name.endswith(suffix):
				return name[:-len(suffix)]

		# Float/Int with size > 1: suffix is '1', '2', etc.
		name = first_par.name
		if name.endswith('1'):
			return name[:-1]

		return first_par.name

	def _getClaudiusVersion(self):
		"""Get the Claudius version string."""
		try:
			return self.ownerComp.ext.Claudius.CLAUDIUS_VERSION
		except Exception as e:
			self._log(f'Could not get Claudius version from ext: {e}', 'DEBUG')
			try:
				# Fallback: check the module-level constant
				import importlib
				return '1.0.0'
			except Exception as e2:
				self._log(f'Claudius version fallback failed: {e2}', 'DEBUG')
				return 'unknown'

	def _resolveOutputPath(self, output_file, root_op):
		"""Resolve the output file path, saving into the externalizations folder."""
		from pathlib import Path

		if output_file == 'auto':
			project_dir = Path(project.folder)
			# Use .toe project name when exporting from root
			if root_op.path == '/':
				safe_name = project.name.removesuffix('.toe')
			else:
				safe_name = root_op.name.replace('/', '_')

			# Use the Embody externalizations folder if configured
			try:
				ext_folder = self.ownerComp.ext.Embody.ExternalizationsFolder
				if ext_folder:
					out_dir = project_dir / ext_folder
					out_dir.mkdir(parents=True, exist_ok=True)
					return str(out_dir / f'{safe_name}.tdn')
			except Exception as e:
				self._log(f'Could not resolve externalizations folder: {e}', 'WARNING')

			# Fallback to project directory
			return str(project_dir / f'{safe_name}.tdn')

		return str(output_file)

	def _trackTDNExport(self, root_path, file_path):
		"""Add/update a TDN entry in the externalizations table."""
		try:
			embody_ext = self.ownerComp.ext.Embody
			table = embody_ext.Externalizations
			if not table:
				return

			from pathlib import Path
			rel_path = embody_ext.normalizePath(
				str(Path(file_path).relative_to(project.folder)))
			timestamp = datetime.now(timezone.utc).strftime(
				'%Y-%m-%d %H:%M:%S UTC')

			# Update existing row if found
			for i in range(1, table.numRows):
				if (table[i, 'path'].val == root_path
						and table[i, 'type'].val == 'tdn'):
					table[i, 'rel_file_path'] = rel_path
					table[i, 'timestamp'] = timestamp
					return

			# Add new row
			table.appendRow(
				[root_path, 'tdn', rel_path, timestamp, '', '', ''])
		except Exception as e:
			self._log(f'Failed to track TDN export: {e}', 'WARNING')

	def _log(self, message, level='INFO'):
		"""Log via Embody's centralized logger."""
		try:
			embody_ext = self.ownerComp.ext.Embody
			if hasattr(embody_ext, 'Log'):
				embody_ext.Log(message, level, _depth=2)
				return
		except Exception:
			pass  # Fallback below handles this — avoid recursion in logger
		# Fallback if Embody ext unavailable
		print(f'[TDN][{level}] {message}')
