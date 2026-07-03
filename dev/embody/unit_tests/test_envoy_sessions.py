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
