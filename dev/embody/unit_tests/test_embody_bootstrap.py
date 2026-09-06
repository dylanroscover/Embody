"""
Test suite: embody_bootstrap.py -- offline install of Embody into a .toe.

Pure parts (toc parsing, graft planning, the expanded .text framing, the
provisioning DAT, install discovery, manifest verification) run anywhere.
The integration test drives TouchDesigner's own toeexpand/toecollapse over
the bare fixture and the release tox, and skips where those tools are absent
(CI runners have no TouchDesigner).
"""

import hashlib
import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile

# Paths from this file, not project.folder: under the pytest tier the
# conftest points project.folder at a throwaway sandbox that holds only the
# bridge copies.
_HERE = os.path.dirname(os.path.abspath(__file__))          # dev/embody/unit_tests
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_path = os.path.join(os.path.dirname(_HERE), 'embody_bootstrap.py')
_spec = importlib.util.spec_from_file_location('embody_bootstrap_t', _path)
eb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eb)

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

FIXTURE = os.path.join(_HERE, 'fixtures', 'bare_project.toe')
RELEASE_DIR = os.path.join(_REPO, 'release')
SITE_COPY = os.path.join(_REPO, 'platform', 'apps', 'web', 'public', 'bootstrap.py')


def _release_tox():
    if not os.path.isdir(RELEASE_DIR):
        return None
    toxes = sorted(f for f in os.listdir(RELEASE_DIR) if f.startswith('Embody-v') and f.endswith('.tox'))
    return os.path.join(RELEASE_DIR, toxes[-1]) if toxes else None


class TestToc(EmbodyTestCase):

    def _write(self, data):
        d = tempfile.mkdtemp(prefix='eb_toc_')
        p = os.path.join(d, 'x.toc')
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def test_reads_entries_and_drops_header(self):
        p = self._write(b'# 4 0 0 0 1\n.build\nEmbody.n\nEmbody/help.n\n')
        self.assertEqual(eb.read_toc(p), ['.build', 'Embody.n', 'Embody/help.n'])

    def test_rejects_bom_and_cr(self):
        with self.assertRaises(eb.BootstrapError):
            eb.read_toc(self._write(b'\xef\xbb\xbf.build\n'))
        with self.assertRaises(eb.BootstrapError):
            eb.read_toc(self._write(b'.build\r\nEmbody.n\r\n'))

    def test_write_is_lf_only(self):
        p = self._write(b'')
        eb.write_toc(p, ['.build', 'a.n'])
        self.assertEqual(open(p, 'rb').read(), b'.build\na.n\n')


class TestGraftPlan(EmbodyTestCase):
    TOX = ['.build', 'Embody.n', 'Embody.cparm', 'Embody.parm', 'Embody/help.n']
    PROJ = ['.build', '.root', 'project1.n', 'project1/noise1.n']

    def test_plan_adds_everything_but_build(self):
        plan = eb.plan_graft(self.PROJ, self.TOX)
        self.assertEqual(plan['root'], 'Embody')
        self.assertEqual(plan['add'], self.TOX[1:])
        self.assertEqual(plan['remove'], [])
        self.assertFalse(plan['existing'])

    def test_refuses_when_already_installed(self):
        with self.assertRaises(eb.BootstrapError) as cm:
            eb.plan_graft(self.PROJ + ['Embody.n', 'Embody/old.n'], self.TOX)
        self.assertEqual(cm.exception.code, 'embody.bootstrap.already_installed')

    def test_replace_removes_the_old_entries(self):
        plan = eb.plan_graft(self.PROJ + ['Embody.n', 'Embody/old.n', 'Embody.parm'], self.TOX, replace=True)
        self.assertEqual(sorted(plan['remove']), ['Embody.n', 'Embody.parm', 'Embody/old.n'])
        self.assertTrue(plan['existing'])

    def test_root_name_needs_a_root_entry(self):
        with self.assertRaises(eb.BootstrapError):
            eb.tox_root_name(['.build', 'Embody/help.n'])

    def test_entries_of_matches_only_that_root(self):
        self.assertEqual(eb.entries_of('Embody', ['Embody.n', 'Embody2.n', 'Embody/x.n', 'Embodyx/y.n', 'Embody.parm']),
                         ['Embody.n', 'Embody/x.n', 'Embody.parm'])


