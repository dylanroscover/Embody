"""
TDN -- TouchDesigner Network open format (.tdn)

Exports and imports TouchDesigner networks as human-readable YAML files
(a strict JSON superset, so legacy JSON .tdn still load). Only non-default
properties are stored, keeping the output minimal.

This extension lives on the Embody COMP and is callable via:
  - MCP tools (export_network / import_network) through Envoy
  - TD UI (keyboard shortcut Ctrl+Shift+N, pulse parameters)
  - Direct Python: op.Embody.ext.TDN.ExportNetwork(...)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import yaml  # PyYAML (pre-installed in TD and shell python)
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Optional, Union

# CSafe is ~10x faster AND -- critically -- CSafeLoader reads legacy
# tab-indented JSON .tdn that the pure-python SafeLoader REJECTS. Fall back
# to pure-python Safe* only if libyaml is truly absent.
try:
	_TDN_BaseDumper = yaml.CSafeDumper
	_TDN_BaseLoader = yaml.CSafeLoader
except AttributeError:
	_TDN_BaseDumper = yaml.SafeDumper
	_TDN_BaseLoader = yaml.SafeLoader


class _TDNYamlDumper(_TDN_BaseDumper):
	"""Private subclass so TDN representers never leak into the global SafeDumper."""
	pass


def _tdn_str_representer(dumper, data):
	# Multi-line strings -> literal block scalar (|) for readability.
	# Single-line -> default; SafeDumper auto-quotes ambiguous scalars so
	# they round-trip as str. Tab-bearing multi-line falls back to
	# double-quoted (lossless, not pretty); boilerplate-omission removes
	# the only such strings in practice.
	style = '|' if '\n' in data else None
	return dumper.represent_scalar('tag:yaml.org,2002:str', data, style=style)


def _tdn_list_representer(dumper, data):
	# Short pure-numeric vectors (position/size/color, <=4) stay inline [a, b];
	# everything else block style. bool is excluded (isinstance(True,int)).
	flow = (len(data) <= 4
			and all(isinstance(x, (int, float)) and not isinstance(x, bool)
					for x in data))
	return dumper.represent_sequence('tag:yaml.org,2002:seq', data,
									 flow_style=flow)


_TDNYamlDumper.add_representer(str, _tdn_str_representer)
_TDNYamlDumper.add_representer(list, _tdn_list_representer)


def tdn_dump(data) -> str:
	"""Serialize a TDN document to deterministic, readable YAML v2.0.

	Always ends with a single trailing newline (yaml.dump emits one; the
	defensive guard locks the contract test_export_file_not_truncated relies on).
	"""
	out = yaml.dump(data, Dumper=_TDNYamlDumper, sort_keys=False,
					width=4096, allow_unicode=True)
	return out if out.endswith('\n') else out + '\n'


def tdn_load(text):
	"""Parse a .tdn document. Reads YAML v2.0 AND legacy JSON v1.x.

	CRITICAL back-compat: existing .tdn are TAB-indented JSON (json.dumps
	indent='\\t'), which YAML FORBIDS as indentation. CSafeLoader is lenient
	and reads them, but pure-python SafeLoader raises ScannerError on the tab.
	So: json-first when the doc starts with { or [, else YAML. json.loads is
	fed the BOM/whitespace-STRIPPED text (a leading UTF-8 BOM makes json.loads
	raise 'Unexpected UTF-8 BOM'); the inner except is narrowed to
	JSONDecodeError so a genuinely-corrupt brace-doc does NOT silently degrade
	to a lenient YAML re-parse on non-decode errors.
	"""
	stripped = text.lstrip('﻿').lstrip()
	if stripped[:1] in ('{', '['):
		try:
			return json.loads(stripped)   # FIX (Review 1 HIGH): stripped, not text
		except json.JSONDecodeError:      # FIX (Review 1 LOW): narrowed except
			pass
	return yaml.load(text, Loader=_TDN_BaseLoader)


TDN_VERSION = '2.0'  # was '1.5'

# Parameters to always skip (Embody-managed or internal)
SKIP_PARAMS = {
	'externaltox', 'enableexternaltox', 'reloadtox',
	'reinitextensions', 'savebackup',
	'savecustom', 'reloadcustom',
	'pageindex',  # UI state (visible parameter page tab), not config
}

# Embody-managed About page parameters -- excluded from TDN export
# because they are reconstructed from externalizations.tsv at import time.
_EMBODY_ABOUT_PARS = {'Build', 'Date', 'Touchbuild'}

# Built-in parameter styles to skip (actions, not state)
SKIP_BUILTIN_STYLES = {'Pulse', 'Momentary', 'Header'}

# Parameters to skip on palette clones -- TD plumbing that interferes
# with parameter round-tripping. The clone expression causes TD to
# override user-set values (like buttontype) on rebuild.
_PALETTE_CLONE_SKIP_PARAMS = {'clone', 'enablecloning'}

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

# Default flag values -- only export flags that differ
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
	# Embody-managed recovery/restore markers -- live on the COMP shell in
	# the .toe, never inside the .tdn (serializing _tdn_rel_path would make
	# every pasted/imported copy claim the original's file).
	'_tdn_rel_path', '_pending_tox_restore',
}
_SYSTEM_PATH_PREFIXES = tuple(p + '/' for p in SYSTEM_PATHS)


# =============================================================================
# C1 clipboard envelope (_embody_tdn) -- byte-parity with
# platform/packages/contracts/envelope.ts. Module-level so it stays headless
# unit-testable (import TDNExt; TDNExt.tdn_sha256(...)) and so the class methods
# below can call it directly. Trusted own-network Copy/Paste is the only thing
# that needs it; the untrusted community layer lives in CollectionExt.
# =============================================================================
EMBODY_TDN_MARKER = "_embody_tdn"
EMBODY_TDN_VERSION = 1
ENVELOPE_SOURCES = ("embody", "embody.tools")


def is_embody_tdn_envelope(value) -> bool:
	if not isinstance(value, dict):
		return False
	return (value.get(EMBODY_TDN_MARKER) == EMBODY_TDN_VERSION
			and value.get("source") in ENVELOPE_SOURCES
			and isinstance(value.get("sha256"), str)
			and isinstance(value.get("tdn"), dict))


def canonical_tdn_bytes(tdn: dict) -> bytes:
	"""Canonical JSON bytes used for TDN hashing (must match the TS side)."""
	return json.dumps(tdn, sort_keys=True, separators=(",", ":"),
					  ensure_ascii=False).encode("utf-8")


def tdn_sha256(tdn: dict) -> str:
	return hashlib.sha256(canonical_tdn_bytes(tdn)).hexdigest()


def wrap_tdn(tdn: dict, source: str, slug=None, version=None) -> dict:
	if source not in ENVELOPE_SOURCES:
		raise ValueError("Invalid envelope source: %s" % source)
	# copy_id: a fresh per-copy nonce (NOT part of the sha256, ignored by every
	# validator) so each Copy is a distinct clipboard payload -- this is what lets
	# the clipboard watcher re-prompt on a re-copy. Mirrors the web side.
	env = {EMBODY_TDN_MARKER: EMBODY_TDN_VERSION, "source": source,
		   "copy_id": os.urandom(8).hex(),
		   "sha256": tdn_sha256(tdn), "tdn": tdn}
	if slug is not None:
		env["slug"] = slug
	if version is not None:
		env["version"] = version
	return env


def to_clipboard_str(envelope: dict) -> str:
	# Pretty-printed so a pasted envelope is human-readable. Indentation does
	# NOT affect integrity: the sha256 is computed over canonical_tdn_bytes(tdn)
	# (sorted keys, no spaces), and unwrap_clipboard parses with json.loads
	# (whitespace-insensitive), so round-trips and web byte-parity are preserved.
	return json.dumps(envelope, ensure_ascii=False, indent=2)


def unwrap_clipboard(text: str):
	try:
		value = json.loads(text)
	except Exception:
		return None
	return value if is_embody_tdn_envelope(value) else None


def verify_envelope_integrity(envelope: dict) -> bool:
	try:
		return tdn_sha256(envelope["tdn"]) == envelope["sha256"]
	except Exception:
		return False


def resolve_tdn_name(tdn, slug=None):
	"""Best name for a network being pasted from a TDN: the `network_path`
	basename (a required field, so always present except for a whole-project
	"/" export) -> envelope `slug` -> None. The caller sanitizes the result
	with tdu.validName and supplies its own final fallback. Pure (no TD state)
	so it stays headless unit-testable.
	"""
	if isinstance(tdn, dict):
		path = (tdn.get("network_path") or "").rstrip("/")
		if path:
			base = path.split("/")[-1]
			if base:
				return base
	if slug:
		return str(slug)
	return None


class TDNExt:
	"""Extension for exporting/importing TouchDesigner networks as .tdn (YAML v2.0)."""

	def __init__(self, ownerComp: 'COMP') -> None:
		self.ownerComp: 'COMP' = ownerComp
		self._export_state: Optional[dict[str, Any]] = None
		# Per-OPType caches for export performance.
		# Built-in parameter defaults and exportable names are stable per type,
		# so we cache them to avoid repeated Python-to-C++ bridge calls.
		self._defaults_cache: dict[str, dict[str, Any]] = {}
		self._exportable_cache: dict[str, set[str]] = {}
		self._seq_default_blocks_cache: dict[tuple[str, str], int] = {}
		# Divergent defaults: params where TD's p.default lies (differs
		# from the actual creation value). Loaded lazily from the
		# divergent_defaults tableDAT inside the Embody COMP.
		self._divergent_defaults: dict[str, dict[str, Any]] = {}
		self._divergent_loaded: bool = False
		# On-the-fly fallback cache for unknown TD builds.
		self._runtime_creation_cache: dict[str, dict[str, Any]] = {}
		# Per-OPType creation FLAG values. Flag defaults vary by type --
		# object COMPs (geometryCOMP etc.) create with render/display ON,
		# so the global DEFAULT_FLAGS table alone would silently drop a
		# user's render-off through the round-trip. Populated lazily by
		# _getCreationFlagDefaults; deliberately NOT cleared per export
		# (creation defaults are stable for a TD session).
		self._flag_defaults_cache: dict[str, dict[str, bool]] = {}
		self._scan_workspace: Optional['COMP'] = None
		# Palette component catalog: {name: {'type': op_type, 'min_children': N}}.
		# Populated by CatalogManagerExt after palette scan completes.
		# Used by _isPaletteClone() as the primary detection method.
		self._palette_catalog: dict[str, dict] = {}
		# TD's live default compute-shader text, captured lazily once for
		# boilerplate omission (see _defaultComputeShaderText).
		self._default_compute_text: Optional[str] = None
		# Clipboard auto-paste watcher: prompt ONCE when a new _embody_tdn
		# envelope appears on the OS clipboard. No keyboard shortcut -- TD's
		# native Cmd/Ctrl+V paste cannot be suppressed, so a paste key always
		# double-fires TD's own operator-clipboard paste. Generation-guarded
		# run()-loop, re-armed on every reinit (stale loops self-terminate).
		self._clip_last_sig = None
		try:
			_clip_gen = self.ownerComp.fetch('_clip_watch_gen', 0) + 1
			self.ownerComp.store('_clip_watch_gen', _clip_gen)
			# Pending run() calls can outlive COMP replacement during upgrades.
			run("o = op(%r)\nif o and o.valid: o.ext.TDN._clipboardWatchTick(%d)" %
				(self.ownerComp.path, _clip_gen),
				fromOP=self.ownerComp, delayMilliSeconds=2500)
		except Exception:
			pass

	# =========================================================================
	# TDN SERIALIZATION (YAML v2.0) -- exposed for cross-extension access via
	# parent.Embody.ext.TDN.tdn_dump / parent.Embody.ext.TDN.tdn_load.
	# Internal callers use the module-level funcs directly.
	# =========================================================================

	tdn_dump = staticmethod(tdn_dump)
	tdn_load = staticmethod(tdn_load)

	# =========================================================================
	# CRASH SAFETY -- atomic writes, backup rotation, validation
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
			# tdn_path not under project_folder -- fall back to flat name
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
		content or the complete new content -- never a partial write.

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
			tmp_path = None  # Rename succeeded -- no cleanup needed
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
			doc = tdn_load(text)
		except Exception as e:
			return {'valid': False, 'error': f'Invalid TDN: {e}'}
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
		3. Post-write validation (read back + TDN parse)
		4. If validation fails, restore from .bak

		Returns {'success': True} or {'error': '...'}.
		"""
		# Step 1: Backup rotation (only if file already exists)
		try:
			TDNExt._rotate_backups(tdn_path, project_folder)
		except Exception:
			# Backup failure should not block the write -- log but continue.
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

		# Step 4: Validation failed -- attempt restore from backup
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
		# source_file is project.name, rewritten on every export but not
		# content. Dropping it keeps pre-save equality and diff_tdn aligned
		# (_normalize_tdn_for_compare reuses this set for diffing).
		'source_file',
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
			result = tdn_load(p.read_text(encoding='utf-8'))
			# A valid .tdn is a mapping. YAML happily parses garbage like
			# 'not valid json {{{' into a scalar string (legacy JSON would have
			# raised), so reject any non-dict as corrupt/unreadable.
			return result if isinstance(result, dict) else None
		except Exception:
			return None

	# =====================================================================
	# SEMANTIC DIFF (live network vs on-disk .tdn) -- powers Envoy's diff_tdn
	# =====================================================================
	# Single source of truth for "what counts as a change": reuses
	# _TDN_VOLATILE_KEYS and the import-side expanders (_resolve_par_templates,
	# _merge_type_defaults) so the diff can never drift from import/pre-save.

	_DIFF_SCHEMA_VERSION = '1.0'
	# Root-document keys that are the externalized COMP's OWN content (the rest
	# of the top level is metadata, container, or diffed separately).
	_DIFF_ROOT_CONTENT_KEYS = (
		'type', 'parameters', 'custom_pars', 'sequences',
		'flags', 'color', 'tags', 'comment', 'storage',
	)
	# Operator keys NOT compared as own-fields (identity / diffed separately so
	# a deep child edit never marks an ancestor modified).
	_DIFF_OP_SKIP_KEYS = frozenset({'name', 'children', 'annotations'})

	@staticmethod
	def _normalize_dat_content(node):
		"""Convert legacy v1.5 array-of-lines dat_content to the v2.0 joined
		string in place, so an unchanged DAT does not diff across the v1.5->v2.0
		format bump. Mirrors tdn_textconv._normalize_dat_content."""
		if isinstance(node, dict):
			if (node.get('dat_content_format') == 'text'
					and isinstance(node.get('dat_content'), list)):
				node['dat_content'] = '\n'.join(node['dat_content'])
			for value in node.values():
				TDNExt._normalize_dat_content(value)
		elif isinstance(node, list):
			for item in node:
				TDNExt._normalize_dat_content(item)

	@staticmethod
	def _normalize_tdn_for_compare(tdn):
		"""Return a NEW normalized copy of a TDN dict (input untouched).

		Drops volatile header keys (_TDN_VOLATILE_KEYS), expands par_templates
		and type_defaults into the operators via the same import-side expanders,
		then drops the now-redundant compression blocks. After this two
		semantically-equal exports compare equal regardless of compression.
		"""
		import copy
		if not isinstance(tdn, dict):
			return {}
		out = copy.deepcopy(tdn)
		for key in TDNExt._TDN_VOLATILE_KEYS:
			out.pop(key, None)
		# Reconcile a legacy v1.5 (array-of-lines) on-disk dat_content with the
		# v2.0 (joined string) live form so an unchanged DAT does not false-diff.
		TDNExt._normalize_dat_content(out)
		ops = out.get('operators', [])
		if isinstance(ops, list):
			TDNExt._resolve_par_templates(ops, out.get('par_templates', {}) or {})
			TDNExt._merge_type_defaults(ops, out.get('type_defaults', {}) or {})
		out.pop('par_templates', None)
		out.pop('type_defaults', None)
		return out

	@staticmethod
	def _diff_index_by_name(items, warnings, parent_path, side):
		"""Index op/annotation dicts by name; warn on duplicate siblings."""
		idx = {}
		for item in items or []:
			if not isinstance(item, dict):
				continue
			name = item.get('name')
			if name in idx:
				warnings.append(
					'Duplicate sibling name %r under %r (%s); diff may be '
					'ambiguous' % (name, parent_path or '/', side))
			idx[name] = item
		return idx

	@staticmethod
	def _diff_field_change(key, old, new):
		"""Structured change for one differing field.

		parameters -> list of {name, old, new}; everything else -> {old, new}.
		"""
		if key == 'parameters' and (isinstance(old, dict) or isinstance(new, dict)):
			old = old or {}
			new = new or {}
			items = []
			for pname in sorted(set(old) | set(new)):
				ov = old.get(pname)
				nv = new.get(pname)
				if ov != nv:
					items.append({'name': pname, 'old': ov, 'new': nv})
			return items
		return {'old': old, 'new': new}

	@staticmethod
	def _diff_own_fields(live_op, disk_op, keys=None):
		"""Return {key: change} for differing own-fields, or {} if identical."""
		if keys is None:
			keys = (set(live_op) | set(disk_op)) - TDNExt._DIFF_OP_SKIP_KEYS
		changes = {}
		for key in keys:
			# old = disk (saved baseline), new = live (unsaved current)
			ov = disk_op.get(key)
			nv = live_op.get(key)
			# tags/flags are set-like; compare order-insensitively
			if key in ('tags', 'flags') and isinstance(ov, list) and isinstance(nv, list):
				if sorted(ov) == sorted(nv):
					continue
				ov, nv = sorted(ov), sorted(nv)
			if ov != nv:
				changes[key] = TDNExt._diff_field_change(key, ov, nv)
		return changes

	@staticmethod
	def _diff_join(parent_path, name):
		if not parent_path:
			return str(name)
		return parent_path.rstrip('/') + '/' + str(name)

	@staticmethod
	def _diff_annotations(live_anns, disk_anns, parent_path, added, removed,
						  modified, warnings):
		live_idx = TDNExt._diff_index_by_name(
			live_anns, warnings, parent_path, 'live-annotation')
		disk_idx = TDNExt._diff_index_by_name(
			disk_anns, warnings, parent_path, 'disk-annotation')
		for name in live_idx:
			if name not in disk_idx:
				a = live_idx[name]
				added.append({'path': parent_path, 'name': name,
							  'type': a.get('mode'), 'kind': 'annotation'})
		for name in disk_idx:
			if name not in live_idx:
				a = disk_idx[name]
				removed.append({'path': parent_path, 'name': name,
								'type': a.get('mode'), 'kind': 'annotation'})
		for name in live_idx:
			if name not in disk_idx:
				continue
			keys = (set(live_idx[name]) | set(disk_idx[name])) - {'name'}
			changes = TDNExt._diff_own_fields(
				live_idx[name], disk_idx[name], keys=keys)
			if changes:
				modified.append({
					'path': parent_path, 'name': name,
					'type': live_idx[name].get('mode'), 'kind': 'annotation',
					'changed_keys': sorted(changes.keys()), 'changes': changes})

	@staticmethod
	def _diff_level(live_ops, disk_ops, parent_path, added, removed, modified,
					warnings):
		live_idx = TDNExt._diff_index_by_name(
			live_ops, warnings, parent_path, 'live')
		disk_idx = TDNExt._diff_index_by_name(
			disk_ops, warnings, parent_path, 'disk')

		def _entry(path, op_def, kind):
			return {'path': path, 'name': op_def.get('name'),
					'type': op_def.get('type'), 'kind': kind}

		for name in live_idx:
			if name not in disk_idx:
				added.append(_entry(TDNExt._diff_join(parent_path, name),
									live_idx[name], 'op'))
		for name in disk_idx:
			if name not in live_idx:
				removed.append(_entry(TDNExt._diff_join(parent_path, name),
									  disk_idx[name], 'op'))
		for name in live_idx:
			if name not in disk_idx:
				continue
			lo = live_idx[name]
			do = disk_idx[name]
			path = TDNExt._diff_join(parent_path, name)
			changes = TDNExt._diff_own_fields(lo, do)
			if changes:
				entry = _entry(path, lo, 'op')
				entry['changed_keys'] = sorted(changes.keys())
				entry['changes'] = changes
				modified.append(entry)
			TDNExt._diff_level(lo.get('children', []) or [],
							   do.get('children', []) or [],
							   path, added, removed, modified, warnings)
			TDNExt._diff_annotations(lo.get('annotations', []) or [],
									 do.get('annotations', []) or [],
									 path, added, removed, modified, warnings)

	@staticmethod
	def _diff_header_warnings(live_raw, disk_raw):
		warnings = []
		lb = live_raw.get('td_build')
		db = disk_raw.get('td_build')
		if lb and db and lb != db:
			warnings.append(
				'TD build differs (disk %s vs live %s); round-trip may shift '
				'parameter defaults' % (db, lb))
		lv = live_raw.get('version')
		dv = disk_raw.get('version')
		if lv and dv and lv != dv:
			warnings.append('TDN format version differs (disk %s vs live %s)'
							% (dv, lv))
		return warnings

	@staticmethod
	def _diff_normalized(live_raw, disk_raw, comp_path='', file=None,
						 file_exists=True, baseline='disk',
						 max_changed_ops=200, max_bytes=60000):
		"""Semantic diff of two raw TDN documents.

		The first argument is the NEW side, the second is the OLD side, so
		per-field changes report old=<2nd>, new=<1st>. `baseline` labels what
		the OLD side is ('disk' = on-disk .tdn for live-vs-disk; 'head' = git
		HEAD for committed-vs-working). Normalizes both internally, compares the
		root COMP (as a pseudo-op), every operator (matched by name per level),
		and annotations. Returns the diff envelope. Pure -- no TD access;
		unit-testable with dicts.
		"""
		warnings = TDNExt._diff_header_warnings(
			live_raw if isinstance(live_raw, dict) else {},
			disk_raw if isinstance(disk_raw, dict) else {})
		live = TDNExt._normalize_tdn_for_compare(live_raw)
		disk = TDNExt._normalize_tdn_for_compare(disk_raw)

		added, removed, modified = [], [], []

		root_changes = TDNExt._diff_own_fields(
			live, disk, keys=TDNExt._DIFF_ROOT_CONTENT_KEYS)
		if root_changes:
			modified.append({
				'path': comp_path,
				'name': (comp_path.rstrip('/').rsplit('/', 1)[-1]
						 if comp_path else live.get('network_path', '')),
				'type': live.get('type') or disk.get('type'),
				'kind': 'root',
				'changed_keys': sorted(root_changes.keys()),
				'changes': root_changes})

		TDNExt._diff_annotations(live.get('annotations', []) or [],
								 disk.get('annotations', []) or [],
								 comp_path, added, removed, modified, warnings)
		TDNExt._diff_level(live.get('operators', []) or [],
						   disk.get('operators', []) or [],
						   comp_path, added, removed, modified, warnings)

		counts = {'added': len(added), 'removed': len(removed),
				  'modified': len(modified)}
		changed = bool(added or removed or modified)

		dropped = 0
		if max_changed_ops is not None:
			total = len(added) + len(removed) + len(modified)
			if total > max_changed_ops:
				budget = max_changed_ops
				modified = modified[:budget]
				budget -= len(modified)
				added = added[:budget]
				budget -= len(added)
				removed = removed[:budget]
				dropped = total - (len(added) + len(removed) + len(modified))

		envelope = {
			'schema_version': TDNExt._DIFF_SCHEMA_VERSION,
			'baseline': baseline,
			'comp_path': comp_path,
			'file': file,
			'file_exists': file_exists,
			'changed': changed,
			'counts': counts,
			'added': added,
			'removed': removed,
			'modified': modified,
			'truncated': {'ops': dropped > 0, 'dropped': dropped,
						  'bytes': 0, 'max_bytes': max_bytes},
			'warnings': warnings}

		body_stripped = False
		try:
			size = len(json.dumps(envelope, ensure_ascii=False))
		except (TypeError, ValueError):
			size = 0
		if max_bytes and size > max_bytes:
			for entry in envelope['modified']:
				if 'changes' in entry:
					del entry['changes']
					body_stripped = True
			try:
				size = len(json.dumps(envelope, ensure_ascii=False))
			except (TypeError, ValueError):
				pass
			if body_stripped:
				envelope['warnings'].append(
					'Output exceeded max_bytes; per-field change bodies omitted '
					'(changed_keys retained). Raise max_bytes or scope '
					'comp_path for full detail.')
		envelope['truncated']['ops'] = envelope['truncated']['ops'] or body_stripped
		envelope['truncated']['bytes'] = size
		return envelope

	def DiffLiveVsDisk(self, comp_path='/', max_changed_ops=200, max_bytes=60000):
		"""Diff a single TDN-externalized COMP: its live network vs the on-disk
		.tdn -- i.e. what is UNSAVED.

		This is the view git cannot provide: git only sees files on disk, never
		TouchDesigner's live in-memory network. A save rewrites the .tdn, so the
		result is empty right after saving. For committed/history diffs use git
		(the .tdn git diff driver keeps those clean); for every TDN COMP at once,
		use DiffAllLiveVsDisk.

		Read-only and non-interactive: the live export suppresses
		palette-handling prompts and never mutates TD state. Returns the diff
		envelope, or {'error': ...}.
		"""
		import os
		target = op(comp_path)
		if not target:
			return {'error': 'Operator not found: %s' % comp_path}
		try:
			rel = self.ownerComp.ext.Embody._getStrategyFilePath(comp_path, 'tdn')
		except Exception as e:
			return {'error': 'Failed to resolve .tdn path: %s' % e}
		if not rel:
			return {'error': '%s is not TDN-externalized (no .tdn file is '
							 'tracked for it)' % comp_path}
		try:
			abs_path = str(self.ownerComp.ext.Embody.buildAbsolutePath(
				self.ownerComp.ext.Embody.normalizePath(rel)))
		except Exception:
			abs_path = rel

		# Live export, non-interactive (no palette prompt, no par mutation),
		# vs the on-disk .tdn.
		prev = getattr(self, '_tdn_suppress_palette_prompt', False)
		self._tdn_suppress_palette_prompt = True
		try:
			live_res = self.ExportNetwork(root_path=comp_path, output_file=None)
		except Exception as e:
			self._tdn_suppress_palette_prompt = prev
			return {'error': 'Live export failed: %s' % e}
		self._tdn_suppress_palette_prompt = prev
		if not isinstance(live_res, dict) or 'tdn' not in live_res:
			return {'error': 'Live export failed: %s' % live_res}
		live_tdn = live_res['tdn']

		if not os.path.isfile(abs_path):
			return {'error': 'On-disk .tdn not found: %s' % abs_path,
					'comp_path': comp_path, 'file': abs_path,
					'file_exists': False}
		disk_tdn = self._read_existing_tdn(abs_path)
		if disk_tdn is None:
			return {'error': 'On-disk .tdn is missing or corrupt: %s' % abs_path,
					'comp_path': comp_path, 'file': abs_path,
					'file_exists': True}

		return TDNExt._diff_normalized(
			live_tdn, disk_tdn, comp_path=comp_path, file=abs_path,
			file_exists=True, baseline='disk',
			max_changed_ops=max_changed_ops, max_bytes=max_bytes)

	def DiffAllLiveVsDisk(self, max_comps=200, max_changed_ops=50,
						  max_bytes=60000):
		"""Project-wide unsaved diff: every live TDN-externalized COMP vs its
		on-disk .tdn. Answers "what has changed across the whole project that
		isn't saved yet."

		Returns a summary listing the CHANGED COMPs (each with its per-COMP diff
		envelope, capped by max_changed_ops/max_bytes), plus counts of clean and
		skipped COMPs. Rows whose COMP no longer exists live (e.g. stale table
		entries) are skipped without an export. For full detail on one COMP,
		call DiffLiveVsDisk(comp_path).

		Read-only and non-interactive. Returns {'error': ...} only on a
		top-level failure; per-COMP errors are collected under 'skipped'.
		"""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table:
				return {'error': 'Externalizations table not found'}
			headers = [table[0, c].val for c in range(table.numCols)]
			has_strategy = 'strategy' in headers
			tdn_paths = []
			for row in range(1, table.numRows):
				strat = (table[row, 'strategy'].val if has_strategy
						 else table[row, 'type'].val) or 'tox'
				if strat == 'tdn':
					tdn_paths.append(table[row, 'path'].val)
		except Exception as e:
			return {'error': 'Failed to read externalizations: %s' % e}

		changed, skipped = [], []
		clean_count = 0
		examined = 0
		truncated = False
		for comp_path in tdn_paths:
			# Skip rows whose COMP is not live -- can't diff (no live network).
			if op(comp_path) is None:
				skipped.append({'comp_path': comp_path, 'reason': 'not live'})
				continue
			if examined >= max_comps:
				truncated = True
				break
			examined += 1
			env = self.DiffLiveVsDisk(
				comp_path, max_changed_ops=max_changed_ops, max_bytes=max_bytes)
			if isinstance(env, dict) and env.get('error'):
				skipped.append({'comp_path': comp_path, 'reason': env['error']})
			elif env.get('changed'):
				changed.append(env)
			else:
				clean_count += 1

		return {
			'schema_version': TDNExt._DIFF_SCHEMA_VERSION,
			'baseline': 'disk',
			'scope': 'project',
			'changed_count': len(changed),
			'clean_count': clean_count,
			'skipped_count': len(skipped),
			'examined': examined,
			'changed': changed,
			'skipped': skipped,
			'truncated': {'comps': truncated, 'max_comps': max_comps},
		}

	# =========================================================================
	# PROMOTED METHODS (uppercase -- callable directly on op.Embody)
	# =========================================================================

	def ExportNetwork(self, root_path: str = '/', include_dat_content: Optional[bool] = None,
					  output_file: Optional[str] = None, max_depth: Optional[int] = None,
					  cleanup_protected: Optional[list[str]] = None,
					  embed_all: bool = False,
					  include_storage: Optional[bool] = None,
					  skip_cleanup: bool = False) -> dict[str, Any]:
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

		if include_storage is None:
			per_comp = root_op.fetch('embed_storage_in_tdn', None, search=False)
			if per_comp is not None:
				include_storage = per_comp
			else:
				include_storage = self.ownerComp.par.Embedstorageintdns.eval()

		options = {
			'include_dat_content': include_dat_content,
			'include_storage': include_storage,
			'max_depth': max_depth,
			'embed_all': embed_all,
		}

		try:
			self._defaults_cache.clear()
			self._exportable_cache.clear()
			self._seq_default_blocks_cache.clear()
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
				'source_file': project.name,
				'exported_at': datetime.now(timezone.utc).strftime(
					'%Y-%m-%dT%H:%M:%SZ'),
				'network_path': root_path,
				'options': {
					'include_dat_content': include_dat_content,
					'include_storage': include_storage,
				},
			}
			# Omit `build` when there is no build number (untracked /
			# portable networks -- e.g. a specimen with no TSV row and no
			# Build par). Matches the format's omit-when-absent philosophy
			# rather than emitting a noisy `build: null`.
			if tdn['build'] is None:
				del tdn['build']
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
			# Target COMP's own built-in/custom sequences (v1.3+)
			root_sequences = self._exportBuiltinSequences(root_op)
			if root_sequences:
				tdn['sequences'] = root_sequences

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
			if options.get('include_storage', True):
				root_storage = self._exportStorage(root_op)
				if root_storage:
					tdn['storage'] = root_storage
			else:
				# Preserve Embody control keys even when storage is excluded
				root_storage = self._exportStorage(root_op)
				control_keys = {k: v for k, v in root_storage.items()
								if k in ('embed_dats_in_tdn', 'embed_storage_in_tdn')}
				if control_keys:
					tdn['storage'] = control_keys

			tdn['operators'] = operators

			# Root-level annotations
			annotations = self._exportAnnotations(root_op)
			if annotations:
				tdn['annotations'] = annotations

			result = {'success': True, 'tdn': tdn}

			# Write to file if requested
			if output_file:
				# Scan from project folder -- TDN paths mirror TD hierarchy
				scan_folder = str(project.folder)
				filepath = self._resolveOutputPath(output_file, root_op)
				content = TDNExt._compact_json_dumps(tdn)

				# Stale-file cleanup scans the whole project folder with rglob,
				# which is hundreds of ms (the dominant checkpoint cost). Autosave
				# checkpoints pass skip_cleanup=True: a checkpoint re-writes ONE
				# COMP's .tdn and orphans nothing, so the scan is unnecessary on the
				# main thread. Orphans (from a removed child COMP) are reclaimed by
				# the continuity sweep / next full save; recovery is tsv-driven, so a
				# no-row orphan .tdn is ignored -- never resurrected.
				before_tdn = set()
				if not skip_cleanup:
					before_tdn = TDNExt._collectExistingTDNFiles(
						scan_folder, root_path)
					# Only files Embody tracks are deletion candidates --
					# never reclaim a stray the user placed themselves.
					before_tdn = self._restrictToTrackedTDN(before_tdn)

				write_result = TDNExt._safe_write_tdn(
					filepath, content, scan_folder)
				if not write_result.get('success'):
					return {'error':
						f'Safe write failed: {write_result.get("error")}'}

				if not skip_cleanup:
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

				# These warnings recursively scan descendants and can pop a modal
				# ui.messageBox -- never do that on a frequent autosave checkpoint
				# (skip_cleanup). Reserved for explicit user/save exports.
				if not skip_cleanup:
					# Locked non-DAT operators whose frozen content won't
					# survive a TDN round-trip.
					self._warnLockedNonDATs(root_op, context='export')
					# One-time warning for large monolithic TDN files.
					if not options.get('embed_all'):
						self._warnLargeTDN(filepath, root_path)

			return result

		except Exception as e:
			self._log(f'Export failed: {e}', 'ERROR')
			return {'error': f'Export failed: {e}'}
		finally:
			self._cleanupScanWorkspace()

	def ExportNetworkAsync(self, root_path: str = '/', include_dat_content: Optional[bool] = None,
						   output_file: Optional[str] = None, max_depth: Optional[int] = None,
						   embed_all: bool = False,
						   include_storage: Optional[bool] = None) -> None:
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
			'source_file': project.name,
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

		if include_storage is None:
			per_comp = root_op.fetch('embed_storage_in_tdn', None, search=False)
			if per_comp is not None:
				include_storage = per_comp
			else:
				include_storage = self.ownerComp.par.Embedstorageintdns.eval()

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
			# Only files Embody tracks are deletion candidates -- never
			# reclaim a stray the user placed themselves. Computed on the
			# main thread, BEFORE the write/track step, so a re-pathed
			# row's OLD file is still reclaimed.
			before_tdn = self._restrictToTrackedTDN(before_tdn)
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
				'include_storage': include_storage,
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
			starts -- scandir suffers extreme GIL contention from bg threads.
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
				'source_file': state['metadata'].get('source_file', ''),
				'exported_at': datetime.now(timezone.utc).strftime(
					'%Y-%m-%dT%H:%M:%SZ'),
				'network_path': state['root_path'],
				'options': {
					'include_dat_content':
						state['options']['include_dat_content'],
					'include_storage':
						state['options'].get('include_storage', True),
				},
			}
			# Omit `build` when absent (see sync path above) -- no noisy
			# `build: null` for untracked/portable networks.
			if tdn['build'] is None:
				del tdn['build']
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
			if root_meta.get('sequences'):
				tdn['sequences'] = root_meta['sequences']
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
				'Thread Manager at capacity -- export queued but may be '
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
					root_sequences = self._exportBuiltinSequences(root_op)
					if root_sequences:
						root_meta['sequences'] = root_sequences
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
					if state['options'].get('include_storage', True):
						root_storage = self._exportStorage(root_op)
						if root_storage:
							root_meta['storage'] = root_storage
					else:
						root_storage = self._exportStorage(root_op)
						control_keys = {
							k: v for k, v in root_storage.items()
							if k in ('embed_dats_in_tdn',
									 'embed_storage_in_tdn')}
						if control_keys:
							root_meta['storage'] = control_keys
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
		self._cleanupScanWorkspace()
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
		self._cleanupScanWorkspace()
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

	@staticmethod
	def _validateOpDefs(op_defs, path='operators'):
		"""Structural validation of an operators list, pre-import.

		Returns an error string describing the first malformed entry, or
		None when the structure is sound. Checks only SHAPE (every entry a
		mapping, every children value a list, recursively) -- per-field
		tolerance stays with the phases, which skip/warn per item. This
		runs before clear_first so a malformed document can never destroy
		children and then fail. Pure; unit-testable without TD.
		"""
		if not isinstance(op_defs, list):
			return f'{path} must be a list, got {type(op_defs).__name__}'
		for i, op_def in enumerate(op_defs):
			if not isinstance(op_def, dict):
				return (f'{path}[{i}] must be a mapping, got '
						f'{type(op_def).__name__}')
			children = op_def.get('children')
			if children is not None:
				err = TDNExt._validateOpDefs(
					children, f'{path}[{i}].children')
				if err:
					return err
		return None

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
			try:
				_file_newer = (tuple(int(x) for x in str(tdn_version).split('.'))
							   > tuple(int(x) for x in TDN_VERSION.split('.')))
			except Exception:
				_file_newer = (str(tdn_version) != TDN_VERSION)
			if _file_newer:
				self._log(
					f'TDN file is v{tdn_version}, newer than this build '
					f'(v{TDN_VERSION}); some content may not import', 'WARNING')

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

		if not isinstance(op_defs, list):
			msg = f'operators must be a list, got {type(op_defs).__name__}'
			ui.status = f'TDN Import: {msg}'
			return {'error': msg}

		# Structural validation BEFORE anything destructive. Hand-edited
		# .tdn files (an explicitly supported workflow) can carry stray
		# scalars or malformed nesting; the dict-walking pre-phases below
		# would raise on those, and with the old ordering that raise
		# landed AFTER clear_first had already destroyed the children --
		# leaving the COMP empty with no error result. Reject cheaply
		# here, while the network is still untouched.
		structure_error = TDNExt._validateOpDefs(op_defs)
		if structure_error:
			msg = f'Malformed TDN document: {structure_error}'
			ui.status = f'TDN Import: {msg}'
			self._log(msg, 'ERROR')
			return {'error': msg}

		# ------------------------------------------------------------------
		# PURE PRE-PHASES -- dict-only transforms that need no live network
		# state. All of these run BEFORE clear_first so any surprise in the
		# document can still abort the import with the children intact.
		# ------------------------------------------------------------------

		# Pre-phase: Resolve templates and merge type defaults
		if isinstance(tdn, dict):
			par_templates = tdn.get('par_templates', {})
			type_defaults = tdn.get('type_defaults', {})
			if par_templates and not isinstance(par_templates, dict):
				self._log(
					f'Ignoring malformed par_templates '
					f'({type(par_templates).__name__})', 'WARNING')
				par_templates = {}
			if type_defaults and not isinstance(type_defaults, dict):
				self._log(
					f'Ignoring malformed type_defaults '
					f'({type(type_defaults).__name__})', 'WARNING')
				type_defaults = {}
			if par_templates:
				TDNExt._resolve_par_templates(op_defs, par_templates)
			if type_defaults:
				TDNExt._merge_type_defaults(op_defs, type_defaults)

		# Pre-phase: Never overwrite a preserved excluded child. Excluded
		# COMPs survive the clear_first pass below; if a stale .tdn still
		# lists an op with the same name, drop that entry so the later
		# create/merge phases don't reuse and mutate the app-owned COMP.
		# Computed from live children BEFORE the clear -- the destroy pass
		# preserves exactly the excluded set, so the names are identical.
		excluded_names = {c.name for c in dest.children
						  if self._hasExcludeTag(c)}
		if excluded_names:
			before = len(op_defs)
			op_defs = [d for d in op_defs
					   if d.get('name') not in excluded_names]
			if len(op_defs) != before:
				self._log(
					f'Skipping {before - len(op_defs)} stale import '
					f'entry(ies) matching preserved excluded COMP(s) in '
					f'{target_path}', 'INFO')

		# Pre-phase: Skip children of nested TDN-externalized COMPs.
		# If a child COMP has its own .tdn entry in the externalizations table,
		# its own file is the source of truth -- not the parent's snapshot.
		tdn_paths = self._getTDNExternalizedPaths()
		if tdn_paths:
			tdn_paths.discard(target_path)  # We ARE importing this one
			if tdn_paths:
				skipped = self._stripNestedTDNChildren(
					op_defs, target_path, tdn_paths)
				for sp in skipped:
					self._log(
						f'Skipping children of {sp} -- has its own TDN '
						f'externalization (source of truth)', 'INFO')

		# Pre-phase: Skip children of nested TOX-externalized COMPs.
		# Same principle as TDN: the .tox file owns the child's internals.
		# Pre-fix .tdn files may still have these children embedded; strip
		# them so we don't write stale snapshots into the live network.
		tox_paths = self._getTOXExternalizedPaths()
		if tox_paths:
			tox_paths.discard(target_path)  # We ARE importing this one
		if tox_paths:
			skipped = self._stripNestedTOXChildren(
				op_defs, target_path, tox_paths)
			for sp in skipped:
				self._log(
					f'Skipping children of {sp} -- has its own TOX '
					f'externalization (source of truth)', 'INFO')

		# Cross-validate tdn_ref pointers against table and disk
		ref_warnings = self._validateTDNRefs(op_defs, target_path)
		for w in ref_warnings:
			self._log(w, 'WARNING')

		# Cross-validate tox_ref pointers against table and disk
		tox_ref_warnings = self._validateTOXRefs(op_defs, target_path)
		for w in tox_ref_warnings:
			self._log(w, 'WARNING')

		# ------------------------------------------------------------------
		# MUTATING SECTION -- from here on the live network is touched.
		# Everything below runs inside the error boundary so any failure
		# returns {'error': ...} (which reconstruction/post-save callers
		# use to trigger backup rollback) instead of escaping.
		# ------------------------------------------------------------------
		captured_externals = []
		try:
			# Capture external wires on dest's own connectors before clear
			# so they can be re-wired after the rebuild. When dest has no
			# live wires (cold open, or already-stripped comp during
			# post-save), fall back to wires stashed on dest via
			# comp.store() by StripCompChildren.
			if clear_first:
				try:
					captured_externals = self._captureExternalConnections(dest)
				except Exception as e:
					self._log(
						f'External capture failed on {target_path}: {e}', 'DEBUG')
				if not captured_externals:
					try:
						stashed = dest.fetch(
							'_tdn_external_wires', [], search=False)
						if stashed:
							captured_externals = list(stashed)
					except Exception:
						pass

			if clear_first:
				# Excluded COMPs are invisible to TDN -- the owning app owns
				# their lifecycle. Never destroy them during clear_first: they
				# are absent from the .tdn, so reconstruction would not recreate
				# them, making destruction permanent data loss.
				excluded_children = {
					c.path for c in dest.children if self._hasExcludeTag(c)}
				if excluded_children:
					self._log(
						f'Preserving {len(excluded_children)} excluded COMP(s) '
						f'during clear of {dest.path}', 'DEBUG')
				# Clear dock relationships pointing INTO the destroy set before
				# destroying -- TD's engine raises an uncatchable tdError if a
				# dock target is destroyed before its docked operator. This MUST
				# include a preserved excluded child docked to a soon-destroyed
				# sibling, else the preservation reintroduces that crash. Docks
				# between two preserved excluded children are left intact.
				for child in list(dest.children):
					try:
						if (child.dock is not None
								and child.dock.path not in excluded_children):
							child.dock = None
					except Exception:
						pass
				for child in list(dest.children):
					if child.path in excluded_children:
						continue
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

			# Phase 2.5: Expand built-in parameter sequences (v1.3+)
			self._expandSequences(dest, op_defs)

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

			# Phase 8.5: Restore TOX content for tox_ref shells.
			# _createOps deliberately leaves these empty so the .tox
			# file is the source of truth (not the parent .tdn snapshot).
			# Set externaltox from the ref string and call _reloadTox so
			# the child's internals are present immediately after import,
			# without waiting for the next project open.
			self._restoreTOXShells(dest)

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
			# recreating extension source DATs) has already happened --
			# this overwrites any defaults the extension set.
			if isinstance(tdn, dict):
				# Type validation (v1.1+) -- warn if destination type differs
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

				# Built-in parameter sequences (v1.3+)
				tdn_sequences = tdn.get('sequences', {})
				for seq_name, blocks in tdn_sequences.items():
					try:
						seq = self._getSequenceByName(dest, seq_name)
						seq.numBlocks = len(blocks)
						for i, block_data in enumerate(blocks):
							if not block_data:
								continue
							block = seq[i]
							for base_name, value in block_data.items():
								par = getattr(block.par, base_name, None)
								if par is None:
									try:
										par = block.par[base_name]
									except Exception:
										par = None
								if par is not None:
									self._setParValue(dest, par.name, value)
					except Exception as e:
						self._log(
							f'Failed to set sequence {seq_name} on '
							f'{dest.path}: {e}', 'WARNING')

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

			# Restore external connections captured before clear.
			# Also consume any stashed wires on dest.
			ext_restored = 0
			if captured_externals:
				try:
					ext_restored = self._restoreExternalConnections(
						dest, captured_externals)
				except Exception as e:
					self._log(
						f'External restore failed on {target_path}: {e}',
						'WARNING')
			try:
				dest.unstore('_tdn_external_wires')
			except Exception:
				pass

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
			if ext_restored:
				result['restored_external_connections'] = ext_restored
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
			ui.status = f'TDN Import: File not found -- {file_path}'
			return {'error': f'TDN file not found: {file_path}'}

		try:
			with open(file_path, 'r', encoding='utf-8') as f:
				tdn_data = tdn_load(f.read())
		except Exception as e:
			self._log(f'Invalid TDN file: {e}', 'ERROR')
			ui.status = f'TDN Import: Invalid TDN -- {e}'
			return {'error': f'Invalid TDN file: {e}'}

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

		# Keys that carry no user-meaningful data -- operators with only
		# these keys are auto-created defaults (e.g. torus1 inside a
		# geoCOMP) that TD recreates automatically on COMP creation.
		_TRIVIAL_KEYS = {'name', 'type', 'position', 'size'}

		result = []
		for child in children:
			# Skip system/internal paths (exact match or children)
			if child.path in SYSTEM_PATHS or child.path.startswith(
					_SYSTEM_PATH_PREFIXES):
				continue

			# Annotations (annotateCOMP) are captured exclusively by the
			# dedicated `annotations:` section via _exportAnnotations -- a
			# compact title/text/box form. Serializing them here too would
			# double-capture the same COMP and (because _exportCustomPars
			# emits ALL of an annotate's custom pars, default or not) dump
			# ~180 lines of Opviewer*/Body* noise per annotation for zero
			# added fidelity. The import rebuilds them from `annotations:`
			# (Phase 7a), so the op-list entry is pure dead weight.
			if child.type == 'annotate':
				continue

			# Skip COMPs tagged for exclusion -- the whole subtree is
			# invisible to TDN (no inline export, no tdn_ref). The owning
			# application manages their lifecycle.
			#
			# Exclusion is honored ONLY for direct children (depth 0) of the
			# exported boundary, because the strip/clear passes only preserve
			# direct children. A nested excluded COMP (depth > 0) is NOT
			# preserved by those passes, so if we also skipped it here the
			# .tdn would omit it while strip destroyed it -- permanent data
			# loss. Instead we serialize a nested excluded COMP as ordinary
			# content (it round-trips and survives), and warn that the tag had
			# no effect at this depth.
			if self._hasExcludeTag(child):
				if depth == 0:
					continue
				self._log(
					f'Excluded COMP {child.path} is nested under a '
					f'non-excluded COMP -- whole-subtree exclusion only '
					f'applies to direct children of a TDN boundary. It will '
					f'be serialized as normal content. Tag the intervening '
					f'COMP(s), or make it a direct child, to exclude it.',
					'WARNING')

			if child.name in skip:
				continue

			op_data = self._exportSingleOp(child, options, depth)
			if op_data is not None:
				# Skip bare auto-created defaults -- TD recreates these
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
		# Backstop: a COMP tagged for exclusion is invisible to TDN, but ONLY
		# when it is a direct child of the boundary (depth 0) -- the strip/
		# clear passes only preserve direct children, so a nested excluded
		# COMP must be serialized as normal content or it is lost. This guards
		# direct and async callers of the depth-0 case; _exportChildren
		# already handles the depth>0 warning.
		if depth == 0 and self._hasExcludeTag(target):
			return None
		data = {
			'name': target.name,
			'type': target.OPType,
		}

		# Parameters (built-in, non-default only)
		params = self._exportBuiltinParams(target)
		if params:
			data['parameters'] = params

		# Built-in parameter sequences (v1.3+)
		sequences = self._exportBuiltinSequences(target)
		if sequences:
			data['sequences'] = sequences

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
		if options.get('include_storage', True):
			storage = self._exportStorage(target)
			if storage:
				data['storage'] = storage
		else:
			# Preserve Embody control keys even when storage is excluded
			storage = self._exportStorage(target)
			control_keys = {k: v for k, v in storage.items()
							if k in ('embed_dats_in_tdn', 'embed_storage_in_tdn')}
			if control_keys:
				data['storage'] = control_keys

		# Operator connections (left/right wires)
		connections = self._exportConnections(target)
		if connections:
			data['inputs'] = connections

		# COMP connections (top/bottom wires)
		if hasattr(target, 'inputCOMPConnectors'):
			comp_conns = self._exportCompConnections(target)
			if comp_conns:
				data['comp_inputs'] = comp_conns

		# DAT content -- include when the include_dat_content option is True,
		# OR when the DAT lives inside an animationCOMP (keys, channels, graph,
		# attributes tableDATs hold all keyframe data -- must always be saved).
		# Skip content for read-only DATs (e.g. glsl1_info, popto1) --
		# TD auto-generates their content and rejects writes on import.
		if target.family == 'DAT' and (
				options.get('include_dat_content', True) or
				self._isInsideAnimationCOMP(target)):
			if self._isDATEditable(target):
				content_data = self._exportDATContent(target)
				if content_data:
					data.update(content_data)
			else:
				data['dat_read_only'] = True

		# Emit child-reference metadata for COMPs whose contents are
		# managed by a separate file (TDN/TOX externalization, or a
		# palette clone). This runs even when recurse=False (async
		# modular export) so the resulting shell carries a tdn_ref /
		# tox_ref / palette_clone marker instead of an unmarked empty
		# COMP. Without this, async exports of a parent containing
		# externalized children produce shells that look like normal
		# leaf COMPs and the importer cannot tell them apart.
		if hasattr(target, 'children'):
			is_palette = self._isPaletteClone(target)
			handling = self._resolvePaletteHandling(target) if is_palette else None
			if is_palette and handling == 'blackbox':
				data['palette_clone'] = True
				# For palette clones, the correct comparison baseline
				# is the clone source's values, not p.default.
				# _exportBuiltinParams uses p.default, which can differ
				# from the clone source (e.g. buttontype: p.default is
				# "momentary" but clone source is "toggledown"). This
				# causes user-set values that match p.default to be
				# silently dropped from the export, then lost on rebuild.
				# Fix: re-check against clone source, add any diffs.
				clone_source_params = self._getCloneSourceDiffs(target)
				if clone_source_params:
					if 'parameters' not in data:
						data['parameters'] = {}
					data['parameters'].update(clone_source_params)
				# Strip clone/enablecloning -- TD plumbing that
				# parent.create() auto-sets on rebuild.
				if 'parameters' in data:
					for skip_par in _PALETTE_CLONE_SKIP_PARAMS:
						data['parameters'].pop(skip_par, None)
					if not data['parameters']:
						del data['parameters']
			elif self._hasTDNTag(target) and not options.get('embed_all'):
				# Child's network managed by its own .tdn file.
				# Write a tdn_ref pointer for cross-validation.
				tdn_ref = self._resolveTDNRef(target)
				if tdn_ref:
					data['tdn_ref'] = tdn_ref
			elif self._hasTOXTag(target) and not options.get('embed_all'):
				# Child's network managed by its own .tox file.
				# Write a tox_ref pointer for cross-validation.
				# The .tox is opaque (binary); editing it directly in TD
				# updates the file on save. The TDN exporter must not
				# recurse into the child or its internals would be
				# duplicated into the parent .tdn, defeating the
				# externalization. Use TDN strategy for git-diffable
				# nesting; use TOX for opaque encapsulation.
				tox_ref = self._resolveTOXRef(target)
				if tox_ref:
					data['tox_ref'] = tox_ref
			elif recurse:
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

	# =====================================================================
	# Divergent defaults - correct for p.default lying
	# =====================================================================

	def _loadDivergentDefaults(self):
		"""Load creation defaults, checking sources in priority order:

		1. CatalogManager (already populated from .embody/ catalog file)
		2. Embedded divergent_defaults tableDAT (bootstrap for known builds)
		3. Empty dict (on-the-fly fallback handles unknown types)
		"""
		self._divergent_loaded = True

		# Priority 1: CatalogManager may have already populated us
		if self._divergent_defaults:
			return

		self._divergent_defaults = {}

		# Priority 2: Try loading from .embody/ catalog file
		import json, os
		build_str = f'{app.version}.{app.build}'
		try:
			catalog_mgr = self.ownerComp.ext.CatalogManager
			catalog_path = catalog_mgr._getCatalogPath(build_str)
			if os.path.isfile(catalog_path):
				catalog = catalog_mgr._readCatalog(catalog_path)
				if catalog:
					self._divergent_defaults = catalog
					self._log(
						f'Loaded catalog from .embody/ for build '
						f'{build_str} ({len(catalog)} types)', 'DEBUG')
					return
		except Exception:
			pass

		# Priority 3: Fall back to embedded tableDAT
		table = self.ownerComp.op('divergent_defaults')
		if table is None or table.numRows < 2:
			return

		headers = [table[0, c].val for c in range(table.numCols)]
		build_cols = headers[3:]  # Skip op_type, par_name, style

		if build_str in build_cols:
			col_name = build_str
		elif build_cols:
			col_name = build_cols[-1]
			self._log(
				f'Divergent defaults: no column for build {build_str}, '
				f'using {col_name}', 'DEBUG')
		else:
			return

		col_idx = headers.index(col_name)

		for row_idx in range(1, table.numRows):
			op_type = table[row_idx, 0].val
			par_name = table[row_idx, 1].val
			style = table[row_idx, 2].val
			val_str = table[row_idx, col_idx].val

			if not val_str:
				continue

			val = self._deserializeDivergentValue(val_str, style)

			if op_type not in self._divergent_defaults:
				self._divergent_defaults[op_type] = {}
			self._divergent_defaults[op_type][par_name] = val

		self._log(
			f'Loaded divergent defaults from tableDAT: '
			f'{len(self._divergent_defaults)} op types from column '
			f'{col_name}', 'DEBUG')

	@staticmethod
	def _deserializeDivergentValue(val_str, style):
		"""Convert a stored divergent default string back to a typed value."""
		if style in ('Float', 'XY', 'XYZ', 'XYZW', 'UV', 'UVW', 'WH',
					 'RGB', 'RGBA'):
			try:
				return float(val_str)
			except ValueError:
				return val_str
		if style == 'Int':
			try:
				return int(val_str)
			except ValueError:
				return val_str
		if style == 'Toggle':
			return val_str == 'True'
		return val_str

	def _getDivergentDefaults(self, op_type):
		"""Get divergent defaults for an op type, loading if needed.

		Returns a dict of {par_name: creation_value} for params where
		p.default lies, or an empty dict if none.

		If the table loaded successfully, a missing op_type means "no
		divergent defaults for this type" - return {} without probing.
		On-the-fly probing only runs when the table has no data at all
		(missing DAT, empty table, or no build columns).
		"""
		if not self._divergent_loaded:
			self._loadDivergentDefaults()
		# If table loaded, trust it: missing type = no divergences
		if self._divergent_defaults:
			return self._divergent_defaults.get(op_type, {})
		# No table data - fall back to on-the-fly probing
		return self._getCreationValueOnTheFly(op_type)

	def _getCreationValueOnTheFly(self, op_type):
		"""Create a temp op, find params where val != default, cache result.

		This is the fallback when the divergent_defaults table doesn't
		have a column for the current TD build.
		"""
		if op_type in self._runtime_creation_cache:
			return self._runtime_creation_cache[op_type]

		vals = {}
		try:
			if self._scan_workspace is None:
				self._scan_workspace = self.ownerComp.create(
					baseCOMP, '_defaults_workspace')
				self._scan_workspace.viewer = False

			import td as _td
			cls = getattr(_td, op_type, None)
			if cls is not None:
				temp = self._scan_workspace.create(cls, '_probe')
				for p in temp.pars():
					if p.isCustom or p.readOnly or p.sequence is not None:
						continue
					if p.name in SKIP_PARAMS:
						continue
					if p.style in SKIP_BUILTIN_STYLES:
						continue
					try:
						if p.val != p.default:
							# Skip name-dependent values
							if '_probe' not in str(p.val):
								vals[p.name] = p.val
					except Exception:
						pass
				temp.destroy()
		except Exception as e:
			self._log(
				f'On-the-fly default probe failed for {op_type}: {e}',
				'DEBUG')

		self._runtime_creation_cache[op_type] = vals
		return vals

	def _getCreationFlagDefaults(self, target):
		"""Per-OPType creation values for the DEFAULT_FLAGS set.

		TD's flag defaults vary by operator type: a fresh geometryCOMP has
		render=True and display=True, a fresh cameraCOMP has display=True
		(verified live 2026-07-03), while most families match the global
		DEFAULT_FLAGS table. Comparing every op against DEFAULT_FLAGS made
		"render off" on an object COMP look like the default -- it was
		omitted from export and came back ON after a round-trip (hidden
		scene objects reappearing in renders).

		Probes one throwaway instance per OPType (same pattern and
		workspace as _getCreationValueOnTheFly), cached for the extension
		lifetime. Falls back to DEFAULT_FLAGS on any probe failure.
		"""
		op_type = target.OPType
		cached = self._flag_defaults_cache.get(op_type)
		if cached is not None:
			return cached

		defaults = dict(DEFAULT_FLAGS)
		try:
			if self._scan_workspace is None or not self._scan_workspace.valid:
				self._scan_workspace = self.ownerComp.create(
					baseCOMP, '_defaults_workspace')
				self._scan_workspace.viewer = False
			import td as _td
			cls = getattr(_td, op_type, None)
			if cls is not None:
				temp = self._scan_workspace.create(cls, '_flagprobe')
				try:
					for flag_name in DEFAULT_FLAGS:
						if flag_name == 'allowCooking' and not temp.isCOMP:
							continue
						try:
							defaults[flag_name] = bool(getattr(temp, flag_name))
						except Exception:
							pass
				finally:
					temp.destroy()
		except Exception as e:
			self._log(
				f'Creation flag-default probe failed for {op_type}: {e}',
				'DEBUG')

		self._flag_defaults_cache[op_type] = defaults
		return defaults

	def _cleanupScanWorkspace(self):
		"""Destroy the on-the-fly scan workspace if it exists."""
		if self._scan_workspace is not None:
			try:
				self._scan_workspace.destroy()
			except Exception:
				pass
			self._scan_workspace = None

	def _getCreationDefault(self, op_type, par_name, par):
		"""Get the true creation default for a parameter.

		Checks the divergent defaults catalog first, falls back to p.default.
		"""
		divergent = self._getDivergentDefaults(op_type)
		if par_name in divergent:
			return divergent[par_name]
		return par.default

	def _buildParCache(self, target):
		"""Build per-OPType cache of exportable parameter names and defaults.

		On the first operator of each OPType, we iterate all parameters and
		record which are exportable (non-custom, non-readOnly except `file`, non-skip) and
		their default values. Subsequent operators of the same type skip all
		those per-parameter attribute checks (isCustom, readOnly, style) and
		default lookups -- replacing ~4 Python-to-C++ bridge calls per parameter
		with a single Python dict lookup.

		Uses the divergent defaults catalog to correct for params where
		TD's p.default doesn't match the actual creation value (e.g.
		cameraCOMP tz: p.default=0 but creation value is 5).
		"""
		op_type = target.OPType
		divergent = self._getDivergentDefaults(op_type)
		exportable = {}
		defaults = {}
		for p in target.pars():
			if p.isCustom:
				continue
			if p.sequence is not None:
				continue
			if p.readOnly and p.name != 'file':
				continue
			if p.name in SKIP_PARAMS:
				continue
			if p.style in SKIP_BUILTIN_STYLES:
				continue
			exportable[p.name] = True
			defaults[p.name] = divergent.get(p.name, p.default)
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

	def _exportBuiltinSequences(self, target):
		"""Export built-in parameter sequences with non-default block data.

		Discovers all sequences on the operator via par.isSequence headers,
		then exports each sequence's blocks as an array of base-name dicts.

		Returns dict of {seq_name: [block_data, ...]} or empty dict.
		Only includes sequences where numBlocks differs from default OR
		any block parameter has a non-default value.
		"""
		sequences = {}
		seen = set()

		for p in target.pars():
			if not p.isSequence:
				continue
			seq = p.sequence
			if seq is None or seq.name in seen:
				continue
			seen.add(seq.name)

			seq_data = self._exportSequenceBlocks(target, seq)
			if seq_data is not None:
				sequences[seq.name] = seq_data

		return sequences

	def _exportSequenceBlocks(self, target, seq):
		"""Export blocks for a single sequence.

		Returns list of block dicts ({base_name: value}), or None if
		the sequence is entirely at defaults and can be omitted.

		Note: TD creates new wrapper objects for p.sequenceBlock on each
		access, so identity (``is``) and equality (``==``) comparisons
		fail. We compare by block index instead.
		"""
		# Group sequence parameters by block index
		block_pars = {}  # {block_index: [par, ...]}
		for p in target.pars():
			if p.sequence is None or p.sequence.name != seq.name:
				continue
			if p.isSequence:
				continue  # Skip the header par
			sb = p.sequenceBlock
			if sb is None:
				continue
			idx = sb.index
			block_pars.setdefault(idx, []).append(p)

		blocks = []
		has_any_nondefault = False

		for block in seq.blocks:
			block_data = {}
			for p in block_pars.get(block.index, []):
				base_name = self._getSequenceBaseName(p, seq)
				value = self._getParValue(p)

				if value is not None:
					creation_default = self._getCreationDefault(
						target.OPType, p.name, p)
					default = self._serializeValue(creation_default)
					if self._valuesDiffer(value, default):
						block_data[base_name] = value
						has_any_nondefault = True

			blocks.append(block_data)

		default_count = self._getDefaultSequenceBlockCount(target, seq)

		if len(blocks) == default_count and not has_any_nondefault:
			return None

		return blocks

	@staticmethod
	def _getSequenceBaseName(par, seq):
		"""Extract base name from a sequence parameter's full name.

		E.g., 'comb2oper' with seq.name='comb' -> 'oper'
		"""
		after_prefix = par.name[len(seq.name):]
		return after_prefix.lstrip('0123456789')

	def _getDefaultSequenceBlockCount(self, target, seq):
		"""Get default numBlocks for a sequence on this op type.

		Cached per (OPType, seq_name). Defaults to 1 -- most built-in
		sequences start with 1 block. The worst case of a wrong default
		is a redundant [{}] in the TDN output (harmless).
		"""
		cache_key = (target.OPType, seq.name)
		if cache_key not in self._seq_default_blocks_cache:
			self._seq_default_blocks_cache[cache_key] = 1
		return self._seq_default_blocks_cache[cache_key]

	def _exportCustomPars(self, target):
		"""Export ALL custom parameters grouped by page.

		Returns a dict keyed by page name, where each value is a list of
		parameter definitions (without the 'page' field -- the key IS the page).

		For custom sequences: only the sequence header and the block 0 template
		parameters are exported as custom par definitions. Per-block instance
		parameters (block index > 0) are skipped -- their values are stored in
		the operator's `sequences` key by `_exportBuiltinSequences`. The
		template par's `name` field is normalized to its base name (the
		original capitalized form, e.g. `Itemlabel` instead of `Items0itemlabel`)
		so import can call `page.appendStr('Itemlabel')` correctly.
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

				# Skip per-block instance parameters (block index > 0).
				# Only block 0 represents the template; the rest are
				# auto-generated by TD's sequence machinery.
				sb = p.sequenceBlock
				if sb is not None and sb.index > 0:
					seen_names.add(p.name)
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
					# For sequence template pars, normalize the name to its
					# base form (strip "{seqName}0" prefix). The base name
					# must start with an uppercase letter for appendStr/etc.
					if sb is not None and sb.index == 0 and p.sequence is not None:
						base = self._getSequenceBaseName(p, p.sequence)
						# Capitalize first letter to satisfy TD's naming
						if base:
							base = base[0].upper() + base[1:]
							par_def['name'] = base
							par_def['sequence'] = p.sequence.name
							# Sequence template values are stored in
							# the `sequences` key, not `value` here
							par_def.pop('value', None)
							par_def.pop('values', None)
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

		# Label -- only if different from name
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
				# Dynamically populated -- store the source, not the entries
				par_def['menuSource'] = first_par.menuSource
			else:
				# Manually defined -- store entries
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

		# Current values -- only if different from default
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
		Example: ['viewer', 'display'] or ['-expose', '-render']

		Defaults are per-OPType creation values (_getCreationFlagDefaults),
		not the global DEFAULT_FLAGS table -- object COMPs create with
		render/display ON, so only a per-type baseline round-trips a
		user's render-off/display-off correctly.
		"""
		flags = []
		defaults = self._getCreationFlagDefaults(target)
		for flag_name, default_val in defaults.items():
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
			if abs(alpha - 1.0) > 1e-6 and alpha > 0:
				data['backAlpha'] = round(alpha, 4)

			titleHeight = ann.par.Titleheight.eval()
			if abs(titleHeight - 30) > 1e-6 and titleHeight > 0:
				data['titleHeight'] = titleHeight

			bodyFontSize = ann.par.Bodyfontsize.eval()
			if abs(bodyFontSize - 10) > 1e-6 and bodyFontSize > 0:
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

	def _captureExternalConnections(self, comp) -> list:
		"""Capture sibling<->comp wires on comp's own connectors.

		Records wires going INTO comp's input connectors (from external
		siblings) and wires going OUT of comp's output connectors (to
		external siblings). Used to preserve external connections across
		strip/rebuild cycles where the internal in*/out* operators that
		define comp's connectors are destroyed and recreated.

		Returns a list of dicts with: direction ('input'|'output'),
		kind ('op'|'comp'), local_index, remote, remote_index.
		Returns [] when there's nothing to capture.
		"""
		parent = comp.parent()
		if not parent:
			return []
		conns = []

		def _rel(other):
			try:
				return other.name if other.parent() == parent else other.path
			except Exception:
				return other.path

		def _find_remote_index(remote_op, target_comp, remote_attr):
			try:
				for ri, r_conn in enumerate(getattr(remote_op, remote_attr, [])):
					for rc in r_conn.connections:
						if rc.owner is target_comp:
							return ri
			except Exception:
				pass
			return 0

		# INPUTS: walk comp's own input connectors.
		# conn.owner on an input connector yields the source (remote) op.
		for kind, local_attr, remote_out_attr in (
				('op', 'inputConnectors', 'outputConnectors'),
				('comp', 'inputCOMPConnectors', 'outputCOMPConnectors')):
			try:
				for i, connector in enumerate(getattr(comp, local_attr, [])):
					for c in connector.connections:
						src = c.owner
						if src is comp:
							continue
						conns.append({
							'direction': 'input',
							'kind': kind,
							'local_index': i,
							'remote': _rel(src),
							'remote_index': _find_remote_index(
								src, comp, remote_out_attr),
						})
			except Exception as e:
				self._log(
					f'External capture ({local_attr}) error on '
					f'{comp.path}: {e}', 'DEBUG')

		# OUTPUTS: walk comp's own output connectors.
		# conn.owner on an output connector yields the destination (remote) op.
		for kind, local_attr, remote_in_attr in (
				('op', 'outputConnectors', 'inputConnectors'),
				('comp', 'outputCOMPConnectors', 'inputCOMPConnectors')):
			try:
				for i, connector in enumerate(getattr(comp, local_attr, [])):
					for c in connector.connections:
						dst = c.owner
						if dst is comp:
							continue
						conns.append({
							'direction': 'output',
							'kind': kind,
							'local_index': i,
							'remote': _rel(dst),
							'remote_index': _find_remote_index(
								dst, comp, remote_in_attr),
						})
			except Exception as e:
				self._log(
					f'External capture ({local_attr}) error on '
					f'{comp.path}: {e}', 'DEBUG')

		return conns

	def _restoreExternalConnections(self, comp, conns) -> int:
		"""Restore captured external connections. Returns count restored.

		Tolerant of missing/renamed remote ops and connector count changes.
		Logs WARNING and skips individual wires on failure; never raises.
		"""
		if not conns:
			return 0
		parent = comp.parent()
		if not parent:
			return 0

		def _resolve(ref):
			o = parent.op(ref)
			if o:
				return o
			return op(ref)

		restored = 0
		for entry in conns:
			try:
				direction = entry.get('direction')
				kind = entry.get('kind', 'op')
				local_idx = entry.get('local_index', 0)
				remote_ref = entry.get('remote')
				remote_idx = entry.get('remote_index', 0)

				remote = _resolve(remote_ref) if remote_ref else None
				if not remote:
					self._log(
						f'External restore: remote op not found: '
						f'{remote_ref} ({direction} {kind}[{local_idx}] '
						f'on {comp.path})', 'WARNING')
					continue

				if direction == 'input':
					local_attr = ('inputConnectors' if kind == 'op'
									else 'inputCOMPConnectors')
					remote_attr = ('outputConnectors' if kind == 'op'
									else 'outputCOMPConnectors')
					local_conns = getattr(comp, local_attr, [])
					remote_conns = getattr(remote, remote_attr, [])
					if (local_idx >= len(local_conns)
							or remote_idx >= len(remote_conns)):
						self._log(
							f'External restore: connector index out of '
							f'range for {remote_ref}[{remote_idx}] -> '
							f'{comp.name}[{local_idx}] ({kind})', 'WARNING')
						continue
					remote_conns[remote_idx].connect(local_conns[local_idx])
					restored += 1
				elif direction == 'output':
					local_attr = ('outputConnectors' if kind == 'op'
									else 'outputCOMPConnectors')
					remote_attr = ('inputConnectors' if kind == 'op'
									else 'inputCOMPConnectors')
					local_conns = getattr(comp, local_attr, [])
					remote_conns = getattr(remote, remote_attr, [])
					if (local_idx >= len(local_conns)
							or remote_idx >= len(remote_conns)):
						self._log(
							f'External restore: connector index out of '
							f'range for {comp.name}[{local_idx}] -> '
							f'{remote_ref}[{remote_idx}] ({kind})', 'WARNING')
						continue
					local_conns[local_idx].connect(remote_conns[remote_idx])
					restored += 1
			except Exception as e:
				self._log(
					f'External restore error on {comp.path}: {entry}: {e}',
					'WARNING')

		if restored:
			self._log(
				f'Restored {restored} external connection(s) on {comp.path}',
				'INFO')
		return restored

	def _isDATEditable(self, dat_op):
		"""Test whether a DAT's content is writable, WITHOUT mutating it.

		Some auto-created companion DATs (e.g. glsl1_info, popto1) are
		read-only -- TD auto-generates their content and rejects writes
		with "The operator is not editable". This check is per-instance
		(not per-OPType) because read-only companions share OPType
		'textDAT' with regular editable text DATs.

		Uses TD's native `DAT.isEditable`. The previous write-probe
		(`dat.text = dat.text`) CORRUPTED live table DATs: a table's
		`.text` is its tab/newline-delimited flattening, which cannot
		represent cells containing embedded newlines or tabs, so writing
		it back destroyed those characters in the live network on every
		export (verified 2026-07-03). isEditable matches the old probe's
		semantics exactly -- True for locked DATs (content still exports),
		False for wired and auto-generated DATs -- with zero side effects.

		Fail-open on any error: the import side already downgrades
		"not editable" write failures gracefully, while returning False
		here would silently drop user content from the export.
		"""
		try:
			return bool(dat_op.isEditable)
		except Exception:
			return True

	# TD's default compute-shader template, used as a FALLBACK only when live
	# capture fails. Live capture (below) is authoritative -- TD's default could
	# drift across builds. Note the literal tab before 'vec4 color;'.
	_DEFAULT_COMPUTE_SHADER_FALLBACK = (
		'// Example Compute Shader\n\n'
		'// uniform float exampleUniform;\n\n'
		'layout (local_size_x = 8, local_size_y = 8) in;\n'
		'void main()\n'
		'{\n'
		'\tvec4 color;\n'
		'\t//color = texelFetch(sTD2DInputs[0], ivec2(gl_GlobalInvocationID.xy), 0);\n'
		'\tcolor = vec4(1.0);\n'
		'\t// We need to use TDImageStoreOutput() so that 8-bit textures that are sRGB\n'
		'\t// encoded can be written to correctly from incoming linear values.\n'
		'\t// imageStore() does not do this automatically, while pixel shader outputs do.\n'
		'\tTDImageStoreOutput(0, gl_GlobalInvocationID, color);\n'
		'}\n'
	)

	def _defaultComputeShaderText(self):
		"""Return TD's LIVE default compute-shader text, captured once and cached.

		Creates a throwaway glslTOP, reads its docked <name>_compute DAT text,
		destroys it, and caches the result on the instance. Falls back to a
		hardcoded literal only if live capture fails (e.g. headless tests).
		"""
		cached = getattr(self, '_default_compute_text', None)
		if cached is not None:
			return cached
		text = None
		throwaway = None
		try:
			throwaway = self.ownerComp.create(glslTOP)
			compute = throwaway.op(f'{throwaway.name}_compute')
			if compute is not None:
				text = compute.text
		except Exception as e:
			self._log(f'Live compute-shader default capture failed: {e}', 'DEBUG')
		finally:
			try:
				if throwaway is not None:
					throwaway.destroy()
			except Exception:
				pass
		if text is None:
			text = self._DEFAULT_COMPUTE_SHADER_FALLBACK
		self._default_compute_text = text
		return text

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
				# Boilerplate omission: a docked compute companion DAT whose
				# text is TD's default compute-shader template carries no
				# information -- TD auto-recreates it on glsl op create/import,
				# and _setDATContent only writes when dat_content is PRESENT.
				# Omitting it shrinks the file and removes the only tab-bearing
				# strings (so every surviving shader stays a clean | block).
				if (target.family == 'DAT' and target.dock is not None
						and target.name == f'{target.dock.name}_compute'
						and text == self._defaultComputeShaderText()):
					return None  # omit; TD recreates this exact default on glsl create
				if text:
					# v2.0: store the plain string; YAML's literal block scalar (|)
					# renders multi-line scripts readably and diffs line-by-line.
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
		are reused rather than duplicated -- if an operator with the target
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
			# rampTOP keys) -- but only if the op was NOT present before
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
				# TD ignores the name param for some palette types
				# (e.g. annotateCOMP). Explicitly rename to match the TDN.
				if new_op.name != name:
					try:
						new_op.name = name
					except Exception:
						self._log(
							f'Operator "{name}" auto-named to '
							f'"{new_op.name}"', 'WARNING')
				created.append(new_op.path)
				op_def['_created_op'] = new_op
			except Exception as e:
				self._log(
					f'Failed to create {op_type} "{name}": {e}', 'WARNING')
				continue

			# Recurse into children for COMPs
			children = op_def.get('children', [])
			tdn_ref = op_def.get('tdn_ref')
			tox_ref = op_def.get('tox_ref')
			if tdn_ref and new_op.isCOMP:
				# This COMP's children come from a separate .tdn file.
				# Shell created here; network populated by
				# ReconstructTDNComps() in depth-sorted order.
				self._log(
					f'Skipping children of {new_op.path} -- '
					f'managed by {tdn_ref}', 'DEBUG')
			elif tox_ref and new_op.isCOMP:
				# This COMP's contents come from a separate .tox file.
				# Shell created here; the .tox load happens via
				# RestoreTOXComps (frame 45 on project open) or the
				# externalizations-table reconciliation pass after
				# import. Setting `externaltox` + calling loadTox here
				# is intentionally NOT done: doing so during a parent
				# import would conflict with RestoreTOXComps\' own pass
				# at frame 45 (it loads the .tox and sets externaltox),
				# and TDN reconstruction (frame 60) runs AFTER that --
				# so the .tox would be loaded, then wiped by
				# clear_first=True. Instead we mark the shell with
				# storage so a post-import pass can re-trigger restore.
				try:
					new_op.store('_pending_tox_restore', tox_ref)
				except Exception:
					pass
				self._log(
					f'Skipping children of {new_op.path} -- '
					f'managed by {tox_ref}', 'DEBUG')
			elif children and new_op.isCOMP:
				# Clear auto-created default children (e.g. torus1
				# inside a geometryCOMP) before importing TDN children.
				# These defaults aren't in the TDN (filtered by
				# _TRIVIAL_KEYS on export) so they'd persist alongside
				# the intended children if not removed here.
				for default_child in list(new_op.children):
					try:
						default_child.destroy()
					except Exception:
						pass
				self._createOps(new_op, children, created)

	def _createCustomPars(self, parent, op_defs):
		"""Phase 2: Create custom parameters on all operators."""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			custom_pars = op_def.get('custom_pars', {})
			if custom_pars and target.isCOMP:
				# Palette clones already have their custom parameters from
				# the clone source. Replacing them with appendXXX(replace=True)
				# destroys the internal parameter bindings that the clone's
				# rendering network depends on. Skip creation; Phase 3 sets
				# values on the existing parameters directly.
				if not op_def.get('palette_clone', False):
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
		"""Create custom parameters on a single operator.

		Custom sequences (Sequence-style headers + template pars marked
		with a `sequence` field) are created in this order:
		  1. Sequence header via appendSequence(name)
		  2. Template pars (each with a `sequence` field) via their normal
		     append method -- they auto-join the sequence because they
		     follow the sequence header in the page
		  3. After all template pars for a sequence are added, blockSize
		     is set to the count of template ParGroups for that sequence.
		Block-instance values (numBlocks + per-block values) are restored
		later in Phase 2.5 (_expandSequences).
		"""
		pages = {}  # Cache pages by name
		# Track template par counts per sequence for blockSize setting
		seq_template_counts = {}  # {seq_name: count}

		for par_def in custom_par_defs:
			style = par_def.get('style', 'Float')
			par_name = par_def.get('name', '')
			label = par_def.get('label', par_name)
			page_name = par_def.get('page', 'Custom')

			# Track template par counts: each par with a `sequence` field
			# is one ParGroup in that sequence's block template
			belongs_to_seq = par_def.get('sequence')
			if belongs_to_seq:
				seq_template_counts[belongs_to_seq] = (
					seq_template_counts.get(belongs_to_seq, 0) + 1)

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
			# (e.g., 'Tintr' -> 'Tint' for RGB, 'Posx' -> 'Pos' for XYZ)
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

		# After all custom pars are created, set blockSize for each
		# custom sequence so its template is fully formed before
		# Phase 2.5 sets numBlocks and block values.
		for seq_name, count in seq_template_counts.items():
			try:
				seq = self._getSequenceByName(target, seq_name)
				seq.blockSize = count
			except Exception as e:
				self._log(
					f'Failed to set blockSize={count} on sequence '
					f'{seq_name} of {target.path}: {e}', 'WARNING')

	def _expandSequences(self, parent, op_defs):
		"""Phase 2.5: Expand built-in and custom parameter sequences.

		Sets numBlocks for each sequence (creating parameter slots),
		then sets non-default block parameter values. Must run before
		Phase 3 (_setParameters) so the sequence parameters exist.

		Custom sequences (defined via page.appendSequence) require
		blockSize to be set before numBlocks. This is done in
		_createCustomParsOnOp during Phase 2 based on the template
		par count from the TDN.
		"""
		for op_def in op_defs:
			sequences = op_def.get('sequences')
			target = self._resolveOp(parent, op_def) if sequences else None

			if sequences and target:
				for seq_name, blocks in sequences.items():
					try:
						seq = self._getSequenceByName(target, seq_name)
					except Exception:
						self._log(
							f'Sequence "{seq_name}" not found on '
							f'{target.path}', 'WARNING')
						continue

					try:
						seq.numBlocks = len(blocks)
					except Exception as e:
						self._log(
							f'Failed to set numBlocks={len(blocks)} on '
							f'sequence {seq_name} of {target.path}: {e}',
							'WARNING')
						continue

					for i, block_data in enumerate(blocks):
						if not block_data:
							continue
						block = seq[i]
						for base_name, value in block_data.items():
							par = self._resolveSequenceBlockPar(
								target, seq, block, i, base_name)
							if par is None:
								self._log(
									f'Sequence param "{base_name}" not found '
									f'in {seq_name}[{i}] on {target.path}',
									'WARNING')
								continue
							self._setParValue(target, par.name, value)

			# Recurse into children
			children = op_def.get('children', [])
			if children:
				resolved = target or self._resolveOp(parent, op_def)
				if resolved and resolved.isCOMP:
					self._expandSequences(resolved, children)

	@staticmethod
	def _getSequenceByName(target, seq_name):
		"""Resolve a built-in/custom sequence by name.

		`op.seq[name]` (subscript) silently returns None for some operators --
		notably POPs, whose point/prim/attr sequences appear in iteration but
		are not addressable by subscript -- and `op.seq.name` (attribute)
		raises there. Iteration finds them reliably on every op type, so
		resolve by iterating. Returns None when no sequence matches.
		"""
		try:
			return next((s for s in target.seq if s.name == seq_name), None)
		except Exception:
			return None

	@staticmethod
	def _resolveSequenceBlockPar(target, seq, block, block_index, base_name):
		"""Find a parameter inside a sequence block by base name.

		Tries (in order):
		  1. block.par.{baseName} -- works for built-in sequences
		  2. block.par[{baseName}] -- bracket access fallback
		  3. target.par.{seqName}{blockIndex}{lowercase baseName} -- works
		     for custom sequences where block.par attribute access returns
		     None (TD's custom-sequence block.par lookup is broken).
		"""
		# Try attribute access first (works for built-in seqs)
		par = getattr(block.par, base_name, None)
		if par is not None:
			return par
		# Try bracket access
		try:
			par = block.par[base_name]
			if par is not None:
				return par
		except Exception:
			pass
		# Try full prefixed name on the target (works for custom seqs).
		# Custom sequence pars are stored as {seqName}{blockIndex}{baseName_lower}
		full_lower = f'{seq.name}{block_index}{base_name.lower()}'
		par = getattr(target.par, full_lower, None)
		if par is not None:
			return par
		# Last resort: try without lowercasing (in case of edge cases)
		full_orig = f'{seq.name}{block_index}{base_name}'
		par = getattr(target.par, full_orig, None)
		return par

	def _setParameters(self, parent, op_defs):
		"""Phase 3: Set parameter values on all operators."""
		for op_def in op_defs:
			target = self._resolveOp(parent, op_def)
			if not target:
				continue

			# Built-in parameters
			is_palette_clone = op_def.get('palette_clone', False)
			for par_name, value in op_def.get('parameters', {}).items():
				# Skip clone/enablecloning on palette clones -- these
				# are auto-set by parent.create() and should not be
				# overwritten. Old TDN files may still contain them.
				if is_palette_clone and par_name in _PALETTE_CLONE_SKIP_PARAMS:
					continue
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
			# A custom par whose value equals its (non-standard) default has
			# its value OMITTED on export; the param is recreated with the
			# right .default but its .val stays at 0/min. Initialize .val from
			# the default so default-valued custom params round-trip. Single-
			# component only: Pulse has no value, and a multi-component def
			# carries one 'default' that does not map cleanly across components.
			elif ('values' not in par_def and 'default' in par_def
					and style not in ('Pulse', 'Momentary', 'Header')):
				suffixes = STYLE_SUFFIXES.get(style, [])
				size = par_def.get('size') or 1
				if not suffixes and size == 1:
					self._setParValue(target, par_name, par_def['default'])

			# Multi-component values
			if 'values' in par_def:
				suffixes = STYLE_SUFFIXES.get(style, [])
				values = par_def['values']

				if suffixes:
					# Strip first suffix from par_name to get base
					# (e.g., 'Tintr' -> 'Tint' for RGBA)
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
					src_conns = source.outputConnectors
					tgt_conns = target.inputConnectors
					if src_conns and dest_index < len(tgt_conns):
						src_conns[0].connect(tgt_conns[dest_index])
					elif (src_conns and target.isCOMP
							and hasattr(target, 'inputCOMPConnectors')
							and dest_index < len(
								target.inputCOMPConnectors)):
						# COMPs that accept SOP/TOP/CHOP wire inputs
						# (geometryCOMP, cameraCOMP, lightCOMP, etc.)
						# may not expose inputConnectors until a cook
						# cycle runs. Fall back to COMP connectors.
						src_conns[0].connect(
							target.inputCOMPConnectors[dest_index])
					else:
						self._log(
							f'No connector available: {source_ref} -> '
							f'{target.name}[{dest_index}] '
							f'(out={len(src_conns)}, '
							f'in={len(tgt_conns)})', 'WARNING')
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

			# Skip DATs marked as read-only during export (v1.2+).
			if op_def.get('dat_read_only'):
				children = op_def.get('children', [])
				if children and target.isCOMP:
					self._setDATContent(target, children)
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
						# v2.0 writes a plain string; v1.5 wrote a list of lines -- join for back-compat:
						target.text = '\n'.join(content) if isinstance(content, list) else content
				except Exception as e:
					# Downgrade "not editable" errors to DEBUG -- expected
					# for auto-generated companion DATs (info DATs, etc.)
					# from older .tdn files without the dat_read_only flag.
					err_str = str(e).lower()
					if 'not editable' in err_str:
						self._log(
							f'Skipping read-only DAT {target.path} '
							f'(auto-generated content)', 'DEBUG')
					else:
						self._log(
							f'Failed to set DAT content on '
							f'{target.path}: {e}', 'WARNING')

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
				# No dock on this op -- still recurse into children
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
				if backAlpha is not None and backAlpha > 0:
					ann.par.Backcoloralpha = backAlpha

				titleHeight = ann_def.get('titleHeight')
				if titleHeight is not None and titleHeight > 0:
					ann.par.Titleheight = titleHeight

				bodyFontSize = ann_def.get('bodyFontSize')
				if bodyFontSize is not None and bodyFontSize > 0:
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

	def _restoreTOXShells(self, dest) -> int:
		"""Phase 8.5: Load .tox content for empty shells created from tox_ref.

		_createOps tags any COMP it built from a `tox_ref` entry with a
		`_pending_tox_restore` storage key holding the relative tox path.
		This pass walks `dest`'s subtree, sets `externaltox` from that
		storage, calls `_reloadTox` (which toggles `enableexternaltox` to
		force TD to re-read the .tox), then clears the marker.

		Without this, `ReconstructTDNComps` (frame 60) with `clear_first=
		True` would destroy any TOX child that `RestoreTOXComps` (frame 45)
		had just rebuilt, and the .tox content would never reappear until
		the next project open.

		Args:
			dest: The reconstructed COMP operator

		Returns:
			Number of TOX shells restored.
		"""
		embody = self.ownerComp.ext.Embody
		restored = 0

		def _walk(comp):
			nonlocal restored
			for child in list(getattr(comp, 'children', ()) or ()):
				try:
					pending = child.fetch('_pending_tox_restore', None,
										search=False)
				except Exception:
					pending = None
				if pending:
					try:
						normalized = embody.normalizePath(pending)
						child.par.externaltox = normalized
						child.par.enableexternaltox = True
						embody._reloadTox(child)
						child.unstore('_pending_tox_restore')
						restored += 1
					except Exception as e:
						self._log(
							f'Failed to restore TOX shell {child.path}: {e}',
							'WARNING')
				else:
					if hasattr(child, 'children'):
						_walk(child)

		_walk(dest)
		if restored:
			self._log(
				f'Restored {restored} TOX shell(s) under {dest.path} from .tox',
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
			# Skip excluded COMPs and their whole subtree -- invisible to TDN.
			if self._hasExcludeTag(child):
				continue
			paths.append(child.path)

			# Recurse into COMPs (but skip palette clone children
			# and TDN-tagged COMP children unless embed_all)
			if hasattr(child, 'children'):
				if self._isPaletteClone(child) and (
						self._resolvePaletteHandling(child) == 'blackbox'):
					continue
				if not embed_all and self._hasTDNTag(child):
					continue
				if not embed_all and self._hasTOXTag(child):
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

	def _restrictToTrackedTDN(self, files: set) -> set:
		"""Restrict stale-cleanup deletion candidates to files Embody tracks.

		The stale sweep previously treated EVERY pre-existing .tdn under
		the scan folder as a deletion candidate; anything not just
		re-written and not in cleanup_protected was unlinked. For a
		whole-project ('/') export that deleted files Embody never owned
		(manual snapshots, downloaded specimens awaiting import), and for
		sub-COMP exports it overrode an explicit "Keep Files" continuity
		decision (an untracked-but-kept file inside the export subtree).

		A file Embody never tracked is never Embody's to delete: deletion
		candidates are the intersection with the externalizations table's
		.tdn paths. Callers intersect BEFORE the write/track step, so a
		row whose path is about to change still contributes its OLD file
		as a legitimate candidate. Untracked orphans are left on disk
		(clutter over data loss); recovery is tsv-driven and ignores them.
		"""
		if not files:
			return set()
		try:
			tracked = self.ownerComp.ext.Embody._getAllTrackedTDNFiles()
		except Exception:
			# No table -> nothing is provably Embody's -> delete nothing.
			return set()
		resolved_tracked = set()
		for p in tracked:
			try:
				resolved_tracked.add(str(Path(p).resolve()))
			except Exception:
				pass
		kept = set()
		for f in files:
			try:
				if str(Path(f).resolve()) in resolved_tracked:
					kept.add(f)
			except Exception:
				pass
		return kept

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

	# Per-COMP storage key and valid values for palette handling decisions.
	_PALETTE_HANDLING_KEY = '_tdn_palette_handling'
	_PALETTE_HANDLING_VALUES = ('blackbox', 'fullexport')

	def _resolvePaletteHandling(self, target):
		"""Resolve how to handle a detected palette COMP during TDN export.

		Precedence:
		  1. Per-COMP storage override (`_tdn_palette_handling`).
		  2. Embody `Tdnpalettehandling` par:
		       - `blackbox` / `fullexport` -> return directly.
		       - `ask` -> prompt user via `_promptPaletteHandling`, which
		         stores the decision on the target and returns it.
		  3. Fallback: `blackbox` (safe default, preserves old behavior).
		"""
		try:
			stored = target.fetch(self._PALETTE_HANDLING_KEY, None,
								  search=False)
		except Exception:
			stored = None
		if stored in self._PALETTE_HANDLING_VALUES:
			return stored

		try:
			par_val = self.ownerComp.par.Tdnpalettehandling.eval()
		except Exception:
			par_val = 'blackbox'

		if par_val in self._PALETTE_HANDLING_VALUES:
			return par_val

		# par_val == 'ask' (or unexpected).
		# Read-only / non-interactive callers (e.g. diff_tdn's gather) set
		# _tdn_suppress_palette_prompt so export never blocks on a modal dialog
		# and never mutates the Tdnpalettehandling par. Fall back to the safe
		# 'blackbox' handling and warn instead of prompting.
		if getattr(self, '_tdn_suppress_palette_prompt', False):
			try:
				self._log(
					f'Palette handling is "ask" but export is non-interactive '
					f'for {target.path}; using "blackbox" (palette internals '
					f'export as references). Set Tdnpalettehandling explicitly '
					f'to silence this.', 'WARNING')
			except Exception:
				pass
			return 'blackbox'

		return self._promptPaletteHandling(target)

	def _promptPaletteHandling(self, target):
		"""Prompt user for palette handling on this COMP; persist the choice.

		Four buttons:
		  0: Black Box (this COMP)     -> stored on target
		  1: Full Export (this COMP)   -> stored on target
		  2: Black Box for All         -> Tdnpalettehandling = blackbox
		  3: Full Export for All       -> Tdnpalettehandling = fullexport
		Returns the effective handling string.
		"""
		try:
			embody = self.ownerComp.ext.Embody
			choice = embody._messageBox(
				'Embody - Palette Component Detected',
				f'Palette component "{target.name}" ({target.OPType}) found '
				f'in TDN export at {target.path}.\n\n'
				f'- Black Box: reference the palette only; internals are '
				f're-dropped on import. Recommended for stock palette COMPs.\n'
				f'- Full Export: export all internals. Use when this COMP has '
				f'been heavily customized internally.',
				buttons=['Black Box', 'Full Export',
						 'Black Box for All', 'Full Export for All'])
		except Exception as e:
			self._log(
				f'Palette prompt failed on {target.path}: {e} '
				f'(defaulting to blackbox)', 'WARNING')
			return 'blackbox'

		if choice == 0:
			target.store(self._PALETTE_HANDLING_KEY, 'blackbox')
			return 'blackbox'
		if choice == 1:
			target.store(self._PALETTE_HANDLING_KEY, 'fullexport')
			return 'fullexport'
		if choice == 2:
			try:
				self.ownerComp.par.Tdnpalettehandling = 'blackbox'
			except Exception:
				pass
			return 'blackbox'
		if choice == 3:
			try:
				self.ownerComp.par.Tdnpalettehandling = 'fullexport'
			except Exception:
				pass
			return 'fullexport'
		return 'blackbox'

	def _isPaletteClone(self, target):
		"""Check if a COMP is a palette component from TD's shipped palette.

		Detection uses two strategies, in order:

		1. Catalog lookup (fast path): if the operator's name and OPType
		   both match a known palette entry, it's a palette component.
		   The catalog is built by CatalogManagerExt at startup.

		2. Clone expression heuristic (fallback): checks the clone
		   parameter for known system prefixes (TDBasicWidgets, TDResources,
		   TDTox, /sys/). Catches components whose clone was set by the
		   palette drag-and-drop mechanism but whose name was changed.
		"""
		if not target.isCOMP:
			return False

		# --- Strategy 1: catalog lookup ---
		if self._palette_catalog:
			entry = self._palette_catalog.get(target.name)
			if entry:
				# Support both dict format {type, min_children} and legacy str
				if isinstance(entry, dict):
					expected_type = entry.get('type', '')
					min_children = entry.get('min_children', 0)
				else:
					expected_type = entry
					min_children = 0
				if target.OPType == expected_type:
					# Child count floor: reject user COMPs with same name that
					# have far fewer children than the real palette component.
					# Threshold is half the scanned count (tolerates user mods).
					floor = max(1, min_children // 2) if min_children > 0 else 0
					if floor == 0 or len(target.children) >= floor:
						return True

		# --- Strategy 2: clone expression heuristic ---
		# Exclude /sys/TDTox/defaultCOMPs/* - these are TD's native-operator
		# templates (every fresh buttonCOMP/panelCOMP/etc. clones from there
		# by default). Not palette components; internals are minimal and
		# export cleanly. Treated like any other normal COMP.
		clone_par = getattr(target.par, 'clone', None)
		if not clone_par:
			return False
		try:
			clone_op = clone_par.eval()
			if clone_op and hasattr(clone_op, 'path'):
				cpath = clone_op.path
				if cpath.startswith('/sys/TDTox/defaultCOMPs/'):
					return False
				if cpath.startswith('/sys/'):
					return True
			if clone_par.mode == ParMode.EXPRESSION:
				expr = clone_par.expr
				if 'defaultCOMPs' in expr:
					return False
				if any(s in expr for s in (
						'TDBasicWidgets', 'TDResources', 'TDTox')):
					return True
		except Exception as e:
			self._log(
				f'Error checking palette clone for {target.path}: {e}',
				'DEBUG')
		return False

	@staticmethod
	def _isInsideAnimationCOMP(target):
		"""Return True if target has an animationCOMP in its immediate parent.

		animationCOMP stores all keyframe data in direct-child tableDATs
		(keys, channels, graph, attributes). Checking only the direct parent
		is sufficient -- these DATs are never nested deeper inside the COMP.
		"""
		p = target.parent()
		return p is not None and p.OPType == 'animationCOMP'

	def _getCloneSourceDiffs(self, target):
		"""Find params that differ from the clone source but match p.default.

		For palette clones, _exportBuiltinParams compares against p.default.
		But p.default can differ from the clone source's actual value (e.g.
		buttontype: p.default is "momentary" but clone source is "toggledown").
		This method finds params that were wrongly skipped because they match
		p.default but differ from the clone source -- these need to be exported
		so they survive the strip/restore rebuild cycle.
		"""
		clone_par = getattr(target.par, 'clone', None)
		if not clone_par:
			return {}
		try:
			clone_source = clone_par.eval()
			if not clone_source or not hasattr(clone_source, 'par'):
				return {}
		except Exception:
			return {}

		op_type = target.OPType
		if op_type not in self._exportable_cache:
			self._buildParCache(target)
		exportable = self._exportable_cache[op_type]
		defaults = self._defaults_cache[op_type]

		diffs = {}
		for p in target.pars():
			name = p.name
			if name not in exportable:
				continue
			if name in _PALETTE_CLONE_SKIP_PARAMS:
				continue
			if p.mode != ParMode.CONSTANT:
				continue  # Expressions already exported by _exportBuiltinParams
			try:
				current = p.val
				builtin_default = defaults.get(name)
				# Only interested in params that matched p.default
				# (and were therefore skipped) but differ from clone source
				if not self._valuesDiffer(current, builtin_default):
					src_par = getattr(clone_source.par, name, None)
					if src_par is not None:
						src_val = src_par.val if src_par.mode == ParMode.CONSTANT else src_par.eval()
						if self._valuesDiffer(current, src_val):
							diffs[name] = self._serializeValue(current)
			except Exception:
				pass
		return diffs

	def _getTDNExternalizedPaths(self) -> set:
		"""Return a set of all TDN-strategy COMP paths from the externalizations table."""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table or table.numRows < 2:
				return set()
			if table[0, 'strategy'] is None:
				return set()
		except Exception:
			return set()
		paths = set()
		for i in range(1, table.numRows):
			if table[i, 'strategy'].val == 'tdn':
				paths.add(table[i, 'path'].val)
		return paths

	def _getTOXExternalizedPaths(self) -> set:
		"""Return a set of all TOX-strategy COMP paths from the externalizations table."""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table or table.numRows < 2:
				return set()
			if table[0, 'strategy'] is None:
				return set()
		except Exception:
			return set()
		paths = set()
		for i in range(1, table.numRows):
			if table[i, 'strategy'].val == 'tox':
				paths.add(table[i, 'path'].val)
		return paths

	def _stripNestedTDNChildren(self, op_defs: list, parent_path: str,
								tdn_paths: set) -> list:
		"""Remove children from op_defs for COMPs with their own TDN entry.

		The child COMP shell is still created (its operator definition remains),
		but its children array is emptied -- the child's own .tdn file is the
		source of truth for its internal network.

		Args:
			op_defs: List of operator definitions (mutated in place)
			parent_path: TD path of the COMP being imported into
			tdn_paths: Set of all TDN-strategy paths from externalizations table

		Returns:
			List of child paths that were skipped (for logging)
		"""
		skipped = []
		for op_def in op_defs:
			name = op_def.get('name')
			if not name:
				continue
			child_path = f"{parent_path.rstrip('/')}/{name}"
			children = op_def.get('children')
			if children and child_path in tdn_paths:
				op_def['children'] = []
				skipped.append(child_path)
			elif children:
				skipped.extend(
					self._stripNestedTDNChildren(children, child_path, tdn_paths))
		return skipped

	def _stripNestedTOXChildren(self, op_defs: list, parent_path: str,
								tox_paths: set) -> list:
		"""Remove children from op_defs for COMPs with their own TOX entry.

		The child COMP shell is still created (its operator definition remains),
		but its children array is emptied -- the child's own .tox file is the
		source of truth for its internal network. RestoreTOXComps loads the
		.tox content on project open; for runtime imports the externaltox
		parameter is preserved and the user can manually reload.

		Backward-compat path: pre-fix .tdn files may have TOX children
		embedded. This strip prevents those stale snapshots from being
		written into the live network.

		Args:
			op_defs: List of operator definitions (mutated in place)
			parent_path: TD path of the COMP being imported into
			tox_paths: Set of all TOX-strategy paths from externalizations table

		Returns:
			List of child paths that were skipped (for logging)
		"""
		skipped = []
		for op_def in op_defs:
			name = op_def.get('name')
			if not name:
				continue
			child_path = f"{parent_path.rstrip('/')}/{name}"
			children = op_def.get('children')
			if children and child_path in tox_paths:
				op_def['children'] = []
				skipped.append(child_path)
			elif children:
				skipped.extend(
					self._stripNestedTOXChildren(children, child_path, tox_paths))
		return skipped

	def _hasTDNTag(self, target):
		"""Check if a COMP has its own TDN externalization tag."""
		if not target.isCOMP:
			return False
		tdn_tag = self.ownerComp.par.Tdntag.val
		return tdn_tag in target.tags

	def _hasExcludeTag(self, target):
		"""Check if a COMP is tagged to be excluded from the TDN system.

		An excluded COMP (and its whole subtree) is invisible to TDN:
		never exported, never written to disk, never stripped on save,
		never destroyed or recreated by reconstruction. The owning
		application is solely responsible for its lifecycle. Annotation
		COMPs are never eligible -- their lifecycle is TDN's, not the app's.

		Exclusion is honored only when the tagged COMP is a DIRECT CHILD of
		a TDN boundary (the exported/stripped root). The strip and clear
		passes only preserve direct children, so a COMP tagged for exclusion
		but nested below a non-excluded intermediate cannot be preserved --
		it is serialized as normal content instead (with a warning) so it
		round-trips rather than being lost. To exclude such a COMP, tag the
		intervening COMP(s) or make it a direct child of the boundary.
		"""
		if not target.isCOMP or target.type == 'annotate':
			return False
		exclude_tag = self.ownerComp.par.Tdnexcludetag.eval()
		return bool(exclude_tag) and exclude_tag in target.tags

	def _hasTOXTag(self, target):
		"""Check if a COMP has its own TOX externalization tag."""
		if not target.isCOMP:
			return False
		tox_tag = self.ownerComp.par.Toxtag.val
		return tox_tag in target.tags

	def _resolveTOXRef(self, target) -> 'Optional[str]':
		"""Look up a TOX-tagged child COMP's relative file path.

		Returns the child's .tox file path (relative to the project
		externalization folder) from the externalizations table, or
		None if the child isn't tracked.
		"""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table or table.numRows < 2:
				return None
			for i in range(1, table.numRows):
				if (table[i, 'path'].val == target.path
						and table[i, 'strategy'].val == 'tox'):
					return table[i, 'rel_file_path'].val
		except Exception:
			pass
		return None

	def _resolveTDNRef(self, target) -> 'Optional[str]':
		"""Look up a TDN-tagged child COMP's relative file path.

		Returns the child's .tdn file path (relative to the project
		externalization folder) from the externalizations table, or
		None if the child isn't tracked.
		"""
		try:
			table = self.ownerComp.ext.Embody.Externalizations
			if not table or table.numRows < 2:
				return None
			for i in range(1, table.numRows):
				if (table[i, 'path'].val == target.path
						and table[i, 'strategy'].val == 'tdn'):
					return table[i, 'rel_file_path'].val
		except Exception:
			pass
		return None

	def _validateTDNRefs(self, op_defs: list, parent_path: str) -> list:
		"""Cross-validate tdn_ref pointers against the externalizations table.

		Checks two independent sources of truth:
		1. Each tdn_ref in the file corresponds to a table entry
		2. Each referenced .tdn file exists on disk

		Returns list of warning messages (empty = all valid).
		"""
		warnings = []
		tdn_paths = self._getTDNExternalizedPaths()

		for op_def in op_defs:
			tdn_ref = op_def.get('tdn_ref')
			name = op_def.get('name', '?')
			child_path = f"{parent_path.rstrip('/')}/{name}"

			if tdn_ref:
				# Check 1: table entry exists for this child
				if child_path not in tdn_paths:
					warnings.append(
						f'tdn_ref for {child_path} points to {tdn_ref} '
						f'but no matching entry in externalizations table')

				# Check 2: referenced file exists on disk
				try:
					abs_path = self.ownerComp.ext.Embody.buildAbsolutePath(
						tdn_ref)
					if not abs_path.is_file():
						warnings.append(
							f'tdn_ref for {child_path}: file not found: '
							f'{tdn_ref}')
				except Exception:
					warnings.append(
						f'tdn_ref for {child_path}: cannot resolve path: '
						f'{tdn_ref}')

			# Recurse into children
			children = op_def.get('children', [])
			if children:
				warnings.extend(
					self._validateTDNRefs(children, child_path))

		return warnings

	def _validateTOXRefs(self, op_defs: list, parent_path: str) -> list:
		"""Cross-validate tox_ref pointers against the externalizations table.

		Parity with _validateTDNRefs. Checks two independent sources:
		1. Each tox_ref in the file corresponds to a table entry with strategy=tox
		2. Each referenced .tox file exists on disk

		Returns list of warning messages (empty = all valid).
		"""
		warnings = []
		tox_paths = self._getTOXExternalizedPaths()

		for op_def in op_defs:
			tox_ref = op_def.get('tox_ref')
			name = op_def.get('name', '?')
			child_path = f"{parent_path.rstrip('/')}/{name}"

			if tox_ref:
				# Check 1: table entry exists for this child
				if child_path not in tox_paths:
					warnings.append(
						f'tox_ref for {child_path} points to {tox_ref} '
						f'but no matching entry in externalizations table')

				# Check 2: referenced file exists on disk
				try:
					abs_path = self.ownerComp.ext.Embody.buildAbsolutePath(
						tox_ref)
					if not abs_path.is_file():
						warnings.append(
							f'tox_ref for {child_path}: file not found: '
							f'{tox_ref}')
				except Exception:
					warnings.append(
						f'tox_ref for {child_path}: cannot resolve path: '
						f'{tox_ref}')

			# Recurse into children
			children = op_def.get('children', [])
			if children:
				warnings.extend(
					self._validateTOXRefs(children, child_path))

		return warnings

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
	def _compact_json_dumps(data):  # name kept; body now YAML v2.0
		"""Serialize a TDN dict to a readable, deterministic YAML v2.0 string.

		The name is retained so all callers (TDNExt 555/804, execute.py 187)
		stay valid. _tdn_list_representer inlines short numeric vectors and
		literal block scalars render multi-line scripts readably -- replacing
		the old json.dumps(indent='\\t') + regex array-inlining serializer.
		"""
		return tdn_dump(data)

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

		Returns (par_templates_dict, operators) -- operators modified in-place.
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

	@staticmethod
	def _stripBuildSuffix(name: str) -> str:
		"""Strip trailing build number (.NNN) from a project name for stable filenames.

		Only removes a trailing dot-digits suffix -- the auto-incrementing build
		number that TD appends on save. Preserves deliberate user versioning.

		Examples: 'Embody-5.302' -> 'Embody-5', 'Embody-5' -> 'Embody-5',
		'demo' -> 'demo', 'Embody5' -> 'Embody5', 'Embody_5' -> 'Embody_5'.
		"""
		import re
		return re.sub(r'\.\d+$', '', name)

	def _resolveOutputPath(self, output_file, root_op):
		"""Resolve the output file path, matching the operator's TD path structure."""
		from pathlib import Path

		if output_file == 'auto':
			project_dir = Path(project.folder)

			if root_op.path == '/':
				# Root export: strip build number for stable git-diffable name
				raw_name = project.name.removesuffix('.toe')
				safe_name = TDNExt._stripBuildSuffix(raw_name)
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
		"""Add/update a TDN entry in the externalizations table.

		Enrollment is deliberate, not a side effect: a NEW row is appended
		only for COMPs carrying the TDN tag (plus the whole-project root
		'/', which cannot carry tags and is excluded from strip/reconstruct
		by _getTDNStrategyComps anyway). Previously ANY ad-hoc file export
		inside the project folder appended a strategy='tdn' row, silently
		subscribing an untagged COMP to the full save-strip/reconstruction
		lifecycle. Updates to EXISTING rows are always applied.

		Tracked COMPs also get a `_tdn_rel_path` storage pointer stamped on
		the COMP itself -- a redundant recovery breadcrumb that survives in
		the .toe shell even if the externalizations table is lost (see
		EmbodyExt.RecoverOrphanShells).
		"""
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

			target = op(root_path)

			def _stamp_recovery_pointer():
				# Skip '/' -- root cannot go missing, and its storage is
				# the project's own.
				if target is None or root_path == '/':
					return
				try:
					target.store('_tdn_rel_path', rel_path)
				except Exception:
					pass

			# Check for strategy column (new schema)
			headers = [table[0, c].val for c in range(table.numCols)]
			has_strategy = 'strategy' in headers

			# Update existing row if found -- check strategy='tdn' or type='tdn'
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
					_stamp_recovery_pointer()
					return

			# New rows require deliberate enrollment: the TDN tag (or the
			# whole-project root). An ad-hoc export of an untagged COMP
			# writes its file but does NOT join the lifecycle.
			if root_path != '/':
				try:
					tdn_tag = self.ownerComp.par.Tdntag.val
				except Exception:
					tdn_tag = ''
				if not (target is not None and tdn_tag
						and tdn_tag in target.tags):
					self._log(
						f'Ad-hoc TDN export of untagged {root_path} not '
						f'tracked -- file written but not enrolled in the '
						f'save/reconstruct lifecycle (tag it to enroll)',
						'INFO')
					return

			# Add new row (schema-aware)
			if has_strategy:
				comp_type = target.type if target else 'base'
				table.appendRow([root_path, comp_type, 'tdn', rel_path,
								 timestamp, '', build_str, tb_str])
			else:
				table.appendRow([root_path, 'tdn', rel_path, timestamp,
								 '', build_str, tb_str])
			_stamp_recovery_pointer()
		except Exception as e:
			self._log(f'Failed to track TDN export: {e}', 'WARNING')

	def _warnLargeTDN(self, filepath: str, root_path: str) -> None:
		"""Show a one-time warning when a TDN file exceeds the size threshold.

		Uses the Tdncascadewarn parameter (ask/quiet) to control whether
		the dialog is shown. 'Don't show again' sets the parameter to
		'quiet' permanently.
		"""
		LARGE_TDN_THRESHOLD = 5_000_000  # 5 MB

		# Already using cascade -- no point warning
		if self.ownerComp.par.Tdncascade.eval():
			return

		warn_pref = getattr(self.ownerComp.par, 'Tdncascadewarn', None)
		if warn_pref is None or warn_pref.eval() != 'ask':
			return

		try:
			file_size = Path(filepath).stat().st_size
		except Exception:
			return

		if file_size < LARGE_TDN_THRESHOLD:
			return

		size_mb = file_size / (1024 * 1024)
		msg = (
			f'The TDN file for {root_path} is {size_mb:.1f} MB.\n\n'
			f'Large TDN files are difficult to diff in git. '
			f'Enable "Cascade to Children" on the TDN page '
			f'to split each child COMP into its own .tdn file.')
		choice = self.ownerComp.ext.Embody._messageBox(
			'Large TDN File',
			msg,
			buttons=['OK', "Don't show again"])

		if choice == 1:  # Don't show again
			self.ownerComp.par.Tdncascadewarn = 'quiet'
			self._log('Large TDN warning silenced', 'INFO')

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
				if self._isInsideCloneOrReplicant(child, root_op):
					continue
				locked.append(child)

		if not locked:
			return

		# Build summary
		names = [f'{c.path} ({c.family})' for c in locked[:10]]
		summary = ', '.join(names)
		if len(locked) > 10:
			summary += f', ... and {len(locked) - 10} more'

		if context == 'export':
			self._log(
				f'Locked non-DAT operators in {root_op.path}: {summary} '
				f'-- frozen data will not persist through TDN', 'WARNING')
			try:
				ui.messageBox(
					'Embody -- Locked Content Warning',
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
			# Import context -- log only, no dialog (reconstruction is automated)
			self._log(
				f'Restored lock flag on {len(locked)} non-DAT operator(s) '
				f'in {root_op.path}: {summary} -- these operators have no '
				f'frozen data and should be unlocked to re-cook', 'WARNING')

	def _isInsideCloneOrReplicant(self, child, root_op):
		"""True if child is a descendant of a clone or replicant COMP.

		Lock state inside clones is inherited from the master (user
		should fix it there). Lock state inside replicants is
		regenerated per-template by the replicatorCOMP. In both cases
		warning the user is noise, not signal.
		"""
		p = child.parent()
		while p is not None and p is not root_op and p.path != '/':
			if p.replicator is not None:
				return True
			clone_par = getattr(p.par, 'clone', None)
			enable_par = getattr(p.par, 'enablecloning', None)
			if clone_par is not None and enable_par is not None:
				try:
					if clone_par.eval() and enable_par.eval():
						return True
				except Exception:
					pass
			p = p.parent()
		return False

	def _log(self, message, level='INFO'):
		"""Log via Embody's centralized logger."""
		try:
			self.ownerComp.ext.Embody.Log(message, level, _depth=2)
			return
		except Exception:
			pass  # Fallback below handles this -- avoid recursion in logger
		# Fallback if Embody ext unavailable
		print(f'[TDN][{level}] {message}')

	# =========================================================================
	# CLIPBOARD -- copy/paste networks as portable _embody_tdn envelopes.
	# Own-network round-trips (source 'embody') import directly -- trusted.
	# Community envelopes (source 'embody.tools') are delegated to ext.Collection,
	# which scans + default-inerts them before import so a stranger's code can
	# never execute on paste. Envelope helpers are module-level (above the class);
	# the untrusted sandbox lives in the Collection sub-COMP.
	# =========================================================================

	def CopyNetworkToClipboard(self, comp: 'OP') -> dict:
		"""Export a COMP's network and write it to the OS clipboard as an
		_embody_tdn envelope (source 'embody' -- a trusted own-network copy).
		"""
		comp = op(comp) if isinstance(comp, str) else comp
		if comp is None or not comp.isCOMP:
			return {'ok': False, 'reason': 'not_a_comp'}
		export = self.ExportNetwork(root_path=comp.path, include_dat_content=True)
		if not isinstance(export, dict) or not export.get('success'):
			return {'ok': False, 'reason': 'export_failed', 'detail': (export or {}).get('error')}
		env = wrap_tdn(export.get('tdn'), source='embody', slug=comp.name)
		ui.clipboard = to_clipboard_str(env)
		# Outbound copy: seed the clipboard watcher's "already seen" signature with
		# what we just wrote, so it does NOT turn around and offer to paste our own
		# export back in -- the user copied this to share/export (outbound), not to
		# re-import it (inbound). _clipboardWatchPoll computes sig = (len(raw),
		# hash(raw)) from ui.clipboard; re-read it here so the sig matches exactly.
		# An inbound TDN (web "embody it", a foreign envelope) is a different string
		# -> a different sig -> still prompts, so inbound paste is unaffected.
		try:
			raw = ui.clipboard or ''
			self._clip_last_sig = (len(raw), hash(raw))
		except Exception:
			pass
		op_count = len(env['tdn'].get('operators', []))
		self._log("Copied %s TDN to clipboard (%d ops)" % (comp.name, op_count), 'SUCCESS')
		return {'ok': True, 'name': comp.name, 'op_count': op_count, 'sha256': env['sha256']}

	def CopySelectedToClipboard(self) -> dict:
		"""Ctrl+Shift+C handler: copy the COMP selected in the current network
		to the OS clipboard as a portable _embody_tdn envelope. Mirror of
		PasteNetworkAsNewComp (Ctrl+Shift+V). No-op (logged) when the current
		network has no single COMP selected.
		"""
		pane = ui.panes.current
		owner = pane.owner if pane else None
		if owner is None or not owner.isCOMP:
			return {'ok': False, 'reason': 'no_current_network'}
		comps = [c for c in owner.selectedChildren if c.isCOMP]
		if not comps:
			self._log('Copy TDN: select a COMP in the network first', 'INFO')
			return {'ok': False, 'reason': 'no_comp_selected'}
		if len(comps) > 1:
			self._log('Copy TDN: %d COMPs selected; copying the first (%s)'
					  % (len(comps), comps[0].name), 'INFO')
		return self.CopyNetworkToClipboard(comps[0])

	def _planPasteFromClipboard(self) -> dict:
		"""Turn the clipboard into an import plan. Never executes anything.

		Own envelope source -> direct import. Community envelope source -> hand
		the inner tdn to ext.Collection (scan + default-inert) so nothing runs
		on paste. A bare .tdn document with no envelope -- e.g. a .tdn file's
		text copied from an editor -- carries NO provenance, so it is sandboxed
		(inert) like community content; use ImportNetworkFromFile for a trusted
		local file. Returns {'ok': False, ...} with no usable TDN.
		"""
		raw = ui.clipboard or ''
		env = unwrap_clipboard(raw)
		if env is None:
			# No _embody_tdn envelope -- maybe a bare .tdn document (YAML or
			# JSON), e.g. a .tdn file's text copied from an editor. It carries
			# NO provenance, so treat it as untrusted: route through the
			# Collection sandbox (scan + default-inert) exactly like community
			# content, so pasting a stranger's .tdn can't run code on paste.
			# (A trusted local file should use ImportNetworkFromFile.)
			doc = None
			if raw.strip():
				try:
					doc = tdn_load(raw)
				except Exception:
					doc = None
			if isinstance(doc, dict) and 'operators' in doc:
				collection = self.ownerComp.op('Collection')
				if collection is None:
					return {'ok': False, 'reason': 'collection_unavailable'}
				inert = collection.ext.Collection.PlanCommunityPaste(doc)
				return {'ok': True, 'source': 'file',
						'mode': inert.get('mode', 'inert'),
						'tdn': inert.get('tdn'),
						'capability': inert.get('capability'),
						'summary': inert.get('summary'),
						'slug': doc.get('slug'), 'version': doc.get('version'),
						'integrity_ok': True}
			return {'ok': False, 'reason': 'no_tdn'}
		tdn = env.get('tdn')
		source = env.get('source')
		plan = {'ok': True, 'source': source, 'slug': env.get('slug'),
				'version': env.get('version'),
				'integrity_ok': verify_envelope_integrity(env)}
		if source == 'embody':
			plan.update({'mode': 'direct', 'tdn': tdn, 'capability': None, 'summary': None})
			return plan
		# Community / untrusted -> the Collection sandbox owns scan + inert.
		collection = self.ownerComp.op('Collection')
		if collection is None:
			return {'ok': False, 'reason': 'collection_unavailable'}
		inert = collection.ext.Collection.PlanCommunityPaste(tdn if isinstance(tdn, dict) else {})
		plan.update({'mode': inert.get('mode', 'inert'), 'tdn': inert.get('tdn'),
					 'capability': inert.get('capability'), 'summary': inert.get('summary')})
		return plan

	def _importPlanned(self, target: 'OP', plan: dict):
		"""Import plan['tdn'] into target. For an UNTRUSTED plan (community/file --
		anything but a trusted own 'embody' envelope) the import runs with the
		target's cooking suspended: ImportNetwork sets parameters before flags, so a
		bypassed IO/Script op would briefly carry attacker-set file/url params before
		its bypass flag lands. Suspending cooking closes that window (and is harmless
		for the clean 'live' case, which cooks normally once restored)."""
		if plan.get('source') == 'embody':
			return self.ImportNetwork(target.path, plan['tdn'])
		prev = target.allowCooking
		target.allowCooking = False
		try:
			return self.ImportNetwork(target.path, plan['tdn'])
		finally:
			target.allowCooking = prev

	def PasteNetworkFromClipboard(self, target: 'OP') -> dict:
		"""Import a clipboard _embody_tdn envelope INTO the target COMP."""
		target = op(target) if isinstance(target, str) else target
		if target is None or not target.isCOMP:
			return {'ok': False, 'reason': 'not_a_comp'}
		plan = self._planPasteFromClipboard()
		if not plan.get('ok'):
			return plan
		res = self._importPlanned(target, plan)
		verdict = (plan.get('capability') or {}).get('verdict')
		self._log("Pasted TDN into %s (mode=%s, source=%s, verdict=%s)"
				  % (target.name, plan['mode'], plan.get('source'), verdict), 'SUCCESS')
		return {'ok': True, 'target': target.path, 'mode': plan['mode'],
				'source': plan.get('source'), 'verdict': verdict,
				'summary': plan.get('summary'), 'import': res}

	def _placeAndSelectPasted(self, pane, owner, base) -> None:
		"""Place the pasted COMP at a clear spot beside the existing network, select it on
		its own + make it the current op, then PAN the view to centre it so the user sees
		it immediately.

		Why pan rather than place-at-view-centre: TD's network-view rectangle
		(pane.bottomLeft/topRight) reports stale coordinates from script and does not match
		the visible area, and pane.home()/homeSelected() are no-ops unless the pane is
		focused. But pane.x / pane.y -- the network coordinate at the pane centre -- IS
		writable, so assigning it reliably re-centres the view on the new COMP regardless of
		split-pane / toolbar / multi-monitor layout."""
		kids = [c for c in owner.children if c is not base]
		try:
			if kids:
				base.nodeX = max(c.nodeX + c.nodeWidth for c in kids) + 200
				base.nodeY = sum(c.nodeY for c in kids) / float(len(kids))
			else:
				base.nodeX = 0.0
				base.nodeY = 0.0
		except Exception:
			pass
		try:
			for c in owner.children:
				c.selected = (c is base)
			base.current = True
		except Exception:
			pass
		try:
			pane.x = base.nodeX + base.nodeWidth / 2.0
			pane.y = base.nodeY + base.nodeHeight / 2.0
		except Exception:
			pass

	def PasteNetworkAsNewComp(self) -> dict:
		"""Ctrl+Shift+V handler: create a new COMP at the current network and
		paste the clipboard TDN into it, named after the TDN (its network_path
		basename, else slug). Accepts an Embody envelope or a bare .tdn
		document. No-op (logged) when the clipboard holds neither.
		"""
		pane = ui.panes.current
		owner = pane.owner if pane else None
		if owner is None or not owner.isCOMP:
			return {'ok': False, 'reason': 'no_current_network'}
		plan = self._planPasteFromClipboard()
		if not plan.get('ok'):
			self._log('Paste TDN: clipboard holds no Embody envelope or .tdn document', 'INFO')
			return plan

		# Name the new COMP after the TDN (network_path basename, else envelope
		# slug), sanitized; TD uniquifies collisions.
		comp_name = tdu.validName(resolve_tdn_name(plan.get('tdn'), plan.get('slug')) or 'pasted_tdn') or 'pasted_tdn'
		base = owner.create(baseCOMP, comp_name)
		self._placeAndSelectPasted(pane, owner, base)
		res = self._importPlanned(base, plan)
		verdict = (plan.get('capability') or {}).get('verdict')
		self._log("Pasted new COMP '%s' from clipboard TDN (mode=%s, source=%s, verdict=%s)"
				  % (base.name, plan['mode'], plan.get('source'), verdict), 'SUCCESS')
		return {'ok': True, 'comp': base.path, 'mode': plan['mode'],
				'source': plan.get('source'), 'verdict': verdict,
				'summary': plan.get('summary'), 'import': res}

	def ClipboardHasNetwork(self) -> bool:
		"""True if the OS clipboard holds a valid _embody_tdn envelope."""
		try:
			return unwrap_clipboard(ui.clipboard or '') is not None
		except Exception:
			return False

	def _clipboardWatchTick(self, gen: int = 0) -> None:
		"""Self-rescheduling clipboard watcher (the no-shortcut paste trigger).
		Generation-guarded: a reinit bumps the stored gen, so a prior instance's
		loop ends on its next tick. Never raises out (self-healing loop)."""
		if gen and gen != self.ownerComp.fetch('_clip_watch_gen', 0):
			return
		try:
			self._clipboardWatchPoll()
		except Exception:
			pass
		# Pending run() calls can outlive COMP replacement during upgrades.
		run("o = op(%r)\nif o and o.valid: o.ext.TDN._clipboardWatchTick(%d)" %
			(self.ownerComp.path, gen),
			fromOP=self.ownerComp, delayMilliSeconds=1500)

	def _tdWindowActive(self) -> bool:
		"""Is the TD window the active (frontmost) application the user is working in?

		TD exposes no focus getter (only windowCOMP.setForeground(), a setter), so we
		compare the OS frontmost-application PID to TD's own PID (app.processId) via a
		fast, no-subprocess platform call -- NSWorkspace on macOS, GetForegroundWindow
		on Windows. (Cursor-rollover was tried first but only updates on a mouse-move
		over TD, so switching back with the cursor parked left the prompt stuck.)
		Fail-open (True) on any error / unknown platform, so the watcher is never
		permanently muted -- worst case it reverts to prompting whenever content
		appears."""
		try:
			pid = self._osFrontmostPid()
			return True if pid is None else (pid == app.processId)
		except Exception:
			return True

	def _osFrontmostPid(self):
		"""PID of the OS frontmost application, or None if it cannot be determined."""
		import sys
		plat = sys.platform
		try:
			if plat == 'darwin':
				return self._macFrontmostPid()
			if plat.startswith('win'):
				return self._winFrontmostPid()
		except Exception:
			return None
		return None

	def _macFrontmostPid(self):
		import ctypes
		import ctypes.util
		ctypes.cdll.LoadLibrary('/System/Library/Frameworks/AppKit.framework/AppKit')
		objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc'))
		objc.objc_getClass.restype = ctypes.c_void_p
		objc.objc_getClass.argtypes = [ctypes.c_char_p]
		objc.sel_registerName.restype = ctypes.c_void_p
		objc.sel_registerName.argtypes = [ctypes.c_char_p]
		msg = objc.objc_msgSend
		msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
		msg.restype = ctypes.c_void_p
		ws = objc.objc_getClass(b'NSWorkspace')
		shared = msg(ws, objc.sel_registerName(b'sharedWorkspace'))
		front = msg(shared, objc.sel_registerName(b'frontmostApplication'))
		if not front:
			return None
		msg.restype = ctypes.c_int32
		return int(msg(front, objc.sel_registerName(b'processIdentifier')))

	def _winFrontmostPid(self):
		import ctypes
		user32 = ctypes.windll.user32
		# Explicit handle types -- a 64-bit HWND would be truncated under the default
		# c_int restype, yielding a wrong/zero handle on 64-bit Windows.
		user32.GetForegroundWindow.restype = ctypes.c_void_p
		user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
		hwnd = user32.GetForegroundWindow()
		if not hwnd:
			return None
		pid = ctypes.c_ulong(0)
		user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
		return int(pid.value) or None

	def _clipboardWatchPoll(self) -> None:
		"""One poll: when the OS clipboard changes to a NEW Embody envelope,
		offer (via the Embody message box, which self-suppresses during saves and
		tests) to paste it as a new COMP in the current network."""
		me = self.ownerComp
		par = getattr(me.par, 'Clipboardautopaste', None)
		if par is None or not par.eval():
			return
		if me.par.Performmode.eval():
			return
		raw = ui.clipboard or ''
		sig = (len(raw), hash(raw))
		if sig == self._clip_last_sig:
			return
		# Only surface the prompt while the user is actually in the TD window. If TD is
		# not the window they are working in (e.g. they copied a specimen in the browser),
		# do NOT record the sig -- a later poll re-checks the CURRENT clipboard once they
		# are back in TD, so if they changed their mind and copied a different specimen
		# meanwhile, that newer one wins.
		if not self._tdWindowActive():
			return
		# Record before prompting so a dismissed envelope never re-nags.
		self._clip_last_sig = sig
		env = unwrap_clipboard(raw)
		if env is None:
			return
		pane = ui.panes.current
		owner = pane.owner if pane else None
		if owner is None or not owner.isCOMP:
			return
		name = resolve_tdn_name(env.get('tdn'), env.get('slug')) or 'network'
		note = self._clipboardSafetyNote(env)
		choice = self.ownerComp.ext.Embody._messageBox(
			'Embody TDN from clipboard',
			'A TDN named "%s" is on your clipboard.%s\n\n'
			'Embody it into %s as a new COMP?' % (name, note, owner.path),
			buttons=['Embody it', 'Dismiss'])
		if choice == 0:
			self.PasteNetworkAsNewComp()

	def _clipboardSafetyNote(self, env: dict) -> str:
		"""One-line provenance/safety note for the paste prompt.

		Empty for a trusted own 'embody' envelope. For community ('embody.tools')
		content the inner TDN is scanned: a 'clean' specimen pastes live and working;
		anything flagged lists the risky surfaces that will be disabled on paste,
		and reassures that pure value expressions are KEPT (so the network still
		renders). Best-effort -- any scan error degrades to no note."""
		if env.get('source') == 'embody':
			return ''
		try:
			collection = self.ownerComp.op('Collection')
			if collection is None:
				return ''
			cap = collection.ext.Collection.ScanTdn(env.get('tdn')) or {}
		except Exception:
			return ''
		verdict = cap.get('verdict')
		if verdict == 'clean':
			return '\n\nScanned clean -- pastes in live and ready to render.'
		if verdict == 'blocked':
			return ('\n\nImported in safe mode (could not be fully scanned) -- a few surfaces '
					'stay inactive; all parameters and expressions are live.')
		counts = cap.get('counts') or {}
		parts = []
		if counts.get('extensions'):
			parts.append('%d extension(s)' % counts['extensions'])
		if counts.get('execute_dats'):
			parts.append('%d script/callback surface(s)' % counts['execute_dats'])
		if counts.get('web_ops') or counts.get('denylisted_types'):
			parts.append('IO/network op(s)')
		if counts.get('storage_payloads'):
			parts.append('stored data')
		if counts.get('external_refs'):
			parts.append('external reference(s)')
		detail = ', '.join(parts) if parts else 'a few surfaces'
		return ('\n\nFrom embody.tools -- %s imported inactive for safety; all parameters '
				'and expressions stay live, so it renders.' % detail)
