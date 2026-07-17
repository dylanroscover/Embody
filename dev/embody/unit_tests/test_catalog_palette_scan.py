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
		self._finalize_calls = []

	def tearDown(self):
		import os
		for name in ('_getPaletteDir', '_finalizePaletteScan',
					 '_getCatalogPath'):
			self.cat.__dict__.pop(name, None)
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
		}
		self._ensure_calls = []

	def tearDown(self):
		import os
		for name in ('_getCatalogPath', '_ensurePalette',
					 '_patchCrossBuildDefaults', '_getPaletteDir',
					 '_finalizePaletteScan'):
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