class TestTextFraming(EmbodyTestCase):

    def test_roundtrip(self):
        body = b'def onStart():\n    return\n'
        blob = eb.encode_text(body)
        self.assertTrue(blob.startswith(b'2\n*'))
        self.assertEqual(struct.unpack('>5I', blob[3:23]), (1, 1, 1, 1, 2))
        self.assertEqual(struct.unpack('>I', blob[23:27])[0], len(body))
        self.assertEqual(eb.decode_text(blob), body)

    def test_decode_rejects_bad_framing_and_length(self):
        with self.assertRaises(eb.BootstrapError):
            eb.decode_text(b'nope')
        blob = eb.encode_text(b'abc') + b'x'
        with self.assertRaises(eb.BootstrapError):
            eb.decode_text(blob)


class TestProvisioning(EmbodyTestCase):

    def test_files_and_script_for_each_assistant(self):
        files = eb.provisioning_files('claudecode')
        self.assertEqual(sorted(files), ['embody_provision.n', 'embody_provision.parm', 'embody_provision.text'])
        self.assertTrue(files['embody_provision.n'].startswith(b'DAT:execute\n'))
        self.assertIn(b'start 0 on', files['embody_provision.parm'])
        body = eb.decode_text(files['embody_provision.text']).decode()
        self.assertIn("ASSISTANT = 'claudecode'", body)
        self.assertIn("CLIENT = ''", body)
        self.assertIn('_applyWizardSetup(', body)
        self.assertIn('me.destroy()', body)
        body = eb.decode_text(eb.provisioning_files('opencode')['embody_provision.text']).decode()
        self.assertIn("ASSISTANT = 'other'", body)
        self.assertIn("CLIENT = 'opencode'", body)
        body = eb.decode_text(eb.provisioning_files('none')['embody_provision.text']).decode()
        self.assertIn("ASSISTANT = 'none'", body)

    def test_unknown_assistant(self):
        with self.assertRaises(eb.BootstrapError):
            eb.assistant_choice('clippy')

    def test_apply_writes_and_indexes(self):
        d = tempfile.mkdtemp(prefix='eb_prov_')
        toc = os.path.join(d, 'p.toc')
        eb.write_toc(toc, ['.build', 'project1.n'])
        added = eb.apply_provisioning(d, toc, 'claudecode')
        self.assertEqual(added, ['embody_provision.n', 'embody_provision.parm', 'embody_provision.text'])
        self.assertEqual(eb.read_toc(toc)[-3:], added)
        for rel in added:
            self.assertTrue(os.path.isfile(os.path.join(d, rel)))
        with self.assertRaises(eb.BootstrapError):
            eb.apply_provisioning(d, toc, 'claudecode')


class TestSiteCopy(EmbodyTestCase):

    def test_site_copy_is_byte_identical(self):
        """embody.tools/bootstrap.py must serve exactly the tracked script."""
        self.assertTrue(os.path.isfile(SITE_COPY), SITE_COPY)
        self.assertEqual(open(SITE_COPY, 'rb').read(), open(_path, 'rb').read(),
                         'platform/apps/web/public/bootstrap.py has drifted from dev/embody/embody_bootstrap.py')


