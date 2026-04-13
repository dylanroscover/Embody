"""
CatalogManager — background scanner and cross-build default patching.

On every startup, checks if a creation-values catalog exists for the
current TD build in .embody/. If not, runs a background scan (1-2 ops
per frame, no dropped frames) and writes the catalog. After scanning,
compares against the source build of each TDN-externalized COMP and
patches any parameters whose creation defaults shifted between builds.
"""

import json
import os


# Parameters to skip during scanning (UI state, not config)
_SKIP_PARAMS = frozenset({
	'pageindex', 'externaltox', 'enableexternaltox',
	'reloadtox', 'reinitextensions', 'savebackup',
	'savecustom', 'reloadcustom',
})

# Parameter styles that are actions, not state
_SKIP_STYLES = frozenset({'Pulse', 'Momentary', 'Header'})

# Operator family suffixes for discovering creatable types
_FAMILIES = ('TOP', 'CHOP', 'SOP', 'DAT', 'MAT', 'COMP', 'POP')

# Abstract base types in the td module that match _FAMILIES suffixes
# but are not creatable operators (e.g. td.CHOP, td.COMP, td.DAT).
_ABSTRACT_TYPES = frozenset({
	'TOP', 'CHOP', 'SOP', 'DAT', 'MAT', 'COMP', 'POP',
	'ObjectCOMP',
})


