"""
TDN — TouchDesigner Network open format (.tdn)

Exports and imports TouchDesigner networks as human-readable JSON files.
Only non-default properties are stored, keeping the output minimal.

This extension lives on the Embody COMP and is callable via:
  - MCP tools (export_network / import_network) through Envoy
  - TD UI (keyboard shortcut Ctrl+Shift+N, pulse parameters)
  - Direct Python: op.Embody.ext.TDN.ExportNetwork(...)
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Optional, Union

TDN_VERSION = '1.1'

# Parameters to always skip (Embody-managed or internal)
SKIP_PARAMS = {
	'externaltox', 'enableexternaltox', 'reloadtox',
	'reinitextensions', 'savebackup',
	'savecustom', 'reloadcustom',
	'pageindex',  # UI state (visible parameter page tab), not config
}

# Embody-managed About page parameters — excluded from TDN export
# because they are reconstructed from externalizations.tsv at import time.
_EMBODY_ABOUT_PARS = {'Build', 'Date', 'Touchbuild'}

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

# Storage keys to skip during TDN export (runtime/transient state)
SKIP_STORAGE_KEYS = {
	'_tdn_stripped_paths', '_git_root',
	'envoy_running', 'envoy_shutdown_event',
	'expanded_paths', 'manage_file_path', 'visible_count', 'hover',
}
_SYSTEM_PATH_PREFIXES = tuple(p + '/' for p in SYSTEM_PATHS)


class TDNExt:
	"""Extension for exporting/importing TouchDesigner networks as .tdn JSON."""

	def __init__(self, ownerComp: 'COMP') -> None:
		self.ownerComp: 'COMP' = ownerComp
		self._export_state: Optional[dict[str, Any]] = None
		# Per-OPType caches for export performance.
		# Built-in parameter defaults and exportable names are stable per type,
		# so we cache them to avoid repeated Python-to-C++ bridge calls.
		self._defaults_cache: dict[str, dict[str, Any]] = {}
		self._exportable_cache: dict[str, set[str]] = {}

	# =========================================================================
	# CRASH SAFETY — atomic writes, backup rotation, validation
	# =========================================================================

	@staticmethod
	def _get_backup_path(tdn_path: str, project_folder: str,
						 suffix: str = '.bak') -> Path:
		"""Compute backup path for a .tdn file.

		Mirrors the relative directory structure under
		{project_folder}/.tdn_backup/.

		Example:
			tdn_path:       /proj/embody/Foo/bar.tdn
			project_folder: /proj
			result:         /proj/.tdn_backup/embody/Foo/bar.tdn.bak
		"""
		tdn = Path(tdn_path)
		proj = Path(project_folder)
		try:
			rel = tdn.relative_to(proj)
		except ValueError:
			# tdn_path not under project_folder — fall back to flat name
			rel = Path(tdn.name)
		backup_dir = proj / '.tdn_backup'
		return backup_dir / (str(rel) + suffix)

	@staticmethod
	def _rotate_backups(tdn_path: str, project_folder: str) -> None:
		"""Rotate backup copies before overwriting a .tdn file.

		Keeps 2 generations: .bak (previous) and .bak2 (one before that).
		Uses shutil.copy2 (not rename) so the original stays in place
		until the atomic write replaces it.

		No-op if tdn_path does not yet exist on disk (first export).
		"""
		src = Path(tdn_path)
		if not src.is_file():
			return

		bak = TDNExt._get_backup_path(tdn_path, project_folder, '.bak')
		bak2 = TDNExt._get_backup_path(tdn_path, project_folder, '.bak2')

		# Rotate: .bak -> .bak2
		if bak.is_file():
			bak2.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(str(bak), str(bak2))

		# Copy current -> .bak
		bak.parent.mkdir(parents=True, exist_ok=True)
		shutil.copy2(str(src), str(bak))

	@staticmethod
	def _atomic_write(filepath: str, content: str) -> None:
		"""Write content to filepath atomically using temp-file-then-rename.

		Guarantees that `filepath` always contains either the complete old
		content or the complete new content — never a partial write.

		The temp file is created in the same directory as filepath to
		ensure os.replace() is atomic (same filesystem).
		"""
		target = Path(filepath)
		target.parent.mkdir(parents=True, exist_ok=True)
		tmp_fd = None
		tmp_path = None
		try:
			tmp_fd, tmp_path = tempfile.mkstemp(
				dir=str(target.parent), suffix='.tdn.tmp')
			with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
				tmp_fd = None  # os.fdopen takes ownership of the fd
				f.write(content)
				f.flush()
				os.fsync(f.fileno())
			os.replace(tmp_path, filepath)
			tmp_path = None  # Rename succeeded — no cleanup needed
		finally:
			if tmp_fd is not None:
				os.close(tmp_fd)
			if tmp_path is not None:
				try:
					os.unlink(tmp_path)
				except OSError:
					pass

	@staticmethod
	def _validate_tdn_file(filepath: str) -> dict:
		"""Read back a .tdn file and verify it's valid.

		Returns {'valid': True} or {'valid': False, 'error': '...'}.
		"""
		try:
			text = Path(filepath).read_text(encoding='utf-8')
		except Exception as e:
			return {'valid': False, 'error': f'Read failed: {e}'}
		if not text:
			return {'valid': False, 'error': 'File is empty'}
		try:
			doc = json.loads(text)
		except json.JSONDecodeError as e:
			return {'valid': False, 'error': f'Invalid JSON: {e}'}
		if not isinstance(doc, dict):
			return {'valid': False, 'error': 'Root is not a JSON object'}
		if doc.get('format') != 'tdn':
			return {'valid': False,
					'error': f'Missing or wrong format key: {doc.get("format")}'}
		if 'operators' not in doc:
			return {'valid': False, 'error': 'Missing operators key'}
		return {'valid': True}

	@staticmethod
	def _safe_write_tdn(tdn_path: str, content: str,
						project_folder: str) -> dict:
		"""Write a .tdn file with full crash safety.

		1. Rotate backups (.bak, .bak2)
		2. Atomic write (temp file + rename + fsync)
		3. Post-write validation (read back + JSON parse)
		4. If validation fails, restore from .bak

		Returns {'success': True} or {'error': '...'}.
		"""
		# Step 1: Backup rotation (only if file already exists)
		try:
			TDNExt._rotate_backups(tdn_path, project_folder)
		except Exception:
			# Backup failure should not block the write — log but continue.
			# The write itself is still atomic.
			pass

		# Step 2: Atomic write
		try:
			TDNExt._atomic_write(tdn_path, content)
		except Exception as e:
			return {'error': f'Atomic write failed: {e}'}

		# Step 3: Post-write validation
		validation = TDNExt._validate_tdn_file(tdn_path)
		if validation.get('valid'):
			return {'success': True}

		# Step 4: Validation failed — attempt restore from backup
		error_msg = validation.get('error', 'unknown')
		bak = TDNExt._get_backup_path(tdn_path, project_folder, '.bak')
		if bak.is_file():
			try:
				shutil.copy2(str(bak), tdn_path)
				return {'error': f'Validation failed ({error_msg}), '
								 f'restored from backup'}
			except Exception as restore_e:
				return {'error': f'Validation failed ({error_msg}) '
								 f'and backup restore failed: {restore_e}'}
		return {'error': f'Validation failed ({error_msg}), no backup available'}

	def _get_backup_path_instance(self, tdn_path: str,
								  suffix: str = '.bak') -> Path:
		"""Instance wrapper for _get_backup_path using project.folder."""
		return TDNExt._get_backup_path(tdn_path, str(project.folder), suffix)

	# =========================================================================
	# CONTENT COMPARISON
	# =========================================================================

	_TDN_VOLATILE_KEYS = frozenset({
		'build', 'generator', 'td_build', 'exported_at',
	})

	@staticmethod
	def _tdn_content_equal(new_tdn: dict, existing_tdn: dict) -> bool:
		"""Compare two TDN dicts ignoring volatile header metadata.

		Returns True if all non-volatile keys (operators, parameters,
		connections, annotations, custom_pars, options, etc.) are identical.
		"""
		for key in new_tdn:
			if key in TDNExt._TDN_VOLATILE_KEYS:
				continue
			if new_tdn[key] != existing_tdn.get(key):
				return False
		for key in existing_tdn:
			if key in TDNExt._TDN_VOLATILE_KEYS:
				continue
			if key not in new_tdn:
				return False
		return True

	@staticmethod
	def _read_existing_tdn(file_path: str) -> Optional[dict]:
		"""Read and parse an existing .tdn file from disk.

		Returns the parsed dict, or None if the file is missing, corrupt,
		or unreadable.
		"""
		try:
			p = Path(file_path)
			if not p.is_file():
				return None
			return json.loads(p.read_text(encoding='utf-8'))
		except Exception:
			return None

	# =========================================================================
	# PROMOTED METHODS (uppercase — callable directly on op.Embody)
	# =========================================================================

	def ExportNetwork(self, root_path: str = '/', include_dat_content: Optional[bool] = None,
					  output_file: Optional[str] = None, max_depth: Optional[int] = None,
					  cleanup_protected: Optional[list[str]] = None,
					  embed_all: bool = False) -> dict[str, Any]:
		"""
		Export a TouchDesigner network to .tdn JSON format.

		Args:
			root_path: COMP path to export from (default '/')
			include_dat_content: Include text/table content of DATs
			output_file: File path to write JSON to. 'auto' generates a name.
						 None returns the dict without writing to disk.
			max_depth: Maximum recursion depth (None = unlimited)
			cleanup_protected: List of absolute .tdn file paths that must NOT
				be deleted by stale-file cleanup. Used by SaveTDN to protect
				.tdn files belonging to other independently-tracked TDN COMPs.
			embed_all: If True, recurse into TDN-tagged COMPs instead of
				skipping their children. Produces a self-contained export.

		Returns:
			dict with 'success' and 'tdn' keys, or 'error' key on failure
		"""
		root_op = op(root_path)
		if not root_op:
			return {'error': f'Operator not found: {root_path}'}
		if not hasattr(root_op, 'children'):
			return {'error': f'{root_path} is not a COMP'}

		# Resolve from per-COMP storage, falling back to global toggle
		if include_dat_content is None:
			per_comp = root_op.fetch('embed_dats_in_tdn', None, search=False)
			if per_comp is not None:
				include_dat_content = per_comp
			else:
				include_dat_content = self.ownerComp.par.Embeddatsintdns.eval()

		options = {
			'include_dat_content': include_dat_content,
			'max_depth': max_depth,
			'embed_all': embed_all,
		}

		try:
			self._defaults_cache.clear()
			self._exportable_cache.clear()
			operators = self._exportChildren(root_op, options, depth=0)

			# Post-processing optimizations
			type_defaults = TDNExt._compute_type_defaults(operators)
			if type_defaults:
				TDNExt._strip_type_defaults(operators, type_defaults)
			par_templates, operators = TDNExt._extract_par_templates(operators)

			build_num = self._getBuildNumber(root_op)
			tdn = {
				'format': 'tdn',
				'version': TDN_VERSION,
				'build': build_num,
				'generator': f'Embody/{self._getEmbodyVersion()}',
				'td_build': f'{app.version}.{app.build}',
				'exported_at': datetime.now(timezone.utc).strftime(
					'%Y-%m-%dT%H:%M:%SZ'),
				'network_path': root_path,
				'options': {
					'include_dat_content': include_dat_content,
				},
			}
			if type_defaults:
				tdn['type_defaults'] = type_defaults
			if par_templates:
				tdn['par_templates'] = par_templates
			# Target COMP's own type (v1.1+)
			tdn['type'] = root_op.OPType

			# Target COMP's own parameters (custom + non-default built-in)
			root_custom_pars = self._exportCustomPars(root_op)
			if root_custom_pars:
				tdn['custom_pars'] = root_custom_pars
			root_builtin_params = self._exportBuiltinParams(root_op)
			if root_builtin_params:
				tdn['parameters'] = root_builtin_params

			# Target COMP's own metadata (v1.1+)
			root_flags = self._exportFlags(root_op)
			if root_flags:
				tdn['flags'] = root_flags
			root_color = tuple(root_op.color)
			if self._colorsDiffer(root_color, DEFAULT_COLOR):
				tdn['color'] = [round(c, 4) for c in root_color]
			root_tags = list(root_op.tags)
			if root_tags:
				tdn['tags'] = root_tags
			if root_op.comment:
				tdn['comment'] = root_op.comment
			root_storage = self._exportStorage(root_op)
			if root_storage:
				tdn['storage'] = root_storage

			tdn['operators'] = operators

			# Root-level annotations
			annotations = self._exportAnnotations(root_op)
			if annotations:
				tdn['annotations'] = annotations

			result = {'success': True, 'tdn': tdn}

			# Write to file if requested
			if output_file:
				# Scan from project folder — TDN paths mirror TD hierarchy
				scan_folder = str(project.folder)
				before_tdn = TDNExt._collectExistingTDNFiles(
					scan_folder, root_path)

				filepath = self._resolveOutputPath(output_file, root_op)
				content = TDNExt._compact_json_dumps(tdn)
				write_result = TDNExt._safe_write_tdn(
					filepath, content, scan_folder)
				if not write_result.get('success'):
					return {'error':
						f'Safe write failed: {write_result.get("error")}'}

				protected = [filepath]
				if cleanup_protected:
					protected.extend(cleanup_protected)
				stale = TDNExt._cleanupStaleTDNFiles(
					before_tdn, protected, scan_folder)
				if stale:
					self._log(
						f'Cleaned up {len(stale)} stale .tdn file(s)',
						'INFO')

				result['file'] = filepath
				self._trackTDNExport(root_path, filepath,
					build_num=build_num,
					touch_build=f'{app.version}.{app.build}')
				self._log(
					f'Exported network to {filepath}', 'SUCCESS')

				# Warn about locked non-DAT operators whose frozen
				# content won't survive a TDN round-trip
				self._warnLockedNonDATs(root_op, context='export')

			return result

		except Exception as e:
			self._log(f'Export failed: {e}', 'ERROR')
			return {'error': f'Export failed: {e}'}

	def ExportNetworkAsync(self, root_path: str = '/', include_dat_content: Optional[bool] = None,
						   output_file: Optional[str] = None, max_depth: Optional[int] = None,
						   embed_all: bool = False) -> None:
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
			embed_all: If True, recurse into TDN-tagged COMPs instead of
				skipping their children. Produces a self-contained export.
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

		# Clear per-OPType caches for fresh export
		self._defaults_cache.clear()
		self._exportable_cache.clear()

		# Phase 1: Collect all operator paths (fast tree walk, single frame)
		op_paths = self._collectAllPaths(root_op, max_depth, embed_all=embed_all)

		# Resolve output path now (needs TD access)
		resolved_path = None
		if output_file:
			resolved_path = self._resolveOutputPath(output_file, root_op)

		# Collect metadata now (needs TD access)
		metadata = {
			'generator': f'Embody/{self._getEmbodyVersion()}',
			'td_build': f'{app.version}.{app.build}',
			'build': self._getBuildNumber(root_op),
			'project_name': project.name.removesuffix('.toe'),
			'project_folder': str(project.folder),
			'ext_folder': self.ownerComp.ext.Embody.ExternalizationsFolder,
		}

		# Resolve from per-COMP storage, falling back to global toggle
		if include_dat_content is None:
			per_comp = root_op.fetch('embed_dats_in_tdn', None, search=False)
			if per_comp is not None:
				include_dat_content = per_comp
			else:
				include_dat_content = self.ownerComp.par.Embeddatsintdns.eval()

		done_event = Event()

		# Pre-collect existing .tdn files on the main thread.
		# rglob/scandir suffers extreme GIL contention when called from a
		# background thread (~30s vs ~70ms), so we do it here.
		before_tdn = set()
		protected_files = []
		if resolved_path:
			proj_folder = metadata['project_folder']
			before_tdn = TDNExt._collectExistingTDNFiles(
				proj_folder, root_path)
			# Protect .tdn files belonging to other tracked TDN COMPs
			# so the stale-file cleanup doesn't delete them.
			protected_files = list(
				self.ownerComp.ext.Embody._getAllTrackedTDNFiles(
					exclude_path=root_path))

		self._export_state = {
			'paths': op_paths,
			'index': 0,
			'batch_size': 200,
			'results': {},
			'options': {
				'include_dat_content': include_dat_content,
				'max_depth': max_depth,
				'embed_all': embed_all,
			},
			'root_path': root_path,
			'output_file': resolved_path,
			'metadata': metadata,
			'before_tdn': before_tdn,
			'protected_files': protected_files,
			'done_event': done_event,
			'done': False,
			'error': None,
			'result': None,
		}

		# Capture state ref for worker closure
		state = self._export_state

		def worker():
			"""Worker thread: wait for batches, then assemble and write file.

			File scanning (rglob) is done on the main thread before this
			starts — scandir suffers extreme GIL contention from bg threads.
			"""
			done_event.wait(timeout=300)  # 5 minute safety timeout

			if not done_event.is_set():
				state['error'] = 'Export timed out (5 minutes)'
				raise RuntimeError(state['error'])

			if state['error']:
				raise RuntimeError(state['error'])

			# Assemble hierarchy from flat results (pure Python, no TD)
			operators = TDNExt._assembleHierarchy(
				state['results'], state['root_path'])

			# Attach annotations to assembled hierarchy (pure Python dicts)
			ann_results = state.get('annotation_results', {})
			TDNExt._attachAnnotations(
				operators, state['root_path'], ann_results)

			# Post-processing optimizations
			type_defaults = TDNExt._compute_type_defaults(operators)
			if type_defaults:
				TDNExt._strip_type_defaults(operators, type_defaults)
			par_templates, operators = TDNExt._extract_par_templates(operators)

			tdn = {
				'format': 'tdn',
				'version': TDN_VERSION,
				'build': state['metadata'].get('build'),
				'generator': state['metadata']['generator'],
				'td_build': state['metadata']['td_build'],
				'exported_at': datetime.now(timezone.utc).strftime(
					'%Y-%m-%dT%H:%M:%SZ'),
				'network_path': state['root_path'],
				'options': {
					'include_dat_content':
						state['options']['include_dat_content'],
				},
			}
			if type_defaults:
				tdn['type_defaults'] = type_defaults
			if par_templates:
				tdn['par_templates'] = par_templates

			# Target COMP's own metadata (captured on main thread)
			root_meta = state.get('root_meta', {})
			if root_meta.get('type'):
				tdn['type'] = root_meta['type']
			if root_meta.get('custom_pars'):
				tdn['custom_pars'] = root_meta['custom_pars']
			if root_meta.get('parameters'):
				tdn['parameters'] = root_meta['parameters']
			if root_meta.get('flags'):
				tdn['flags'] = root_meta['flags']
			if root_meta.get('color'):
				tdn['color'] = root_meta['color']
			if root_meta.get('tags'):
				tdn['tags'] = root_meta['tags']
			if root_meta.get('comment'):
				tdn['comment'] = root_meta['comment']
			if root_meta.get('storage'):
				tdn['storage'] = root_meta['storage']

			tdn['operators'] = operators

			# Root-level annotations
			root_anns = ann_results.get(state['root_path'])
			if root_anns:
				tdn['annotations'] = root_anns

			# Count total operators
			def count_ops(ops):
				n = len(ops)
				for o in ops:
					n += count_ops(o.get('children', []))
				return n

			op_count = count_ops(operators)

			# Write to file (file I/O is fine in worker thread)
			if state['output_file']:
				# Use pre-collected .tdn files (collected on main thread
				# to avoid GIL contention with rglob/scandir)
				before_tdn = state.get('before_tdn', set())
				base_folder = state['metadata']['project_folder']

				content = TDNExt._compact_json_dumps(tdn)
				write_result = TDNExt._safe_write_tdn(
					state['output_file'], content, base_folder)
				if not write_result.get('success'):
					state['result'] = {
						'error': f'Safe write failed: '
								 f'{write_result.get("error")}'}
					return

				protected = [state['output_file']] + state.get(
					'protected_files', [])
				stale = TDNExt._cleanupStaleTDNFiles(
					before_tdn, protected,
					base_folder)

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
		thread = thread_manager.EnqueueTask(task, standalone=True)
		if thread is None:
			self._log(
				'Thread Manager at capacity — export queued but may be '
				'delayed. Try restarting Envoy to free stale threads.',
				'WARNING')

		self._log(
			f'Exporting {len(op_paths)} operators from {root_path}...',
			'INFO')

	def ExportProjectTDNInteractive(self):
		"""Export project TDN with a dialog if TDN-tagged COMPs exist.

		Shows a ui.messageBox letting the user choose between a full
		(self-contained) export or a modular export that skips children
		of TDN-managed COMPs. If no TDN-tagged COMPs exist, exports
		everything directly without prompting.
		"""
		tdn_tag = self.ownerComp.par.Tdntag.val
		tdn_comps = root.findChildren(tags=[tdn_tag])
		# Exclude Embody + descendants, non-COMPs, and system paths
		embody_path = self.ownerComp.path + '/'
		tdn_comps = [c for c in tdn_comps
					 if c.isCOMP
					 and not c.path.startswith(embody_path)
					 and c != self.ownerComp
					 and c.path not in SYSTEM_PATHS
					 and not c.path.startswith(_SYSTEM_PATH_PREFIXES)]

		if not tdn_comps:
			self.ExportNetworkAsync(output_file='auto', embed_all=True)
			return

		choice = ui.messageBox(
			'Embody \u2014 Export Project TDN',
			f'This project has {len(tdn_comps)} COMP(s) with their own '
			f'.tdn files.\n\n'
			'  Full: Self-contained file with all COMPs embedded.\n'
			'  Modular: Skip children of TDN-managed COMPs.\n',
			buttons=['Cancel', 'Full', 'Modular'])

		if choice not in (1, 2):
			return
		self.ExportNetworkAsync(
			output_file='auto', embed_all=(choice == 1))

	def _onExportRefresh(self):
		"""RefreshHook: Process a batch of operators per frame (main thread)."""
		state = self._export_state
		if state is None or state['done']:
			return

		try:
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
				# Collect annotations on main thread before signaling worker
				ann_results = {}
				root_op = op(state['root_path'])
				if root_op:
					root_anns = self._exportAnnotations(root_op)
					if root_anns:
						ann_results[state['root_path']] = root_anns
					# Capture target COMP's own metadata (main thread only)
					root_meta = {'type': root_op.OPType}
					root_custom_pars = self._exportCustomPars(root_op)
					if root_custom_pars:
						root_meta['custom_pars'] = root_custom_pars
					root_builtin = self._exportBuiltinParams(root_op)
					if root_builtin:
						root_meta['parameters'] = root_builtin
					root_flags = self._exportFlags(root_op)
					if root_flags:
						root_meta['flags'] = root_flags
					root_color = tuple(root_op.color)
					if self._colorsDiffer(root_color, DEFAULT_COLOR):
						root_meta['color'] = [
							round(c, 4) for c in root_color]
					root_tags = list(root_op.tags)
					if root_tags:
						root_meta['tags'] = root_tags
					if root_op.comment:
						root_meta['comment'] = root_op.comment
					root_storage = self._exportStorage(root_op)
					if root_storage:
						root_meta['storage'] = root_storage
					state['root_meta'] = root_meta
				for path, data in state['results'].items():
					target_op = op(path)
					if target_op and target_op.isCOMP:
						comp_anns = self._exportAnnotations(target_op)
						if comp_anns:
							ann_results[path] = comp_anns
				state['annotation_results'] = ann_results
				state['done'] = True
				state['done_event'].set()
		except Exception as e:
			self._log(f'Export batch failed: {e}', 'ERROR')
			state['error'] = str(e)
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
				self._trackTDNExport(state['root_path'], result['file'],
					build_num=state['metadata'].get('build'),
					touch_build=state['metadata'].get('td_build'))
			self._log(msg, 'SUCCESS')
			if result.get('cleaned_up'):
				self._log(
					f"Cleaned up {result['cleaned_up']} stale .tdn file(s)",
					'INFO')

		self._export_state = None
		self._refreshList()

		# Chain next re-export if queue active
		if getattr(self, '_reexport_queue', None):
			run("args[0]._processNextReexport()", self, delayFrames=1)

	def _onExportError(self, e):
		"""ExceptHook: Log error (main thread)."""
		self._log(f'Export failed: {e}', 'ERROR')
		self._export_state = None
		self._reexport_queue = None
		self._refreshList()

	def _refreshList(self):
		"""Recook the list data source and reset the list COMP."""
		inject = self.ownerComp.op('list/inject_parents')
		lister = self.ownerComp.op('list/list1')
		if inject:
			inject.cook(force=True)
		if lister:
			lister.reset()

	def ReexportAllTDNs(self) -> None:
		"""Re-export all tracked TDN files with current toggle setting."""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table:
				self._log('No TDN exports to update', 'INFO')
				return

			tdn_entries = []
			headers = [table[0, c].val for c in range(table.numCols)]
			has_strategy = 'strategy' in headers
			for i in range(1, table.numRows):
				is_tdn = False
				if has_strategy:
					is_tdn = table[i, 'strategy'].val == 'tdn'
				else:
					is_tdn = table[i, 'type'].val == 'tdn'
				if is_tdn:
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

	def ImportNetwork(self, target_path: str, tdn: Union[dict[str, Any], list[dict[str, Any]]],
					  clear_first: bool = False, restore_file_links: bool = False) -> dict[str, Any]:
		"""
		Import a .tdn network into a COMP, recreating all operators.

		Args:
			target_path: Destination COMP path to import into
			tdn: The .tdn dict (full document or just the 'operators' list)
			clear_first: Delete all existing children before importing
			restore_file_links: Re-establish file/syncfile parameters on DATs
				that are tracked in the externalizations table (used during
				TDN reconstruction on project open)

		Returns:
			dict with 'success', 'created_count', 'created_paths' or 'error'
		"""
		dest = op(target_path)
		if not dest:
			msg = f'Destination not found: {target_path}'
			ui.status = f'TDN Import: {msg}'
			return {'error': msg}
		if not hasattr(dest, 'create'):
			msg = f'{target_path} is not a COMP'
			ui.status = f'TDN Import: {msg}'
			return {'error': msg}

		# Accept full .tdn document or just the operators array
		if isinstance(tdn, dict) and 'operators' in tdn:
			# Version compatibility checks
			tdn_version = tdn.get('version', '1.0')
			if tdn_version != TDN_VERSION:
				self._log(
					f'TDN version mismatch: file is v{tdn_version}, '
					f'current is v{TDN_VERSION}', 'WARNING')

			source_td = tdn.get('td_build', '')
			current_td = f'{app.version}.{app.build}'
			if source_td and source_td != current_td:
				self._log(
					f'TDN exported from TD {source_td} '
					f'(current: {current_td})', 'INFO')

			build_num = tdn.get('build')
			if build_num is not None:
				self._log(f'Importing TDN build {build_num} into {target_path}', 'DEBUG')

			op_defs = tdn['operators']
		elif isinstance(tdn, list):
			op_defs = tdn
		else:
			ui.status = 'TDN Import: Invalid .tdn format'
			return {'error': 'Invalid .tdn format'}

		if clear_first:
			# Clear dock relationships before destroying — TD's engine
			# raises an uncatchable tdError if a dock target is destroyed
			# before its docked operator.
			for child in list(dest.children):
				try:
					if child.dock is not None:
						child.dock = None
				except Exception:
					pass
			for child in list(dest.children):
				try:
					child.destroy()
				except Exception as e:
					self._log(f'Failed to destroy {child.path}: {e}', 'WARNING')
			# Also destroy utility operators (annotations) which .children skips
			try:
				for u_op in dest.findChildren(depth=1, includeUtility=True):
					if u_op.type == 'annotate':
						try:
							u_op.destroy()
						except Exception as e:
							self._log(f'Failed to destroy annotation {u_op.path}: {e}', 'WARNING')
			except Exception:
				pass

		# Pre-phase: Resolve templates and merge type defaults
		if isinstance(tdn, dict):
			par_templates = tdn.get('par_templates', {})
			type_defaults = tdn.get('type_defaults', {})
			if par_templates:
				TDNExt._resolve_par_templates(op_defs, par_templates)
			if type_defaults:
				TDNExt._merge_type_defaults(op_defs, type_defaults)

		try:
			created = []

			# Snapshot pre-existing children so Phase 1 can distinguish
			# them from auto-created companions during merge imports.
			pre_existing = (
				set() if clear_first
				else {c.name for c in dest.children})

			# Phase 1: Create all operators (depth-first)
			self._createOps(dest, op_defs, created, pre_existing)

			# Phase 2: Create custom parameters
			self._createCustomPars(dest, op_defs)

			# Phase 3: Set parameter values
			self._setParameters(dest, op_defs)

			# Phase 4: Set flags
			self._setFlags(dest, op_defs)

			# Phase 4a: Warn about locked non-DAT operators
			self._warnLockedNonDATs(dest, context='import')

			# Phase 5: Wire connections
			self._wireConnections(dest, op_defs)

			# Phase 6: Set DAT content
			self._setDATContent(dest, op_defs)

			# Phase 6a: Restore storage
			self._restoreStorage(dest, op_defs)

			# Phase 7: Set positions (last)
			self._setPositions(dest, op_defs)

			# Phase 7b: Set docking relationships
			self._setDocking(dest, op_defs)

			# Phase 7a: Create annotations
			ann_created = []
			if isinstance(tdn, dict):
				top_anns = tdn.get('annotations', [])
				if top_anns:
					self._createAnnotationsFromList(
						dest, top_anns, ann_created)
			self._importNestedAnnotations(dest, op_defs, ann_created)
			created.extend(ann_created)

			# Phase 8: Restore file links on externalized DATs
			restored_count = 0
			if restore_file_links:
				restored_count = self._restoreFileLinks(dest)

			# Cleanup temporary operator references from Phase 1
			def _cleanupRefs(defs):
				for d in defs:
					d.pop('_created_op', None)
					children = d.get('children', [])
					if children:
						_cleanupRefs(children)
			_cleanupRefs(op_defs)

			# Phase 9: Apply target COMP's own properties from TDN.
			# Runs AFTER child creation so extension reinit (triggered by
			# recreating extension source DATs) has already happened —
			# this overwrites any defaults the extension set.
			if isinstance(tdn, dict):
				# Type validation (v1.1+) — warn if destination type differs
				tdn_type = tdn.get('type')
				if tdn_type and dest.OPType != tdn_type:
					self._log(
						f'Type mismatch: TDN expects {tdn_type} but '
						f'destination is {dest.OPType}', 'WARNING')

				# Custom parameters
				tdn_custom = tdn.get('custom_pars', {})
				if tdn_custom:
					flat_defs = self._flattenCustomPars(tdn_custom)
					self._createCustomParsOnOp(dest, flat_defs)
					self._setCustomParValues(dest, flat_defs)

				# Built-in parameters
				tdn_params = tdn.get('parameters', {})
				for par_name, value in tdn_params.items():
					self._setParValue(dest, par_name, value)

				# Flags (v1.1+)
				tdn_flags = tdn.get('flags', [])
				if tdn_flags:
					if isinstance(tdn_flags, list):
						for entry in tdn_flags:
							try:
								if entry.startswith('-'):
									setattr(dest, entry[1:], False)
								else:
									setattr(dest, entry, True)
							except Exception as e:
								self._log(
									f'Failed to set flag {entry} on '
									f'{dest.path}: {e}', 'DEBUG')
					elif isinstance(tdn_flags, dict):
						for flag_name, value in tdn_flags.items():
							try:
								setattr(dest, flag_name, value)
							except Exception as e:
								self._log(
									f'Failed to set flag {flag_name} on '
									f'{dest.path}: {e}', 'DEBUG')

				# Color (v1.1+)
				tdn_color = tdn.get('color')
				if tdn_color:
					try:
						dest.color = tdn_color
					except Exception as e:
						self._log(
							f'Failed to set color on {dest.path}: {e}',
							'DEBUG')

				# Tags (v1.1+)
				tdn_tags = tdn.get('tags')
				if tdn_tags:
					for tag in tdn_tags:
						dest.tags.add(tag)

				# Comment (v1.1+)
				tdn_comment = tdn.get('comment')
				if tdn_comment is not None:
					dest.comment = tdn_comment

				# Storage (v1.1+)
				tdn_storage = tdn.get('storage', {})
				for key, value in tdn_storage.items():
					try:
						deserialized = self._deserializeStorageValue(value)
						dest.store(key, deserialized)
					except Exception as e:
						self._log(
							f'Failed to restore storage key "{key}" '
							f'on {dest.path}: {e}', 'WARNING')

			self._log(
				f'Imported {len(created)} operators into {target_path}',
				'SUCCESS')
			result = {
				'success': True,
				'destination': target_path,
				'created_count': len(created),
				'created_paths': created,
			}
			if restored_count:
				result['restored_file_links'] = restored_count
			return result

		except Exception as e:
			self._log(f'Import failed: {e}', 'ERROR')
			ui.status = f'TDN Import failed: {e}'
			return {'error': f'Import failed: {e}'}

	def ImportNetworkFromFile(self, file_path: str, target_path: str = '/',
							   clear_first: bool = False) -> dict[str, Any]:
		"""
		Load a .tdn JSON file from disk and import it into a COMP.

		Args:
			file_path: Path to the .tdn file on disk
			target_path: Destination COMP path (default '/')
			clear_first: Delete all existing children before importing
		"""
		if not file_path:
			self._log('No TDN file specified', 'WARNING')
			ui.status = 'TDN Import: No file specified'
			return {'error': 'No TDN file specified'}

		import os
		if not os.path.isfile(file_path):
			self._log(f'TDN file not found: {file_path}', 'ERROR')
			ui.status = f'TDN Import: File not found — {file_path}'
			return {'error': f'TDN file not found: {file_path}'}

		try:
			with open(file_path, 'r', encoding='utf-8') as f:
				tdn_data = json.load(f)
		except json.JSONDecodeError as e:
			self._log(f'Invalid JSON in TDN file: {e}', 'ERROR')
			ui.status = f'TDN Import: Invalid JSON — {e}'
			return {'error': f'Invalid JSON in TDN file: {e}'}
		except Exception as e:
			self._log(f'Failed to read TDN file: {e}', 'ERROR')
			ui.status = f'TDN Import: {e}'
			return {'error': f'Failed to read TDN file: {e}'}

		self._log(f'Importing from {file_path} into {target_path}...', 'INFO')
		return self.ImportNetwork(target_path, tdn_data, clear_first=clear_first)

	# =========================================================================
	# EXPORT INTERNALS
	# =========================================================================

	def _exportChildren(self, parent_op, options, depth):
		"""Recursively export children of a COMP."""
		max_depth = options.get('max_depth')
		if max_depth is not None and depth > max_depth:
			return []

		children = list(parent_op.children)

		# Detect accumulated companion duplicates (e.g. timer1_callbacks1,
		# timer1_callbacks2) left over from previous import cycles.
		# Signal: name minus trailing digits yields a sibling with the same
		# OPType, and both are docked to the same target operator.
		sibling_map = {c.name: c for c in children}
		skip = set()
		for child in children:
			name = child.name
			base = name.rstrip('0123456789')
			if base == name or base not in sibling_map:
				continue
			sibling = sibling_map[base]
			if sibling.OPType != child.OPType:
				continue
			if (child.dock is not None and sibling.dock is not None
					and child.dock.path == sibling.dock.path):
				skip.add(name)
				self._log(
					f'Skipping duplicate companion "{name}" '
					f'(original: "{base}")', 'INFO')

		# Keys that carry no user-meaningful data — operators with only
		# these keys are auto-created defaults (e.g. torus1 inside a
		# geoCOMP) that TD recreates automatically on COMP creation.
		_TRIVIAL_KEYS = {'name', 'type', 'position', 'size'}

		result = []
		for child in children:
			# Skip system/internal paths (exact match or children)
			if child.path in SYSTEM_PATHS or child.path.startswith(
					_SYSTEM_PATH_PREFIXES):
				continue

			if child.name in skip:
				continue

			op_data = self._exportSingleOp(child, options, depth)
			if op_data is not None:
				# Skip bare auto-created defaults — TD recreates these
				# when the parent COMP is created, so they're noise
				if not (set(op_data.keys()) - _TRIVIAL_KEYS):
					self._log(
						f'Skipping default child "{child.name}" '
						f'(no customizations)', 'DEBUG')
					continue
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

		# Position (omit if at origin [0, 0])
		if target.nodeX != 0 or target.nodeY != 0:
			data['position'] = [target.nodeX, target.nodeY]

		# Size (only if non-default)
		if (target.nodeWidth, target.nodeHeight) != DEFAULT_NODE_SIZE:
			data['size'] = [target.nodeWidth, target.nodeHeight]

		# Color (only if non-default)
		color = tuple(target.color)
		if self._colorsDiffer(color, DEFAULT_COLOR):
			data['color'] = [round(c, 4) for c in color]

		# Comment
		if target.comment:
			data['comment'] = target.comment

		# Tags
		tags = list(target.tags)
		if tags:
			data['tags'] = tags

		# Docking (omit when not docked)
		if target.dock is not None:
			dock_op = target.dock
			if dock_op.parent() == target.parent():
				data['dock'] = dock_op.name
			else:
				data['dock'] = dock_op.path

		# Storage (all serializable entries, skipping transient/internal keys)
		storage = self._exportStorage(target)
		if storage:
			data['storage'] = storage

		# Operator connections (left/right wires)
		connections = self._exportConnections(target)
		if connections:
			data['inputs'] = connections

		# COMP connections (top/bottom wires)
		if hasattr(target, 'inputCOMPConnectors'):
			comp_conns = self._exportCompConnections(target)
			if comp_conns:
				data['comp_inputs'] = comp_conns

		# DAT content — include when the include_dat_content option is True.
		if target.family == 'DAT' and options.get('include_dat_content', True):
			content_data = self._exportDATContent(target)
			if content_data:
				data.update(content_data)

		# Recurse into COMP children (sync mode only)
		# Skip children of palette clones — they come from /sys/ and
		# don't need to be stored (TD recreates them from the clone source)
		# Skip children of COMPs with their own TDN tag — those are
		# managed by their own .tdn file to avoid redundant nesting
		if recurse and hasattr(target, 'children'):
			if self._isPaletteClone(target):
				data['palette_clone'] = True
			elif self._hasTDNTag(target) and not options.get('embed_all'):
				pass  # Child's network managed by its own .tdn file
			else:
				max_depth = options.get('max_depth')
				if max_depth is None or depth < max_depth:
					children = self._exportChildren(
						target, options, depth + 1)
					if children:
						data['children'] = children
					comp_annotations = self._exportAnnotations(target)
					if comp_annotations:
						data['annotations'] = comp_annotations

		return data

	def _buildParCache(self, target):
		"""Build per-OPType cache of exportable parameter names and defaults.

		On the first operator of each OPType, we iterate all parameters and
		record which are exportable (non-custom, non-readOnly except `file`, non-skip) and
		their default values. Subsequent operators of the same type skip all
		those per-parameter attribute checks (isCustom, readOnly, style) and
		default lookups — replacing ~4 Python-to-C++ bridge calls per parameter
		with a single Python dict lookup.
		"""
		op_type = target.OPType
		exportable = {}
		defaults = {}
		for p in target.pars():
			if p.isCustom:
				continue
			if p.readOnly and p.name != 'file':
				continue
			if p.name in SKIP_PARAMS:
				continue
			if p.style in SKIP_BUILTIN_STYLES:
				continue
			exportable[p.name] = True
			defaults[p.name] = p.default
		self._exportable_cache[op_type] = exportable
		self._defaults_cache[op_type] = defaults

	def _exportBuiltinParams(self, target):
		"""Export non-default built-in parameter values.

		Uses per-OPType caching to avoid redundant cross-bridge calls for
		isCustom, readOnly, style, and default on every parameter. For 412
		operators with ~100 params each, this eliminates ~160,000 bridge calls.
		"""
		op_type = target.OPType
		if op_type not in self._exportable_cache:
			self._buildParCache(target)

		exportable = self._exportable_cache[op_type]
		defaults = self._defaults_cache[op_type]
		params = {}

		for p in target.pars():
			name = p.name
			if name not in exportable:
				continue

			try:
				mode = p.mode
				if mode == ParMode.EXPRESSION:
					params[name] = '=' + p.expr
				elif mode == ParMode.BIND:
					params[name] = '~' + p.bindExpr
				elif mode == ParMode.CONSTANT:
					current = p.val
					default = defaults.get(name)
					if self._valuesDiffer(current, default):
						params[name] = self._serializeValue(current)
				# Skip EXPORT mode (set by the exporter op, not importable)
			except Exception as e:
				self._log(f'Error reading param {name} on {target.path}: {e}', 'DEBUG')

		return params

	def _exportCustomPars(self, target):
		"""Export ALL custom parameters grouped by page.

		Returns a dict keyed by page name, where each value is a list of
		parameter definitions (without the 'page' field — the key IS the page).
		"""
		if not hasattr(target, 'customPages'):
			return {}

		pages_dict = {}
		seen_names = set()

		for page in target.customPages:
			page_pars = []
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

				# Export the group as a single definition (without page)
				par_def = self._exportCustomParGroup(page, group)
				if par_def:
					page_pars.append(par_def)

			if page_pars:
				pages_dict[page.name] = page_pars

		# Filter Embody-managed About pages (metadata lives in externalizations.tsv)
		if 'About' in pages_dict:
			about_par_names = {d.get('name') for d in pages_dict['About']}
			if about_par_names <= _EMBODY_ABOUT_PARS:
				del pages_dict['About']

		return pages_dict

	# Standard defaults TD assigns to newly created custom parameters
	_STANDARD_DEFAULTS = {0, 0.0, '', False}

	def _exportCustomParGroup(self, page, group):
		"""Export a custom parameter group (tuplet) definition."""
		first_par = group[0]
		style = first_par.style
		base_name = self._getGroupBaseName(first_par, group)

		par_def = {
			'name': base_name,
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

		# Help text
		if first_par.help:
			par_def['help'] = first_par.help

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
				return '=' + p.expr
			elif p.mode == ParMode.BIND:
				return '~' + p.bindExpr
			elif p.mode == ParMode.CONSTANT:
				return self._serializeValue(p.eval())
			return None
		except Exception as e:
			self._log(f'Error reading value for param {p.name}: {e}', 'DEBUG')
			return None

	def _exportFlags(self, target):
		"""Export flags that differ from defaults as a string array.

		Flags with a default of False are listed by name when True.
		Flags with a default of True are listed with a '-' prefix when False.
		Example: ['viewer', 'display'] or ['-expose']
		"""
		flags = []
		for flag_name, default_val in DEFAULT_FLAGS.items():
			if flag_name == 'allowCooking' and not target.isCOMP:
				continue
			try:
				actual = getattr(target, flag_name)
				if actual != default_val:
					if default_val:
						# True-default flag set to False: use '-' prefix
						flags.append('-' + flag_name)
					else:
						flags.append(flag_name)
			except Exception as e:
				self._log(f'Error reading flag {flag_name} on {target.path}: {e}', 'DEBUG')
		return flags

	def _exportStorage(self, target):
		"""Export serializable storage entries from an operator.

		Skips keys in SKIP_STORAGE_KEYS and values that cannot be
		serialized to JSON. Returns dict or empty dict.
		"""
		try:
			raw_storage = target.storage
		except Exception:
			return {}

		if not raw_storage:
			return {}

		result = {}
		for key, value in raw_storage.items():
			if key in SKIP_STORAGE_KEYS:
				continue
			try:
				serialized = self._serializeStorageValue(value)
				result[key] = serialized
			except (TypeError, ValueError, RecursionError) as e:
				self._log(
					f'Skipping non-serializable storage key '
					f'"{key}" on {target.path}: {type(value).__name__} - {e}',
					'DEBUG')
		return result

	def _serializeStorageValue(self, value):
		"""Convert a storage value to a JSON-safe representation.

		Primitive types (str, int, float, bool, None) are stored directly.
		Collections (list, dict) are recursed. Non-JSON types (tuple, set,
		bytes) use a $type/$value wrapper. Unserializable types raise TypeError.
		"""
		if value is None:
			return None
		if isinstance(value, bool):
			return value
		if isinstance(value, int):
			return value
		if isinstance(value, float):
			rounded = round(value, 10)
			if rounded == int(rounded) and abs(rounded) < 2**53:
				return int(rounded)
			return rounded
		if isinstance(value, str):
			return value
		if isinstance(value, list):
			return [self._serializeStorageValue(v) for v in value]
		if isinstance(value, dict):
			result = {}
			for k, v in value.items():
				if not isinstance(k, str):
					raise TypeError(f'Non-string dict key: {type(k).__name__}')
				result[k] = self._serializeStorageValue(v)
			return result
		if isinstance(value, tuple):
			return {
				'$type': 'tuple',
				'$value': [self._serializeStorageValue(v) for v in value]
			}
		if isinstance(value, set):
			items = sorted(value, key=lambda x: (type(x).__name__, x))
			return {
				'$type': 'set',
				'$value': [self._serializeStorageValue(v) for v in items]
			}
		if isinstance(value, bytes):
			import base64
			return {
				'$type': 'bytes',
				'$value': base64.b64encode(value).decode('ascii')
			}
		raise TypeError(f'Cannot serialize {type(value).__name__}')

	def _deserializeStorageValue(self, value):
		"""Convert a JSON storage value back to a Python object.

		Handles $type/$value wrappers for tuple, set, and bytes.
		"""
		if value is None:
			return None
		if isinstance(value, bool):
			return value
		if isinstance(value, (int, float)):
			return value
		if isinstance(value, str):
			return value
		if isinstance(value, list):
			return [self._deserializeStorageValue(v) for v in value]
		if isinstance(value, dict):
			if '$type' in value and '$value' in value and len(value) == 2:
				type_name = value['$type']
				raw = value['$value']
				if type_name == 'tuple':
					return tuple(
						self._deserializeStorageValue(v) for v in raw)
				elif type_name == 'set':
					return set(
						self._deserializeStorageValue(v) for v in raw)
				elif type_name == 'bytes':
					import base64
					return base64.b64decode(raw)
				else:
					self._log(
						f'Unknown $type "{type_name}" in storage, '
						f'treating as plain dict', 'WARNING')
			return {
				k: self._deserializeStorageValue(v)
				for k, v in value.items()
			}
		return value

	def _exportAnnotations(self, parent_op):
		"""Export annotations (comment, networkbox, annotate) from a COMP.

		Returns a list of annotation dicts. Only non-default properties
		are included to keep .tdn files compact.
		"""
		try:
			annotations = parent_op.findChildren(
				type=annotateCOMP, depth=1, includeUtility=True)
		except Exception:
			return []

		if not annotations:
			return []

		result = []
		for ann in sorted(annotations, key=lambda a: a.name):
			data = {'name': ann.name}

			mode = ann.par.Mode.eval()
			data['mode'] = mode

			title = ann.par.Titletext.eval()
			if title:
				data['title'] = title

			body = ann.par.Bodytext.eval()
			if body:
				data['text'] = body

			if ann.nodeX != 0 or ann.nodeY != 0:
				data['position'] = [ann.nodeX, ann.nodeY]

			data['size'] = [ann.nodeWidth, ann.nodeHeight]

			color = (
				ann.par.Backcolorr.eval(),
				ann.par.Backcolorg.eval(),
				ann.par.Backcolorb.eval(),
			)
			if self._colorsDiffer(color, DEFAULT_COLOR):
				data['color'] = [round(c, 4) for c in color]

			opacity = ann.par.Opacity.eval()
			if abs(opacity - 1.0) > 1e-6:
				data['opacity'] = round(opacity, 4)

			alpha = ann.par.Backcoloralpha.eval()
			if abs(alpha - 1.0) > 1e-6:
				data['backAlpha'] = round(alpha, 4)

			titleHeight = ann.par.Titleheight.eval()
			if abs(titleHeight - 30) > 1e-6:
				data['titleHeight'] = titleHeight

			bodyFontSize = ann.par.Bodyfontsize.eval()
			if abs(bodyFontSize - 10) > 1e-6:
				data['bodyFontSize'] = bodyFontSize

			result.append(data)

		return result

	def _exportConnections(self, target):
		"""Export operator (left/right) input connections as a string array.

		Array position = input index. Entries are source operator names
		(sibling) or full paths (cross-network). Null entries for gaps.
		Example: ['noise1'] or ['noise1', null, 'level1']
		"""
		inputs = []
		max_index = -1
		conn_map = {}
		for i, inp in enumerate(target.inputs):
			if inp is not None:
				# Use sibling name if same parent, otherwise full path
				if inp.parent() == target.parent():
					conn_map[i] = inp.name
				else:
					conn_map[i] = inp.path
				max_index = i

		if max_index < 0:
			return []

		# Build array with nulls for gaps
		for i in range(max_index + 1):
			inputs.append(conn_map.get(i))

		return inputs

	def _exportCompConnections(self, target):
		"""Export COMP (top/bottom) input connections as a string array."""
		inputs = []
		max_index = -1
		conn_map = {}
		try:
			for i, connector in enumerate(target.inputCOMPConnectors):
				for conn in connector.connections:
					source = conn.owner
					if source.parent() == target.parent():
						conn_map[i] = source.name
					else:
						conn_map[i] = source.path
					max_index = i
		except Exception as e:
			self._log(f'Error exporting COMP connections on {target.path}: {e}', 'DEBUG')

		if max_index < 0:
			return []

		for i in range(max_index + 1):
			inputs.append(conn_map.get(i))

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

	def _resolveOp(self, parent, op_def):
		"""Get the actual created operator for an op_def.

		Uses the stored reference from Phase 1 if available (handles
		auto-renamed operators correctly), falls back to name lookup.
		"""
		created = op_def.get('_created_op')
		if created and created.valid:
			return created
		return parent.op(op_def.get('name', ''))

	def _createOps(self, parent, op_defs, created, pre_existing=None):
		"""Phase 1: Create all operators depth-first.

		Stores a reference to each created operator in op_def['_created_op']
		so that Phases 2-7 can resolve the correct operator even when TD
		auto-renamed it due to name conflicts.

		Auto-created companion DATs (e.g. timerCHOP callbacks, rampTOP keys)
		are reused rather than duplicated — if an operator with the target
		name already exists in the parent AND was NOT present before import
		started, it was auto-created by a sibling's create() earlier in
		this same import pass.

		Args:
			pre_existing: Set of operator names that existed in the parent
				before import started. Operators in this set are NOT reused
				(a new op is created, possibly auto-renamed by TD).
		"""
		if pre_existing is None:
			pre_existing = set()

		for op_def in op_defs:
			name = op_def.get('name')
			op_type = op_def.get('type')
			if not name or not op_type:
				continue

			# Reuse auto-created companions (e.g. timerCHOP callbacks,
			# rampTOP keys) — but only if the op was NOT present before
			# import started (i.e. it was auto-created by a sibling's
			# create() earlier in this same import pass).
			existing = parent.op(name)
			if existing is not None and name not in pre_existing:
				created.append(existing.path)
				op_def['_created_op'] = existing
				self._log(
					f'Reusing existing operator "{name}"', 'INFO')
				children = op_def.get('children', [])
				if children and existing.isCOMP:
					self._createOps(existing, children, created)
				continue

			try:
				new_op = parent.create(op_type, name)
				created.append(new_op.path)
				op_def['_created_op'] = new_op
				if new_op.name != name:
					self._log(
						f'Operator "{name}" renamed to "{new_op.name}" '
						f'(name conflict)', 'WARNING')
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
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			custom_pars = op_def.get('custom_pars', {})
			if custom_pars and target.isCOMP:
				# Normalize to flat list with page info
				flat_defs = self._flattenCustomPars(custom_pars)
				self._createCustomParsOnOp(target, flat_defs)

			# Recurse
			children = op_def.get('children', [])
			if children and target.isCOMP:
				self._createCustomPars(target, children)

	@staticmethod
	def _flattenCustomPars(custom_pars):
		"""Normalize custom_pars to a flat list with 'page' on each def.

		Accepts:
		  - Dict keyed by page name (v1.0 format): {'About': [...], 'Controls': [...]}
		  - Legacy flat array with 'page' on each def: [{'name': ..., 'page': ...}]
		"""
		if isinstance(custom_pars, list):
			return custom_pars
		if isinstance(custom_pars, dict):
			flat = []
			for page_name, page_defs in custom_pars.items():
				if isinstance(page_defs, list):
					for par_def in page_defs:
						d = dict(par_def)
						d['page'] = page_name
						flat.append(d)
			return flat
		return []

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

			# Multi-component styles: strip first suffix from par name
			# (e.g., 'Tintr' → 'Tint' for RGB, 'Posx' → 'Pos' for XYZ)
			actual_par_name = par_name
			suffixes = STYLE_SUFFIXES.get(style, [])
			if suffixes:
				first_suffix = suffixes[0]
				if par_name.endswith(first_suffix):
					actual_par_name = par_name[:-len(first_suffix)]

				# TD reports 'RGBA' for both RGB (3) and RGBA (4),
				# 'XYZW' for both XYZ (3) and XYZW (4). Infer from
				# the values array length.
				values_count = len(par_def.get('values', []))
				if style == 'RGBA' and values_count <= 3:
					method_name = 'appendRGB'
				elif style == 'XYZW':
					if values_count <= 2:
						method_name = 'appendXY'
					elif values_count <= 3:
						method_name = 'appendXYZ'

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

				append_method(actual_par_name, **kwargs)

				# Set properties on the created parameter(s)
				par = getattr(target.par, par_name, None)
				if par is None:
					# Try with first suffix (e.g., Posx for XYZ)
					suffixes = STYLE_SUFFIXES.get(style, [])
					if suffixes:
						par = getattr(
							target.par, par_name + suffixes[0], None)
				if par is None:
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

				# Help text
				if par_def.get('help'):
					par.help = par_def['help']

			except Exception as e:
				self._log(
					f'Failed to create custom par "{par_name}": {e}',
					'WARNING')

	def _setParameters(self, parent, op_defs):
		"""Phase 3: Set parameter values on all operators."""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			# Built-in parameters
			for par_name, value in op_def.get('parameters', {}).items():
				self._setParValue(target, par_name, value)

			# Custom parameter values
			flat_defs = TDNExt._flattenCustomPars(
				op_def.get('custom_pars', {}))
			self._setCustomParValues(target, flat_defs)

			# Recurse
			children = op_def.get('children', [])
			if children and target.isCOMP:
				self._setParameters(target, children)

	def _setCustomParValues(self, target, flat_defs):
		"""Set custom parameter values on a single operator from flat defs.

		Handles single values, multi-component values (RGB, XYZ, etc.),
		and expression/bind modes via _setParValue shorthand.
		"""
		for par_def in flat_defs:
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

				if suffixes:
					# Strip first suffix from par_name to get base
					# (e.g., 'Tintr' → 'Tint' for RGBA)
					base_name = par_name
					first_suffix = suffixes[0]
					if par_name.endswith(first_suffix):
						base_name = par_name[:-len(first_suffix)]

					# TD reports 'RGBA' for both RGB and RGBA;
					# use values count to pick correct suffixes
					actual_suffixes = suffixes[:len(values)]

					for suffix, val in zip(actual_suffixes, values):
						if val is not None:
							self._setParValue(
								target, base_name + suffix, val)
				elif style in ('Float', 'Int') and len(values) > 1:
					# Numeric multi-component: suffix is 1, 2, 3...
					for i, val in enumerate(values):
						if val is not None:
							self._setParValue(
								target, f'{par_name}{i+1}', val)

	def _setParValue(self, target, par_name, value):
		"""Set a single parameter value (constant, expression, or bind).

		Expression shorthand: strings starting with '=' are expressions,
		strings starting with '~' are bind expressions. Use '==' or '~~'
		to escape a literal leading '=' or '~'.
		"""
		par = getattr(target.par, par_name, None)
		if par is None:
			return

		try:
			if isinstance(value, str):
				if value.startswith('='):
					if value.startswith('=='):
						# Escaped literal '='
						par.val = value[1:]
					else:
						par.expr = value[1:]
						par.mode = ParMode.EXPRESSION
				elif value.startswith('~'):
					if value.startswith('~~'):
						# Escaped literal '~'
						par.val = value[1:]
					else:
						par.bindExpr = value[1:]
						par.mode = ParMode.BIND
				else:
					par.val = value
			elif isinstance(value, dict):
				# Legacy v1.0 format support
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
		"""Phase 4: Set operator flags.

		Accepts array format: ['viewer', '-expose'] where '-' prefix means False.
		Also accepts legacy dict format: {'viewer': true} for compatibility.
		"""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			flags_data = op_def.get('flags', [])
			if isinstance(flags_data, list):
				for entry in flags_data:
					try:
						if entry.startswith('-'):
							setattr(target, entry[1:], False)
						else:
							setattr(target, entry, True)
					except Exception as e:
						self._log(f'Failed to set flag {entry} on {target.path}: {e}', 'DEBUG')
			elif isinstance(flags_data, dict):
				# Legacy dict format
				for flag_name, value in flags_data.items():
					try:
						setattr(target, flag_name, value)
					except Exception as e:
						self._log(f'Failed to set flag {flag_name} on {target.path}: {e}', 'DEBUG')

			# Recurse
			children = op_def.get('children', [])
			if children and target.isCOMP:
				self._setFlags(target, children)

	def _wireConnections(self, parent, op_defs):
		"""Phase 5: Wire all connections.

		Accepts string array format: ['noise1', null, 'level1'] where
		position = input index. Also accepts legacy dict format for compat.
		"""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			# Operator connections (left/right)
			self._wireConnectionList(
				parent, target, op_def.get('inputs', []), comp=False)

			# COMP connections (top/bottom)
			self._wireConnectionList(
				parent, target, op_def.get('comp_inputs', []), comp=True)

			# Recurse
			children = op_def.get('children', [])
			if children and target.isCOMP:
				self._wireConnections(target, children)

	def _wireConnectionList(self, parent, target, conn_list, comp=False):
		"""Wire a list of connections (operator or COMP level).

		conn_list can be:
		  - String array: ['source1', null, 'source2'] (position = index)
		  - Legacy dict array: [{'index': 0, 'source': 'name'}]
		"""
		for i, entry in enumerate(conn_list):
			# Determine source_ref and dest_index
			if entry is None:
				continue
			if isinstance(entry, str):
				source_ref = entry
				dest_index = i
			elif isinstance(entry, dict):
				# Legacy format
				source_ref = entry.get('source')
				dest_index = entry.get('index', 0)
				if not source_ref:
					continue
			else:
				continue

			# Resolve source (sibling name or full path)
			source = parent.op(source_ref)
			if not source:
				source = op(source_ref)  # Try full path

			if not source:
				self._log(
					f'Connection source not found: {source_ref} -> '
					f'{target.name}[{dest_index}]', 'WARNING')
				continue

			try:
				if comp:
					if hasattr(source, 'outputCOMPConnectors'):
						source.outputCOMPConnectors[0].connect(
							target.inputCOMPConnectors[dest_index])
				else:
					source.outputConnectors[0].connect(
						target.inputConnectors[dest_index])
			except Exception as e:
				kind = 'COMP ' if comp else ''
				self._log(
					f'Failed to connect {kind}{source_ref} -> '
					f'{target.name}[{dest_index}]: {e}', 'WARNING')

	def _setDATContent(self, parent, op_defs):
		"""Phase 6: Set DAT text/table content."""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
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
			if children and target.isCOMP:
				self._setDATContent(target, children)

	def _restoreStorage(self, parent, op_defs):
		"""Phase 6a: Restore operator storage from TDN data."""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			storage = op_def.get('storage', {})
			if storage:
				for key, value in storage.items():
					try:
						deserialized = self._deserializeStorageValue(value)
						target.store(key, deserialized)
					except Exception as e:
						self._log(
							f'Failed to restore storage key "{key}" '
							f'on {target.path}: {e}', 'WARNING')

			startup_storage = op_def.get('startup_storage', {})
			if startup_storage:
				for key, value in startup_storage.items():
					try:
						deserialized = self._deserializeStorageValue(value)
						target.storeStartupValue(key, deserialized)
					except Exception as e:
						self._log(
							f'Failed to restore startup storage key "{key}" '
							f'on {target.path}: {e}', 'WARNING')

			# Recurse
			children = op_def.get('children', [])
			if children and target.isCOMP:
				self._restoreStorage(target, children)

	def _setPositions(self, parent, op_defs):
		"""Phase 7: Set positions (last, since creation can shift things)."""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			pos = op_def.get('position', [0, 0])
			try:
				target.nodeX = pos[0]
				target.nodeY = pos[1]
			except Exception as e:
				self._log(f'Failed to set position on {target.path}: {e}', 'DEBUG')

			if 'size' in op_def:
				try:
					size = op_def['size']
					target.nodeWidth = size[0]
					target.nodeHeight = size[1]
				except Exception as e:
					self._log(f'Failed to set size on {target.path}: {e}', 'DEBUG')

			if 'color' in op_def:
				try:
					target.color = tuple(op_def['color'])
				except Exception as e:
					self._log(f'Failed to set color on {target.path}: {e}', 'DEBUG')

			if 'comment' in op_def:
				try:
					target.comment = op_def['comment']
				except Exception as e:
					self._log(f'Failed to set comment on {target.path}: {e}', 'DEBUG')

			if 'tags' in op_def:
				try:
					for tag in op_def['tags']:
						target.tags.add(tag)
				except Exception as e:
					self._log(f'Failed to set tags on {target.path}: {e}', 'DEBUG')

			# Recurse
			children = op_def.get('children', [])
			if children and target.isCOMP:
				self._setPositions(target, children)

	def _setDocking(self, parent, op_defs):
		"""Phase 7b: Restore docking relationships."""
		for op_def in op_defs:
			dock_ref = op_def.get('dock')
			children = op_def.get('children', [])

			if not dock_ref:
				# No dock on this op — still recurse into children
				if children:
					target = self._resolveOp(parent, op_def)
					if target and target.isCOMP:
						self._setDocking(target, children)
				continue

			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			# Resolve dock target: sibling name first, then full path
			dock_target = parent.op(dock_ref)
			if not dock_target:
				dock_target = op(dock_ref)

			if dock_target:
				try:
					target.dock = dock_target
				except Exception as e:
					self._log(
						f'Failed to dock {target.path} to {dock_ref}: {e}',
						'WARNING')
			else:
				self._log(
					f'Dock target not found: {dock_ref} for {target.path}',
					'WARNING')

			# Recurse into children
			if children and target.isCOMP:
				self._setDocking(target, children)

	def _createAnnotationsFromList(self, parent, annotations_data, created):
		"""Phase 7a: Create annotations in a COMP from an annotations array.

		Args:
			parent: The COMP to create annotations in
			annotations_data: List of annotation dicts from the .tdn
			created: List to append created annotation paths to
		"""
		for ann_def in annotations_data:
			try:
				name = ann_def.get('name')

				# Reuse existing annotateCOMP if one with this name already
				# exists (e.g., palette clone from the operators array).
				ann = None
				if name:
					existing = parent.op(name)
					if existing and existing.type == 'annotate':
						ann = existing

				if ann is None:
					ann = parent.create('annotateCOMP')
					ann.utility = True  # Match TD UI behavior
					if name:
						try:
							ann.name = name
						except Exception:
							pass  # annotateCOMPs can't always be renamed
				else:
					ann.utility = True

				mode = ann_def.get('mode', 'annotate')
				ann.par.Mode = mode

				title = ann_def.get('title', '')
				if title:
					ann.par.Titletext = title

				text = ann_def.get('text', '')
				if text:
					ann.par.Bodytext = text

				pos = ann_def.get('position', [0, 0])
				ann.nodeX = pos[0]
				ann.nodeY = pos[1]

				size = ann_def.get('size')
				if size:
					ann.nodeWidth = size[0]
					ann.nodeHeight = size[1]

				color = ann_def.get('color')
				if color:
					ann.par.Backcolorr = color[0]
					ann.par.Backcolorg = color[1]
					ann.par.Backcolorb = color[2]

				opacity = ann_def.get('opacity')
				if opacity is not None:
					ann.par.Opacity = opacity

				backAlpha = ann_def.get('backAlpha')
				if backAlpha is not None:
					ann.par.Backcoloralpha = backAlpha

				titleHeight = ann_def.get('titleHeight')
				if titleHeight is not None:
					ann.par.Titleheight = titleHeight

				bodyFontSize = ann_def.get('bodyFontSize')
				if bodyFontSize is not None:
					ann.par.Bodyfontsize = bodyFontSize

				created.append(ann.path)
			except Exception as e:
				self._log(
					f'Failed to create annotation '
					f'"{ann_def.get("name", "?")}": {e}', 'WARNING')

	def _importNestedAnnotations(self, parent, op_defs, created):
		"""Recursively create annotations from nested COMP data."""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target or not target.isCOMP:
				continue

			ann_data = op_def.get('annotations', [])
			if ann_data:
				self._createAnnotationsFromList(target, ann_data, created)

			children = op_def.get('children', [])
			if children:
				self._importNestedAnnotations(target, children, created)

	def _restoreFileLinks(self, dest) -> int:
		"""Phase 8: Restore file/syncfile parameters on externalized DATs.

		After TDN reconstruction, DATs that were previously externalized need
		their `file` parameter re-established so TD can sync content from disk.
		Looks up each DAT in the externalizations table and restores the link.

		Args:
			dest: The reconstructed COMP operator

		Returns:
			Number of DATs whose file links were restored.
		"""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table or table.numRows < 2:
				return 0
		except Exception as e:
			self._log(f'Cannot restore file links: {e}', 'WARNING')
			return 0

		# Build lookup: op_path -> rel_file_path for DATs under dest
		dest_prefix = dest.path.rstrip('/') + '/'
		file_map = {}  # {op_path: rel_file_path}
		headers = [table[0, c].val for c in range(table.numCols)]
		has_strategy = 'strategy' in headers

		for i in range(1, table.numRows):
			row_path = table[i, 'path'].val
			if not row_path.startswith(dest_prefix):
				continue

			# Skip COMP entries (TOX/TDN strategies)
			row_type = table[i, 'type'].val
			if has_strategy:
				strategy = table[i, 'strategy'].val
				if strategy in ('tox', 'tdn'):
					continue
			else:
				if row_type in ('base', 'container', 'window',
								'opviewer', 'replicator', 'tdn'):
					continue

			rel_path = table[i, 'rel_file_path'].val
			if rel_path:
				file_map[row_path] = rel_path

		if not file_map:
			return 0

		restored = 0
		for op_path, rel_path in file_map.items():
			dat = op(op_path)
			if not dat or dat.family != 'DAT':
				continue

			try:
				normalized = self.ownerComp.ext.Embody.normalizePath(rel_path)
				dat.par.file = normalized
				dat.par.file.readOnly = True
				dat.par.syncfile = True
				restored += 1
			except Exception as e:
				self._log(
					f'Failed to restore file link on {op_path}: {e}',
					'WARNING')

		if restored:
			self._log(
				f'Restored file links on {restored} DAT(s) in {dest.path}',
				'INFO')

		return restored

	# =========================================================================
	# ASYNC EXPORT HELPERS
	# =========================================================================

	def _collectAllPaths(self, parent_op, max_depth=None, depth=0,
					   embed_all=False):
		"""Recursively collect all exportable operator paths."""
		paths = []
		for child in parent_op.children:
			# Skip system/internal paths (exact match or children)
			if child.path in SYSTEM_PATHS or child.path.startswith(
					_SYSTEM_PATH_PREFIXES):
				continue
			paths.append(child.path)

			# Recurse into COMPs (but skip palette clone children
			# and TDN-tagged COMP children unless embed_all)
			if hasattr(child, 'children'):
				if self._isPaletteClone(child):
					continue
				if not embed_all and self._hasTDNTag(child):
					continue
				if max_depth is None or depth < max_depth:
					paths.extend(
						self._collectAllPaths(child, max_depth, depth + 1,
											  embed_all))

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

	@staticmethod
	def _attachAnnotations(operators, root_path, annotation_results):
		"""Attach annotation data from the main-thread collection to
		the assembled operator hierarchy (pure Python, no TD access).

		Args:
			operators: Assembled operator list (from _assembleHierarchy)
			root_path: TD root path of the export
			annotation_results: {parent_path: [annotation_dicts]} from main thread
		"""
		def _attach_recursive(ops, parent_path):
			for op_data in ops:
				op_path = parent_path.rstrip('/') + '/' + op_data['name']
				anns = annotation_results.get(op_path)
				if anns:
					op_data['annotations'] = anns
				children = op_data.get('children', [])
				if children:
					_attach_recursive(children, op_path)
		_attach_recursive(operators, root_path)

	# =========================================================================
	# PER-COMP SPLIT
	# =========================================================================

	@staticmethod
	def _splitPerComp(ops, root_path, project_name, base_dir):
		"""Split operator list into per-COMP files for multi-file TDN export.

		Returns a dict mapping absolute file path -> list of op defs for that
		file. COMPs with a 'children' key get their own .tdn file; leaf ops
		stay in their parent file. 'children' is replaced with 'tdn_ref'
		(path relative to base_dir, forward-slash separated) in parent entries.

		Args:
			ops: List of operator defs (may include 'children' for COMPs)
			root_path: TD root path being exported (e.g., '/' or '/embody')
			project_name: Project name used as stem of root file for '/' exports
			base_dir: Absolute path to the base output directory

		Returns:
			dict mapping str(absolute_file_path) -> list of op defs
		"""
		from pathlib import Path
		base = Path(base_dir)
		result = {}

		if root_path == '/':
			root_file = base / f'{project_name}.tdn'
			root_dir = base
		else:
			path_obj = base / root_path.lstrip('/')
			root_file = path_obj.parent / (path_obj.name + '.tdn')
			root_dir = path_obj

		result[str(root_file)] = []

		def process(op_list, file_key, current_dir):
			for op_def in op_list:
				if 'children' in op_def:
					comp_name = op_def['name']
					comp_file = current_dir / f'{comp_name}.tdn'
					tdn_ref = str(comp_file.relative_to(base)).replace('\\', '/')
					entry = {k: v for k, v in op_def.items() if k != 'children'}
					entry['tdn_ref'] = tdn_ref
					result[file_key].append(entry)
					child_key = str(comp_file)
					result[child_key] = []
					process(op_def['children'], child_key, current_dir / comp_name)
				else:
					result[file_key].append(op_def)

		process(ops, str(root_file), root_dir)
		return result

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

	def _isPaletteClone(self, target):
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

	def _hasTDNTag(self, target):
		"""Check if a COMP has its own TDN externalization tag."""
		if not target.isCOMP:
			return False
		tdn_tag = self.ownerComp.par.Tdntag.val
		return tdn_tag in target.tags

	def _serializeValue(self, val):
		"""Convert a parameter value to a JSON-safe type.

		Strings starting with '=' or '~' are escaped with a double prefix
		to avoid collision with the expression/bind shorthand.
		"""
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
			# Escape strings that start with = or ~ to avoid shorthand collision
			if val.startswith('=') or val.startswith('~'):
				return val[0] + val
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

	# =========================================================================
	# POST-PROCESSING OPTIMIZATIONS
	# =========================================================================

	@staticmethod
	def _compact_json_dumps(data):
		"""Serialize to JSON with tab indentation, inlining short arrays/objects.

		Short arrays (<=80 chars when inlined) are collapsed to single lines.
		This includes position, size, color, tags, connection arrays, and
		custom parameter value arrays.
		"""
		import re
		raw = json.dumps(data, indent='\t', ensure_ascii=False)

		def try_compact(match):
			content = match.group(0)
			# Remove internal newlines and tabs
			compacted = re.sub(r'\s*\n\s*', ' ', content)
			compacted = re.sub(r'\[\s+', '[', compacted)
			compacted = re.sub(r'\s+\]', ']', compacted)
			compacted = re.sub(r'\{\s+', '{', compacted)
			compacted = re.sub(r'\s+\}', '}', compacted)
			compacted = re.sub(r',\s+', ', ', compacted)
			if len(compacted) <= 80:
				return compacted
			return content

		# Compact arrays of simple values (numbers, short strings, booleans, null)
		raw = re.sub(
			r'\[\s*\n\s*(?:(?:-?\d+(?:\.\d+)?|"[^"\n]{0,50}"|true|false|null)'
			r'(?:,\s*\n\s*(?:-?\d+(?:\.\d+)?|"[^"\n]{0,50}"|true|false|null))*'
			r')\s*\n\s*\]',
			try_compact, raw)

		return raw + '\n'

	@staticmethod
	def _compute_type_defaults(operators):
		"""Find per-type properties shared by ALL operators of that type.

		A property enters type_defaults ONLY if present on every single
		operator of that type with the same value. This eliminates the need
		for a 'reset to default' marker.

		Supported properties: parameters, flags, size, color, tags.

		Returns dict: {op_type: {'parameters': {...}, 'flags': [...], ...}}
		"""
		from collections import Counter, defaultdict

		type_counts = Counter()
		# {op_type: {(par_name, val_json): count}}
		type_par_counts = defaultdict(lambda: Counter())
		# {op_type: {json_key: count}} for atomic properties
		type_flags_counts = defaultdict(lambda: Counter())
		type_size_counts = defaultdict(lambda: Counter())
		type_color_counts = defaultdict(lambda: Counter())
		type_tags_counts = defaultdict(lambda: Counter())

		def walk(ops):
			for op_data in ops:
				op_type = op_data.get('type', '')
				type_counts[op_type] += 1
				for pname, pval in op_data.get('parameters', {}).items():
					key = (pname, json.dumps(pval, sort_keys=True))
					type_par_counts[op_type][key] += 1
				if 'flags' in op_data:
					type_flags_counts[op_type][json.dumps(sorted(op_data['flags']))] += 1
				if 'size' in op_data:
					type_size_counts[op_type][json.dumps(op_data['size'])] += 1
				if 'color' in op_data:
					type_color_counts[op_type][json.dumps(op_data['color'])] += 1
				if 'tags' in op_data:
					type_tags_counts[op_type][json.dumps(sorted(op_data['tags']))] += 1
				if 'children' in op_data:
					walk(op_data['children'])

		walk(operators)

		result = {}
		for op_type, count in type_counts.items():
			if count < 2:
				continue

			type_default = {}

			# Parameters (dict-level merge on import)
			unanimous = {}
			for (pname, pval_json), pcount in type_par_counts[op_type].items():
				if pcount == count:
					unanimous[pname] = json.loads(pval_json)
			if unanimous:
				type_default['parameters'] = unanimous

			# Atomic properties (whole-value replacement on import)
			for prop, counter in [
				('flags', type_flags_counts),
				('size', type_size_counts),
				('color', type_color_counts),
				('tags', type_tags_counts),
			]:
				prop_counter = counter[op_type]
				if len(prop_counter) == 1:
					val_json, val_count = next(iter(prop_counter.items()))
					if val_count == count:
						type_default[prop] = json.loads(val_json)

			if type_default:
				result[op_type] = type_default

		return result

	@staticmethod
	def _strip_type_defaults(operators, type_defaults):
		"""Remove properties from operators that match their type_defaults.

		Strips parameters (per-key), flags, size, color, and tags (whole-value).
		Modifies operators in-place.
		"""
		def walk(ops):
			for op_data in ops:
				op_type = op_data.get('type', '')
				td = type_defaults.get(op_type, {})

				# Parameters (per-key stripping)
				td_params = td.get('parameters', {})
				if td_params and 'parameters' in op_data:
					for pname in list(op_data['parameters'].keys()):
						if pname in td_params:
							pval = op_data['parameters'][pname]
							if json.dumps(pval, sort_keys=True) == json.dumps(td_params[pname], sort_keys=True):
								del op_data['parameters'][pname]
					if not op_data['parameters']:
						del op_data['parameters']

				# Atomic properties (whole-value stripping)
				if 'flags' in td and 'flags' in op_data:
					if sorted(op_data['flags']) == sorted(td['flags']):
						del op_data['flags']
				if 'size' in td and 'size' in op_data:
					if op_data['size'] == td['size']:
						del op_data['size']
				if 'color' in td and 'color' in op_data:
					if op_data['color'] == td['color']:
						del op_data['color']
				if 'tags' in td and 'tags' in op_data:
					if sorted(op_data['tags']) == sorted(td['tags']):
						del op_data['tags']

				if 'children' in op_data:
					walk(op_data['children'])

		walk(operators)

	@staticmethod
	def _merge_type_defaults(op_defs, type_defaults):
		"""Merge type_defaults into operator defs for import.

		Parameters use dict-level merge (operator keys override individual
		defaults). Flags, size, color, and tags use whole-value replacement
		(operator either has its own or inherits entirely from type_defaults).

		Modifies in-place.
		"""
		if not type_defaults:
			return

		def walk(ops):
			for op_def in ops:
				op_type = op_def.get('type', '')
				td = type_defaults.get(op_type, {})
				if not td:
					if 'children' in op_def:
						walk(op_def['children'])
					continue

				# Parameters (dict-level merge)
				td_params = td.get('parameters', {})
				if td_params:
					if 'parameters' not in op_def:
						op_def['parameters'] = {}
					merged = dict(td_params)
					merged.update(op_def['parameters'])
					op_def['parameters'] = merged

				# Atomic properties (whole-value, op-specific wins)
				for prop in ('flags', 'size', 'color', 'tags'):
					if prop in td and prop not in op_def:
						op_def[prop] = list(td[prop])

				if 'children' in op_def:
					walk(op_def['children'])

		walk(op_defs)

	@staticmethod
	def _extract_par_templates(operators):
		"""Extract repeated custom parameter page definitions into templates.

		Groups by page: if the same page definition (all par defs sans values)
		appears on 2+ operators, it becomes a named template.

		Returns (par_templates_dict, operators) — operators modified in-place.
		"""
		from collections import defaultdict

		def def_key(par_def):
			"""Hashable key from a par definition, excluding value/values."""
			return json.dumps(
				{k: v for k, v in par_def.items() if k not in ('value', 'values')},
				sort_keys=True)

		def page_key(page_defs):
			"""Hashable key for a page's full definition set."""
			return json.dumps([def_key(p) for p in page_defs], sort_keys=True)

		# Pass 1: count occurrences of each page definition
		page_counts = defaultdict(int)

		def count_walk(ops):
			for op_data in ops:
				cp = op_data.get('custom_pars', {})
				if isinstance(cp, dict):
					for page_name, page_defs in cp.items():
						if isinstance(page_defs, list):
							pk = page_key(page_defs)
							page_counts[pk] += 1
				if 'children' in op_data:
					count_walk(op_data['children'])

		count_walk(operators)

		# Build templates for pages appearing 2+ times
		templates = {}
		key_to_name = {}
		name_seen = defaultdict(int)

		# Sort by key for deterministic naming
		for pk in sorted(page_counts.keys()):
			count = page_counts[pk]
			if count < 2:
				continue
			defs_json_list = json.loads(pk)
			# Reconstruct the actual definitions
			defs = [json.loads(d) for d in defs_json_list]

			# Derive name from first par's page (which we don't have here,
			# so use the template content to build a name)
			# Since we don't store page in the def, we'll name by content hash
			# Actually, let's find the page name from operators
			template_name = None
			# We'll set name during replacement pass when we see the page key
			key_to_name[pk] = defs  # Store defs temporarily

		if not key_to_name:
			return {}, operators

		# Pass 2: replace inline definitions with template references
		# Also collect page names for template naming
		pk_to_page_name = {}

		def replace_walk(ops):
			for op_data in ops:
				cp = op_data.get('custom_pars', {})
				if isinstance(cp, dict):
					for page_name in list(cp.keys()):
						page_defs = cp[page_name]
						if not isinstance(page_defs, list):
							continue
						pk = page_key(page_defs)
						if pk not in key_to_name:
							continue

						# Record page name for this template
						if pk not in pk_to_page_name:
							pk_to_page_name[pk] = page_name

						# Build template reference with value overrides
						ref = {'$t': pk}  # Placeholder, will replace with real name
						for par_def in page_defs:
							pname = par_def.get('name', '')
							if 'value' in par_def:
								ref[pname] = par_def['value']
							elif 'values' in par_def:
								ref[pname] = par_def['values']
						cp[page_name] = ref
				if 'children' in op_data:
					replace_walk(op_data['children'])

		replace_walk(operators)

		# Assign template names from collected page names
		final_templates = {}
		pk_to_final_name = {}
		for pk, defs in key_to_name.items():
			page_name = pk_to_page_name.get(pk, 'custom')
			base_name = page_name.lower().replace(' ', '_')
			name_seen[base_name] += 1
			if name_seen[base_name] > 1:
				template_name = f'{base_name}_{name_seen[base_name]}'
			else:
				template_name = base_name
			final_templates[template_name] = defs
			pk_to_final_name[pk] = template_name

		# Replace placeholder pk in $t references with real names
		def finalize_walk(ops):
			for op_data in ops:
				cp = op_data.get('custom_pars', {})
				if isinstance(cp, dict):
					for page_name, page_val in cp.items():
						if isinstance(page_val, dict) and '$t' in page_val:
							pk = page_val['$t']
							if pk in pk_to_final_name:
								page_val['$t'] = pk_to_final_name[pk]
				if 'children' in op_data:
					finalize_walk(op_data['children'])

		finalize_walk(operators)

		return final_templates, operators

	@staticmethod
	def _resolve_par_templates(op_defs, par_templates):
		"""Resolve $t template references in custom_pars back to full definitions.

		Modifies op_defs in-place.
		"""
		if not par_templates:
			return

		def walk(ops):
			for op_def in ops:
				cp = op_def.get('custom_pars', {})
				if isinstance(cp, dict):
					for page_name, page_val in list(cp.items()):
						if isinstance(page_val, dict) and '$t' in page_val:
							template_name = page_val['$t']
							template_defs = par_templates.get(template_name, [])
							if not template_defs:
								continue
							# Reconstruct full definitions with value overrides
							resolved = []
							for par_def in template_defs:
								merged = dict(par_def)
								pname = par_def.get('name', '')
								if pname in page_val:
									override = page_val[pname]
									if isinstance(override, list):
										merged['values'] = override
									else:
										merged['value'] = override
								resolved.append(merged)
							cp[page_name] = resolved
				if 'children' in op_def:
					walk(op_def['children'])

		walk(op_defs)

	def _getEmbodyVersion(self):
		"""Get the Embody version string from the ownerComp's Version parameter."""
		try:
			return str(self.ownerComp.par.Version.eval())
		except Exception:
			return 'unknown'

	def _getBuildNumber(self, root_op):
		"""Get build number from externalizations table, falling back to COMP par."""
		# TSV is source of truth
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if table:
				headers = [table[0, c].val for c in range(table.numCols)]
				has_strategy = 'strategy' in headers
				for i in range(1, table.numRows):
					if table[i, 'path'].val != root_op.path:
						continue
					is_tdn = False
					if has_strategy:
						is_tdn = table[i, 'strategy'].val == 'tdn'
					else:
						is_tdn = table[i, 'type'].val == 'tdn'
					if is_tdn:
						try:
							return int(table[i, 'build'].val)
						except (ValueError, TypeError):
							pass
		except Exception:
			pass
		# Fall back to COMP parameter
		if hasattr(root_op.par, 'Build'):
			try:
				return int(root_op.par.Build.eval())
			except (ValueError, TypeError):
				pass
		return None

	def _resolveOutputPath(self, output_file, root_op):
		"""Resolve the output file path, matching the operator's TD path structure."""
		from pathlib import Path

		if output_file == 'auto':
			project_dir = Path(project.folder)

			if root_op.path == '/':
				# Root export: place in externalizations folder if configured
				safe_name = project.name.removesuffix('.toe')
				try:
					ext_folder = self.ownerComp.ext.Embody.ExternalizationsFolder
					if ext_folder:
						out_dir = project_dir / ext_folder
						out_dir.mkdir(parents=True, exist_ok=True)
						return str(out_dir / f'{safe_name}.tdn')
				except Exception as e:
					self._log(f'Could not resolve externalizations folder: {e}', 'WARNING')
				return str(project_dir / f'{safe_name}.tdn')
			else:
				# Non-root: use full TD path to mirror operator hierarchy
				rel_path = root_op.path.lstrip('/') + '.tdn'
				out_path = project_dir / rel_path
				out_path.parent.mkdir(parents=True, exist_ok=True)
				return str(out_path)

		return str(output_file)

	def _trackTDNExport(self, root_path, file_path, build_num=None, touch_build=None):
		"""Add/update a TDN entry in the externalizations table."""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table:
				return

			from pathlib import Path
			rel_path = self.ownerComp.ext.Embody.normalizePath(
				str(Path(file_path).relative_to(project.folder)))
			timestamp = datetime.now(timezone.utc).strftime(
				'%Y-%m-%d %H:%M:%S UTC')

			build_str = str(build_num) if build_num is not None else ''
			tb_str = str(touch_build) if touch_build is not None else ''

			# Check for strategy column (new schema)
			headers = [table[0, c].val for c in range(table.numCols)]
			has_strategy = 'strategy' in headers

			# Update existing row if found — check strategy='tdn' or type='tdn'
			for i in range(1, table.numRows):
				row_path = table[i, 'path'].val
				if row_path != root_path:
					continue
				is_tdn_row = False
				if has_strategy and table[i, 'strategy'].val == 'tdn':
					is_tdn_row = True
				elif table[i, 'type'].val == 'tdn':
					is_tdn_row = True
				if is_tdn_row:
					table[i, 'rel_file_path'] = rel_path
					table[i, 'timestamp'] = timestamp
					table[i, 'dirty'] = ''
					table[i, 'build'] = build_str
					table[i, 'touch_build'] = tb_str
					return

			# Add new row (schema-aware)
			if has_strategy:
				# Determine COMP type
				target = op(root_path)
				comp_type = target.type if target else 'base'
				table.appendRow([root_path, comp_type, 'tdn', rel_path,
								 timestamp, '', build_str, tb_str])
			else:
				table.appendRow([root_path, 'tdn', rel_path, timestamp,
								 '', build_str, tb_str])
		except Exception as e:
			self._log(f'Failed to track TDN export: {e}', 'WARNING')

	def _warnLockedNonDATs(self, root_op, context='export'):
		"""Scan a network for locked non-DAT operators and warn.

		Locked TOPs, CHOPs, and SOPs will have their lock flag preserved
		in TDN but their frozen content (pixels, channels, geometry) is
		NOT stored. This warns users so they aren't surprised by data loss.

		Args:
			root_op: The COMP to scan (recursively)
			context: 'export' shows ui.messageBox + log; 'import' logs only
		"""
		locked = []
		for child in root_op.findChildren():
			if child.lock and child.family in ('TOP', 'CHOP', 'SOP'):
				locked.append(child)

		if not locked:
			return

		# Build summary
		names = [f'{c.name} ({c.family})' for c in locked[:10]]
		summary = ', '.join(names)
		if len(locked) > 10:
			summary += f', ... and {len(locked) - 10} more'

		if context == 'export':
			self._log(
				f'Locked non-DAT operators in {root_op.path}: {summary} '
				f'— frozen data will not persist through TDN', 'WARNING')
			try:
				ui.messageBox(
					'Embody — Locked Content Warning',
					f'{len(locked)} locked non-DAT operator(s) in '
					f'{root_op.path}:\n\n{summary}\n\n'
					f'TDN preserves the lock flag but cannot store '
					f'frozen pixel, channel, or geometry data. '
					f'After reload these operators will be locked '
					f'but empty.\n\n'
					f'To preserve locked content, either:\n'
					f'  - Unlock the operator(s) (they will re-cook '
					f'from inputs)\n'
					f'  - Switch this COMP to TOX strategy instead '
					f'of TDN',
					buttons=['OK'])
			except Exception:
				pass  # Non-fatal if dialog fails
		else:
			# Import context — log only, no dialog (reconstruction is automated)
			self._log(
				f'Restored lock flag on {len(locked)} non-DAT operator(s) '
				f'in {root_op.path}: {summary} — these operators have no '
				f'frozen data and should be unlocked to re-cook', 'WARNING')

	def _log(self, message, level='INFO'):
		"""Log via Embody's centralized logger."""
		try:
			self.ownerComp.ext.Embody.Log(message, level, _depth=2)
			return
		except Exception:
			pass  # Fallback below handles this — avoid recursion in logger
		# Fallback if Embody ext unavailable
		print(f'[TDN][{level}] {message}')
