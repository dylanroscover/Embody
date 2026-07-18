"""
Test suite: CatalogManager palette-scan timeline guard + scan resilience.

Loading a shipped palette .tox during the runtime palette scan runs that
component's init code, which can mutate GLOBAL timeline state (pause
playback, change cookRate). CatalogManager snapshots that state at the
START of every chunk and restores it right after that chunk's loadTox
calls -- undoing a palette component's side effects WITHOUT reverting user
changes made between chunks (issue #60: the old scan-wide snapshot
un-paused a user's mid-scan pause after every chunk).

- _snapshotTimeState / _restoreTimeState round-trip the relevant globals
- restore is a no-op when nothing changed and when no snapshot was taken
- restore puts back play, rate, cookRate, realTime when a load clobbers them
- a user pause made BETWEEN chunks survives the next chunk (per-chunk bracket)
- interrupted scans checkpoint partial palette results and resume (issue #60)
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestCatalogPaletteScanTimelineGuard(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.cat = self.embody.ext.CatalogManager
		self.tl = self.embody.time
		# Remember the real state so the suite leaves nothing dirty.
		self._orig = {
			'play': self.tl.play,
			'rate': self.tl.rate,
			'cookRate': project.cookRate,
			'realTime': project.realTime,
		}
		self.cat._time_snapshot = None

	def tearDown(self):
		self.cat._time_snapshot = None
		try:
			self.tl.play = self._orig['play']
			self.tl.rate = self._orig['rate']
			project.cookRate = self._orig['cookRate']
			project.realTime = self._orig['realTime']
		finally:
			super().tearDown()

	# =================================================================
	# Round-trip
	# =================================================================

	def test_A01_restore_undoes_pause(self):
		"""A palette load that pauses the timeline is undone on restore."""
		self.tl.play = True
		self.cat._snapshotTimeState()
		self.tl.play = False  # simulate a misbehaving palette component
		self.cat._restoreTimeState()
		self.assertTrue(self.tl.play,
			'restore must put playback back the way the snapshot found it')

	def test_A02_restore_undoes_cookrate_change(self):
		"""A palette load that changes cookRate is undone on restore."""
		project.cookRate = 60
		self.cat._snapshotTimeState()
		project.cookRate = 24  # simulate e.g. tdvr forcing a frame rate
		self.cat._restoreTimeState()
		self.assertEqual(project.cookRate, 60)

	def test_A03_restore_undoes_realtime_change(self):
		"""A palette load that flips realTime is undone on restore."""
		project.realTime = True
		self.cat._snapshotTimeState()
		project.realTime = False
		self.cat._restoreTimeState()
		self.assertTrue(project.realTime)

	def test_A04_snapshot_then_no_change_then_restore_is_noop(self):
		"""Restore with an unchanged state leaves everything alone."""
		self.tl.play = True
		project.cookRate = 60
		self.cat._snapshotTimeState()
		self.cat._restoreTimeState()
		self.assertTrue(self.tl.play)
		self.assertEqual(project.cookRate, 60)

	def test_A05_restore_without_snapshot_is_noop(self):
		"""Restore is safe (and does nothing) when no snapshot was taken."""
		self.cat._time_snapshot = None
		self.tl.play = False
		self.cat._restoreTimeState()  # must not raise, must not touch state
		self.assertFalse(self.tl.play)

	def test_A06_snapshot_captures_all_tracked_keys(self):
		"""The snapshot dict carries every key _restoreTimeState looks for."""
		self.cat._snapshotTimeState()
		snap = self.cat._time_snapshot
		self.assertIsInstance(snap, dict)
		for key in ('play', 'rate', 'cookRate', 'realTime'):
			self.assertIn(key, snap, f'snapshot missing {key!r}')


class TestCatalogPaletteScanChunkBracket(EmbodyTestCase):
	"""Per-chunk snapshot bracket: a user pause between chunks is honored.

	Issue #60 regression: the old implementation snapshotted timeline state
	ONCE at scan start and re-imposed it after every chunk, so a user who
	paused the timeline mid-scan was un-paused chunk after chunk. The
	bracket must re-snapshot at chunk start (adopting the user's change)
	and only undo mutations made INSIDE the chunk.
	"""

	def setUp(self):
		super().setUp()
		import os, tempfile
		self.cat = self.embody.ext.CatalogManager
		self.tl = self.embody.time
		self._orig = {
			'play': self.tl.play,
			'rate': self.tl.rate,
			'cookRate': project.cookRate,
			'realTime': project.realTime,
		}
		self._orig_status = str(self.embody.par.Status)
		# Scratch dir holding a real (empty) mini .tox the chunk can load.
		self._tmpdir = tempfile.mkdtemp(prefix='embody_palette_test_')
		mini = self.sandbox.create(baseCOMP, 'minipalette')
		self._tox_path = os.path.join(self._tmpdir, 'minipalette.tox')
		mini.save(self._tox_path)
		mini.destroy()
		# Saved CatalogManager state to restore.
		self._cat_saved = {
			'_palette_queue': self.cat._palette_queue,
			'_palette_results': self.cat._palette_results,
			'_palette_checkpointed': self.cat._palette_checkpointed,
			'_op_catalog_pending': getattr(self.cat, '_op_catalog_pending', None),
			'_time_snapshot': self.cat._time_snapshot,
			'_palette_workspace': self.cat._palette_workspace,
		}
		# Defensive: keep any accidental checkpoint/finalize write off the
		# REAL catalog file, whatever future cadence constants do.
		self._scratch_catalog = os.path.join(
			self._tmpdir, 'catalog_scratch.json')
		self.cat._getCatalogPath = lambda build: self._scratch_catalog
		# The chunk body now writes the freeze sentinel before every
		# load - keep it off the REAL .embody path, and off the live
		# blocked set (the suite's mini tox must never be convicted).
		self._sentinel_path = os.path.join(
			self._tmpdir, 'palette_scan_inflight.json')
		self.cat._inflightSentinelPath = lambda: self._sentinel_path
		self._saved_blocked = set(self.cat._palette_blocked)
		self._finalize_calls = []

	def tearDown(self):
		import os
		for name in ('_getPaletteDir', '_finalizePaletteScan',
					 '_getCatalogPath', '_inflightSentinelPath'):
			self.cat.__dict__.pop(name, None)
		self.cat._palette_blocked = self._saved_blocked
		try:
			os.unlink(self._sentinel_path)
		except OSError:
			pass
		for key, val in self._cat_saved.items():
			setattr(self.cat, key, val)
		self.embody.par.Status = self._orig_status
		for fname in (self._tox_path, self._scratch_catalog,
					  self._scratch_catalog + '.tmp'):
			try:
				os.unlink(fname)
			except OSError:
				pass
		try:
			os.rmdir(self._tmpdir)
		except OSError:
			pass
		try:
			self.tl.play = self._orig['play']
			self.tl.rate = self._orig['rate']
			project.cookRate = self._orig['cookRate']
			project.realTime = self._orig['realTime']
		finally:
			super().tearDown()

	def test_B01_user_pause_between_chunks_survives_next_chunk(self):
		"""A stale play=True snapshot must NOT un-pause a user's pause."""
		self.cat._palette_queue = ['minipalette.tox']
		self.cat._palette_results = {}
		self.cat._palette_checkpointed = 0
		self.cat._op_catalog_pending = {}
		self.cat._palette_workspace = self.sandbox.create(
			baseCOMP, 'palette_ws')
		self.cat._getPaletteDir = lambda: self._tmpdir
		self.cat._finalizePaletteScan = (
			lambda: self._finalize_calls.append(True))
		# Stage the OLD bug's precondition: a snapshot from scan start
		# claiming the timeline was playing...
		self.cat._time_snapshot = {
			'play': True,
			'rate': self.tl.rate,
			'cookRate': project.cookRate,
			'realTime': project.realTime,
		}
		# ...then the user pauses between chunks.
		self.tl.play = False

		self.cat._processPaletteChunk()

		self.assertFalse(self.tl.play,
			'per-chunk bracket must adopt the user pause, not restore the '
			'stale play=True snapshot (issue #60)')
		self.assertIn('minipalette', self.cat._palette_results,
			'the chunk must actually have loaded the mini palette tox')
		self.assertTrue(self._finalize_calls,
			'an emptied queue must finalize in-band')

	def test_B02_chunk_undoes_mutation_made_inside_bracket(self):
		"""Mutations between snapshot and restore are still undone."""
		self.cat._palette_queue = []
		self.cat._palette_results = {}
		# Simulate the bracket directly: snapshot with the CURRENT state,
		# mutate (as a palette load would), restore.
		self.tl.play = True
		self.cat._snapshotTimeState()
		self.tl.play = False
		self.cat._restoreTimeState()
		self.assertTrue(self.tl.play,
			'a mutation inside the bracket must still be undone')


class TestCatalogScanResume(EmbodyTestCase):
	"""Interrupted-scan resilience: checkpoints, resume routing, atomic write.

	Issue #60: the catalog was only written at the very END of the full
	palette scan, so closing a struggling TD mid-first-launch restarted the
	whole scan (op-type probe + every palette .tox) on every subsequent
	launch, forever.
	"""

	def setUp(self):
		super().setUp()
		import os, tempfile
		self.cat = self.embody.ext.CatalogManager
		self.tdn = self.embody.ext.TDN
		self._tmpdir = tempfile.mkdtemp(prefix='embody_catalog_test_')
		self._catalog_path = os.path.join(self._tmpdir, 'catalog_test.json')
		self._orig_status = str(self.embody.par.Status)
		self._tdn_saved = (
			self.tdn._divergent_loaded,
			self.tdn._divergent_defaults,
			self.tdn._palette_catalog,
		)
		self._cat_saved = {
			'_scan_in_flight': self.cat._scan_in_flight,
			'_palette_results': self.cat._palette_results,
			'_palette_checkpointed': self.cat._palette_checkpointed,
			'_op_catalog_pending': getattr(self.cat, '_op_catalog_pending', None),
			'_build_str': self.cat._build_str,
			'_pending_resume': getattr(self.cat, '_pending_resume', None),
			'_palette_blocked': set(self.cat._palette_blocked),
		}
		# _startPaletteScan consumes the freeze sentinel - keep the REAL
		# .embody sentinel out of reach so a leftover from another suite
		# (or a real scan) is neither consumed nor convicted here.
		self._sentinel_path = os.path.join(
			self._tmpdir, 'palette_scan_inflight.json')
		self.cat._inflightSentinelPath = lambda: self._sentinel_path
		self._ensure_calls = []

	def tearDown(self):
		import os
		for name in ('_getCatalogPath', '_ensurePalette',
					 '_patchCrossBuildDefaults', '_getPaletteDir',
					 '_finalizePaletteScan', '_inflightSentinelPath'):
			self.cat.__dict__.pop(name, None)
		for key, val in self._cat_saved.items():
			setattr(self.cat, key, val)
		(self.tdn._divergent_loaded,
		 self.tdn._divergent_defaults,
		 self.tdn._palette_catalog) = self._tdn_saved
		self.embody.par.Status = self._orig_status
		for fname in list(os.listdir(self._tmpdir)):
			try:
				os.unlink(os.path.join(self._tmpdir, fname))
			except OSError:
				pass
		try:
			os.rmdir(self._tmpdir)
		except OSError:
			pass
		super().tearDown()

	def _writeCatalogFile(self, catalog):
		import json
		with open(self._catalog_path, 'w', encoding='utf-8') as f:
			f.write(json.dumps(catalog))

	def _armEnsureCatalogs(self):
		"""Shadow the collaborators EnsureCatalogs reaches so the test
		neither rescans nor patches live operators."""
		self.cat._getCatalogPath = lambda build: self._catalog_path
		self.cat._patchCrossBuildDefaults = lambda catalog: None
		self.cat._ensurePalette = (
			lambda op_catalog, resume_results=None:
				self._ensure_calls.append((op_catalog, resume_results)))
		# Make the TDNExt idempotency guard falsy so EnsureCatalogs
		# actually enters its body.
		self.tdn._palette_catalog = {}
		self.cat._scan_in_flight = False
		self.cat._pending_resume = None

	def test_C01_checkpoint_writes_partial_marker(self):
		self.cat._getCatalogPath = lambda build: self._catalog_path
		self.cat._build_str = 'test.build'
		self.cat._op_catalog_pending = {'noiseTOP': {'foo': 1}}
		self.cat._palette_results = {'compA': {'type': 'x', 'min_children': 0}}
		self.cat._palette_checkpointed = 0

		self.cat._checkpointPaletteScan()

		import json
		with open(self._catalog_path, encoding='utf-8') as f:
			data = json.loads(f.read())
		self.assertTrue(data.get('_palette_partial'),
			'checkpoint must carry the partial marker')
		self.assertIn('compA', data.get('_palette', {}))
		self.assertIn('noiseTOP', data)
		self.assertEqual(self.cat._palette_checkpointed, 1)

	def test_C02_write_catalog_is_atomic_no_tmp_left(self):
		import json, os
		self.cat._writeCatalog(self._catalog_path, {'a': {'b': 2}})
		self.assertFalse(os.path.isfile(self._catalog_path + '.tmp'),
			'atomic write must not leave a .tmp file behind')
		with open(self._catalog_path, encoding='utf-8') as f:
			self.assertEqual(json.loads(f.read()), {'a': {'b': 2}})

	def test_C03_ensurecatalogs_stages_deferred_resume(self):
		self._armEnsureCatalogs()
		self._writeCatalogFile({
			'noiseTOP': {'foo': 1},
			'_palette': {'compA': {'type': 'x', 'min_children': 0}},
			'_palette_partial': True,
		})

		self.cat.EnsureCatalogs()

		# The resume is DEFERRED past the frame 30-90 restore phases:
		# EnsureCatalogs stages state and schedules _resumePaletteScan.
		self.assertIsNotNone(self.cat._pending_resume,
			'a partial catalog must stage a deferred palette resume')
		op_catalog, resume = self.cat._pending_resume
		self.assertEqual(op_catalog, {'noiseTOP': {'foo': 1}},
			'resume must strip reserved _ keys from the op catalog')
		self.assertEqual(resume, {'compA': {'type': 'x', 'min_children': 0}})
		self.assertTrue(self.cat._scan_in_flight,
			'resume must mark the scan in flight')

		# Fire the deferred hop synchronously: it must consume the staged
		# state and route into _ensurePalette (shadowed here).
		self.cat._resumePaletteScan()
		self.assertLen(self._ensure_calls, 1)
		self.assertIsNone(self.cat._pending_resume,
			'the staged resume must be consumed exactly once')
		self.cat._resumePaletteScan()  # second fire is a no-op
		self.assertLen(self._ensure_calls, 1)

	def test_C04_ensurecatalogs_missing_palette_key_resumes(self):
		self._armEnsureCatalogs()
		self._writeCatalogFile({'noiseTOP': {'foo': 1}})

		self.cat.EnsureCatalogs()

		self.assertIsNotNone(self.cat._pending_resume,
			'an op-type-only catalog (interrupted before the palette '
			'phase) must stage a deferred palette resume')

	def test_C05_ensurecatalogs_complete_catalog_short_circuits(self):
		self._armEnsureCatalogs()
		self._writeCatalogFile({
			'noiseTOP': {'foo': 1},
			'_palette': {'compA': {'type': 'x', 'min_children': 0}},
		})

		self.cat.EnsureCatalogs()

		self.assertLen(self._ensure_calls, 0,
			'a complete catalog must not trigger any palette scan')
		self.assertFalse(self.cat._scan_in_flight)
		self.assertIsNone(self.cat._pending_resume)

	def test_C06_ensurecatalogs_empty_palette_reads_as_complete(self):
		"""Explicit empty _palette = 'no palette dir' final state, not
		an interrupted scan -- must not resume every launch."""
		self._armEnsureCatalogs()
		self._writeCatalogFile({'noiseTOP': {'foo': 1}, '_palette': {}})

		self.cat.EnsureCatalogs()

		self.assertLen(self._ensure_calls, 0)

	def test_C07_ensurecatalogs_in_flight_guard_blocks_reentry(self):
		self._armEnsureCatalogs()
		self._writeCatalogFile({'noiseTOP': {'foo': 1}})
		self.cat._scan_in_flight = True

		self.cat.EnsureCatalogs()

		self.assertLen(self._ensure_calls, 0,
			'a scan already in flight must not be double-started')
		self.assertIsNone(self.cat._pending_resume)

	def test_C08_start_palette_scan_all_done_finalizes(self):
		"""Resume where every enumerated .tox is already checkpointed:
		finalize immediately (complete catalog), schedule nothing."""
		import os
		finalized = []
		for stem in ('compA', 'compB'):
			with open(os.path.join(self._tmpdir, stem + '.tox'), 'wb') as f:
				f.write(b'')
		self.cat._getPaletteDir = lambda: self._tmpdir
		self.cat._finalizePaletteScan = lambda: finalized.append(True)
		resume = {
			'compA': {'type': 'x', 'min_children': 0},
			'compB': {'type': 'y', 'min_children': 1},
		}

		self.cat._startPaletteScan({'noiseTOP': {}}, resume_results=resume)

		self.assertTrue(finalized,
			'nothing left to scan must finalize immediately')
		self.assertEqual(self.cat._palette_results, resume)

	def test_C09_shifted_defaults_skips_reserved_keys(self):
		"""_findShiftedDefaults must tolerate reserved catalog keys: the
		partial-checkpoint marker is a BOOL (crashed .items() when a resumed
		catalog reached _patchCrossBuildDefaults) and _palette maps component
		names, not params (produced garbage shifted entries)."""
		source = {
			'noiseTOP': {'foo': 1},
			'_palette': {'compA': {'type': 'x'}},
		}
		current = {
			'noiseTOP': {'foo': 2},
			'_palette': {'compA': {'type': 'y'}},
			'_palette_partial': True,
		}
		shifted = self.cat._findShiftedDefaults(source, current)
		self.assertEqual(shifted, {'noiseTOP': {'foo': (1, 2)}})

	def test_C10_scan_status_never_overwrites_disabled(self):
		"""Scan status writes must not flip a Disabled Embody -- Update()
		gates on Status == 'Disabled', so a 'Scanning...'/'Enabled' write
		would silently re-enable a user's disabled Embody."""
		self.embody.par.Status = 'Disabled'
		self.cat._setScanStatus('Scanning palette (1/2)')
		self.assertEqual(str(self.embody.par.Status), 'Disabled')
		self.cat._setScanStatus('Enabled')
		self.assertEqual(str(self.embody.par.Status), 'Disabled',
			'finalize must never re-enable a Disabled Embody')
		self.embody.par.Status = 'Enabled'
		self.cat._setScanStatus('Scanning palette (1/2)')
		self.assertEqual(str(self.embody.par.Status),
			'Scanning palette (1/2)',
			'normal scan status writes must still go through')


class TestCatalogPaletteSentinel(EmbodyTestCase):
	"""In-flight sentinel: palette freeze forensics + poisoned-component skip.

	TD 2025.33070 wedges its frame loop within a frame of geoPanel.tox
	loading (loadTox RETURNS, then the next frame never comes), so a user's
	first launch froze at 'Scanning palette (89/251)' and every relaunch
	resumed straight back into the same wedge. The sentinel is written
	right before every palette loadTox and removed only on clean outcomes
	(finalize, graceful abort, extension teardown); a launch that finds one
	convicts that component and skips it for the build, permanently
	(persisted under the reserved '_palette_blocked' catalog key).
	"""

	def setUp(self):
		super().setUp()
		import os, tempfile
		self.cat = self.embody.ext.CatalogManager
		self.tdn = self.embody.ext.TDN
		self._tmpdir = tempfile.mkdtemp(prefix='embody_sentinel_test_')
		self._sentinel_path = os.path.join(
			self._tmpdir, 'palette_scan_inflight.json')
		self.cat._inflightSentinelPath = lambda: self._sentinel_path
		self._orig_status = str(self.embody.par.Status)
		self._cat_saved = {
			'_build_str': self.cat._build_str,
			'_palette_blocked': set(self.cat._palette_blocked),
			'_palette_results': self.cat._palette_results,
			'_palette_checkpointed': self.cat._palette_checkpointed,
			'_op_catalog_pending': getattr(
				self.cat, '_op_catalog_pending', None),
			'_scan_in_flight': self.cat._scan_in_flight,
			'_sentinel_written': self.cat._sentinel_written,
			'_sentinel_path_cache': self.cat._sentinel_path_cache,
			'_workspace': self.cat._workspace,
			'_palette_workspace': self.cat._palette_workspace,
		}
		self._tdn_saved = (
			self.tdn._divergent_loaded,
			self.tdn._divergent_defaults,
			self.tdn._palette_catalog,
		)
		self.cat._build_str = 'test.build'
		self.cat._palette_blocked = set()
		self.cat._sentinel_written = False
		self.cat._sentinel_path_cache = None

	def tearDown(self):
		import os
		for name in ('_inflightSentinelPath', '_getCatalogPath',
					 '_getPaletteDir', '_finalizePaletteScan'):
			self.cat.__dict__.pop(name, None)
		for key, val in self._cat_saved.items():
			setattr(self.cat, key, val)
		(self.tdn._divergent_loaded,
		 self.tdn._divergent_defaults,
		 self.tdn._palette_catalog) = self._tdn_saved
		self.embody.par.Status = self._orig_status
		for fname in list(os.listdir(self._tmpdir)):
			try:
				os.unlink(os.path.join(self._tmpdir, fname))
			except OSError:
				pass
		try:
			os.rmdir(self._tmpdir)
		except OSError:
			pass
		super().tearDown()

	# =================================================================
	# Sentinel write / consume round-trip
	# =================================================================

	def test_D01_sentinel_roundtrip_convicts_and_deletes(self):
		import os
		self.cat._writeInflightSentinel('geoPanel', 'Techniques/geoPanel.tox')
		self.assertTrue(os.path.isfile(self._sentinel_path),
			'sentinel must exist on disk after the pre-load write')
		self.assertEqual(self.cat._consumeInflightSentinel(), 'geoPanel',
			'a build-matched sentinel must convict its component')
		self.assertFalse(os.path.isfile(self._sentinel_path),
			'consume must delete the sentinel')

	def test_D02_no_sentinel_returns_none(self):
		self.assertIsNone(self.cat._consumeInflightSentinel())

	def test_D03_stale_build_sentinel_is_dropped(self):
		import os
		self.cat._writeInflightSentinel('geoPanel', 'Techniques/geoPanel.tox')
		self.cat._build_str = 'other.build'
		self.assertIsNone(self.cat._consumeInflightSentinel(),
			'a sentinel from another TD build must not convict anything')
		self.assertFalse(os.path.isfile(self._sentinel_path),
			'stale sentinels must still be deleted')

	def test_D04_corrupt_sentinel_is_dropped(self):
		import os
		with open(self._sentinel_path, 'w', encoding='utf-8') as f:
			f.write('{not json')
		self.assertIsNone(self.cat._consumeInflightSentinel(),
			'an unreadable sentinel must be ignored, never raise')
		self.assertFalse(os.path.isfile(self._sentinel_path))

	def test_D05_clear_without_sentinel_is_noop(self):
		self.cat._clearInflightSentinel()  # must not raise

	def test_D10_teardown_clears_only_own_sentinel(self):
		"""onDestroyTD must be inert unless THIS session wrote a sentinel.

		The teardown clear used to resolve the project root via
		ext.Embody - which wedged project.save() when ExportPortableTox's
		file-ref strip reinitialized the extension mid-save - and it
		deleted sibling instances' live sentinels. With the guard,
		teardown does nothing unless a legacy scan wrote a sentinel this
		session, and clearing uses the cached path."""
		import os
		self.cat._workspace = None
		self.cat._palette_workspace = None
		# Foreign sentinel on disk, nothing written this session:
		with open(self._sentinel_path, 'w', encoding='utf-8') as f:
			f.write('{"build": "other", "name": "y"}')
		self.cat._sentinel_written = False
		self.cat.onDestroyTD()
		self.assertTrue(os.path.isfile(self._sentinel_path),
			'teardown must not delete a sentinel this session did not write')
		os.unlink(self._sentinel_path)
		# Own sentinel: written this session -> teardown clears via cache.
		self.cat._writeInflightSentinel('geoPanel', 'Techniques/geoPanel.tox')
		self.assertTrue(self.cat._sentinel_written)
		self.assertEqual(self.cat._sentinel_path_cache, self._sentinel_path)
		self.cat.onDestroyTD()
		self.assertFalse(os.path.isfile(self._sentinel_path),
			'teardown must clear a sentinel this session wrote')
		self.assertFalse(self.cat._sentinel_written)

	# =================================================================
	# Blocklist and blocked-path filtering
	# =================================================================

	def test_D06_geopanel_in_static_blocklist(self):
		blocklist = self.embody.op(
			'CatalogManagerExt').module._PALETTE_SCAN_BLOCKLIST
		self.assertIn('geopanel', blocklist,
			'geoPanel wedges TD 2025.33070 - it must stay blocklisted')

	def test_D07_filter_blocked_paths_splits_by_stem(self):
		self.cat._palette_blocked = {'compA'}
		kept, dropped = self.cat._filterBlockedPaths(
			['x/compA.tox', 'y/compB.tox'])
		self.assertEqual(kept, ['y/compB.tox'])
		self.assertEqual(dropped, ['x/compA.tox'])
		self.cat._palette_blocked = set()
		kept, dropped = self.cat._filterBlockedPaths(['y/compB.tox'])
		self.assertEqual((kept, dropped), (['y/compB.tox'], []))

	# =================================================================
	# Persistence through checkpoint and startup consumption
	# =================================================================

	def test_D08_checkpoint_carries_blocked_list(self):
		import json, os
		catalog_path = os.path.join(self._tmpdir, 'catalog_test.json')
		self.cat._getCatalogPath = lambda build: catalog_path
		self.cat._op_catalog_pending = {'noiseTOP': {'foo': 1}}
		self.cat._palette_results = {}
		self.cat._palette_checkpointed = 0
		self.cat._palette_blocked = {'geoPanel'}

		self.cat._checkpointPaletteScan()

		with open(catalog_path, encoding='utf-8') as f:
			data = json.loads(f.read())
		self.assertEqual(data.get('_palette_blocked'), ['geoPanel'],
			'checkpoints must persist convicted components')

	def test_D09_start_palette_scan_consumes_sentinel_and_skips(self):
		"""A leftover sentinel + everything else checkpointed: the poisoned
		component is convicted and skipped, the scan finalizes with zero
		loads, and the sentinel is gone."""
		import os
		finalized = []
		for stem in ('compA', 'compB'):
			with open(os.path.join(self._tmpdir, stem + '.tox'), 'wb') as f:
				f.write(b'')
		self.cat._getPaletteDir = lambda: self._tmpdir
		self.cat._finalizePaletteScan = lambda: finalized.append(True)
		self.cat._writeInflightSentinel('compA', 'compA.tox')
		resume = {'compB': {'type': 'y', 'min_children': 1}}

		self.cat._startPaletteScan({'noiseTOP': {}}, resume_results=resume)

		self.assertIn('compA', self.cat._palette_blocked,
			'the sentinel-named component must be convicted')
		self.assertTrue(finalized,
			'with the poisoned component skipped and the rest resumed, '
			'the scan must finalize without loading anything')
		self.assertFalse(os.path.isfile(self._sentinel_path),
			'startup must consume the sentinel')


class TestCatalogToeexpandScan(EmbodyTestCase):
	"""Background (toeexpand) palette scan: parser, routing, poller drain.

	The primary palette scan expands each .tox with TD's bundled toeexpand
	on a worker thread and reads the placed type + child count from the
	expansion - nothing is loaded into TD, so no palette component's init
	code can drop frames or wedge the frame loop (geoPanel / chromaKey on
	TD 2025.33070). The legacy loadTox scan remains as fallback only.
	"""

	def setUp(self):
		super().setUp()
		import os, tempfile
		self.cat = self.embody.ext.CatalogManager
		self.tdn = self.embody.ext.TDN
		self._tmpdir = tempfile.mkdtemp(prefix='embody_toex_test_')
		self._orig_status = str(self.embody.par.Status)
		self._cat_saved = {
			'_build_str': self.cat._build_str,
			'_palette_results': self.cat._palette_results,
			'_palette_checkpointed': self.cat._palette_checkpointed,
			'_palette_blocked': set(self.cat._palette_blocked),
			'_op_catalog_pending': getattr(
				self.cat, '_op_catalog_pending', None),
			'_scan_in_flight': self.cat._scan_in_flight,
			'_tox_scan_queue': self.cat._tox_scan_queue,
			'_tox_scan_done': self.cat._tox_scan_done,
			'_tox_scan_stop': self.cat._tox_scan_stop,
			'_tox_scan_total': self.cat._tox_scan_total,
			'_tox_scan_fail_count': self.cat._tox_scan_fail_count,
		}
		self._tdn_saved = (
			self.tdn._divergent_loaded,
			self.tdn._divergent_defaults,
			self.tdn._palette_catalog,
		)
		self.cat._build_str = 'test.build'

	def tearDown(self):
		import os, shutil
		for name in ('_startToeexpandScan', '_startPaletteScan',
					 '_finalizePaletteScan', '_checkpointPaletteScan',
					 '_loadBootstrapPalette', '_getCatalogPath',
					 '_inflightSentinelPath'):
			self.cat.__dict__.pop(name, None)
		for key, val in self._cat_saved.items():
			setattr(self.cat, key, val)
		(self.tdn._divergent_loaded,
		 self.tdn._divergent_defaults,
		 self.tdn._palette_catalog) = self._tdn_saved
		self.embody.par.Status = self._orig_status
		shutil.rmtree(self._tmpdir, ignore_errors=True)
		super().tearDown()

	# =================================================================
	# Pure parsers
	# =================================================================

	def _writeNodeFile(self, rel, first_line):
		import os
		path = os.path.join(self._tmpdir, rel)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, 'w', encoding='ascii') as f:
			f.write(first_line + '\n')
		return path

	def test_E01_optype_from_node_header(self):
		p = self._writeNodeFile('a.n', 'COMP:base')
		self.assertEqual(self.cat._opTypeFromNodeFile(p), 'baseCOMP')
		p = self._writeNodeFile('b.n', 'TOP:noise 12 34')
		self.assertEqual(self.cat._opTypeFromNodeFile(p), 'noiseTOP')
		p = self._writeNodeFile('g.n', 'COMP:geo')
		self.assertEqual(self.cat._opTypeFromNodeFile(p), 'geometryCOMP',
			'.n header tokens must map through the alias table')
		p = self._writeNodeFile('i.init', 'type = COMP:container')
		self.assertEqual(self.cat._opTypeFromNodeFile(p), 'containerCOMP',
			'old-format .init headers must parse too')
		p = self._writeNodeFile('c.n', 'garbage header')
		self.assertIsNone(self.cat._opTypeFromNodeFile(p))
		self.assertIsNone(self.cat._opTypeFromNodeFile(
			self._tmpdir + '/missing.n'))

	def test_E02_parse_expanded_tox_type_and_children(self):
		import os
		expand = os.path.join(self._tmpdir, 'foo.tox.dir')
		self._writeNodeFile('foo.tox.dir/foo.n', 'COMP:base')
		self._writeNodeFile('foo.tox.dir/foo/childA.n', 'TOP:noise')
		self._writeNodeFile('foo.tox.dir/foo/childB.n', 'COMP:container')
		self._writeNodeFile('foo.tox.dir/foo/childB/inner.n', 'TOP:level')
		with open(os.path.join(expand, 'foo', 'notanode.parm'), 'w') as f:
			f.write('x')
		self.assertEqual(
			self.cat._parseExpandedTox(expand, 'foo'), ('baseCOMP', 2),
			'type from the root .n, children = direct child .n count only')

	def test_E03_parse_expanded_tox_wrapper_fallbacks(self):
		"""Roots not named like the file mirror the legacy wrapper record
		(sickCore.tox's root is 'sickComp' -> the loadTox scan recorded
		the wrapper: baseCOMP with 1 child)."""
		import os
		expand = os.path.join(self._tmpdir, 'sickCore.tox.dir')
		self._writeNodeFile('sickCore.tox.dir/sickComp.n', 'COMP:container')
		self._writeNodeFile('sickCore.tox.dir/sickComp/a.n', 'TOP:noise')
		self.assertEqual(
			self.cat._parseExpandedTox(expand, 'sickCore'), ('baseCOMP', 1))
		expand2 = os.path.join(self._tmpdir, 'bar.tox.dir')
		self._writeNodeFile('bar.tox.dir/one.n', 'COMP:base')
		self._writeNodeFile('bar.tox.dir/two.n', 'COMP:base')
		self.assertEqual(
			self.cat._parseExpandedTox(expand2, 'bar'), ('baseCOMP', 2),
			'multiple top-level nodes record as the wrapper would')
		self.assertIsNone(self.cat._parseExpandedTox(
			os.path.join(self._tmpdir, 'missing.dir'), 'x'))

	def test_E03b_parse_old_format_expansion(self):
		"""Old-format toeexpand output (template.tox): no .n files - the
		root is <stem>.init and children are <stem>/<child>.init."""
		import os
		expand = os.path.join(self._tmpdir, 'template.tox.dir')
		self._writeNodeFile('template.tox.dir/template.init',
			'type = COMP:container')
		for child in ('help', 'icon', 'local', 'template'):
			self._writeNodeFile(
				f'template.tox.dir/template/{child}.init', 'x = 1')
		self._writeNodeFile(
			'template.tox.dir/template/help.parm', 'noise')
		self.assertEqual(
			self.cat._parseExpandedTox(expand, 'template'),
			('containerCOMP', 4),
			'old-format roots must parse type + .init child count')

	def test_E04_toeexpand_ships_with_td(self):
		import os, sys
		exe = self.cat._toeexpandExe()
		if sys.platform == 'darwin' and exe is None:
			# macOS bundle location unverified - the scan degrades to the
			# legacy fallback there rather than breaking.
			self.skipTest('toeexpand not found in this macOS bundle')
		self.assertIsNotNone(exe,
			'toeexpand must exist in this TD install (background scan '
			'primary path depends on it)')
		self.assertTrue(os.path.isfile(exe))

	def test_E05_worker_end_to_end_on_real_palette_tox(self):
		"""The pure worker + real toeexpand on one real palette file."""
		import os, queue, threading
		exe = self.cat._toeexpandExe()
		palette = self.cat._getPaletteDir()
		if not exe or not palette:
			self.skipTest('no toeexpand/palette in this environment')
		candidates = []
		for root, _d, files in os.walk(palette):
			for f in files:
				if f.endswith('.tox') and os.path.getsize(
						os.path.join(root, f)) < 50_000:
					candidates.append(os.path.relpath(
						os.path.join(root, f), palette))
			if candidates:
				break
		if not candidates:
			self.skipTest('no small palette .tox available')
		q = queue.Queue()
		done = threading.Event()
		self.cat._toeexpandWorker(
			exe, palette, candidates[:1], q, done, threading.Event(), 60)
		self.assertTrue(done.is_set(), 'worker must always signal done')
		item = q.get_nowait()
		self.assertEqual(item[0], 'result',
			f'real palette expansion must parse, got: {item}')
		self.assertTrue(item[2].endswith('COMP'),
			'palette components place as COMPs')
		self.assertIsInstance(item[3], int)

	# =================================================================
	# Routing and poller drain
	# =================================================================

	def test_E06_ensure_palette_prefers_background_scan(self):
		calls = []
		self.cat._loadBootstrapPalette = lambda build: None
		self.cat._startToeexpandScan = (
			lambda op_catalog, resume_results=None:
				calls.append('toex') or True)
		self.cat._startPaletteScan = (
			lambda op_catalog, resume_results=None:
				calls.append('loadtox'))
		self.cat._ensurePalette({'noiseTOP': {}})
		self.assertEqual(calls, ['toex'],
			'bootstrap miss must route to the background scan first')

	def test_E07_ensure_palette_falls_back_when_unavailable(self):
		calls = []
		self.cat._loadBootstrapPalette = lambda build: None
		self.cat._startToeexpandScan = (
			lambda op_catalog, resume_results=None:
				calls.append('toex') and False)
		self.cat._startPaletteScan = (
			lambda op_catalog, resume_results=None:
				calls.append('loadtox'))
		self.cat._ensurePalette({'noiseTOP': {}})
		self.assertEqual(calls, ['toex', 'loadtox'],
			'no toeexpand must fall back to the legacy in-TD scan')

	def test_E08_poller_drains_results_and_finalizes(self):
		import queue, threading
		finalized = []
		self.cat._finalizePaletteScan = lambda: finalized.append(True)
		self.cat._palette_results = {}
		self.cat._palette_checkpointed = 0
		self.cat._tox_scan_total = 2
		self.cat._tox_scan_fail_count = 0
		q = queue.Queue()
		q.put(('result', 'compA', 'baseCOMP', 3))
		q.put(('fail', 'compB', 'toeexpand rc=1'))
		self.cat._tox_scan_queue = q
		done = threading.Event()
		done.set()
		self.cat._tox_scan_done = done

		self.cat._pollToeexpandScan()

		self.assertEqual(
			self.cat._palette_results.get('compA'),
			{'type': 'baseCOMP', 'min_children': 3})
		self.assertNotIn('compB', self.cat._palette_results,
			'failed expansions must not be recorded')
		self.assertTrue(finalized,
			'done event + drained queue must finalize the scan')

	def test_E09_poller_fatal_falls_back_with_partial_results(self):
		import queue, threading
		fallback = []
		self.cat._finalizePaletteScan = lambda: self.fail(
			'fatal must not finalize')
		self.cat._checkpointPaletteScan = lambda: None
		self.cat._startPaletteScan = (
			lambda op_catalog, resume_results=None:
				fallback.append((op_catalog, dict(resume_results or {}))))
		self.cat._palette_results = {}
		self.cat._palette_checkpointed = 0
		self.cat._op_catalog_pending = {'noiseTOP': {}}
		self.cat._tox_scan_total = 2
		self.cat._tox_scan_fail_count = 0
		q = queue.Queue()
		q.put(('result', 'compA', 'baseCOMP', 3))
		q.put(('fatal', '', 'worker exploded'))
		self.cat._tox_scan_queue = q
		done = threading.Event()
		done.set()
		self.cat._tox_scan_done = done

		self.cat._pollToeexpandScan()

		self.assertLen(fallback, 1,
			'a fatal worker error must fall back to the legacy scan')
		op_catalog, resume = fallback[0]
		self.assertIn('compA', resume,
			'results gathered before the fatal must carry into the resume')

	def test_E10_poller_all_failures_falls_back_not_finalizes(self):
		"""Systemic per-item failure (AV blocking toeexpand, etc.) must
		NOT finalize an empty palette as complete - that would silently
		disable palette-clone detection for the build forever."""
		import queue, threading
		fallback = []
		self.cat._finalizePaletteScan = lambda: self.fail(
			'an all-failed scan must not finalize an empty palette')
		self.cat._startPaletteScan = (
			lambda op_catalog, resume_results=None:
				fallback.append(op_catalog))
		self.cat._palette_results = {}
		self.cat._palette_checkpointed = 0
		self.cat._op_catalog_pending = {'noiseTOP': {}}
		self.cat._tox_scan_total = 2
		self.cat._tox_scan_fail_count = 0
		q = queue.Queue()
		q.put(('fail', 'compA', 'WinError 5'))
		q.put(('fail', 'compB', 'WinError 5'))
		self.cat._tox_scan_queue = q
		done = threading.Event()
		done.set()
		self.cat._tox_scan_done = done

		self.cat._pollToeexpandScan()

		self.assertLen(fallback, 1,
			'zero results + failures must fall back to the in-TD scan')
