"""
Test suite: Envoy instance registry rename + prune logic.

Covers EnvoyExt._instanceKey and the stale-entry pruning inside
_writeEnvoyConfig. The key behaviors:

- Re-registration with same toe_path returns the existing key
  (idempotent, no churn).
- Re-registration with a NEW toe_path (TD's save-time auto-bump
  Foo-5.398 -> Foo-5.399) returns the new basename so the registry
  walks forward.
- Stale registry rows for our own PID are pruned by _writeEnvoyConfig.
- Other PIDs' rows are left alone.

These exercise live state machinery without touching the real
.embody/envoy.json -- _instanceKey is pure (takes a dict, returns a
str) so we can pass crafted state directly.
"""

import os

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestInstanceKeyRename(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.my_pid = os.getpid()

    def test_basename_used_when_registry_empty(self):
        key = self.envoy._instanceKey('dev/Embody-5.398.toe', {})
        self.assertEqual(key, 'Embody-5.398')

    def test_existing_key_reused_when_toe_path_unchanged(self):
        instances = {
            'Embody-5.398': {
                'toe_path': 'dev/Embody-5.398.toe',
                'port': 9870,
                'td_pid': self.my_pid,
            },
        }
        key = self.envoy._instanceKey('dev/Embody-5.398.toe', instances)
        self.assertEqual(key, 'Embody-5.398')

    def test_walks_forward_when_toe_path_changes_for_same_pid(self):
        """The save-time rename case: registry has us under .398 but
        we now own .399. Returns the new basename."""
        instances = {
            'Embody-5.398': {
                'toe_path': 'dev/Embody-5.398.toe',
                'port': 9870,
                'td_pid': self.my_pid,
            },
        }
        key = self.envoy._instanceKey('dev/Embody-5.399.toe', instances)
        self.assertEqual(key, 'Embody-5.399')

    def test_reclaims_own_basename_collision(self):
        """When the new basename collides with another row that ALSO
        belongs to our PID (shouldn't normally happen but possible
        after a crash + restart), reclaim instead of suffixing."""
        instances = {
            'Embody-5.399': {
                'toe_path': 'dev/Embody-5.399.toe',
                'port': 9870,
                'td_pid': self.my_pid,
            },
        }
        key = self.envoy._instanceKey('dev/Embody-5.399.toe', instances)
        self.assertEqual(key, 'Embody-5.399')

    def test_appends_suffix_for_live_foreign_pid_collision(self):
        """A foreign live PID holds the basename; we suffix to -2."""
        instances = {
            'Embody-5.399': {
                'toe_path': 'dev/Embody-5.399.toe',
                'port': 9871,
                'td_pid': self.my_pid + 999999,  # almost certainly dead
            },
        }
        # Force the "alive" check by patching _isPidAlive to return
        # True for the foreign pid -- we want to verify the suffix
        # path, not whether arbitrary pids happen to be alive.
        original = self.envoy._isPidAlive
        self.envoy._isPidAlive = lambda p: True if p != self.my_pid else False
        try:
            key = self.envoy._instanceKey(
                'dev/Embody-5.399.toe', instances)
        finally:
            self.envoy._isPidAlive = original
        self.assertEqual(key, 'Embody-5.399-2')

    def test_reclaims_dead_basename(self):
        """Stale entry under the basename whose PID is dead -- reuse."""
        instances = {
            'Embody-5.399': {
                'toe_path': 'dev/Embody-5.399.toe',
                'port': 9870,
                'td_pid': 999999999,  # not a real PID
            },
        }
        # Force _isPidAlive to return False for the stale pid.
        original = self.envoy._isPidAlive
        self.envoy._isPidAlive = lambda p: p == self.my_pid
        try:
            key = self.envoy._instanceKey(
                'dev/Embody-5.399.toe', instances)
        finally:
            self.envoy._isPidAlive = original
        self.assertEqual(key, 'Embody-5.399')

    def test_old_pid_entry_not_reused_when_toe_changed(self):
        """Sanity: a foreign live PID's row should not be returned even
        if it shares the new basename -- we still need a unique key."""
        instances = {
            'Embody-5.399': {
                'toe_path': 'dev/Embody-5.399.toe',
                'port': 9870,
                'td_pid': 12345,  # foreign live PID
            },
            'Embody-5.398': {
                'toe_path': 'dev/Embody-5.398.toe',
                'port': 9871,
                'td_pid': self.my_pid,
            },
        }
        original = self.envoy._isPidAlive
        self.envoy._isPidAlive = lambda p: p == 12345 or p == self.my_pid
        try:
            key = self.envoy._instanceKey(
                'dev/Embody-5.399.toe', instances)
        finally:
            self.envoy._isPidAlive = original
        # 'Embody-5.399' is owned by a foreign live PID, so we get -2
        self.assertEqual(key, 'Embody-5.399-2')


class TestRegistryDeadPidGC(EmbodyTestCase):
    """_writeEnvoyConfig garbage-collects rows whose td_pid is dead
    on every write. Catches the accumulation that the previous
    deregister-only flow allowed: hard kills, force-quits, OS
    crashes, and Cmd+Q-without-Envoy-stop all leave dead rows.
    """

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.my_pid = os.getpid()

    def _write_test_registry(self, tmp_dir, instances, active_key):
        """Drop a synthetic envoy.json into tmp_dir/.embody/."""
        embody_dir = tmp_dir / '.embody'
        embody_dir.mkdir(parents=True, exist_ok=True)
        config_path = embody_dir / 'envoy.json'
        import json as _json
        config_path.write_text(_json.dumps({
            'active': active_key,
            'td_executable': '/Applications/TouchDesigner.app',
            'instances': instances,
        }))
        return embody_dir, config_path

    def _read_registry(self, config_path):
        import json as _json
        return _json.loads(config_path.read_text())

    def test_dead_rows_pruned_on_write(self):
        import tempfile, json as _json
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix='embody_gc_test_'))
        try:
            embody_dir, config_path = self._write_test_registry(
                tmp,
                {
                    # Live row (matches our PID + current toe)
                    'self_key': {
                        'toe_path': 'dev/whatever.toe',
                        'port': 9870,
                        'td_pid': self.my_pid,
                    },
                    'dead_a': {
                        'toe_path': 'dev/a.toe', 'port': 9871,
                        'td_pid': 999999991,
                    },
                    'dead_b': {
                        'toe_path': 'dev/b.toe', 'port': 9872,
                        'td_pid': 999999992,
                    },
                    'dead_c': {
                        'toe_path': 'dev/c.toe', 'port': 9873,
                        'td_pid': 999999993,
                    },
                },
                active_key='self_key',
            )
            # Force is-alive predicate: our PID + nothing else
            original = self.envoy._isPidAlive
            self.envoy._isPidAlive = lambda p: p == self.my_pid
            try:
                self.envoy._writeEnvoyConfig(embody_dir, port=9870)
            finally:
                self.envoy._isPidAlive = original

            after = self._read_registry(config_path)
            keys = set(after.get('instances', {}).keys())
            # Our row should remain (under whatever current key
            # _writeEnvoyConfig computed). All dead rows gone.
            self.assertNotIn('dead_a', keys)
            self.assertNotIn('dead_b', keys)
            self.assertNotIn('dead_c', keys)
            # Exactly one live row remains -- ours
            self.assertEqual(len(keys), 1)
            remaining = next(iter(after['instances'].values()))
            self.assertEqual(remaining['td_pid'], self.my_pid)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_live_foreign_row_preserved(self):
        """A foreign instance with a live PID stays in the registry."""
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix='embody_gc_test_'))
        try:
            FOREIGN_PID = 12345
            embody_dir, config_path = self._write_test_registry(
                tmp,
                {
                    'foreign_live': {
                        'toe_path': 'dev/other.toe', 'port': 9871,
                        'td_pid': FOREIGN_PID,
                    },
                    'self_key': {
                        'toe_path': 'dev/whatever.toe', 'port': 9870,
                        'td_pid': self.my_pid,
                    },
                    'dead_x': {
                        'toe_path': 'dev/x.toe', 'port': 9872,
                        'td_pid': 999999990,
                    },
                },
                active_key='self_key',
            )
            original = self.envoy._isPidAlive
            self.envoy._isPidAlive = (
                lambda p: p in (self.my_pid, FOREIGN_PID))
            try:
                self.envoy._writeEnvoyConfig(embody_dir, port=9870)
            finally:
                self.envoy._isPidAlive = original

            after = self._read_registry(config_path)
            keys = set(after.get('instances', {}).keys())
            self.assertIn('foreign_live', keys)
            self.assertNotIn('dead_x', keys)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
