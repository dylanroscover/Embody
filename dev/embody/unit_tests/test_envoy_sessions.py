"""
Test suite: multi-session awareness (Phase 1).

Covers:
- Bridge session identity: SESSION_ID format, _init_session_label
  (env override, ASCII sanitization, repo-name derivation), heartbeat
  files carrying sid/label, _list_live_sessions filtering.
- Envoy worker session registry: _touch_session / _sessions_snapshot
  (registration, label refresh, operation attribution, staleness).
- Per-session _logs cursors on the live EnvoyExt: baseline-before-
  execute semantics and the isolation fix (one session polling must
  not consume another session's warnings).

Pure Python + live-extension checks; creates no operators and mutates
only its own registry/cursor keys (cleaned up in tearDown).
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
from threading import Lock
from unittest.mock import patch

# Load the bridge module from disk (pure Python, no TD deps).
# Distinct module name so we never collide with test_envoy_bridge's copy.
_bridge_path = os.path.join(project.folder, 'embody', 'envoy_bridge.py')
_spec = importlib.util.spec_from_file_location('envoy_bridge_sessions', _bridge_path)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

# Neutralize background daemon threads (same rationale as test_envoy_bridge)
bridge.start_orphan_watchdog = lambda *a, **k: None
if hasattr(bridge, 'start_reconciler'):
    bridge.start_reconciler = lambda *a, **k: None

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_envoy_mod = op.Embody.op('EnvoyExt').module


def _bare_worker():
    """EnvoyMCPServer with only the session-registry state, skipping
    __init__ (which imports FastMCP and registers all tools)."""
    W = _envoy_mod.EnvoyMCPServer
    w = W.__new__(W)
    w._sessions = {}
    w._sessions_lock = Lock()
    w._touches = {}
    w._claims = {}
    return w


# =====================================================================
# Bridge session identity
# =====================================================================

class TestBridgeSessionIdentity(EmbodyTestCase):

    def test_session_id_format(self):
        parts = bridge.SESSION_ID.split('-')
        self.assertEqual(len(parts), 3)
        self.assertEqual(int(parts[0]), os.getpid())
        self.assertGreater(int(parts[1]), 0)
        self.assertEqual(len(parts[2]), 4)

    def test_label_env_override(self):
        with patch.dict(os.environ, {'EMBODY_SESSION_LABEL': 'my custom label'}):
            bridge._init_session_label(None)
        self.assertEqual(bridge.SESSION_LABEL, 'my custom label')

    def test_label_ascii_sanitized(self):
        with patch.dict(os.environ, {'EMBODY_SESSION_LABEL': 'café@brænch'}):
            bridge._init_session_label(None)
        bridge.SESSION_LABEL.encode('ascii')  # must not raise
        self.assertTrue(bridge.SESSION_LABEL)

    def test_label_derived_from_config_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, 'MyRepo')
            emb = os.path.join(root, '.embody')
            os.makedirs(emb)
            with patch.dict(os.environ, {'EMBODY_SESSION_LABEL': ''}):
                bridge._init_session_label(os.path.join(emb, 'envoy.json'))
        self.assertTrue(bridge.SESSION_LABEL.startswith('MyRepo'))

    def test_label_never_empty(self):
        with patch.dict(os.environ, {'EMBODY_SESSION_LABEL': ''}):
            bridge._init_session_label(None)
        self.assertTrue(bridge.SESSION_LABEL)


# =====================================================================
# Heartbeat files and live-session listing
# =====================================================================

class TestHeartbeatSessions(EmbodyTestCase):

    def _mk_config(self, td):
        root = os.path.join(td, 'Repo')
        emb = os.path.join(root, '.embody')
        os.makedirs(emb)
        logdir = os.path.join(root, 'dev', 'logs')
        os.makedirs(logdir)
        cfg = os.path.join(emb, 'envoy.json')
        with open(cfg, 'w') as f:
            json.dump({}, f)
        return cfg, logdir

    def test_heartbeat_carries_sid_and_label(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, logdir = self._mk_config(td)
            bridge._touch_heartbeat(cfg)
            path = os.path.join(
                logdir, 'envoy-bridge-{}.heartbeat'.format(os.getpid()))
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data['sid'], bridge.SESSION_ID)
            self.assertTrue(data.get('label'))

    def test_list_live_sessions_includes_self_and_fresh_peer(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, logdir = self._mk_config(td)
            bridge._touch_heartbeat(cfg)
            # A peer pid we can't actually signal would read as dead
            # (inside TD, getppid() is launchd) -- mock liveness instead,
            # matching test_envoy_bridge's mock-heavy convention.
            peer_pid = 54321
            peer = {'pid': peer_pid, 'time': time.time(),
                    'sid': 'p-1-aaaa', 'label': 'peer@x'}
            with open(os.path.join(
                    logdir, 'envoy-bridge-{}.heartbeat'.format(peer_pid)),
                    'w') as f:
                json.dump(peer, f)
            with patch.object(bridge, 'is_process_alive',
                              return_value=True):
                sessions = bridge._list_live_sessions(cfg)
            by_pid = {s['pid']: s for s in sessions}
            self.assertIn(os.getpid(), by_pid)
            self.assertTrue(by_pid[os.getpid()]['self'])
            self.assertEqual(by_pid[os.getpid()]['sid'], bridge.SESSION_ID)
            self.assertIn(peer_pid, by_pid)
            self.assertEqual(by_pid[peer_pid]['label'], 'peer@x')
            self.assertFalse(by_pid[peer_pid]['self'])

    def test_list_live_sessions_drops_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, logdir = self._mk_config(td)
            stale = {'pid': os.getppid(),
                     'time': time.time() - bridge.HEARTBEAT_STALE_S * 3,
                     'sid': 's-1-bbbb', 'label': 'stale@x'}
            with open(os.path.join(
                    logdir, 'envoy-bridge-{}.heartbeat'.format(os.getppid())),
                    'w') as f:
                json.dump(stale, f)
            sessions = bridge._list_live_sessions(cfg)
            self.assertEqual(sessions, [])

    def test_list_live_sessions_drops_dead_pid(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, logdir = self._mk_config(td)
            dead = {'pid': 99999999, 'time': time.time(),
                    'sid': 'd-1-cccc', 'label': 'dead@x'}
            with open(os.path.join(
                    logdir, 'envoy-bridge-99999999.heartbeat'), 'w') as f:
                json.dump(dead, f)
            sessions = bridge._list_live_sessions(cfg)
            self.assertEqual(sessions, [])


# =====================================================================
# Envoy worker session registry
# =====================================================================

class TestWorkerSessionRegistry(EmbodyTestCase):

    def test_register_and_pid_parse(self):
        w = _bare_worker()
        w._touch_session('4242-1751500000-abcd', 'alpha@main')
        entry = w._sessions['4242-1751500000-abcd']
        self.assertEqual(entry['pid'], 4242)
        self.assertEqual(entry['label'], 'alpha@main')
        self.assertEqual(entry['requests'], 1)
        self.assertIsNone(entry['last_tool'])

    def test_malformed_sid_pid_is_none(self):
        w = _bare_worker()
        w._touch_session('weird', None)
        self.assertIsNone(w._sessions['weird']['pid'])
        self.assertEqual(w._sessions['weird']['label'], 'weird')

    def test_operation_attribution_does_not_count_request(self):
        w = _bare_worker()
        w._touch_session('1-2-aaaa', 'a@b')          # middleware touch
        w._touch_session('1-2-aaaa', operation='create_op')  # op touch
        entry = w._sessions['1-2-aaaa']
        self.assertEqual(entry['requests'], 1)
        self.assertEqual(entry['last_tool'], 'create_op')

    def test_label_refresh(self):
        w = _bare_worker()
        w._touch_session('1-2-aaaa', 'old')
        w._touch_session('1-2-aaaa', 'new')
        self.assertEqual(w._sessions['1-2-aaaa']['label'], 'new')

    def test_snapshot_stale_flag_and_order(self):
        w = _bare_worker()
        w._touch_session('1-2-aaaa', 'fresh')
        w._touch_session('3-4-bbbb', 'old')
        w._sessions['3-4-bbbb']['last_seen'] = time.time() - 200
        snap = w._sessions_snapshot()
        self.assertEqual(snap['count'], 2)
        self.assertEqual(snap['sessions'][0]['sid'], '1-2-aaaa')
        self.assertFalse(snap['sessions'][0]['stale'])
        self.assertTrue(snap['sessions'][1]['stale'])

    def test_snapshot_returns_copies(self):
        w = _bare_worker()
        w._touch_session('1-2-aaaa', 'a')
        snap = w._sessions_snapshot()
        snap['sessions'][0]['label'] = 'mutated'
        self.assertEqual(w._sessions['1-2-aaaa']['label'], 'a')

    def test_hour_prune_over_capacity(self):
        w = _bare_worker()
        for i in range(9):
            w._touch_session('{}-1-aaaa'.format(i), 'live')
        w._sessions['0-1-aaaa']['last_seen'] = time.time() - 7200
        w._touch_session('9-1-aaaa', 'trigger')
        self.assertNotIn('0-1-aaaa', w._sessions)
        self.assertIn('9-1-aaaa', w._sessions)


# =====================================================================
# Per-session _logs cursors (the multi-session bug fix)
# =====================================================================

class TestPerSessionLogCursors(EmbodyTestCase):

    SIDS = ('_tc_cursor_a', '_tc_cursor_b', '_tc_cursor_c')

    def setUp(self):
        self.ext = op.Embody.ext.Envoy
        self.assertTrue(hasattr(self.ext, '_log_cursors'),
                        'running EnvoyExt predates per-session cursors')
        for sid in self.SIDS:
            self.ext._log_cursors.pop(sid, None)

    def tearDown(self):
        for sid in self.SIDS:
            self.ext._log_cursors.pop(sid, None)

    def test_own_warning_served_after_baseline(self):
        ext = self.ext
        ext._baselineLogCursor('_tc_cursor_a')
        marker = 'cursor-test-own-{}'.format(int(time.time() * 1000))
        op.Embody.Log(marker, 'WARNING')
        r = {}
        ext._attachNotableLogs(r, '_tc_cursor_a')
        self.assertIn(marker, json.dumps(r.get('_logs', [])))

    def test_two_sessions_both_receive_same_warning(self):
        # THE regression test for the single-cursor bug: session A
        # polling first must not consume session B's copy.
        ext = self.ext
        ext._baselineLogCursor('_tc_cursor_a')
        ext._baselineLogCursor('_tc_cursor_b')
        marker = 'cursor-test-shared-{}'.format(int(time.time() * 1000))
        op.Embody.Log(marker, 'WARNING')
        ra = {}
        ext._attachNotableLogs(ra, '_tc_cursor_a')
        rb = {}
        ext._attachNotableLogs(rb, '_tc_cursor_b')
        self.assertIn(marker, json.dumps(ra.get('_logs', [])))
        self.assertIn(marker, json.dumps(rb.get('_logs', [])))

    def test_no_reserve_after_cursor_advances(self):
        ext = self.ext
        ext._baselineLogCursor('_tc_cursor_a')
        marker = 'cursor-test-once-{}'.format(int(time.time() * 1000))
        op.Embody.Log(marker, 'WARNING')
        first = {}
        ext._attachNotableLogs(first, '_tc_cursor_a')
        second = {}
        ext._attachNotableLogs(second, '_tc_cursor_a')
        self.assertIn(marker, json.dumps(first.get('_logs', [])))
        self.assertNotIn(marker, json.dumps(second.get('_logs', [])))

    def test_baseline_excludes_history(self):
        ext = self.ext
        marker = 'cursor-test-history-{}'.format(int(time.time() * 1000))
        op.Embody.Log(marker, 'WARNING')
        ext._baselineLogCursor('_tc_cursor_c')  # AFTER the warning
        r = {}
        ext._attachNotableLogs(r, '_tc_cursor_c')
        self.assertNotIn(marker, json.dumps(r.get('_logs', [])))


# =====================================================================
# Phase 2: scope extraction, touch map, peer advisories
# =====================================================================

class TestScopeHelpers(EmbodyTestCase):

    def test_scopes_create_op(self):
        scopes = _envoy_mod._scopes_for_operation(
            'create_op', {'parent_path': '/a'}, {'path': '/a/b'})
        self.assertEqual(scopes, ['/a', '/a/b'])

    def test_scopes_special_and_paths(self):
        self.assertEqual(
            _envoy_mod._scopes_for_operation('run_tests', {}),
            ['project:tests'])
        self.assertIn(
            'project:python',
            _envoy_mod._scopes_for_operation('execute_python', {'code': 'x'}))

    def test_scopes_rename_includes_new_path(self):
        scopes = _envoy_mod._scopes_for_operation(
            'rename_op', {'op_path': '/a/old', 'new_name': 'new'})
        self.assertIn('/a/old', scopes)
        self.assertIn('/a/new', scopes)

    def test_scopes_batch_unions_sub_operations(self):
        scopes = _envoy_mod._scopes_for_operation('batch_operations', {
            'operations': [
                {'tool': 'set_parameter', 'params': {'op_path': '/a/x'}},
                {'tool': 'connect_ops', 'params': {
                    'source_path': '/a/x', 'dest_path': '/a/y'}},
            ]})
        self.assertIn('/a/x', scopes)
        self.assertIn('/a/y', scopes)

    def test_overlap_segment_aware(self):
        overlaps = _envoy_mod._scope_overlaps
        self.assertTrue(overlaps('/a/b', '/a/b'))
        self.assertTrue(overlaps('/a/b', '/a/b/c'))
        self.assertTrue(overlaps('/a/b/c', '/a/b'))
        self.assertFalse(overlaps('/a/b', '/a/bc'))
        self.assertTrue(overlaps('file:x/y.py', 'file:x/y.py'))
        self.assertFalse(overlaps('file:x/y.py', 'file:x/y2.py'))
        self.assertFalse(overlaps('project:tests', 'project:python'))


class TestTouchMapAndAdvisories(EmbodyTestCase):

    A = '_tc_sess_a'
    B = '_tc_sess_b'

    def setUp(self):
        import sys as _sys
        self.ext = op.Embody.ext.Envoy
        self.lock = getattr(_sys, '_envoy_sessions_lock', None)
        self.touches = getattr(_sys, '_envoy_touches', None)
        self.assertIsNotNone(self.lock, 'shared lock missing (server never started?)')
        self.assertIsNotNone(self.touches, 'touch store missing')
        self._purge()

    def tearDown(self):
        self._purge()

    def _purge(self):
        with self.lock:
            for k in [k for k in self.touches
                      if k.startswith('/_tc_') or k.startswith('file:_tc')]:
                del self.touches[k]
        for sid in (self.A, self.B):
            self.ext._advisories_served.pop(sid, None)

    def _plant_touch(self, sid, scope, tool='set_parameter', age_s=0.0):
        with self.lock:
            ring = self.touches.setdefault(scope, [])
            ring.append({'sid': sid, 'tool': tool,
                         'ts': time.time() - age_s})

    def test_write_recorded_read_not(self):
        self.ext._recordTouches(self.A, 'set_parameter', ['/_tc_scope_x'])
        self.ext._recordTouches(self.A, 'get_op', ['/_tc_scope_y'])
        with self.lock:
            self.assertIn('/_tc_scope_x', self.touches)
            self.assertNotIn('/_tc_scope_y', self.touches)

    def test_conflict_advisory_for_peer_write(self):
        self._plant_touch(self.A, '/_tc_scope_x')
        result = {}
        self.ext._attachPeerAdvisories(
            result, self.B, 'set_parameter', ['/_tc_scope_x'])
        peers = result.get('_peers', [])
        self.assertEqual(len(peers), 1)
        self.assertTrue(peers[0]['conflict'])
        self.assertEqual(peers[0]['scope'], '/_tc_scope_x')

    def test_no_advisory_for_own_touch(self):
        self._plant_touch(self.A, '/_tc_scope_x')
        result = {}
        self.ext._attachPeerAdvisories(
            result, self.A, 'set_parameter', ['/_tc_scope_x'])
        self.assertNotIn('_peers', result)

    def test_read_advisory_deduped_conflict_not(self):
        # Old touch (age > conflict window): READ advisories dedup.
        self._plant_touch(self.A, '/_tc_scope_x', age_s=120)
        first, second = {}, {}
        self.ext._attachPeerAdvisories(first, self.B, 'get_op', ['/_tc_scope_x'])
        self.ext._attachPeerAdvisories(second, self.B, 'get_op', ['/_tc_scope_x'])
        self.assertIn('_peers', first)
        self.assertFalse(first['_peers'][0]['conflict'])
        self.assertNotIn('_peers', second)
        # Fresh touch + write on the other side: conflict bypasses dedup.
        self._purge()
        self._plant_touch(self.A, '/_tc_scope_x')
        c1, c2 = {}, {}
        self.ext._attachPeerAdvisories(c1, self.B, 'set_parameter', ['/_tc_scope_x'])
        self.ext._attachPeerAdvisories(c2, self.B, 'set_parameter', ['/_tc_scope_x'])
        self.assertTrue(c1.get('_peers') and c1['_peers'][0]['conflict'])
        self.assertTrue(c2.get('_peers') and c2['_peers'][0]['conflict'])

    def test_ancestor_scope_triggers_advisory(self):
        self._plant_touch(self.A, '/_tc_scope_x')
        result = {}
        self.ext._attachPeerAdvisories(
            result, self.B, 'set_parameter', ['/_tc_scope_x/child'])
        self.assertIn('_peers', result)

    def test_advisories_collapse_per_peer_prefer_op_path(self):
        self._plant_touch(self.A, 'file:_tc/x.py')
        self._plant_touch(self.A, '/_tc_scope_x')
        result = {}
        self.ext._attachPeerAdvisories(
            result, self.B, 'set_parameter',
            ['/_tc_scope_x', 'file:_tc/x.py'])
        peers = result.get('_peers', [])
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['scope'], '/_tc_scope_x')

    def test_collapsed_scope_surfaces_on_next_call(self):
        # The file-scope advisory was NOT emitted (collapsed), so it must
        # not be marked served: a later read may still surface it.
        self._plant_touch(self.A, 'file:_tc/x.py', age_s=120)
        self._plant_touch(self.A, '/_tc_scope_x', age_s=120)
        first, second = {}, {}
        self.ext._attachPeerAdvisories(
            first, self.B, 'get_op', ['/_tc_scope_x', 'file:_tc/x.py'])
        self.ext._attachPeerAdvisories(
            second, self.B, 'get_op', ['/_tc_scope_x', 'file:_tc/x.py'])
        self.assertEqual(first['_peers'][0]['scope'], '/_tc_scope_x')
        self.assertIn('_peers', second)
        self.assertEqual(second['_peers'][0]['scope'], 'file:_tc/x.py')

    def test_expand_file_scopes_nearest_two_only(self):
        scopes = self.ext._expandFileScopes(
            ['/embody/unit_tests/test_envoy_sessions'])
        self.assertIn('file:embody/unit_tests/test_envoy_sessions.py', scopes)
        self.assertIn('file:embody/unit_tests.tdn', scopes)
        self.assertNotIn('file:embody.tdn', scopes,
                         'project-root .tdn must not blanket every op')

    def test_snapshot_includes_recent_scopes(self):
        w = _bare_worker()
        w._touch_session('7-1-aaaa', 'lab')
        w._touches['/_tc_scope_w'] = [
            {'sid': '7-1-aaaa', 'tool': 'set_parameter', 'ts': time.time()}]
        snap = w._sessions_snapshot()
        entry = snap['sessions'][0]
        self.assertIn('recent_scopes', entry)
        self.assertEqual(entry['recent_scopes'][0]['scope'], '/_tc_scope_w')


# =====================================================================
# Phase 3: claims, leases, destructive-op gates
# =====================================================================

class TestClaimLeases(EmbodyTestCase):

    def _worker_with_sessions(self):
        w = _bare_worker()
        w._touch_session('11-1-aaaa', 'alpha@x')
        w._touch_session('22-1-bbbb', 'beta@y')
        return w

    def test_claim_grant_and_overlap_refusal(self):
        w = self._worker_with_sessions()
        r1 = w._claim_scope('11-1-aaaa', 'alpha@x', '/_tc_c/sub', 'building', 300)
        self.assertTrue(r1['granted'])
        r2 = w._claim_scope('22-1-bbbb', 'beta@y', '/_tc_c', '', 300)
        self.assertFalse(r2['granted'])
        self.assertEqual(r2['holder']['label'], 'alpha@x')
        self.assertEqual(r2['holder']['scope'], '/_tc_c/sub')
        r3 = w._claim_scope('22-1-bbbb', 'beta@y', '/_tc_c/sub/deeper', '', 300)
        self.assertFalse(r3['granted'])

    def test_own_claims_never_conflict(self):
        w = self._worker_with_sessions()
        self.assertTrue(w._claim_scope(
            '11-1-aaaa', 'alpha@x', '/_tc_c', '', 300)['granted'])
        self.assertTrue(w._claim_scope(
            '11-1-aaaa', 'alpha@x', '/_tc_c/sub', '', 300)['granted'])

    def test_scope_validation_and_ttl_clamp(self):
        w = self._worker_with_sessions()
        self.assertIn('error', w._claim_scope(
            '11-1-aaaa', 'alpha@x', 'not-a-scope', '', 300))
        granted = w._claim_scope('11-1-aaaa', 'alpha@x', '/_tc_c', '', 5)
        self.assertEqual(granted['ttl'], 30)
        granted = w._claim_scope('11-1-aaaa', 'alpha@x', 'project:_tc', '', 99999)
        self.assertEqual(granted['ttl'], 3600)

    def test_release_own_not_held_and_foreign(self):
        w = self._worker_with_sessions()
        w._claim_scope('11-1-aaaa', 'alpha@x', '/_tc_c', '', 300)
        self.assertFalse(w._release_scope('11-1-aaaa', '/_tc_none')['released'])
        foreign = w._release_scope('22-1-bbbb', '/_tc_c')
        self.assertFalse(foreign['released'])
        self.assertEqual(foreign['holder'], 'alpha@x')
        self.assertTrue(w._release_scope('11-1-aaaa', '/_tc_c')['released'])

    def test_expiry_ttl_and_dead_holder(self):
        w = self._worker_with_sessions()
        now = time.time()
        w._claims['/_tc_expired'] = {
            'sid': '11-1-aaaa', 'label': 'alpha@x', 'note': '',
            'ts': now - 100, 'ttl': 30}
        w._claims['/_tc_dead_holder'] = {
            'sid': '33-1-cccc', 'label': 'ghost@z', 'note': '',
            'ts': now, 'ttl': 3600}
        w._claims['/_tc_anon_ok'] = {
            'sid': '_anon', 'label': '_anon', 'note': '',
            'ts': now, 'ttl': 3600}
        with w._sessions_lock:
            w._prune_claims_locked(time.time())
        self.assertNotIn('/_tc_expired', w._claims)
        self.assertNotIn('/_tc_dead_holder', w._claims,
                         'silent holder (not in registry) must expire')
        self.assertIn('/_tc_anon_ok', w._claims,
                      'anon claims live by TTL only -- must NOT be pruned')

    def test_snapshot_lists_claims(self):
        w = self._worker_with_sessions()
        w._claim_scope('11-1-aaaa', 'alpha@x', '/_tc_c', 'my build', 300)
        snap = w._sessions_snapshot()
        alpha = next(s for s in snap['sessions'] if s['sid'] == '11-1-aaaa')
        self.assertEqual(alpha['claims'][0]['scope'], '/_tc_c')
        self.assertEqual(alpha['claims'][0]['note'], 'my build')

    def test_renewal_via_record_touches(self):
        import sys as _sys
        ext = op.Embody.ext.Envoy
        lock = getattr(_sys, '_envoy_sessions_lock')
        claims = getattr(_sys, '_envoy_claims')
        old_ts = time.time() - 200
        with lock:
            claims['/_tc_renew'] = {'sid': '_tc_claim_a', 'label': 'a',
                                    'note': '', 'ts': old_ts, 'ttl': 300}
        try:
            ext._recordTouches('_tc_claim_a', 'set_parameter',
                               ['/_tc_renew/child'])
            with lock:
                self.assertGreater(claims['/_tc_renew']['ts'], old_ts,
                                   'own write must renew the lease')
        finally:
            with lock:
                claims.pop('/_tc_renew', None)
                touches = getattr(_sys, '_envoy_touches', {})
                touches.pop('/_tc_renew/child', None)


class TestDestructiveGates(EmbodyTestCase):

    A = '_tc_gate_a'
    B = '_tc_gate_b'

    def setUp(self):
        import sys as _sys
        self.ext = op.Embody.ext.Envoy
        self.lock = getattr(_sys, '_envoy_sessions_lock')
        self.claims = getattr(_sys, '_envoy_claims')
        self.touches = getattr(_sys, '_envoy_touches')
        self.sessions = getattr(_sys, '_envoy_sessions')
        self._purge()
        now = time.time()
        with self.lock:
            for sid, label in ((self.A, 'gate-a@x'), (self.B, 'gate-b@y')):
                self.sessions[sid] = {
                    'sid': sid, 'label': label, 'pid': None,
                    'first_seen': now, 'last_seen': now,
                    'requests': 1, 'last_tool': None}

    def tearDown(self):
        self._purge()

    def _purge(self):
        with self.lock:
            for k in [k for k in self.claims if '/_tc_gate' in k
                      or k == 'project:tests_tc']:
                del self.claims[k]
            for k in [k for k in self.touches if '/_tc_gate' in k]:
                del self.touches[k]
            for sid in (self.A, self.B):
                self.sessions.pop(sid, None)

    def test_targets_matrix(self):
        dt = self.ext._destructiveTargets
        self.assertEqual(dt('delete_op', {'op_path': '/_tc_gate/x'}),
                         (['/_tc_gate/x'], 'delete_op'))
        self.assertEqual(dt('import_network',
                            {'target_path': '/_tc_gate', 'clear_first': False}),
                         ([], ''))
        scopes, reason = dt('import_network',
                            {'target_path': '/_tc_gate', 'clear_first': True})
        self.assertEqual(scopes, ['/_tc_gate'])
        self.assertEqual(dt('run_tests', {})[0], ['project:tests'])
        scopes, _r = dt('batch_operations', {'operations': [
            {'tool': 'set_parameter', 'params': {'op_path': '/_tc_gate/p'}},
            {'tool': 'delete_op', 'params': {'op_path': '/_tc_gate/x'}},
        ]})
        self.assertEqual(scopes, ['/_tc_gate/x'])
        scopes, _r = dt('batch_operations', {'operations': [
            {'tool': 'delete_op',
             'params': {'op_path': '/_tc_gate/x', 'override': True}},
        ]})
        self.assertEqual(scopes, [])

    def test_gate_refuses_on_live_peer_claim(self):
        with self.lock:
            self.claims['/_tc_gate'] = {
                'sid': self.A, 'label': 'gate-a@x', 'note': 'mine',
                'ts': time.time(), 'ttl': 300}
        gate = self.ext._checkDestructiveGate(
            self.B, 'delete_op', {'op_path': '/_tc_gate/child'})
        self.assertIsNotNone(gate)
        self.assertIn('MULTI-SESSION GATE', gate['error'])
        self.assertEqual(gate['holder']['label'], 'gate-a@x')
        # Holder itself passes
        self.assertIsNone(self.ext._checkDestructiveGate(
            self.A, 'delete_op', {'op_path': '/_tc_gate/child'}))
        # override passes
        self.assertIsNone(self.ext._checkDestructiveGate(
            self.B, 'delete_op',
            {'op_path': '/_tc_gate/child', 'override': True}))

    def test_gate_ignores_stale_holder(self):
        with self.lock:
            self.claims['/_tc_gate'] = {
                'sid': self.A, 'label': 'gate-a@x', 'note': '',
                'ts': time.time(), 'ttl': 300}
            self.sessions[self.A]['last_seen'] = time.time() - 700
        self.assertIsNone(self.ext._checkDestructiveGate(
            self.B, 'delete_op', {'op_path': '/_tc_gate/child'}))

    def test_gate_refuses_on_fresh_peer_write(self):
        with self.lock:
            self.touches['/_tc_gate/x'] = [
                {'sid': self.A, 'tool': 'set_parameter',
                 'ts': time.time() - 5}]
        gate = self.ext._checkDestructiveGate(
            self.B, 'delete_op', {'op_path': '/_tc_gate/x'})
        self.assertIsNotNone(gate)
        self.assertEqual(gate['peer']['label'], 'gate-a@x')
        # Old write (outside the conflict window) passes
        with self.lock:
            self.touches['/_tc_gate/x'] = [
                {'sid': self.A, 'tool': 'set_parameter',
                 'ts': time.time() - 120}]
        self.assertIsNone(self.ext._checkDestructiveGate(
            self.B, 'delete_op', {'op_path': '/_tc_gate/x'}))

    def test_non_destructive_ops_never_gated(self):
        with self.lock:
            self.claims['/_tc_gate'] = {
                'sid': self.A, 'label': 'gate-a@x', 'note': '',
                'ts': time.time(), 'ttl': 300}
        self.assertIsNone(self.ext._checkDestructiveGate(
            self.B, 'set_parameter', {'op_path': '/_tc_gate/child'}))
        self.assertIsNone(self.ext._checkDestructiveGate(
            self.B, 'get_op', {'op_path': '/_tc_gate/child'}))
