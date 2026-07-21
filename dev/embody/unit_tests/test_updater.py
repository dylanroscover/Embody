"""
Test suite: UpdaterExt self-update logic (no network, no swap).

Covers the pure, deterministic pieces of the self-updater: version and
TD-build parsing/comparison, release-manifest validation (the integrity +
min-build gate that runs BEFORE any download or reload), sentinel round-trip,
dev-mode detection (this dev checkout must always refuse self-update), and
the Autoupdate consent par's persistence membership.

The swap itself (in-place external-tox reload) is deliberately NOT exercised
here -- it replaces the live Embody COMP and belongs to the destructive tier /
manual release verification, per destructive-tests.md discipline. Network
paths are not tested live (no billing, no rate-limit burn); the worker logic
is pinned through its pure helpers.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


def _updater_cls():
    """The UpdaterExt CLASS via its module (works without the live child)."""
    dat = op.Embody.op('updater/UpdaterExt')
    if dat is None:
        # Pre-landing fallback: import straight from the externalized source.
        import importlib.util
        from pathlib import Path
        src = (Path(project.folder) / 'embody' / 'Embody' / 'updater'
               / 'UpdaterExt.py')
        spec = importlib.util.spec_from_file_location('UpdaterExt_test', src)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.UpdaterExt
    return dat.module.UpdaterExt


def _valid_manifest():
    return {
        'schema': 1,
        'name': 'Embody',
        'version': '6.0.150',
        'tag': 'v6.0.150',
        'asset': 'Embody-v6.0.150.tox',
        'size': 700000,
        'sha256': 'a' * 64,
        'td_build': '2025.33070',
        'min_td_build': '2025.33070',
    }


class TestUpdaterVersionParsing(EmbodyTestCase):

    def test_parse_version_accepts_v_prefix_and_bare(self):
        U = _updater_cls()
        self.assertEqual(U.parseVersion('v6.0.141'), (6, 0, 141))
        self.assertEqual(U.parseVersion('6.0.141'), (6, 0, 141))
        self.assertEqual(U.parseVersion(' v6.0.141 '), (6, 0, 141))

    def test_parse_version_rejects_non_semver(self):
        U = _updater_cls()
        for bad in ('6.0', '6.0.141.2', 'latest', 'v6.0.141-rc1', '', None):
            self.assertIsNone(
                U.parseVersion(bad),
                f'parseVersion must reject {bad!r} (refuse, never guess)')

    def test_version_compare_is_numeric_not_lexical(self):
        U = _updater_cls()
        # Lexical compare would call '6.0.9' newer than '6.0.10'.
        self.assertTrue(U.parseVersion('6.0.10') > U.parseVersion('6.0.9'))
        self.assertTrue(U.parseVersion('6.1.0') > U.parseVersion('6.0.999'))

    def test_downgrade_is_not_an_update(self):
        # releases/latest is commit-date ordered, NOT semver: a hotfix cut
        # from an old branch can present a LOWER tag. remote <= local must
        # never trigger.
        U = _updater_cls()
        local = U.parseVersion('6.0.141')
        self.assertFalse(U.parseVersion('v6.0.140') > local)
        self.assertFalse(U.parseVersion('v6.0.141') > local)

    def test_parse_build(self):
        U = _updater_cls()
        self.assertEqual(U.parseBuild('2025.33070'), (2025, 33070))
        self.assertIsNone(U.parseBuild('33070'))
        self.assertIsNone(U.parseBuild('2025.33070.1'))
        # Numeric compare: an older build fails a newer floor.
        self.assertTrue(U.parseBuild('2025.32820') < U.parseBuild('2025.33070'))


class TestUpdaterManifest(EmbodyTestCase):

    def test_valid_manifest_passes(self):
        U = _updater_cls()
        self.assertIsNone(U.validateManifest(_valid_manifest()))

    def test_missing_keys_rejected(self):
        U = _updater_cls()
        for key in ('version', 'asset', 'size', 'sha256', 'min_td_build'):
            m = _valid_manifest()
            del m[key]
            err = U.validateManifest(m)
            self.assertIsNotNone(err, f'missing {key} must be rejected')
            self.assertIn(key, err)

    def test_malformed_fields_rejected(self):
        U = _updater_cls()
        cases = [
            ('version', 'six'),
            ('min_td_build', 'build33070'),
            ('size', -1),
            ('size', '700000'),
            ('size', 999_999_999),  # exceeds MAX_ASSET_BYTES cap
            ('sha256', 'nothex'),
            ('sha256', 'A' * 64),  # uppercase hex is not what we emit
        ]
        for key, bad in cases:
            m = _valid_manifest()
            m[key] = bad
            self.assertIsNotNone(
                U.validateManifest(m),
                f'{key}={bad!r} must be rejected')

    def test_asset_path_traversal_rejected(self):
        # asset flows into a filesystem write path -- validateManifest is the
        # gate against traversal / absolute paths.
        U = _updater_cls()
        for bad in ('../../evil.tox', 'C:/Windows/evil.tox',
                    '/etc/passwd.tox', 'sub/dir.tox', 'evil.exe',
                    'Embody-v6.0.150.zip', '..\\evil.tox'):
            m = _valid_manifest()
            m['asset'] = bad
            self.assertIsNotNone(
                U.validateManifest(m),
                f'asset={bad!r} must be rejected as unsafe')

    def test_plain_asset_name_accepted(self):
        U = _updater_cls()
        m = _valid_manifest()
        m['asset'] = 'Embody-v6.0.150.tox'
        self.assertIsNone(U.validateManifest(m))

    def test_non_dict_rejected(self):
        U = _updater_cls()
        for bad in (None, [], 'manifest', 42):
            self.assertIsNotNone(U.validateManifest(bad))

    def test_api_url(self):
        U = _updater_cls()
        self.assertEqual(
            U.apiLatestUrl('dylanroscover', 'Embody'),
            'https://api.github.com/repos/dylanroscover/Embody/releases/latest')


class TestUpdaterGuards(EmbodyTestCase):

    def test_dev_checkout_detected_here(self):
        # In THIS project EmbodyExt.py is file-synced, so a live updater
        # must refuse self-update. Mirror its detector logic directly.
        dat = op.Embody.op('EmbodyExt')
        self.assertIsNotNone(dat)
        self.assertTrue(
            bool(dat.par.file.eval()),
            'dev checkout must present a non-empty EmbodyExt file par -- '
            'the isDevCheckout() detector depends on it')

    def test_autoupdate_is_persisted(self):
        # The consent toggle must survive the very update it triggers.
        self.assertIn('Autoupdate', self.embody_ext._PERSISTED_PARAMS)

    def test_release_manifest_written_by_save_hook(self):
        # The dev save hook must have produced a manifest matching the
        # newest release tox (guards the execute_src_ctrl.py integration).
        # Skips before the first post-updater save.
        import json
        from pathlib import Path
        release_dir = Path(project.folder).parents[0] / 'release'
        manifest_path = release_dir / 'embody-release.json'
        if not manifest_path.is_file():
            self.skipTest('no embody-release.json yet (pre-updater save)')
        data = json.loads(manifest_path.read_text(encoding='utf-8'))
        U = _updater_cls()
        self.assertIsNone(U.validateManifest(data))
        tox = release_dir / data['asset']
        self.assertTrue(tox.is_file(),
                        f'manifest asset {data["asset"]} missing on disk')
        self.assertEqual(tox.stat().st_size, data['size'])
        import hashlib
        self.assertEqual(
            hashlib.sha256(tox.read_bytes()).hexdigest(), data['sha256'])


class TestUpdaterSentinel(EmbodyTestCase):

    def _instance(self):
        """A detached UpdaterExt instance whose paths point at the sandbox."""
        U = _updater_cls()
        inst = U.__new__(U)  # skip __init__ (needs no ownerComp for paths)
        inst._busy = False
        inst._pending = None
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix='updater_test_'))
        inst._updatesDir = lambda create=False: tmp
        return inst, tmp

    def test_sentinel_roundtrip_and_clear(self):
        inst, tmp = self._instance()
        try:
            self.assertIsNone(inst._readSentinel())
            data = {'tag': 'v9.9.9', 'phase': 'reloading'}
            inst._writeSentinel(data)
            self.assertEqual(inst._readSentinel(), data)
            inst._clearSentinel()
            self.assertIsNone(inst._readSentinel())
            inst._clearSentinel()  # idempotent
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_sentinel_reads_as_none(self):
        inst, tmp = self._instance()
        try:
            (tmp / 'pending.json').write_text('{not json', encoding='utf-8')
            self.assertIsNone(inst._readSentinel())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
