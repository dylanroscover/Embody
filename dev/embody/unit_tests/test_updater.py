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


class TestUpdaterStatusLine(EmbodyTestCase):
    """Update Status resting-state contract: 'Disabled' while Auto-Update is
    Off -- never a blank field (an empty read-only par on a fresh install
    reads as broken; shipped that way once in v6.0.145)."""

    @staticmethod
    def _fakes(mode, status='stale v9.9.9 available'):
        class FakePar:
            def __init__(self, val):
                self.val = val
                self.readOnly = True

            def eval(self):
                return self.val

        class FakePars:
            pass

        pars = FakePars()
        pars.Autoupdate = FakePar(mode)
        pars.Updatestatus = FakePar(status)

        class FakeEmbody:
            par = pars

        return FakeEmbody(), pars

    def _harness(self, mode, status='stale v9.9.9 available',
                 dev_checkout=True):
        U = _updater_cls()
        embody, pars = self._fakes(mode, status)

        class Harness(U):
            _embody = embody  # shadow the property with a plain attribute

            def _readSentinel(self):
                return None

            def isDevCheckout(self):
                return dev_checkout

        inst = Harness.__new__(Harness)
        inst._busy = False
        inst._pending = None
        return inst, pars

    def test_startupcheck_off_sets_disabled(self):
        """Off mode must write 'Disabled', replacing any stale status."""
        inst, pars = self._harness('off')
        inst.StartupCheck()
        self.assertEqual(pars.Updatestatus.val, 'Disabled')

    def test_startupcheck_off_restores_readonly(self):
        """The readOnly dance must leave the par locked again."""
        inst, pars = self._harness('off')
        inst.StartupCheck()
        self.assertTrue(pars.Updatestatus.readOnly)

    def test_startupcheck_enabled_never_writes_a_BARE_disabled(self):
        """A non-off mode must never stamp the bare word 'Disabled' --
        the readout rendered that as the user having switched updates
        off. The dev checkout no longer returns silently (silence left
        the release scrub's bare 'Disabled' on the panel all session);
        it states its reason, which the version row renders as 'dev
        checkout', never as off."""
        inst, pars = self._harness('notify', status='')
        inst.StartupCheck()
        got = pars.Updatestatus.val
        self.assertNotEqual(got, 'Disabled',
                            'the bare scrub word is the "updates off" lie')
        if got:
            self.assertIn('dev checkout', got,
                          'a refusal must carry its reason: %r' % got)


class _StubUpdater:
    """Stand-in host: the par-reconciliation methods touch only these.

    Lets the real methods run against a sandbox COMP without performing an
    actual swap (which replaces the live Embody COMP -- destructive tier).
    """

    def __init__(self, embody):
        self._embody = embody
        self.logs = []
        self.dialogs = []

    def _log(self, message, level='INFO'):
        self.logs.append((level, message))

    def _dialog(self, title, body, buttons):
        self.dialogs.append(body)
        return buttons[0]

    def _setPar(self, par, value):
        was = par.readOnly
        par.readOnly = False
        par.val = value
        par.readOnly = was

    # The real reconciliation helpers, so the tests exercise shipping code.
    def _parMatches(self, par, mode, value):
        return _updater_cls()._parMatches(par, mode, value)

    def _setParMode(self, par, mode, value):
        return _updater_cls()._setParMode(self, par, mode, value)


