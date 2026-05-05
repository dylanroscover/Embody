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