class CatalogManagerExt:

	def __init__(self, ownerComp):
		self.ownerComp = ownerComp
		self._scan_queue = []
		self._scan_results = {}   # {op_type: {par_name: val}}
		self._scan_total = 0
		self._scan_count = 0
		self._scan_errors = []
		self._workspace = None
		self._build_str = ''
		self._probe_name = '_catalog_probe'
		# Palette scan state
		self._palette_queue = []          # list of rel_path strings
		self._palette_results = {}        # {name: placed_type}
		self._palette_workspace = None

	def onDestroyTD(self):
		"""Clean up workspace if scan was interrupted."""
		self._cleanupWorkspace()

	def onInitTD(self):
		pass

	# =================================================================
	# Startup Entry Point
	# =================================================================

	def EnsureCatalogs(self):
		"""Ensure op-type defaults + palette catalog are loaded into TDN.

		Called from execute.py onStart and onCreate. Non-blocking.
		  - If .embody/catalog_<build>.json exists: loads from disk (fast).
		  - Otherwise: runs async op-type scan, then bootstrap-palette
		    lookup (or runtime palette scan as fallback), writes cache.
		Idempotent: safe to call repeatedly; returns early when already loaded.
		"""
		# Idempotent: onStart and onCreate both call this; skip when
		# the current run already populated the catalog.
		try:
			tdn_ext = self.ownerComp.ext.TDN
			if tdn_ext._divergent_loaded and tdn_ext._palette_catalog:
				return
		except Exception:
			pass

		self._build_str = f'{app.version}.{app.build}'
		catalog_path = self._getCatalogPath(self._build_str)

		if os.path.isfile(catalog_path):
			# Catalog exists — load it
			catalog = self._readCatalog(catalog_path)
			if catalog:
				self._populateTDNExt(catalog)
				self._log(f'Loaded catalog for build {self._build_str}')
				# Still check for cross-build patches
				self._patchCrossBuildDefaults(catalog)
				return

		# No catalog — start background scan
		self._log(f'No catalog for build {self._build_str}, scanning...')
		self._startBackgroundScan()

	# =================================================================
	# Background Scan
	# =================================================================

	CHUNK_SIZE = 2  # ops per frame — keeps frame time well under 16ms

	def _startBackgroundScan(self):
		"""Begin async scan of all creatable op types."""
		import td as _td

		self._scan_queue = sorted([
			name for name in dir(_td)
			if isinstance(getattr(_td, name, None), type)
			and any(name.endswith(f) for f in _FAMILIES)
			and name not in _ABSTRACT_TYPES
		])
		self._scan_total = len(self._scan_queue)
		self._scan_count = 0
		self._scan_results = {}
		self._scan_errors = []

		# Create hidden workspace inside Embody
		self._workspace = self.ownerComp.create(baseCOMP, '_catalog_workspace')
		self._workspace.viewer = False
		self._workspace.nodeX = -1200
		self._workspace.nodeY = -1600

		self.ownerComp.par.Status = f'Scanning defaults (0/{self._scan_total})'
		run('args[0]._processChunk()', self, delayFrames=1)

	def _processChunk(self):
		"""Process a batch of op types, then yield to main thread."""
		if not self._scan_queue:
			self._finalizeScan()
			return

		chunk = self._scan_queue[:self.CHUNK_SIZE]
		self._scan_queue = self._scan_queue[self.CHUNK_SIZE:]

		import td as _td

		for cls_name in chunk:
			cls = getattr(_td, cls_name, None)
			if cls is None:
				continue

			self._scan_count += 1
			try:
				temp = self._workspace.create(cls, self._probe_name)
				params = {}
				for p in temp.pars():
					if p.isCustom or p.readOnly:
						continue
					if p.sequence is not None:
						continue
					if p.name in _SKIP_PARAMS:
						continue
					if p.style in _SKIP_STYLES:
						continue
					# Skip name-dependent values (callback DATs etc.)
					val = p.val
					if self._probe_name in str(val):
						continue
					# Store native Python types — json.dumps handles
					# int, float, bool, str natively. This preserves
					# type info so _valuesDiffer comparisons work
					# correctly (float 5.0 vs float 5.0, not "5" vs 5.0).
					if isinstance(val, bool):
						params[p.name] = val
					elif isinstance(val, float):
						params[p.name] = val
					elif isinstance(val, int):
						params[p.name] = val
					else:
						params[p.name] = str(val)
				self._scan_results[cls_name] = params
				temp.destroy()
			except Exception as e:
				self._scan_errors.append(f'{cls_name}: {e}')

		# Update status
		self.ownerComp.par.Status = (
			f'Scanning defaults ({self._scan_count}/{self._scan_total})')

		# Finalize in-band when the last chunk finishes. Otherwise, under
		# concurrent heavy work (venv creation during fresh-project
		# startup), the scheduled run() callback can be lost and the
		# scan stalls at "N/N" indefinitely.
		if not self._scan_queue:
			self._finalizeScan()
			return

		# Schedule next chunk
		run('args[0]._processChunk()', self, delayFrames=1)

	def _finalizeScan(self):
		"""Write op-type catalog to disk, load into TDNExt, start palette scan."""
		self._cleanupWorkspace()

		if self._scan_errors:
			# DEBUG: abstract base classes and arg-requiring ops legitimately
			# fail the bare-create probe. Non-actionable for users.
			self._log(f'Scan skipped {len(self._scan_errors)} non-instantiable '
					  f'types (first 5: {self._scan_errors[:5]})', 'DEBUG')

		self._log(f'Scan complete: {self._scan_count} types, '
				  f'{sum(len(v) for v in self._scan_results.values())} params')

		# Load op-type defaults into TDNExt immediately (palette scan follows)
		self._populateTDNExt(self._scan_results)

		# Run cross-build patch check (uses op-type catalog)
		self._patchCrossBuildDefaults(self._scan_results)

		# Try bootstrap palette_catalog tableDAT first — if it covers
		# the current build, skip the palette scan entirely (saves 5-7s
		# per TD build on fresh installs).
		bootstrap_palette = self._loadBootstrapPalette(self._build_str)
		if bootstrap_palette:
			self._log(
				f'Palette bootstrap hit: {len(bootstrap_palette)} entries '
				f'for build {self._build_str} (skipping scan)')
			self._palette_results = bootstrap_palette
			self._op_catalog_pending = self._scan_results
			self._scan_results = {}
			self._scan_errors = []
			self._finalizePaletteScan()
			return

		# Bootstrap miss — fall back to runtime palette scan.
		self._startPaletteScan(self._scan_results)

		# Clean up op-type scan state
		self._scan_results = {}
		self._scan_errors = []

	# =================================================================
	# Palette Component Scan
	# =================================================================

	PALETTE_CHUNK_SIZE = 1  # .tox files per frame — some palette .tox are heavy

	def _startPaletteScan(self, op_catalog):
		"""Begin async scan of all shipped palette .tox components.

		Walks TD's palette directory, loads each .tox into a temp COMP,
		records the placed component's name and OPType, then writes the
		combined catalog (op defaults + _palette mapping) to disk.

		op_catalog is kept in closure so it can be written alongside
		palette results in _finalizePaletteScan.
		"""
		palette_dir = self._getPaletteDir()
		if not palette_dir:
			# Can't find palette — write op-type-only catalog and finish
			self._writeCatalog(self._getCatalogPath(self._build_str), op_catalog)
			self.ownerComp.par.Status = 'Enabled'
			return

		# Enumerate all .tox files
		rel_paths = []
		for root, _dirs, files in os.walk(palette_dir):
			for fname in files:
				if fname.endswith('.tox'):
					full = os.path.join(root, fname)
					rel_paths.append(os.path.relpath(full, palette_dir))

		if not rel_paths:
			self._writeCatalog(self._getCatalogPath(self._build_str), op_catalog)
			self.ownerComp.par.Status = 'Enabled'
			return

		self._palette_queue = sorted(rel_paths)
		self._palette_results = {}
		self._palette_workspace = self.ownerComp.create(
			baseCOMP, '_palette_workspace')
		self._palette_workspace.viewer = False
		self._palette_workspace.nodeX = -1200
		self._palette_workspace.nodeY = -1800

		self._log(f'Palette scan: {len(self._palette_queue)} components')
		self.ownerComp.par.Status = (
			f'Scanning palette (0/{len(self._palette_queue)})')

		# Store op_catalog for combined write in _finalizePaletteScan
		self._op_catalog_pending = op_catalog

		run('args[0]._processPaletteChunk()', self, delayFrames=1)

	def _processPaletteChunk(self):
		"""Process a batch of .tox files, then yield to main thread."""
		if not self._palette_queue:
			self._finalizePaletteScan()
			return

		chunk = self._palette_queue[:self.PALETTE_CHUNK_SIZE]
		self._palette_queue = self._palette_queue[self.PALETTE_CHUNK_SIZE:]

		palette_dir = self._getPaletteDir()
		total = len(self._palette_results) + len(self._palette_queue) + len(chunk)

		for rel_path in chunk:
			tox_path = os.path.join(palette_dir, rel_path)
			name = os.path.splitext(os.path.basename(rel_path))[0]
			wrapper_name = '_pp_' + name[:28]  # short unique name
			try:
				existing = self._palette_workspace.op(wrapper_name)
				if existing:
					existing.destroy()

				wrapper = self._palette_workspace.create(baseCOMP, wrapper_name)
				wrapper.loadTox(tox_path)

				# Determine placed type: if the inner child has the same name
				# as the .tox file, that child IS what TD places in the project.
				# Otherwise the wrapper itself is the placed component.
				children = wrapper.children
				placed_type = wrapper.OPType  # fallback
				if children:
					inner = children[0]
					if inner.name == name:
						placed_type = inner.OPType

				# Child count for false-positive rejection: a user-created
				# COMP with the same name would typically be empty. Record
				# the inner child count (not the wrapper) as a floor.
				if children and inner.name == name:
					child_count = len(inner.children)
				else:
					child_count = len(wrapper.children)

				# Only record the first occurrence of each name (handles
				# TDAbleton Live 11+ vs Live 9&10 duplicates — same type)
				if name not in self._palette_results:
					self._palette_results[name] = {
						'type': placed_type,
						'min_children': child_count,
					}

				wrapper.destroy()
			except Exception as e:
				self._log(f'Palette scan error {name}: {e}')
				existing = self._palette_workspace.op(wrapper_name)
				if existing:
					existing.destroy()

		done = len(self._palette_results)
		self.ownerComp.par.Status = f'Scanning palette ({done}/{total})'

		# Finalize in-band on last chunk — same guard as op-type scan.
		if not self._palette_queue:
			self._finalizePaletteScan()
			return

		run('args[0]._processPaletteChunk()', self, delayFrames=1)

	def _finalizePaletteScan(self):
		"""Write combined catalog (op defaults + palette mapping) to disk."""
		if self._palette_workspace is not None:
			try:
				self._palette_workspace.destroy()
			except Exception:
				pass
			self._palette_workspace = None

		self._log(
			f'Palette scan complete: {len(self._palette_results)} components')

		# Merge palette results into catalog under reserved _palette key
		combined = dict(getattr(self, '_op_catalog_pending', {}))
		combined['_palette'] = self._palette_results

		catalog_path = self._getCatalogPath(self._build_str)
		self._writeCatalog(catalog_path, combined)

		# Push palette mapping into TDNExt
		try:
			self.ownerComp.ext.TDN._palette_catalog = self._palette_results
		except Exception:
			pass

		self.ownerComp.par.Status = 'Enabled'
		self._palette_results = {}
		self._op_catalog_pending = None

	# =================================================================
	# Cross-Build Patching
	# =================================================================

	def _patchCrossBuildDefaults(self, current_catalog):
		"""Compare catalogs across builds and patch shifted defaults.

		For each TDN-externalized COMP, reads the td_build from its .tdn
		file, loads that build's catalog, and patches any params whose
		creation default changed between builds.
		"""
		current_build = self._build_str
		patches = []  # [(op_path, par_name, old_val, new_val)]

		try:
			tdn_comps = self.ownerComp.ext.Embody._getTDNStrategyComps()
		except Exception:
			return

		if not tdn_comps:
			return

		# Cache loaded source catalogs to avoid re-reading
		source_catalogs = {}

		for comp_path, rel_tdn_path in tdn_comps:
			# Read td_build from the .tdn file header
			try:
				abs_path = str(self.ownerComp.ext.Embody.buildAbsolutePath(
					rel_tdn_path))
				if not os.path.isfile(abs_path):
					continue
				with open(abs_path, 'r', encoding='utf-8') as f:
					tdn_doc = json.loads(f.read())
				source_build = tdn_doc.get('td_build', '')
			except Exception:
				continue

			if not source_build or source_build == current_build:
				continue

			# Load source build catalog
			if source_build not in source_catalogs:
				source_path = self._getCatalogPath(source_build)
				if os.path.isfile(source_path):
					source_catalogs[source_build] = self._readCatalog(
						source_path)
				else:
					source_catalogs[source_build] = None

			source_catalog = source_catalogs[source_build]
			if not source_catalog:
				continue

			# Find params that shifted between builds
			shifted = self._findShiftedDefaults(
				source_catalog, current_catalog)
			if not shifted:
				continue

			# Patch operators in this COMP
			comp = op(comp_path)
			if not comp:
				continue

			comp_patches = self._patchComp(comp, shifted, source_catalog)
			patches.extend(comp_patches)

		if patches:
			self._showPatchSummary(patches, current_build)

	def _findShiftedDefaults(self, source_catalog, current_catalog):
		"""Find params whose creation default changed between two builds.

		Returns: {op_type: {par_name: (old_val, new_val)}}
		"""
		shifted = {}
		for op_type, current_params in current_catalog.items():
			source_params = source_catalog.get(op_type, {})
			for par_name, current_val in current_params.items():
				source_val = source_params.get(par_name)
				if source_val is not None and source_val != current_val:
					if op_type not in shifted:
						shifted[op_type] = {}
					shifted[op_type][par_name] = (source_val, current_val)
		return shifted

	def _patchComp(self, comp, shifted, source_catalog):
		"""Patch operators in a COMP where defaults shifted.

		Only patches params where the current value equals the NEW default
		(meaning the user had the OLD default, which was omitted from TDN,
		and TD created it with the wrong new default).

		Returns list of (op_path, par_name, old_val, new_val) tuples.
		"""
		patches = []

		for child in comp.findChildren(depth=-1, includeUtility=False):
			op_type = child.OPType
			if op_type not in shifted:
				continue

			for par_name, (old_val, new_val) in shifted[op_type].items():
				par = getattr(child.par, par_name, None)
				if par is None:
					continue
				if par.mode != ParMode.CONSTANT:
					continue

				current_val = par.val
				# Compare typed values directly
				if self._valuesEqual(current_val, new_val):
					# Current value matches the new default — user had
					# the old default, it was omitted, TD set the new one.
					# Restore the old default.
					self._setParFromCatalogVal(par, old_val)
					patches.append((
						child.path, par_name,
						str(new_val), str(old_val)))

		return patches

	def _showPatchSummary(self, patches, current_build):
		"""Show a summary dialog of cross-build patches applied."""
		lines = []
		for op_path, par_name, from_val, to_val in patches[:20]:
			short = op_path.split('/')[-1]
			lines.append(f'  \u2022 {short}.{par_name}: {from_val} \u2192 {to_val}')

		count = len(patches)
		if count > 20:
			lines.append(f'  ... and {count - 20} more')

		msg = (
			f'{count} parameter{"s" if count != 1 else ""} updated '
			f'to preserve original values:\n\n'
			+ '\n'.join(lines)
		)

		self.ownerComp.ext.Embody._messageBox(
			'Embody \u2014 Cross-Build Parameter Update',
			msg,
			buttons=['OK'])

		self._log(f'Cross-build patch: {count} params updated')
		for op_path, par_name, from_val, to_val in patches:
			self._log(f'  {op_path}.{par_name}: {from_val} -> {to_val}')

	# =================================================================
	# TDNExt Integration
	# =================================================================

	def _populateTDNExt(self, catalog):
		"""Load catalog data into TDNExt.

		Separates the reserved _palette key from op-type parameter data.
		Op-type defaults go into _divergent_defaults; palette name→type
		mapping goes into _palette_catalog.
		"""
		try:
			tdn_ext = self.ownerComp.ext.TDN
		except Exception:
			return

		palette = catalog.get('_palette', {})
		if palette:
			tdn_ext._palette_catalog = palette

		# Strip reserved keys so op-type lookup stays clean
		param_catalog = {k: v for k, v in catalog.items()
						 if not k.startswith('_')}
		tdn_ext._divergent_defaults = param_catalog
		tdn_ext._divergent_loaded = True

	# =================================================================
	# Bootstrap Palette Catalog (shipped tableDAT)
	# =================================================================

	def _loadBootstrapPalette(self, build_str):
		"""Load palette catalog from the embedded palette_catalog tableDAT.

		Returns {name: {'type': str, 'min_children': int}} filtered to
		rows matching build_str, or None if the table is missing or has
		no rows for this build.

		Schema: name | type | min_children | build
		"""
		table = self.ownerComp.op('palette_catalog')
		return self._parseBootstrapPaletteTable(table, build_str)

	def _parseBootstrapPaletteTable(self, table, build_str):
		"""Pure parsing of a palette_catalog-shaped table. Testable."""
		if table is None or table.numRows < 2:
			return None

		try:
			headers = [table[0, c].val for c in range(table.numCols)]
			col_name = headers.index('name')
			col_type = headers.index('type')
			col_min = headers.index('min_children')
			col_build = headers.index('build')
		except (ValueError, Exception) as e:
			self._log(f'Bootstrap palette: bad schema: {e}', 'WARNING')
			return None

		result = {}
		for row_idx in range(1, table.numRows):
			row_build = table[row_idx, col_build].val
			if row_build != build_str:
				continue
			name = table[row_idx, col_name].val
			if not name or name in result:
				continue
			try:
				min_children = int(table[row_idx, col_min].val or 0)
			except ValueError:
				min_children = 0
			result[name] = {
				'type': table[row_idx, col_type].val,
				'min_children': min_children,
			}
		return result if result else None

	def ExportPaletteCatalog(self):
		"""Dev utility: export the live _palette_catalog to palette_catalog tableDAT.

		Call after a successful runtime palette scan on a new TD build
		to bake the results into the shipped bootstrap. Appends rows for
		the current build; does not remove rows for other builds.
		"""
		try:
			palette = dict(self.ownerComp.ext.TDN._palette_catalog)
		except Exception as e:
			self._log(f'ExportPaletteCatalog: no palette catalog: {e}', 'ERROR')
			return
		if not palette:
			self._log('ExportPaletteCatalog: palette catalog is empty', 'WARNING')
			return

		table = self.ownerComp.op('palette_catalog')
		if table is None:
			self._log(
				'ExportPaletteCatalog: palette_catalog tableDAT not found',
				'ERROR')
			return

		build_str = f'{app.version}.{app.build}'

		# Ensure header row exists
		if table.numRows == 0:
			table.appendRow(['name', 'type', 'min_children', 'build'])

		# Drop existing rows for this build (rewrite)
		headers = [table[0, c].val for c in range(table.numCols)]
		col_build = headers.index('build')
		rows_to_delete = []
		for r in range(table.numRows - 1, 0, -1):
			if table[r, col_build].val == build_str:
				rows_to_delete.append(r)
		for r in rows_to_delete:
			table.deleteRow(r)

		# Append current entries (sorted by name for stable diffs)
		for name in sorted(palette):
			entry = palette[name]
			if isinstance(entry, dict):
				t = entry.get('type', '')
				mc = entry.get('min_children', 0)
			else:
				t = entry
				mc = 0
			table.appendRow([name, t, mc, build_str])

		self._log(
			f'ExportPaletteCatalog: wrote {len(palette)} rows for '
			f'build {build_str}', 'SUCCESS')

	# =================================================================
	# Palette Path Helper
	# =================================================================

	@staticmethod
	def _getPaletteDir():
		"""Return the absolute path to TD's shipped palette directory.

		Tries the macOS app bundle path first, then the Windows/flat path.
		Returns None if neither exists.
		"""
		candidates = [
			# macOS app bundle
			os.path.join(app.installFolder,
						 'Contents', 'Resources', 'tfs', 'Samples', 'Palette'),
			# Windows / flat install
			os.path.join(app.installFolder, 'Samples', 'Palette'),
		]
		for path in candidates:
			if os.path.isdir(path):
				return path
		return None

	# =================================================================
	# File I/O
	# =================================================================

	def _getCatalogPath(self, build_str):
		"""Path to .embody/catalog_{build}.json."""
		root = self._findProjectRoot()
		return os.path.join(root, '.embody', f'catalog_{build_str}.json')

	def _findProjectRoot(self):
		"""Find the project root via EmbodyExt (walks up for .git).

		Delegates to EmbodyExt._findProjectRoot() which checks _git_root
		storage first, then walks up from project.folder looking for .git.
		This avoids the path mismatch where project.folder differs from the
		git root (e.g. dev/ vs repo root), which caused duplicate catalogs.
		"""
		try:
			return str(self.ownerComp.ext.Embody._findProjectRoot())
		except Exception:
			return str(project.folder)

	def _readCatalog(self, path):
		"""Read a catalog JSON file. Returns dict or None."""
		try:
			with open(path, 'r', encoding='utf-8') as f:
				return json.loads(f.read())
		except Exception as e:
			self._log(f'Error reading catalog {path}: {e}')
			return None

	def _writeCatalog(self, path, catalog):
		"""Write catalog dict to JSON file."""
		try:
			os.makedirs(os.path.dirname(path), exist_ok=True)
			content = json.dumps(catalog, separators=(',', ':'),
								 sort_keys=True)
			with open(path, 'w', encoding='utf-8') as f:
				f.write(content)
			self._log(f'Wrote catalog to {os.path.basename(path)} '
					  f'({len(catalog)} types)')
		except Exception as e:
			self._log(f'Error writing catalog: {e}')

	# =================================================================
	# Value Helpers
	# =================================================================

	@staticmethod
	def _valuesEqual(a, b):
		"""Compare two values with float tolerance."""
		if isinstance(a, float) and isinstance(b, (float, int)):
			return abs(a - float(b)) < 1e-9
		if isinstance(b, float) and isinstance(a, (float, int)):
			return abs(float(a) - b) < 1e-9
		return a == b

	@staticmethod
	def _setParFromCatalogVal(par, val):
		"""Set a parameter from a catalog value (already typed)."""
		try:
			par.val = val
		except Exception:
			pass

	# =================================================================
	# Utilities
	# =================================================================

	def _cleanupWorkspace(self):
		"""Destroy the scan workspace if it exists."""
		if self._workspace is not None:
			try:
				self._workspace.destroy()
			except Exception:
				pass
			self._workspace = None

	def _log(self, msg, level='INFO'):
		"""Log via Embody's logging system."""
		try:
			self.ownerComp.ext.Embody.Log(
				f'[CatalogManager] {msg}', level)
		except Exception:
			print(f'[CatalogManager] {msg}')
