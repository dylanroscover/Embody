"""
Test suite: the background-job layer (get_job_status / save_project /
run_tests background=True backing).

Long operations outlive the 30s MCP timeout and a mid-operation server
restart severs a synchronous call even though the work completes. Jobs
return their handle immediately and park results in .embody/jobs/, which
survives restarts and reinits.

Everything here exercises the MODULE-level helpers (pure os/json). The
jobs dir resolver is patched to a temp dir with save/restore in
try/finally -- the window is brief, and job WRITES cannot occur mid-suite
(the only writers are run_tests/save_project starts, and the test runner
refuses a concurrent run). The live background paths (run_tests
background=True, save_project) are verified over the wire at release time,
not in-suite -- a background test run inside a test run would be refused
by the runner's own concurrency guard. Note the _jobs_dir patch window
is visible to WORKER-side get_job_status reads from peers (a poll
landing inside it sees the fixture dir); that read is transient and
self-corrects on the peer's next poll.
"""

import json
import os
import shutil
import tempfile
import time

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_envoy_mod = op.Embody.op('EnvoyExt').module


class TestJobHelpers(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='embody_jobs_')
        self._real_jobs_dir = _envoy_mod._jobs_dir
        _envoy_mod._jobs_dir = lambda: self.tmp

    def tearDown(self):
        _envoy_mod._jobs_dir = self._real_jobs_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_new_job_shape(self):
        job = _envoy_mod._new_job('run_tests', {'suite_name': 'x',
                                                'test_name': None})
        self.assertTrue(job['id'].startswith('job_'))
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['kind'], 'run_tests')
        self.assertEqual(job['params'], {'suite_name': 'x'},
                         'None params are dropped')

    def test_job_path_rejects_untrusted_ids(self):
        # The id doubles as the filename: anything but job_<8 hex> must
        # resolve to None, or a crafted id walks the filesystem. The gate
        # uses \Z, not $ -- '$' accepts a trailing newline (panel finding).
        self.assertIsNotNone(_envoy_mod._job_path('job_deadbeef'))
        for bad in ('job_x/../evil', '../../etc/passwd', 'job_DEADBEEF',
                    'job_deadbee', 'job_deadbeef\n', 'nope', '', None):
            self.assertIsNone(_envoy_mod._job_path(bad),
                              'must reject %r' % (bad,))

    def test_write_retries_through_transient_sharing_violation(self):
        # Windows: os.replace fails with PermissionError while a reader
        # holds the destination open -- and the colliding write is usually
        # the terminal 'done'. _write_job retries back-to-back.
        job = _envoy_mod._new_job('run_tests', {})
        real_replace = os.replace
        calls = {'n': 0}

        def flaky(src, dst):
            calls['n'] += 1
            if calls['n'] < 3:
                raise PermissionError('sharing violation')
            return real_replace(src, dst)

        os.replace = flaky
        try:
            _envoy_mod._write_job(job)
        finally:
            os.replace = real_replace
        self.assertEqual(calls['n'], 3)
        self.assertIsNotNone(_envoy_mod._read_job(job['id']),
                             'the record must land once the reader clears')

    def test_write_read_roundtrip(self):
        job = _envoy_mod._new_job('save_project', {})
        _envoy_mod._write_job(job)
        back = _envoy_mod._read_job(job['id'])
        self.assertEqual(back['id'], job['id'])
        self.assertEqual(back['status'], 'running')

    def test_read_garbage_returns_none(self):
        path = os.path.join(self.tmp, 'job_deadbeef.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{not json')
        self.assertIsNone(_envoy_mod._read_job('job_deadbeef'))

    def test_public_flags_stale_running_only(self):
        now = time.time()
        old_running = {'id': 'job_00000001', 'status': 'running',
                       'started': now - 3600}
        pub = _envoy_mod._job_public(old_running, now)
        self.assertTrue(pub.get('stale'),
                        'an hour-old running record means the poll chain '
                        'died -- flag it')
        old_done = {'id': 'job_00000002', 'status': 'done',
                    'started': now - 3600, 'finished': now - 3500}
        self.assertNotIn('stale', _envoy_mod._job_public(old_done, now))
        fresh = {'id': 'job_00000003', 'status': 'running', 'started': now}
        self.assertNotIn('stale', _envoy_mod._job_public(fresh, now))

    def test_list_sorts_prunes_and_keeps_running(self):
        now = time.time()
        expired = {'id': 'job_00000010', 'kind': 'run_tests',
                   'status': 'done', 'started': now - 90000,
                   'finished': now - 90000}
        recent = {'id': 'job_00000011', 'kind': 'run_tests',
                  'status': 'done', 'started': now - 100,
                  'finished': now - 50}
        running = {'id': 'job_00000012', 'kind': 'save_project',
                   'status': 'running', 'started': now - 200000}
        for job in (expired, recent, running):
            _envoy_mod._write_job(job)
        listed = _envoy_mod._list_jobs(now)
        ids = [j['id'] for j in listed]
        self.assertNotIn('job_00000010', ids,
                         'finished records past retention are pruned')
        self.assertFalse(
            os.path.isfile(os.path.join(self.tmp, 'job_00000010.json')),
            'pruning removes the file, not just the listing')
        self.assertIn('job_00000012', ids,
                      'a running record is NEVER pruned, however old -- '
                      'it is flagged stale instead')
        self.assertEqual(ids[0], 'job_00000011', 'newest started first')

    def test_list_caps(self):
        now = time.time()
        for i in range(20):
            _envoy_mod._write_job({'id': 'job_%08x' % i, 'kind': 'k',
                                   'status': 'running',
                                   'started': now - i})
        listed = _envoy_mod._list_jobs(now)
        self.assertEqual(len(listed), _envoy_mod._JOB_LIST_CAP)

    def test_no_jobs_dir_degrades_quietly(self):
        _envoy_mod._jobs_dir = lambda: None
        self.assertIsNone(_envoy_mod._job_path('job_deadbeef'))
        self.assertEqual(_envoy_mod._list_jobs(time.time()), [])
        # write is a silent no-op, never a raise
        _envoy_mod._write_job(_envoy_mod._new_job('run_tests', {}))


