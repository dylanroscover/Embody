"""
Test suite: the shared task ledger (announce_task / update_task backing).

The ledger (.embody/tasks.json) records work-STATE across AI sessions --
in_progress, done_uncommitted (finished work sitting uncommitted in the
tree), committed, abandoned. It exists because claims expire on silence and
each session's todo list is private: on 2026-07-29 a session read another
session's FINISHED uncommitted feature as in-flight work and held its own
batch for nothing.

Everything here is worker-side pure Python + file I/O: a bare
EnvoyMCPServer (skipping __init__, per test_envoy_sessions) whose
_taskLedgerPath is STUBBED to a temp file. The process-global
sys._envoy_repo_root is never touched -- the live Envoy worker serves
peers concurrently, and swapping the global mid-suite would make their
get_sessions calls read (and then persist) test tasks into the REAL
ledger (review finding on the first iteration of this suite).
"""

import json
import os
import shutil
import sys
import tempfile
import time
from threading import Lock

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_envoy_mod = op.Embody.op('EnvoyExt').module


def _bare_worker(ledger_path):
    """EnvoyMCPServer with only the ledger-relevant state, skipping
    __init__ (which imports MCPServer and registers all tools). The
    ledger path is stubbed per instance -- no process-global touched."""
    W = _envoy_mod.EnvoyMCPServer
    w = W.__new__(W)
    w._tasks = {}
    w._sessions_lock = Lock()
    w._taskLedgerPath = lambda: ledger_path
    return w


class TestTaskLedgerPure(EmbodyTestCase):
    """The module-level pure helpers."""

    def test_task_updated_guards_garbage(self):
        m = _envoy_mod
        self.assertEqual(m._task_updated({'updated': 'garbage'}), 0.0)
        self.assertEqual(m._task_updated({}), 0.0)
        self.assertEqual(m._task_updated({'updated': 5.0}), 5.0)

    def test_prune_drops_only_stale_terminal_tasks(self):
        now = time.time()
        old = now - (8 * 24 * 3600)
        tasks = {
            'a': {'status': 'in_progress', 'updated': now - 60},
            'b': {'status': 'done_uncommitted', 'updated': old},
            'c': {'status': 'committed', 'updated': old},
            'd': {'status': 'committed', 'updated': now - 60},
            'e': {'status': 'abandoned', 'updated': old},
            'f': 'not-a-dict',
        }
        kept = _envoy_mod._prune_tasks(tasks, now)
        self.assertEqual(sorted(kept), ['a', 'b', 'd'],
                         'fresh active + recent terminal survive; stale '
                         'terminal and garbage are dropped')

    def test_prune_auto_abandons_long_silent_in_progress(self):
        now = time.time()
        silent = now - (15 * 24 * 3600)
        kept = _envoy_mod._prune_tasks(
            {'a': {'status': 'in_progress', 'updated': silent},
             'b': {'status': 'done_uncommitted', 'updated': silent}}, now)
        self.assertEqual(kept['a']['status'], 'abandoned',
                         'a dead session\'s silent in_progress must not '
                         'grow the ledger forever')
        self.assertEqual(kept['a']['updated_by'], '_ledger_prune')
        self.assertEqual(kept['b']['status'], 'done_uncommitted',
                         'done_uncommitted is load-bearing: NEVER '
                         'auto-abandoned, only a commit or deliberate '
                         'transition clears it')

    def test_task_public_shape_stale_flag_and_empties(self):
        now = time.time()
        pub = _envoy_mod._task_public(
            {'id': 'tsk_1', 'title': 'T', 'status': 'in_progress',
             'session': 's1', 'label': '', 'note': '', 'scopes': [],
             'created': now - 10, 'updated': now - 5}, now)
        self.assertEqual(pub['id'], 'tsk_1')
        self.assertNotIn('label', pub, 'empty fields are dropped')
        self.assertNotIn('commit', pub)
        self.assertNotIn('stale', pub, 'a fresh task is not stale')
        old = _envoy_mod._task_public(
            {'id': 'tsk_2', 'title': 'T', 'status': 'in_progress',
             'session': 's1', 'created': now - 90000,
             'updated': now - 90000}, now)
        self.assertTrue(old.get('stale'),
                        'a day-silent in_progress is flagged stale')


