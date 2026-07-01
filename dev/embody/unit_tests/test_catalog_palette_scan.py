"""
Test suite: CatalogManager palette-scan timeline guard.

Loading a shipped palette .tox during the runtime palette scan runs that
component's init code, which can mutate GLOBAL timeline state (pause
playback, change cookRate). CatalogManager snapshots that state before the
scan and restores it after every chunk so a misbehaving palette component
can never leave the timeline paused.

- _snapshotTimeState / _restoreTimeState round-trip the relevant globals
- restore is a no-op when nothing changed and when no snapshot was taken
- restore puts back play, rate, cookRate, realTime when a load clobbers them
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
