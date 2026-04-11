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

	def onDestroyTD(self):
		"""Clean up workspace if scan was interrupted."""
		self._cleanupWorkspace()

	def onInitTD(self):
		pass

	# =================================================================
	# Startup Entry Point
	# =================================================================

	def CheckAndScan(self):
		"""Check if current build has a catalog. Scan in background if not.

		Called at startup (frame 10) from execute.py. Non-blocking.
		If the catalog exists, loads it into TDNExt immediately.
		If not, starts an async background scan.
		"""
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

		# Schedule next chunk
		run('args[0]._processChunk()', self, delayFrames=1)

	def _finalizeScan(self):
		"""Write catalog to disk and load into TDNExt."""
		self._cleanupWorkspace()

		if self._scan_errors:
			self._log(f'Scan errors ({len(self._scan_errors)}): '
					  f'{self._scan_errors[:5]}')

		self._log(f'Scan complete: {self._scan_count} types, '
				  f'{sum(len(v) for v in self._scan_results.values())} params')

		# Write catalog file
		catalog_path = self._getCatalogPath(self._build_str)
		self._writeCatalog(catalog_path, self._scan_results)

		# Load into TDNExt
		self._populateTDNExt(self._scan_results)

		# Restore status
		self.ownerComp.par.Status = 'Enabled'

		# Run cross-build patch check
		self._patchCrossBuildDefaults(self._scan_results)

		# Clean up scan state
		self._scan_results = {}
		self._scan_errors = []

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
		"""Load catalog data into TDNExt's divergent defaults cache.

		Loads the full catalog. TDNExt's _buildParCache uses
		divergent.get(name, p.default) — for params in the catalog,
		the catalog value is used; for params not in the catalog
		(shouldn't happen with a full catalog), p.default is used.
		"""
		try:
			tdn_ext = self.ownerComp.ext.TDN
		except Exception:
			return

		tdn_ext._divergent_defaults = catalog
		tdn_ext._divergent_loaded = True

	# =================================================================
	# File I/O
	# =================================================================

	def _getCatalogPath(self, build_str):
		"""Path to .embody/catalog_{build}.json."""
		root = self._findProjectRoot()
		return os.path.join(root, '.embody', f'catalog_{build_str}.json')

	def _findProjectRoot(self):
		"""Find the project root (git root or project folder)."""
		try:
			git_root = self.ownerComp.fetch('_git_root', None, search=False)
			if git_root:
				return str(git_root)
		except Exception:
			pass
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

	def _log(self, msg):
		"""Log via Embody's logging system."""
		try:
			self.ownerComp.ext.Embody.Log(
				f'[CatalogManager] {msg}', 'INFO')
		except Exception:
			print(f'[CatalogManager] {msg}')