class TestTaskLedgerFlow(EmbodyTestCase):
    """announce/update/snapshot against a stubbed temp ledger path."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='embody_ledger_')
        self.path = os.path.join(self.tmp, '.embody', 'tasks.json')
        self.w = _bare_worker(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _write_disk(self, tasks):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'tasks': tasks}, f)

    def _read_disk(self):
        with open(self.path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_announce_creates_and_persists(self):
        r = self.w._announce_task('sid1', 'win-A', 'Port the frobnicator',
                                  scopes=['file:dev/x.py', '/embody/y'],
                                  note='step 1 of 3')
        self.assertTrue(r.get('announced'), r)
        task = r['task']
        self.assertTrue(task['id'].startswith('tsk_'))
        self.assertEqual(task['status'], 'in_progress')
        disk = self._read_disk()
        self.assertEqual(disk['schema'], 1)
        self.assertEqual(disk['tasks'][task['id']]['session'], 'sid1')

    def test_announce_validates_scopes(self):
        r = self.w._announce_task('s', 'l', 'W',
                                  scopes=['dev/embody/EnvoyExt.py'])
        self.assertIn('error', r,
                      'an unprefixed file path would silently never match '
                      'preflight -- reject it loudly like claim_scope does')
        r = self.w._announce_task('s', 'l', 'W',
                                  scopes=['file:dev\\embody\\x.py'])
        self.assertEqual(r['task']['scopes'], ['file:dev/embody/x.py'],
                         'file: scopes normalize to forward slashes')

    def test_announce_requires_title_and_caps_inputs(self):
        self.assertIn('error', self.w._announce_task('s', 'l', '   '))
        r = self.w._announce_task(
            's', 'l', 'T' * 500,
            scopes=['/s%d' % i for i in range(20)], note='n' * 1000)
        task = r['task']
        self.assertLessEqual(len(task['title']), _envoy_mod._TASK_TITLE_MAX)
        self.assertLessEqual(len(task['scopes']), _envoy_mod._TASK_SCOPES_MAX)
        self.assertLessEqual(len(task['note']), _envoy_mod._TASK_NOTE_MAX)

    def test_update_transitions_and_unknown_id(self):
        tid = self.w._announce_task('sid1', 'A', 'Work')['task']['id']
        r = self.w._update_task('sid1', 'A', tid, status='done_uncommitted',
                                note='finished, needs commit')
        self.assertEqual(r['task']['status'], 'done_uncommitted')
        miss = self.w._update_task('sid1', 'A', 'tsk_nope')
        self.assertIn('error', miss)
        self.assertEqual(len(miss['active_tasks']), 1,
                         'the unknown-id error must list active tasks so '
                         'the caller can recover without a second call')

    def test_commit_sha_implies_committed_but_empty_is_noop(self):
        tid = self.w._announce_task('sid1', 'A', 'Work')['task']['id']
        r = self.w._update_task('sid1', 'A', tid, commit='   ')
        self.assertEqual(r['task']['status'], 'in_progress',
                         'an empty/whitespace sha must not imply committed')
        self.assertNotIn('commit', r['task'])
        r = self.w._update_task('sid1', 'A', tid, commit='abc1234')
        self.assertEqual(r['task']['status'], 'committed')
        self.assertEqual(r['task']['commit'], 'abc1234')

    def test_updated_by_tracks_latest_writer_only(self):
        tid = self.w._announce_task('sid1', 'A', 'Work')['task']['id']
        r = self.w._update_task('sid2', 'B', tid, status='abandoned')
        self.assertEqual(r['task']['updated_by'], 'sid2',
                         'a peer transition leaves a trace')
        r = self.w._update_task('sid1', 'A', tid, status='in_progress')
        self.assertNotIn('updated_by', r['task'],
                         'the owner\'s own later update clears the stale '
                         'peer attribution')

    def test_merge_newest_updated_wins_both_directions(self):
        # The review's MAJOR: blanket disk-wins rolled a process's own
        # fresh transition back. Pin newest-wins in BOTH directions.
        now = time.time()
        tid = 'tsk_merge'
        # memory NEWER than disk -> memory wins
        self.w._tasks = {tid: {'id': tid, 'title': 'T', 'session': 's',
                               'status': 'done_uncommitted',
                               'created': now - 100, 'updated': now}}
        self._write_disk({tid: {'id': tid, 'title': 'T', 'session': 's',
                                'status': 'in_progress',
                                'created': now - 100,
                                'updated': now - 50}})
        with self.w._sessions_lock:
            merged = self.w._loadTasksLocked()
        self.assertEqual(merged[tid]['status'], 'done_uncommitted',
                         'an older disk view must not roll back a newer '
                         'in-memory transition')
        # disk NEWER than memory -> disk wins
        self._write_disk({tid: {'id': tid, 'title': 'T', 'session': 's',
                                'status': 'committed', 'commit': 'abc',
                                'created': now - 100,
                                'updated': now + 50}})
        with self.w._sessions_lock:
            merged = self.w._loadTasksLocked()
        self.assertEqual(merged[tid]['status'], 'committed')

    def test_snapshot_survives_garbage_updated_field(self):
        # One malformed entry from a foreign writer must not take down the
        # whole surface (get_sessions swallows exceptions -- the tasks
        # field would silently vanish forever). The garbage guard maps the
        # bad stamp to 0.0, which the stale-abandon prune then reads as
        # ancient: the entry is QUARANTINED to abandoned (attributed to
        # _ledger_prune, visible via include_terminal) while healthy
        # entries and the surface itself are untouched.
        now = time.time()
        self._write_disk({
            'tsk_bad': {'id': 'tsk_bad', 'title': 'B', 'session': 's',
                        'status': 'in_progress', 'created': now,
                        'updated': 'garbage'},
            'tsk_ok': {'id': 'tsk_ok', 'title': 'O', 'session': 's',
                       'status': 'in_progress', 'created': now,
                       'updated': now}})
        snap = self.w._tasks_snapshot()
        self.assertEqual({t['id'] for t in snap}, {'tsk_ok'},
                         'healthy entries survive a foreign garbage stamp')
        everything = self.w._tasks_snapshot(include_terminal=True)
        bad = next(t for t in everything if t['id'] == 'tsk_bad')
        self.assertEqual(bad['status'], 'abandoned',
                         'the garbage-stamped entry is quarantined, not '
                         'crashed on and not left polluting the active view')
        self.assertEqual(bad['updated_by'], '_ledger_prune')

    def test_snapshot_active_only_newest_first(self):
        a = self.w._announce_task('s', 'l', 'A')['task']['id']
        b = self.w._announce_task('s', 'l', 'B')['task']['id']
        self.w._update_task('s', 'l', a, commit='deadbee')
        self.w._update_task('s', 'l', b, note='bump')
        snap = self.w._tasks_snapshot()
        self.assertEqual([t['id'] for t in snap], [b],
                         'committed tasks leave the default snapshot')
        everything = self.w._tasks_snapshot(include_terminal=True)
        self.assertEqual(len(everything), 2)
        self.assertEqual(everything[0]['id'], b, 'newest-updated first')

    def test_ledger_tasks_for_files_overlap(self):
        self.w._announce_task(
            's1', 'A', 'Port EnvoyExt',
            scopes=['file:dev/embody/Embody/EnvoyExt.py', '/embody/Envoy'])
        tid2 = self.w._announce_task(
            's2', 'B', 'Docs pass', scopes=['file:docs/index.md'])['task']['id']
        self.w._update_task('s2', 'B', tid2, status='done_uncommitted')
        hits = self.w._ledger_tasks_for_files(
            ['dev/embody/Embody/EnvoyExt.py', 'README.md'])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['overlap'],
                         'dev/embody/Embody/EnvoyExt.py')
        hits = self.w._ledger_tasks_for_files(['docs/index.md'])
        self.assertEqual(hits[0]['status'], 'done_uncommitted')
        self.assertEqual(self.w._ledger_tasks_for_files([]), [],
                         'empty landing set short-circuits')

    def test_no_ledger_path_stays_in_memory(self):
        w = _bare_worker(None)
        r = w._announce_task('s', 'l', 'Rootless work')
        self.assertTrue(r.get('announced'),
                        'no repo root -> no persist, but the ledger must '
                        'still serve this process in memory')
        self.assertEqual(len(w._tasks_snapshot()), 1)
