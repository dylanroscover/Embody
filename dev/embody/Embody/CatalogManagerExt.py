"""
CatalogManager - background scanner and cross-build default patching.

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

# Palette .tox stems (case-insensitive) skipped during the one-time
# first-launch scan. They either run invasive init on loadTox (messageBoxes,
# project.cookRate changes, TDImportCache creation) OR fail loudly because
# their dependencies are absent (Ableton Live, VR hardware, Windows-only
# ctypes.windll) - flooding the textport with harmless-but-alarming errors
# that read like Embody is broken. Loss of palette-clone detection for these
# is acceptable; almost nobody diffs them in TDN networks.
_PALETTE_SCAN_BLOCKLIST = frozenset({
	'tdvr',                # forces 90fps, shows VR framerate messageBox
	'autoui',              # shows "Widget Package Required" messageBox
	'tdabletonpackage',    # Ableton bridge - needs Ableton Live + tdAbleton
	'resources',           # VRWorldExt + findMouse (Windows-only windll)
	'world',               # VRWorldExt + findMouse (Windows-only windll)
	'system',              # findMouse (Windows-only ctypes.windll)
})

# Palette .tox stem PREFIXES (case-insensitive) skipped during the scan.
# The Ableton component family (abletonChain, abletonRack, abletonTrack, ...)
# all raise AttributeError on init without a connected tdAbletonPackage.
_PALETTE_SCAN_BLOCKLIST_PREFIXES = ('ableton',)


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
		# Guard against double-starting a scan: EnsureCatalogs can be
		# reached more than once while a chunked scan is still in flight
		# (an op-type-only catalog on disk does not trip the TDNExt
		# idempotency check). Set when a scan starts, cleared when the
		# palette phase finalizes or bails.
		self._scan_in_flight = False
		# Palette results already checkpointed to disk (see
		# _checkpointPaletteScan); lets an interrupted first launch
		# resume instead of restarting from zero (issue #60).
		self._palette_checkpointed = 0
		# (op_catalog, done_results) staged by EnsureCatalogs for the
		# deferred resume (fired by _resumePaletteScan at ~frame 70).
		self._pending_resume = None
		# Timeline / cook state snapshot, re-taken at the START of every
		# palette chunk and restored right after that chunk's loadTox calls.
		# Loading some palette .tox files runs their init code, which can
		# mutate GLOBAL timeline state (pause playback, change cookRate);
		# the per-chunk bracket undoes that without ever fighting the USER:
		# a pause/rate change made between chunks is captured by the next
		# chunk's snapshot and honored, not reverted (issue #60).
		self._time_snapshot = None

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

		# A chunked scan is already working through its queue - don't
		# start a second one on top of it.
		if self._scan_in_flight:
			return

		self._build_str = f'{app.version}.{app.build}'
		catalog_path = self._getCatalogPath(self._build_str)

		if os.path.isfile(catalog_path):
			# Catalog exists - load it
			catalog = self._readCatalog(catalog_path)
			if catalog:
				# Key PRESENCE marks a completed palette phase - an empty
				# dict is the legitimate final state when no palette dir
				# exists; '_palette_partial' marks a mid-scan checkpoint.
				complete = ('_palette' in catalog
							and not catalog.get('_palette_partial'))
				if complete:
					self._populateTDNExt(catalog)
					self._log(f'Loaded catalog for build {self._build_str}')
					# Still check for cross-build patches
					self._patchCrossBuildDefaults(catalog)
					return
				# Incomplete: the op-type half is cached, but the palette
				# phase never started (missing key) or was interrupted
				# (partial checkpoint). Resume it instead of returning
				# with a permanently incomplete catalog (issue #60:
				# killing a struggling TD mid-scan used to restart from
				# zero). Populate the OP-TYPE half only -- pushing a
				# partial palette into TDNExt would trip the idempotency
				# guard above after a mid-resume extension reinit and
				# wedge the resume until the next launch.
				op_catalog = {k: v for k, v in catalog.items()
							  if not k.startswith('_')}
				done = catalog.get('_palette', {})
				self._populateTDNExt(op_catalog)
				self._patchCrossBuildDefaults(op_catalog)
				self._scan_in_flight = True
				self._pending_resume = (op_catalog, done)
				self._log(
					f'Palette catalog incomplete ({len(done)} entries '
					f'cached) - resuming palette scan')
				# Defer past the frame 30-90 restore phases (execute.py):
				# stacking heavy palette loadTox calls on top of
				# RestoreTOXComps / ReconstructTDNComps makes the first
				# seconds of a resumed launch needlessly choppy.
				run('args[0]._resumePaletteScan()', self, delayFrames=60)
				return

		# No catalog - start background scan
		self._log(f'No catalog for build {self._build_str}, scanning...')
		self._scan_in_flight = True
		self._startBackgroundScan()

	def _resumePaletteScan(self):
		"""Fire a deferred palette-scan resume queued by EnsureCatalogs."""
		pending = self._pending_resume
		self._pending_resume = None
		if not pending:
			return
		op_catalog, done = pending
		self._ensurePalette(op_catalog, resume_results=done)

	# =================================================================
	# Background Scan
	# =================================================================

	CHUNK_SIZE = 2  # ops per frame - keeps frame time well under 16ms

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

		self._setScanStatus(f'Scanning defaults (0/{self._scan_total})')
		run('args[0]._processChunk()', self, delayFrames=1)

	def _setScanStatus(self, text):
		"""Write a scan Status value UNLESS Embody is Disabled.

		EnsureCatalogs runs regardless of the Status par (it is gated only
		on Tdnmode), but Update() gates on Status == 'Disabled' -- so a
		scan writing 'Scanning...' / 'Enabled' over 'Disabled' would
		silently re-enable a user's disabled Embody (panel finding).
		"""
		if self.ownerComp.par.Status != 'Disabled':
			self.ownerComp.par.Status = text

	def _processChunk(self):
		"""Drive one op-type scan chunk; a fatal error must not wedge the scan.

		Any exception escaping the chunk body kills the run() chain -- if
		that happened with _scan_in_flight still True, EnsureCatalogs
		could never retry this session (the old code could). Clear the
		flag and log loudly instead. (A LOST run() callback -- no
		exception -- still wedges until the next launch, where the
		checkpoint resume recovers.)
		"""
		try:
			self._processChunkInner()
		except Exception as e:
			self._log(f'Op-type scan aborted: {e}', 'ERROR')
			self._cleanupWorkspace()
			self._scan_in_flight = False

	def _processChunkInner(self):
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
					# Store native Python types - json.dumps handles
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
		self._setScanStatus(
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

		# Persist the op-type half NOW, before the palette phase begins.
		# The palette scan takes seconds, and the combined write used to
		# be the ONLY write - a TD closed/killed mid-palette-scan lost
		# everything and restarted from zero on the next launch (issue
		# #60). With this file on disk (no '_palette' key = palette phase
		# incomplete), the next EnsureCatalogs resumes at the palette
		# phase instead.
		self._writeCatalog(self._getCatalogPath(self._build_str),
						   dict(self._scan_results))

		self._ensurePalette(self._scan_results)

		# Clean up op-type scan state
		self._scan_results = {}
		self._scan_errors = []

	def _ensurePalette(self, op_catalog, resume_results=None):
		"""Fill the palette half of the catalog.

		Adopts the shipped bootstrap palette_catalog table when it covers
		the current build (skips the runtime scan entirely, saving 5-7s
		per TD build on fresh installs); otherwise runs the runtime
		palette scan, resuming past already-checkpointed results when
		resume_results is given.
		"""
		bootstrap_palette = self._loadBootstrapPalette(self._build_str)
		if bootstrap_palette:
			self._log(
				f'Palette bootstrap hit: {len(bootstrap_palette)} entries '
				f'for build {self._build_str} (skipping scan)')
			self._palette_results = bootstrap_palette
			self._op_catalog_pending = op_catalog
			self._finalizePaletteScan()
			return

		# Bootstrap miss - fall back to runtime palette scan.
		self._startPaletteScan(op_catalog, resume_results=resume_results)

	# =================================================================
	# Palette Component Scan
	# =================================================================

	PALETTE_CHUNK_SIZE = 1  # .tox files per frame - some palette .tox are heavy
	PALETTE_CHECKPOINT_EVERY = 25  # partial-catalog write cadence (components)

	def _startPaletteScan(self, op_catalog, resume_results=None):
		"""Begin async scan of all shipped palette .tox components.

		Walks TD's palette directory, loads each .tox into a temp COMP,
		records the placed component's name and OPType, then writes the
		combined catalog (op defaults + _palette mapping) to disk.

		op_catalog is kept in closure so it can be written alongside
		palette results in _finalizePaletteScan. resume_results seeds
		already-scanned components (from a checkpoint written by an
		interrupted earlier scan) so only the remainder is loaded.
		"""
		palette_dir = self._getPaletteDir()
		if not palette_dir:
			# Can't find palette - finalize with an explicit empty
			# palette mapping so the catalog on disk reads as COMPLETE
			# (key presence marks the palette phase done; its absence
			# would trigger a pointless resume on every launch).
			self._palette_results = (
				dict(resume_results) if resume_results else {})
			self._op_catalog_pending = op_catalog
			self._finalizePaletteScan()
			return

		# Enumerate all .tox files, skipping blocklisted palettes whose
		# loadTox triggers invasive init (messageBoxes, cookRate changes).
		rel_paths = []
		skipped = []
		for root, _dirs, files in os.walk(palette_dir):
			for fname in files:
				if not fname.endswith('.tox'):
					continue
				stem = os.path.splitext(fname)[0].lower()
				if stem in _PALETTE_SCAN_BLOCKLIST or stem.startswith(
						_PALETTE_SCAN_BLOCKLIST_PREFIXES):
					skipped.append(stem)
					continue
				full = os.path.join(root, fname)
				rel_paths.append(os.path.relpath(full, palette_dir))
		if skipped:
			self._log(f'Palette scan skipping blocklisted: {sorted(skipped)}')

		# Resume: drop components a prior interrupted scan already
		# recorded (results are keyed by the .tox stem name).
		if resume_results:
			rel_paths = [
				rp for rp in rel_paths
				if os.path.splitext(os.path.basename(rp))[0]
				not in resume_results]

		if not rel_paths:
			# Nothing (left) to scan: empty palette dir, or every
			# component was already checkpointed. Finalize with whatever
			# results exist - writes the complete catalog.
			self._palette_results = (
				dict(resume_results) if resume_results else {})
			self._op_catalog_pending = op_catalog
			self._finalizePaletteScan()
			return

		self._palette_queue = sorted(rel_paths)
		self._palette_results = (
			dict(resume_results) if resume_results else {})
		self._palette_checkpointed = len(self._palette_results)
		self._palette_workspace = self.ownerComp.create(
			baseCOMP, '_palette_workspace')
		self._palette_workspace.viewer = False
		self._palette_workspace.nodeX = -1200
		self._palette_workspace.nodeY = -1800

		self._log(f'Palette scan: {len(self._palette_queue)} components')
		self._log(
			'First-launch only: building the parameter-default catalog from '
			'the TD palette. Any red errors below come from TD palette samples '
			'whose dependencies are absent (Ableton needs Live, VR needs '
			'hardware, some are Windows-only) -- they are HARMLESS, do not '
			'affect Embody or your project, and this one-time scan is cached '
			'so it will not run again for this TD build.', 'INFO')
		# Match the per-chunk formula (done/total incl. resumed results)
		# so the denominator doesn't jump after the first chunk of a
		# resumed scan.
		done = len(self._palette_results)
		self._setScanStatus(
			f'Scanning palette ({done}/{done + len(self._palette_queue)})')

		# Store op_catalog for combined write in _finalizePaletteScan
		self._op_catalog_pending = op_catalog

		# Timeline/cook state is snapshotted per-chunk inside
		# _processPaletteChunk, never here: a scan-wide snapshot would
		# revert user changes (e.g. pausing the timeline mid-scan) on
		# every chunk (issue #60).

		run('args[0]._processPaletteChunk()', self, delayFrames=1)

	def _processPaletteChunk(self):
		"""Drive one palette chunk; a fatal error must not wedge the scan.

		Same rationale as _processChunk -- additionally checkpoint the
		results gathered so far so the next launch resumes rather than
		redoing them.
		"""
		try:
			self._processPaletteChunkInner()
		except Exception as e:
			self._log(f'Palette scan aborted: {e}', 'ERROR')
			try:
				self._checkpointPaletteScan()
			except Exception:
				pass
			self._cleanupWorkspace()
			self._scan_in_flight = False

	def _processPaletteChunkInner(self):
		"""Process a batch of .tox files, then yield to main thread."""
		if not self._palette_queue:
			self._finalizePaletteScan()
			return

		chunk = self._palette_queue[:self.PALETTE_CHUNK_SIZE]
		self._palette_queue = self._palette_queue[self.PALETTE_CHUNK_SIZE:]

		palette_dir = self._getPaletteDir()
		total = len(self._palette_results) + len(self._palette_queue) + len(chunk)

		# Bracket THIS chunk's loadTox calls: snapshot now, restore right
		# after. The snapshot must be per-chunk, not per-scan - a user
		# pausing the timeline between chunks is captured here and
		# honored, while a palette component's own pause/cookRate
		# mutation inside the bracket is still undone (issue #60: the
		# old scan-wide snapshot un-paused the user's timeline after
		# every chunk for the whole scan). Accepted tradeoff: a palette
		# component that mutates timeline state via a DEFERRED run()
		# lands between brackets and reads as user state - if such a
		# component surfaces, add it to _PALETTE_SCAN_BLOCKLIST.
		self._snapshotTimeState()
		try:
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
					# TDAbleton Live 11+ vs Live 9&10 duplicates - same type)
					if name not in self._palette_results:
						self._palette_results[name] = {
							'type': placed_type,
							'min_children': child_count,
						}

					wrapper.destroy()
				except Exception as e:
					self._log(f'Palette scan error {name}: {e}')
					# Guarded: if the WORKSPACE itself is gone (deleted
					# mid-scan), this re-query raises and would otherwise
					# escape the per-item handler and kill the run chain.
					try:
						existing = self._palette_workspace.op(wrapper_name)
						if existing:
							existing.destroy()
					except Exception:
						pass
		finally:
			# Loading a palette component may have paused playback or
			# changed the cook rate - undo this chunk's global side
			# effects before yielding.
			self._restoreTimeState()

		# Checkpoint partial progress so an interrupted scan (user closes
		# a struggling TD mid-first-launch) resumes on the next open
		# instead of restarting from zero (issue #60).
		if (len(self._palette_results) - self._palette_checkpointed
				>= self.PALETTE_CHECKPOINT_EVERY):
			self._checkpointPaletteScan()

		done = len(self._palette_results)
		self._setScanStatus(f'Scanning palette ({done}/{total})')

		# Finalize in-band on last chunk - same guard as op-type scan.
		if not self._palette_queue:
			self._finalizePaletteScan()
			return

		run('args[0]._processPaletteChunk()', self, delayFrames=1)

	def _checkpointPaletteScan(self):
		"""Write the combined catalog with partial palette results.

		The '_palette_partial' marker tells the next EnsureCatalogs that
		the palette phase is incomplete and should resume from these
		results. The final write in _finalizePaletteScan omits the
		marker, making the catalog complete.
		"""
		combined = dict(self._op_catalog_pending or {})
		combined['_palette'] = self._palette_results
		combined['_palette_partial'] = True
		self._writeCatalog(
			self._getCatalogPath(self._build_str), combined)
		self._palette_checkpointed = len(self._palette_results)

	def _finalizePaletteScan(self):
		"""Write combined catalog (op defaults + palette mapping) to disk."""
		if self._palette_workspace is not None:
			try:
				self._palette_workspace.destroy()
			except Exception:
				pass
			self._palette_workspace = None

		# No restore here: each chunk already restored inside its own
		# snapshot/restore bracket, and a restore at finalize time could
		# revert a USER change made in the one-frame gap since the last
		# chunk (issue #60). Just drop the stale snapshot.
		self._time_snapshot = None

		self._log(
			f'Palette scan complete: {len(self._palette_results)} components')
		self._log(
			'Palette catalog cached. Any palette errors printed above were '
			'expected and can be ignored.', 'INFO')

		# Merge palette results into catalog under reserved _palette key
		combined = dict(self._op_catalog_pending or {})
		combined['_palette'] = self._palette_results

		catalog_path = self._getCatalogPath(self._build_str)
		self._writeCatalog(catalog_path, combined)

		# Push palette mapping into TDNExt
		try:
			self.ownerComp.ext.TDN._palette_catalog = self._palette_results
		except Exception:
			pass

		self._setScanStatus('Enabled')
		self._palette_results = {}
		self._op_catalog_pending = None
		self._scan_in_flight = False

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
					tdn_doc = self.ownerComp.ext.TDN.tdn_load(f.read())
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
			# Reserved keys are not op-type param dicts: '_palette_partial'
			# is a bool (.items() would crash -- catalogs loaded from a
			# resume checkpoint carry it) and '_palette' maps component
			# names, not params (it produced garbage 'shifted' entries).
			if op_type.startswith('_') or not isinstance(current_params, dict):
				continue
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
					# Current value matches the new default - user had
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
		Op-type defaults go into _divergent_defaults; palette name->type
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
		"""Write catalog dict to JSON file (atomic: tmp + replace).

		A TD crash mid-write must not leave a truncated
		catalog_<build>.json - _readCatalog would fail to parse it and
		silently trigger a full rescan on the next launch (issue #60).
		"""
		try:
			os.makedirs(os.path.dirname(path), exist_ok=True)
			content = json.dumps(catalog, separators=(',', ':'),
								 sort_keys=True)
			tmp = path + '.tmp'
			with open(tmp, 'w', encoding='utf-8') as f:
				f.write(content)
			os.replace(tmp, path)
			self._log(f'Wrote catalog to {os.path.basename(path)} '
					  f'({len(catalog)} types)')
		except Exception as e:
			self._log(f'Error writing catalog: {e}')
			try:
				os.unlink(path + '.tmp')
			except OSError:
				pass

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
		"""Destroy the scan workspaces if they exist.

		Covers BOTH workspaces: interrupted palette scans are a designed
		state now (checkpoint/resume), so a mid-scan reinit or close must
		not leak a visible _palette_workspace inside Embody.
		"""
		if self._workspace is not None:
			try:
				self._workspace.destroy()
			except Exception:
				pass
			self._workspace = None
		if self._palette_workspace is not None:
			try:
				self._palette_workspace.destroy()
			except Exception:
				pass
			self._palette_workspace = None

	# --- Timeline / cook state guard (around the palette scan) ---------

	# Global state that loading a palette .tox can clobber. Each entry is
	# (label, getter, setter) over the relevant object.
	def _timeStateAccessors(self):
		tl = self.ownerComp.time
		return [
			('play', lambda: tl.play, lambda v: setattr(tl, 'play', v)),
			('rate', lambda: tl.rate, lambda v: setattr(tl, 'rate', v)),
			('cookRate', lambda: project.cookRate,
			 lambda v: setattr(project, 'cookRate', v)),
			('realTime', lambda: project.realTime,
			 lambda v: setattr(project, 'realTime', v)),
		]

	def _snapshotTimeState(self):
		"""Capture global timeline/cook state before the palette scan."""
		snap = {}
		for label, get, _set in self._timeStateAccessors():
			try:
				snap[label] = get()
			except Exception:
				pass
		self._time_snapshot = snap

	def _restoreTimeState(self):
		"""Restore any global timeline/cook state a palette load changed."""
		snap = self._time_snapshot
		if not snap:
			return
		for label, get, set_ in self._timeStateAccessors():
			if label not in snap:
				continue
			try:
				if get() != snap[label]:
					set_(snap[label])
			except Exception:
				pass

	def _log(self, msg, level='INFO'):
		"""Log via Embody's logging system."""
		try:
			self.ownerComp.ext.Embody.Log(
				f'[CatalogManager] {msg}', level)
		except Exception:
			print(f'[CatalogManager] {msg}')
