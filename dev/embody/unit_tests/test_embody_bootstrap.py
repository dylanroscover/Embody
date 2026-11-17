"""
Test suite: embody_bootstrap.py -- offline install of Embody into a .toe.

Pure parts (toc parsing, graft planning, the expanded .text framing, the
provisioning DAT, install discovery, manifest verification) run anywhere.
The integration test drives TouchDesigner's own toeexpand/toecollapse over
the starter project and the release tox, and skips where those tools are
absent (CI runners have no TouchDesigner).

Also covers starter/: the install script baked into Embody-starter.toe, run
on faked TouchDesigner globals, and the committed project itself.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import types

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

# The blank project the bootstrap is tested on is the one users are told to
# start from. A test fixture stood here until it was mistaken for a project
# base: it carried the smoke harness's folder-wiping Execute DAT (issue #194).
STARTER = os.path.join(_REPO, 'starter', 'Embody-starter.toe')
INSTALLER = os.path.join(_REPO, 'starter', 'embody_install.py')
SMOKE_TEMPLATE = os.path.join(_REPO, 'dev', 'release_testing', 'smoke_template.toe')
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


class _FakeTD:
    """Stands in for root, me, me.parent() and ui in embody_install.py."""

    def __init__(self, found=(), chosen=None):
        self.found, self.chosen = list(found), chosen
        self.loaded, self.destroyed, self.asked = [], 0, 0

    def findChildren(self, **kwargs):
        return self.found

    def parent(self):
        return self

    def destroy(self):
        self.destroyed += 1

    def loadTox(self, path):
        self.loaded.append(path)
        return types.SimpleNamespace(path='/project1/Embody')

    def chooseFile(self, **kwargs):
        self.asked += 1
        return self.chosen


class _Untouchable:
    """A TouchDesigner global as a worker thread must find it: off limits."""

    def __getattr__(self, name):
        raise AssertionError('the download worker touched a TouchDesigner object (.%s)' % name)


class _FakeThreadManager:
    """op.TDResources.ThreadManager: runs the task inline, then its hook the
    way the real one does (SuccessHook bare, ExceptHook with exc_info). While
    the target runs, every TouchDesigner global of the module is untouchable."""

    def __init__(self, mod, busy=False):
        self.mod, self.busy, self.tasks = mod, busy, 0

    def TDTask(self, target, SuccessHook=None, ExceptHook=None):
        return types.SimpleNamespace(target=target, ok=SuccessHook, err=ExceptHook)

    def EnqueueTask(self, task, standalone=False):
        if self.busy:
            return None
        self.tasks += 1
        names = ('root', 'me', 'ui', 'op', 'run')
        real = {n: getattr(self.mod, n) for n in names}
        for n in names:
            setattr(self.mod, n, _Untouchable())
        try:
            task.target()
        except Exception:
            error = sys.exc_info()
        else:
            error = None
        finally:
            for n in names:
                setattr(self.mod, n, real[n])
        task.err(error) if error else task.ok()
        return task


def _installer(td, busy=False):
    """starter/embody_install.py as a module, TouchDesigner's globals faked."""
    spec = importlib.util.spec_from_file_location('embody_install_t', INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.root = mod.me = mod.ui = td
    mod.run = lambda script, *args, **kwargs: eval(script, {'args': args})
    mod.op = types.SimpleNamespace(TDResources=types.SimpleNamespace(
        ThreadManager=_FakeThreadManager(mod, busy=busy)))
    return mod


def _release_get(payload, sha256=None, asset='Embody-v9.9.9.tox'):
    """A requests.get stand-in serving one release."""
    manifest = {'tag': 'v9.9.9', 'asset': asset, 'version': '9.9.9',
                'sha256': sha256 or hashlib.sha256(payload).hexdigest()}

    def get(url, headers=None, timeout=None):
        if url.endswith('.json'):
            return types.SimpleNamespace(json=lambda: manifest)
        return types.SimpleNamespace(content=payload)
    return get


def _no_network(url, headers=None, timeout=None):
    raise OSError('no route to host')


class TestStarterInstaller(EmbodyTestCase):
    """The Execute DAT inside Embody-starter.toe."""

    def test_download_checks_the_sha256(self):
        mod = _installer(_FakeTD())
        path, version = mod.download(_release_get(b'a tox'))
        self.assertEqual(version, '9.9.9')
        self.assertEqual(open(path, 'rb').read(), b'a tox')
        with self.assertRaises(ValueError):
            mod.download(_release_get(b'a tox', sha256='0' * 64))

    def test_download_stays_in_the_temp_folder(self):
        mod = _installer(_FakeTD())
        path, _ = mod.download(_release_get(b'a tox', asset='../../Embody-v9.9.9.tox'))
        self.assertEqual(os.path.dirname(path), tempfile.gettempdir())

    def test_installs_the_downloaded_release_then_removes_itself(self):
        td = _FakeTD()
        mod = _installer(td)
        mod.install(_release_get(b'a tox'))
        self.assertEqual([os.path.basename(p) for p in td.loaded], ['Embody-v9.9.9.tox'])
        self.assertEqual((td.asked, td.destroyed), (0, 1))
        # fetched on a Thread Manager task, never on the main thread
        self.assertEqual(mod.op.TDResources.ThreadManager.tasks, 1)

    def test_asks_for_a_tox_when_no_worker_is_free(self):
        td = _FakeTD(chosen='C:/toxes/Embody-v6.3.4.tox')
        _installer(td, busy=True).install(_release_get(b'a tox'))
        self.assertEqual((td.loaded, td.asked), (['C:/toxes/Embody-v6.3.4.tox'], 1))

    def test_asks_for_a_tox_when_the_download_fails(self):
        td = _FakeTD(chosen='C:/toxes/Embody-v6.3.4.tox')
        _installer(td).install(_no_network)
        self.assertEqual(td.loaded, ['C:/toxes/Embody-v6.3.4.tox'])
        self.assertEqual((td.asked, td.destroyed), (1, 1))

    def test_a_bad_checksum_is_never_loaded(self):
        td = _FakeTD(chosen=None)
        _installer(td).install(_release_get(b'a tox', sha256='0' * 64))
        self.assertEqual(td.loaded, [])
        self.assertEqual(td.asked, 1)

    def test_stays_for_the_next_open_when_the_prompt_is_cancelled(self):
        td = _FakeTD(chosen=None)
        _installer(td).install(_no_network)
        self.assertEqual((td.loaded, td.destroyed), ([], 0))

    def test_leaves_an_existing_embody_alone(self):
        """The bootstrap grafts Embody in before the project ever opens."""
        td = _FakeTD(found=[object()])

        def get(url, headers=None, timeout=None):
            raise AssertionError('fetched a release over an installed Embody')
        _installer(td).install(get)
        self.assertEqual((td.loaded, td.asked, td.destroyed), ([], 0, 1))

    def test_deletes_nothing_but_itself(self):
        """Issue #194: a project file that cleaned its own folder on open."""
        source = open(INSTALLER, encoding='utf-8').read()
        self.assertTrue(source.isascii())
        for call in ('rmtree', 'shutil', 'os.remove', 'unlink', 'rmdir'):
            self.assertNotIn(call, source)


def _td_tools(case):
    """TouchDesigner's toeexpand/toecollapse, or skip the test."""
    installs = eb.find_td_installs()
    tools = eb.td_tools(installs[0][1]) if installs else {}
    if not (tools.get('toeexpand') and tools.get('toecollapse')):
        case.skipTest('TouchDesigner toeexpand/toecollapse not installed')
    return tools


class TestStarterProject(EmbodyTestCase):
    """The committed Embody-starter.toe, expanded with the real tool."""

    def test_holds_one_script_and_it_is_the_installer(self):
        tools = _td_tools(self)
        work = os.path.join(tempfile.mkdtemp(prefix='eb_starter_'), 'starter.toe')
        shutil.copyfile(STARTER, work)
        folder, toc = eb.expand(tools['toeexpand'], work)
        entries = eb.read_toc(toc)
        self.assertEqual([e for e in entries if e.endswith('.text')],
                         ['project1/embody_install.text'])
        baked = open(os.path.join(folder, 'project1', 'embody_install.text'), 'rb').read()
        self.assertEqual(eb.decode_text(baked), open(INSTALLER, 'rb').read(),
                         'Embody-starter.toe is stale: run starter/build_starter.py')
        parm = open(os.path.join(folder, 'project1', 'embody_install.parm'), 'rb').read()
        self.assertIn(b'start 0 on', parm)


class TestSmokeTemplate(EmbodyTestCase):
    """smoke_template.toe opened without smoke_bootstrap.py beside it."""

    def test_its_baked_script_deletes_nothing(self):
        """TouchDesigner loads smoke_bootstrap.py from disk over the baked
        text, so only a lone copy runs it; that copy must stay inert."""
        tools = _td_tools(self)
        work = os.path.join(tempfile.mkdtemp(prefix='eb_smoke_tpl_'), 'smoke_template.toe')
        shutil.copyfile(SMOKE_TEMPLATE, work)
        folder, _toc = eb.expand(tools['toeexpand'], work)
        baked = eb.decode_text(open(os.path.join(folder, 'project1', 'execute1.text'), 'rb').read())
        for call in (b'rmtree', b'os.remove', b'unlink'):
            self.assertNotIn(call, baked,
                             'smoke_template.toe was saved with the smoke script baked in: a lone '
                             'copy would clear its own folder (issue #194). Put the stub back, see '
                             'dev/release_testing/README.md')


class TestBootstrapEndToEnd(EmbodyTestCase):
    """The real tools over the starter project: graft, collapse, re-expand."""

    def setUp(self):
        super().setUp()
        self.tools = _td_tools(self)
        self.tox = _release_tox()
        if not self.tox:
            self.skipTest('release tox missing')

    def test_install_verify_refuse_replace(self):
        d = tempfile.mkdtemp(prefix='eb_e2e_')
        target = os.path.join(d, 'show.toe')
        shutil.copyfile(STARTER, target)
        logs = []
        summary = eb.bootstrap(target, self.tools, tox=self.tox, log=logs.append)
        self.assertTrue(summary['ok'])
        self.assertEqual(summary['graft']['root'], 'Embody')
        self.assertGreater(summary['graft']['entries'], 1000)
        self.assertEqual(summary['verified_entries'], summary['graft']['entries'] + 3)
        self.assertTrue(os.path.isfile(summary['backup']))
        self.assertGreater(os.path.getsize(target), os.path.getsize(STARTER))
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
