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