class TestUpdateParReconciliation(EmbodyTestCase):
    """What an update may and may not do to the component's parameters.

    The in-place reload preserves every live par value. That is REQUIRED for
    the user's settings and WRONG for pars the build owns, so the updater
    reconciles both directions afterwards. Contract:
      - build-owned built-ins are re-asserted from the new build
      - a user's custom par value is NEVER rewritten
      - pars the new build no longer declares are removed, and the user is told
    """

    def _comp(self, with_viz=True):
        # A containerCOMP, like the real Embody: nodeview/opviewer/w/h are
        # panel-COMP pars and do not exist on a baseCOMP.
        comp = self.sandbox.create(containerCOMP, 'upd_host')
        if with_viz:
            comp.create(containerCOMP, 'viz_status')
        return comp

    def _settings(self, comp, values):
        page = comp.appendCustomPage('Settings')
        for name, val in values.items():
            page.appendStr(name)
            getattr(comp.par, name).val = val
        return comp

    # --- build-owned built-ins ---

    _VIEWER = {'builtin_pars': {
        'nodeview': {'mode': 'CONSTANT', 'value': 'opviewer'},
        'opviewer': {'mode': 'CONSTANT', 'value': './viz_status'},
    }}

    def test_the_whole_viewer_pair_moves_not_just_opviewer(self):
        """The v6.0.245 miss, pinned.

        The status readout needs BOTH nodeview and opviewer. Setting opviewer
        alone leaves it inert -- TD greys the field out while Node View is
        'Default Viewer' -- so the update still presents as a no-op. That is
        exactly what shipped in v6.0.245 and had to be corrected.
        """
        comp = self._comp()
        comp.par.nodeview = 'default'          # what a preserving reload keeps
        comp.par.opviewer = './viz_status'     # already right, and still inert
        stub = _StubUpdater(comp)
        _updater_cls()._applyBuildOwnedPars(stub, self._VIEWER)
        self.assertEqual('opviewer', str(comp.par.nodeview.val),
                         'opviewer does nothing while Node View is Default')
        self.assertEqual('./viz_status', comp.par.opviewer.val)

    def test_a_par_is_left_alone_when_its_target_is_absent(self):
        """Never point a viewer at something this build does not ship."""
        comp = self._comp(with_viz=False)
        comp.par.opviewer = './out1'
        stub = _StubUpdater(comp)
        _updater_cls()._applyBuildOwnedPars(stub, self._VIEWER)
        self.assertEqual('./out1', comp.par.opviewer.val)

    def test_already_correct_pars_are_not_rewritten(self):
        comp = self._comp()
        comp.par.nodeview = 'opviewer'
        comp.par.opviewer = './viz_status'
        stub = _StubUpdater(comp)
        _updater_cls()._applyBuildOwnedPars(stub, self._VIEWER)
        self.assertEqual([], [m for lvl, m in stub.logs if 'asserted' in m],
                         'an already-correct par must not be re-set')

    def test_a_bound_par_keeps_its_binding(self):
        """w/h ship in BIND mode. Assigning .val would drop them to CONSTANT
        and silently break the panel sizing (rules/parameters.md)."""
        comp = self._comp()
        comp.par.w.bindExpr = 'me.par.h'
        comp.par.w.mode = type(comp.par.w.mode).BIND
        stub = _StubUpdater(comp)
        _updater_cls()._applyBuildOwnedPars(stub, {'builtin_pars': {
            'w': {'mode': 'BIND', 'value': 'me.par.h'}}})
        self.assertEqual('BIND', comp.par.w.mode.name)
        self.assertEqual('me.par.h', comp.par.w.bindExpr)

    def test_extension_wiring_is_carried_across_an_update(self):
        """The same hole would silently drop an extension a new build adds --
        ext*object/name/promote are built-in pars too."""
        comp = self._comp()
        stub = _StubUpdater(comp)
        _updater_cls()._applyBuildOwnedPars(stub, {'builtin_pars': {
            'ext0name': {'mode': 'CONSTANT', 'value': 'Embody'},
            'ext0promote': {'mode': 'CONSTANT', 'value': '1'},
        }})
        self.assertEqual('Embody', str(comp.par.ext0name.val))
        self.assertTrue(bool(comp.par.ext0promote.eval()))

    def test_a_manifest_without_builtin_pars_asserts_nothing(self):
        comp = self._comp()
        comp.par.nodeview = 'default'
        stub = _StubUpdater(comp)
        _updater_cls()._applyBuildOwnedPars(stub, {'version': '6.0.245'})
        self.assertEqual('default', str(comp.par.nodeview.val))

    def test_the_manifest_declares_the_viewer_pair_and_not_user_placement(self):
        """The reconciliation is only as good as what the exporter records."""
        import json
        from pathlib import Path
        mf = Path(project.folder).parent / 'release' / 'embody-release.json'
        if not mf.is_file():
            self.skipTest('no release manifest in this checkout')
        declared = json.loads(mf.read_text(encoding='utf-8')).get(
            'builtin_pars') or {}
        self.assertIn('nodeview', declared,
                      'without nodeview the status viz stays invisible')
        self.assertIn('opviewer', declared)
        for owned_by_user in ('nodeX', 'nodeY', 'color', 'externaltox'):
            self.assertNotIn(
                owned_by_user, declared,
                '%s belongs to the user or the swap -- an update must never '
                'replay it' % owned_by_user)

    # --- user settings ---

    def test_an_update_never_rewrites_a_custom_par_value(self):
        comp = self._settings(self._comp(), {'Mysetting': 'user-chosen',
                                             'Another': 'also-mine'})
        stub = _StubUpdater(comp)
        manifest = {'custom_pars': ['Mysetting', 'Another']}
        _updater_cls()._pruneRetiredPars(stub, manifest)
        _updater_cls()._applyBuildOwnedPars(stub, self._VIEWER)
        self.assertEqual('user-chosen', comp.par.Mysetting.val)
        self.assertEqual('also-mine', comp.par.Another.val)

    def test_a_par_the_new_build_still_declares_survives(self):
        comp = self._settings(self._comp(), {'Keepme': 'v'})
        stub = _StubUpdater(comp)
        _updater_cls()._pruneRetiredPars(stub, {'custom_pars': ['Keepme']})
        self.assertIsNotNone(getattr(comp.par, 'Keepme', None))
        self.assertEqual('v', comp.par.Keepme.val)

    # --- retired settings ---

    def test_a_retired_par_is_removed_and_the_user_is_told_which(self):
        comp = self._settings(self._comp(), {'Keepme': 'v', 'Goneme': 'x'})
        stub = _StubUpdater(comp)
        _updater_cls()._pruneRetiredPars(stub, {'custom_pars': ['Keepme']})
        self.assertIsNone(getattr(comp.par, 'Goneme', None),
                          'a par this build no longer declares must go')
        self.assertIsNotNone(getattr(comp.par, 'Keepme', None))
        self.assertTrue(any('Goneme' in body for body in stub.dialogs),
                        'the user must be told WHICH settings were removed')

    def test_nothing_is_removed_when_nothing_is_retired(self):
        comp = self._settings(self._comp(), {'Keepme': 'v'})
        stub = _StubUpdater(comp)
        _updater_cls()._pruneRetiredPars(stub, {'custom_pars': ['Keepme']})
        self.assertEqual([], stub.dialogs,
                         'no removals means no dialog')

    def test_a_manifest_without_custom_pars_prunes_nothing(self):
        """Older releases carry no declaration -- with no source of truth the
        updater must not guess, or it would delete every live setting."""
        comp = self._settings(self._comp(), {'Keepme': 'v', 'Alsokeep': 'w'})
        stub = _StubUpdater(comp)
        _updater_cls()._pruneRetiredPars(stub, {'version': '6.0.244'})
        self.assertIsNotNone(getattr(comp.par, 'Keepme', None))
        self.assertIsNotNone(getattr(comp.par, 'Alsokeep', None))
        self.assertEqual([], stub.dialogs)

    def test_the_release_manifest_declares_its_custom_pars(self):
        """The pruning above is only safe because the exporter writes this."""
        import json
        from pathlib import Path
        mf = Path(project.folder).parent / 'release' / 'embody-release.json'
        if not mf.is_file():
            self.skipTest('no release manifest in this checkout')
        data = json.loads(mf.read_text(encoding='utf-8'))
        self.assertIn('custom_pars', data,
                      'writeReleaseManifest must declare custom_pars, or the '
                      'updater silently stops pruning retired settings')
        self.assertIn('Version', data['custom_pars'])