class TestIdempotencyIndex(EmbodyTestCase):
    """The per-key idempotency index (16.5): a redelivered request
    reconciles to the job it already minted instead of running twice.
    One marker file per key, fail-closed on an unreadable marker."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='embody_idem_')
        self._real_jobs_dir = _envoy_mod._jobs_dir
        _envoy_mod._jobs_dir = lambda: self.tmp

    def tearDown(self):
        _envoy_mod._jobs_dir = self._real_jobs_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _make_job(self, key=None):
        job = _envoy_mod._new_job('save_project', {}, idempotency_key=key)
        _envoy_mod._write_job(job)
        return job

    def test_new_job_records_the_key_only_when_given(self):
        self.assertNotIn('idempotency_key', _envoy_mod._new_job('run_tests', {}))
        keyed = _envoy_mod._new_job('run_tests', {}, idempotency_key='abc')
        self.assertEqual(keyed['idempotency_key'], 'abc')

    def test_marker_roundtrip_reconciles_to_the_original_job(self):
        job = self._make_job('deploy-42')
        _envoy_mod._record_job_key('deploy-42', job['id'])
        found = _envoy_mod._job_for_key('deploy-42')
        self.assertEqual(found['id'], job['id'])
        self.assertEqual(found['kind'], 'save_project')

    def test_key_bound_to_another_kind_is_a_conflict(self):
        """A key reused across operations must REFUSE, not silently
        reconcile a save to a test run's handle (its save never running)."""
        job = self._make_job('shared')     # a save_project job
        _envoy_mod._record_job_key('shared', job['id'])
        # asking for the same key as a run_tests reconcile is a conflict
        with self.assertRaises(_envoy_mod._IdemKeyConflict):
            _envoy_mod._job_for_key('shared', expected_kind='run_tests')
        # asking with the matching kind still reconciles
        self.assertEqual(
            _envoy_mod._job_for_key('shared',
                                    expected_kind='save_project')['id'],
            job['id'])

    def test_idem_marker_path_rejects_empty_and_nonstr_keys(self):
        for bad in (None, '', 0, [], {'k': 1}):
            self.assertIsNone(_envoy_mod._idem_marker_path(bad),
                              'no marker for %r' % (bad,))

    def test_a_key_never_seen_is_a_miss(self):
        self.assertIsNone(_envoy_mod._job_for_key('never-used'))

    def test_corrupt_marker_fails_closed(self):
        """PROVEN-CLASS BUG (host side): treating an unreadable marker as
        'no prior job' re-runs the work. A present-but-corrupt marker must
        RAISE, not read as a miss."""
        job = self._make_job('k')
        _envoy_mod._record_job_key('k', job['id'])
        path = _envoy_mod._idem_marker_path('k')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{ half-written, not json')
        with self.assertRaises(_envoy_mod._IdemMarkerUnreadable):
            _envoy_mod._job_for_key('k')

    def test_marker_naming_a_gone_job_reads_as_a_miss(self):
        """A marker whose job record was pruned/cleaned heals: the caller
        re-mints rather than refusing forever."""
        job = self._make_job('k')
        _envoy_mod._record_job_key('k', job['id'])
        os.remove(_envoy_mod._job_path(job['id']))
        self.assertIsNone(_envoy_mod._job_for_key('k'))

    def test_empty_marker_reads_as_a_miss(self):
        # A crash between creating a marker and filling it leaves it empty.
        open(_envoy_mod._idem_marker_path('k'), 'w').close()
        self.assertIsNone(_envoy_mod._job_for_key('k'))

    def test_key_is_hashed_and_cannot_escape_the_jobs_dir(self):
        path = _envoy_mod._idem_marker_path('../../etc/passwd')
        self.assertEqual(os.path.dirname(path), self.tmp)
        name = os.path.basename(path)
        self.assertTrue(name.startswith('idem_') and name.endswith('.json'))
        self.assertNotIn('/', name)
        self.assertNotIn('\\', name)

    def test_markers_are_never_listed_as_jobs(self):
        job = self._make_job('k')
        _envoy_mod._record_job_key('k', job['id'])
        listed = _envoy_mod._list_jobs(time.time())
        ids = [j['id'] for j in listed]
        self.assertEqual(ids, [job['id']])
        # the marker exists on disk but is invisible to the listing
        markers = [f for f in os.listdir(self.tmp) if f.startswith('idem_')]
        self.assertEqual(len(markers), 1)

    def test_orphan_markers_are_pruned_live_ones_kept(self):
        """A marker lives exactly as long as its job record: one naming a
        gone job is pruned, one naming a live job is kept -- so the marker
        can never expire while its job is still reconcilable."""
        live = self._make_job('live')       # writes a real job record
        _envoy_mod._record_job_key('live', live['id'])
        orphan = _envoy_mod._idem_marker_path('orphan')
        with open(orphan, 'w', encoding='utf-8') as f:
            json.dump({'job_id': 'job_00000001',   # no such record
                       'created': time.time()}, f)
        _envoy_mod._list_jobs(time.time())  # pruning runs during a listing
        self.assertFalse(os.path.isfile(orphan), 'marker for a gone job pruned')
        self.assertTrue(os.path.isfile(_envoy_mod._idem_marker_path('live')),
                        'marker for a live job kept')

    def test_marker_helpers_degrade_without_a_jobs_dir(self):
        _envoy_mod._jobs_dir = lambda: None
        self.assertIsNone(_envoy_mod._idem_marker_path('k'))
        self.assertIsNone(_envoy_mod._job_for_key('k'))
        _envoy_mod._record_job_key('k', 'job_00000001')   # silent no-op


class TestIdempotencyReconcile(EmbodyTestCase):
    """The 16.5 headline: a keyed retry RECONCILES to the original job
    (lookup strictly before the guards) instead of starting or being
    refused. Exercises the real _startTestsJob/_save_project handlers on
    the live Envoy ext -- safe because every reconcile/refuse branch
    returns BEFORE any test run or save is started, and even a broken
    branch falls into the runner-active / test-active guard (which also
    starts no work), never a real run or save mid-suite."""

    def setUp(self):
        super().setUp()
        self.envoy = op.Embody.ext.Envoy
        self.tmp = tempfile.mkdtemp(prefix='embody_idem_rec_')
        self._real_jobs_dir = _envoy_mod._jobs_dir
        _envoy_mod._jobs_dir = lambda: self.tmp

    def tearDown(self):
        _envoy_mod._jobs_dir = self._real_jobs_dir
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _seed(self, kind, key):
        job = _envoy_mod._new_job(kind, {}, idempotency_key=key)
        _envoy_mod._write_job(job)
        _envoy_mod._record_job_key(key, job['id'])
        return job

    def test_run_tests_reconciles_to_the_seeded_run(self):
        seeded = self._seed('run_tests', 'reuse-me')
        out = self.envoy._startTestsJob(idempotency_key='reuse-me')
        self.assertEqual(out.get('job_id'), seeded['id'])
        self.assertIn('Reconciled', out.get('hint', ''))
        # no second job was minted
        self.assertEqual(len([j for j in _envoy_mod._list_jobs(time.time())
                              if j['kind'] == 'run_tests']), 1)

    def test_save_project_reconciles_to_the_seeded_save(self):
        seeded = self._seed('save_project', 'reuse-me')
        out = self.envoy._save_project(idempotency_key='reuse-me')
        self.assertEqual(out.get('job_id'), seeded['id'])
        self.assertIn('Reconciled', out.get('hint', ''))

    def test_reconcile_refuses_a_corrupt_marker(self):
        self._seed('save_project', 'k')
        with open(_envoy_mod._idem_marker_path('k'), 'w',
                  encoding='utf-8') as f:
            f.write('{ corrupt')
        out = self.envoy._save_project(idempotency_key='k')
        self.assertIn('unreadable', out.get('error', ''))

    def test_reconcile_refuses_a_cross_kind_key(self):
        """A key that already minted a run_tests job must not silently
        reconcile a save to it (the save would never run)."""
        self._seed('run_tests', 'crossed')
        out = self.envoy._save_project(idempotency_key='crossed')
        self.assertIn('different operation', out.get('error', ''))