class TestDiscoveryAndRelease(EmbodyTestCase):

    def test_find_td_installs_windows_fixture(self):
        root = tempfile.mkdtemp(prefix='eb_td_')
        for build in ('TouchDesigner.2025.33070', 'TouchDesigner.2024.28000', 'NotTD'):
            os.makedirs(os.path.join(root, build, 'bin'))
        found = eb.find_td_installs(platform='win32', root=root)
        self.assertEqual([b for b, _ in found], [(2025, 33070), (2024, 28000)])

    def test_td_tools_shape(self):
        root = tempfile.mkdtemp(prefix='eb_tools_')
        b = os.path.join(root, 'bin')
        os.makedirs(b)
        for exe in ('toeexpand.exe', 'toecollapse.exe', 'TouchDesigner.exe'):
            open(os.path.join(b, exe), 'wb').close()
        tools = eb.td_tools(root, platform='win32')
        self.assertTrue(tools['toeexpand'].endswith('toeexpand.exe'))
        self.assertTrue(tools['toecollapse'].endswith('toecollapse.exe'))
        self.assertIsNone(tools['python'])

    def test_fetch_release_verifies_sha256(self):
        payload = b'not really a tox'
        manifest = {'tag': 'v9.9.9', 'asset': 'Embody-v9.9.9.tox', 'version': '9.9.9',
                    'sha256': hashlib.sha256(payload).hexdigest()}
        def fetch(url):
            return json.dumps(manifest).encode() if url.endswith('.json') else payload
        d = tempfile.mkdtemp(prefix='eb_rel_')
        rel = eb.fetch_release(d, fetch=fetch)
        self.assertEqual(rel['tag'], 'v9.9.9')
        self.assertEqual(open(rel['tox'], 'rb').read(), payload)
        manifest['sha256'] = '0' * 64
        with self.assertRaises(eb.BootstrapError) as cm:
            eb.fetch_release(d, fetch=fetch)
        self.assertEqual(cm.exception.code, 'embody.bootstrap.checksum')


class TestBootstrapEndToEnd(EmbodyTestCase):
    """The real tools over the bare fixture: graft, collapse, re-expand."""

    def setUp(self):
        super().setUp()
        installs = eb.find_td_installs()
        self.tools = eb.td_tools(installs[0][1]) if installs else {}
        if not (self.tools.get('toeexpand') and self.tools.get('toecollapse')):
            self.skipTest('TouchDesigner toeexpand/toecollapse not installed')
        self.tox = _release_tox()
        if not self.tox or not os.path.isfile(FIXTURE):
            self.skipTest('release tox or bare fixture missing')

    def test_install_verify_refuse_replace(self):
        d = tempfile.mkdtemp(prefix='eb_e2e_')
        target = os.path.join(d, 'show.toe')
        shutil.copyfile(FIXTURE, target)
        logs = []
        summary = eb.bootstrap(target, self.tools, tox=self.tox, log=logs.append)
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['graft']['root'], 'Embody')
        self.assertGreater(summary['graft']['entries'], 1000)
        self.assertEqual(summary['verified_entries'], summary['graft']['entries'] + 3)
        self.assertTrue(os.path.isfile(summary['backup']))
        self.assertGreater(os.path.getsize(target), os.path.getsize(FIXTURE))
        # a second install refuses without --replace, and succeeds with it
        with self.assertRaises(eb.BootstrapError) as cm:
            eb.bootstrap(target, self.tools, tox=self.tox, log=logs.append, provision=False)
        self.assertEqual(cm.exception.code, 'embody.bootstrap.already_installed')
        again = eb.bootstrap(target, self.tools, tox=self.tox, log=logs.append, replace=True, provision=False)
        self.assertTrue(again['graft']['replaced'])
        # provisioning DAT from the first run is still there (replace only touches Embody)
        work = os.path.join(d, 'check.toe')
        shutil.copyfile(target, work)
        _dir, toc = eb.expand(self.tools['toeexpand'], work)
        entries = eb.read_toc(toc)
        self.assertIn('embody_provision.text', entries)
        self.assertEqual(len(eb.entries_of('Embody', entries)), summary['graft']['entries'])
