"""
Test suite: release artifact contract -- what the shipped Embody .tox must
NOT carry.

Expands the newest release/Embody-v*.tox with TouchDesigner's toeexpand and
reads the top-level Embody.* files: the COMP's pickled storage must be empty
and no parameter may hold an absolute path into the authoring project. Both
leaked through v6.2.15 (found 2026-09-05 on a bootstrap-provisioned show
file: 'Invalid path for node /embody/externalizations' plus 16 storage
keys). Skips where toeexpand is absent (CI runners have no TouchDesigner).
"""

import importlib.util
import os
import pickle
import shutil
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))          # dev/embody/unit_tests
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_spec = importlib.util.spec_from_file_location(
    'embody_bootstrap_rac', os.path.join(os.path.dirname(_HERE), 'embody_bootstrap.py'))
eb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eb)

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

RELEASE_DIR = os.path.join(_REPO, 'release')


def _release_tox():
    if not os.path.isdir(RELEASE_DIR):
        return None
    toxes = sorted(f for f in os.listdir(RELEASE_DIR)
                   if f.startswith('Embody-v') and f.endswith('.tox'))
    return os.path.join(RELEASE_DIR, toxes[-1]) if toxes else None


class TestReleaseArtifactContract(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        installs = eb.find_td_installs()
        tools = eb.td_tools(installs[0][1]) if installs else {}
        if not tools.get('toeexpand'):
            self.skipTest('TouchDesigner toeexpand not installed')
        self.tox = _release_tox()
        if not self.tox:
            self.skipTest('no release tox in release/')
        self._tmp = tempfile.mkdtemp(prefix='rac_')
        work = os.path.join(self._tmp, os.path.basename(self.tox))
        shutil.copyfile(self.tox, work)
        self.dir, _toc = eb.expand(tools['toeexpand'], work)

    def tearDown(self):
        shutil.rmtree(getattr(self, '_tmp', ''), ignore_errors=True)
        super().tearDown()

    def _read(self, name):
        with open(os.path.join(self.dir, name), 'rb') as f:
            return f.read()

    def test_root_storage_is_empty(self):
        """Embody.n carries `dict <hex pickle>` when the COMP has storage."""
        lines = [l for l in self._read('Embody.n').split(b'\n') if l.startswith(b'dict ')]
        if not lines:
            return
        self.assertEqual(len(lines), 1)
        payload = bytes.fromhex(lines[0][5:].decode('ascii'))
        try:
            storage = pickle.loads(payload)
        except Exception as e:
            self.fail('%s ships COMP storage that does not even unpickle outside TD '
                      '(%s); ExportPortableTox must scrub it' % (os.path.basename(self.tox), e))
        # assertFalse, not assertEqual: a leaked dict diff runs to megabytes
        self.assertFalse(storage, '%s ships COMP storage: %s'
                         % (os.path.basename(self.tox), sorted(storage)))

    def test_no_absolute_authoring_paths_in_parameters(self):
        for name in ('Embody.parm', 'Embody.cparm'):
            self.assertNotIn(b'/embody/', self._read(name),
                             '%s carries an absolute authoring-project path' % name)
