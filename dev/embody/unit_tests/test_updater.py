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

            def _staleInstance(self):
                # StartupCheck stands down for a superseded instance, like
                # every other deferred entry point. This harness has no
                # ownerComp, so the real check would raise and read as stale.
                return False

        inst = Harness.__new__(Harness)
        inst._busy = False
        inst._pending = None
        inst._rearmed = False
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

    def _isSequenceBlockPar(self, par):
        return _updater_cls()._isSequenceBlockPar(par)

    # Sentinel-ownership helpers, real, so the in-flight gate is exercised
    # rather than mocked. _embody is the sandbox comp, which carries a real
    # externaltox par.
    def _sameSession(self, sentinel):
        return _updater_cls()._sameSession(self, sentinel)

    def _swapStillPointed(self, sentinel):
        return _updater_cls()._swapStillPointed(self, sentinel)

    def _updateInFlight(self, sentinel):
        return _updater_cls()._updateInFlight(self, sentinel)

    @staticmethod
    def _sessionMark():
        return _updater_cls()._sessionMark()


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
        """The user is still told WHICH settings went -- but through the
        names the prune RETURNS, which VerifyUpdate folds into the update's
        own dialog after the sentinel is cleared. The prune used to raise
        that modal itself, from inside the critical section, and a modal
        does not stop TouchDesigner's run() callbacks: the startup sweep
        fired into the parked state and offered to roll back a healthy
        update (field report, v6.0.246)."""
        comp = self._settings(self._comp(), {'Keepme': 'v', 'Goneme': 'x'})
        stub = _StubUpdater(comp)
        retired = _updater_cls()._pruneRetiredPars(
            stub, {'custom_pars': ['Keepme']})
        self.assertIsNone(getattr(comp.par, 'Goneme', None),
                          'a par this build no longer declares must go')
        self.assertIsNotNone(getattr(comp.par, 'Keepme', None))
        self.assertIn('Goneme', retired,
                      'the removed names must reach the caller that reports')
        self.assertTrue(any('Goneme' in message
                            for lvl, message in stub.logs if lvl == 'WARNING'),
                        'and the log must name them too')

    def test_nothing_is_removed_when_nothing_is_retired(self):
        comp = self._settings(self._comp(), {'Keepme': 'v'})
        stub = _StubUpdater(comp)
        retired = _updater_cls()._pruneRetiredPars(
            stub, {'custom_pars': ['Keepme']})
        self.assertEqual([], retired, 'nothing retired, nothing reported')
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


class TestSequenceParsAreNeverRetired(EmbodyTestCase):
    """A sequence's BLOCK COUNT is live machine state, not a declaration.

    THE FIELD FAILURE (v6.0.246, macOS): the Convoy Nodes sequence is sized
    to whatever Convoy mesh a machine has seen, so the release manifest --
    a raw snapshot of the developer's own COMP -- declared blocks 0..5 and
    nothing else. A user with a seventh node was shown '4 settings no longer
    exist in this version and were removed' naming four cells of a read-only
    status readout, and lost the row. v6.0.245 shipped the same bug with the
    threshold at four nodes.

    Two independent guards, tested separately below, because either alone
    leaves a hole: the PRODUCER stops declaring machine state, and the
    CONSUMER refuses to destroy sequence blocks whatever the manifest says
    (the only protection for the manifests already published).
    """

    def _comp_with_sequence(self, blocks=3):
        comp = self.sandbox.create(containerCOMP, 'upd_seq')
        page = comp.appendCustomPage('Nodes')
        page.appendSequence('Rows')
        page.appendStr('Rowlabel')
        comp.seq.Rows.blockSize = 1
        comp.seq.Rows.numBlocks = blocks
        settings = comp.appendCustomPage('Settings')
        settings.appendStr('Keepme')
        settings.appendStr('Goneme')
        return comp

    # --- the predicate ---

    def test_the_predicate_separates_header_block_and_plain_pars(self):
        comp = self._comp_with_sequence()
        is_block = _updater_cls()._isSequenceBlockPar
        header = getattr(comp.par, 'Rows', None)
        self.assertIsNotNone(header, 'precondition: the sequence header')
        self.assertFalse(is_block(header),
                         'the header is authored and static -- prunable')
        block = getattr(comp.par, 'Rows0rowlabel', None)
        self.assertIsNotNone(block, 'precondition: a block par exists')
        self.assertTrue(is_block(block), 'a block member is runtime state')
        self.assertFalse(is_block(comp.par.Keepme))

    # --- the consumer guard ---

    def test_undeclared_blocks_survive_a_prune(self):
        """The exact field case: the manifest declares the sequence but
        fewer blocks than this machine has."""
        comp = self._comp_with_sequence(blocks=3)
        stub = _StubUpdater(comp)
        retired = _updater_cls()._pruneRetiredPars(
            stub, {'custom_pars': ['Keepme', 'Goneme', 'Rows']})
        self.assertEqual(3, comp.seq.Rows.numBlocks,
                         'a live sequence must survive intact')
        self.assertIsNotNone(getattr(comp.par, 'Rows0rowlabel', None))
        self.assertEqual([], retired,
                         'runtime blocks are not retired settings')
        self.assertEqual([], stub.dialogs,
                         'and the user is never told they lost a setting')

    def test_a_surplus_of_several_blocks_is_still_untouched(self):
        """Per docs.derivative.ca/Par_Class, destroying a sequential par
        destroys its whole BLOCK and renumbers the survivors -- so a
        by-name loop over several surplus blocks retargets live ones and
        removes more than it reports."""
        comp = self._comp_with_sequence(blocks=6)
        stub = _StubUpdater(comp)
        _updater_cls()._pruneRetiredPars(stub, {'custom_pars': ['Rows']})
        self.assertEqual(6, comp.seq.Rows.numBlocks)

    def test_a_genuinely_retired_plain_par_still_goes(self):
        """The guard must not disable pruning -- only exempt sequences."""
        comp = self._comp_with_sequence()
        stub = _StubUpdater(comp)
        retired = _updater_cls()._pruneRetiredPars(
            stub, {'custom_pars': ['Keepme', 'Rows']})
        self.assertEqual(['Goneme'], retired)
        self.assertIsNone(getattr(comp.par, 'Goneme', None))
        self.assertIsNotNone(getattr(comp.par, 'Keepme', None))
        self.assertEqual(3, comp.seq.Rows.numBlocks)

    def test_the_prune_reports_names_and_opens_no_dialog(self):
        """Reporting moved to VerifyUpdate. A modal raised from inside the
        prune parked the update with its sentinel still on disk, which is
        what let the startup sweep offer to roll back a healthy install."""
        comp = self._comp_with_sequence()
        stub = _StubUpdater(comp)
        retired = _updater_cls()._pruneRetiredPars(
            stub, {'custom_pars': ['Keepme', 'Rows']})
        self.assertEqual(['Goneme'], retired)
        self.assertEqual([], stub.dialogs,
                         'the prune must not dialog from inside the '
                         'critical section')

    def test_a_page_that_was_already_empty_is_left_alone(self):
        """The empty-page sweep used to run over EVERY custom page whenever
        anything at all was retired."""
        comp = self._comp_with_sequence()
        comp.appendCustomPage('Untouched')
        stub = _StubUpdater(comp)
        _updater_cls()._pruneRetiredPars(
            stub, {'custom_pars': ['Keepme', 'Rows']})
        self.assertIn('Untouched', [p.name for p in comp.customPages],
                      'a page this prune did not empty is not ours to '
                      'destroy')

    # --- the producer gate ---

    def test_the_release_manifest_declares_no_sequence_block(self):
        """The test that would have caught v6.0.245 and v6.0.246 at commit
        time. Phrased against every sequence the Embody COMP carries, not
        as a Convoynodes blacklist, so a sequence added later inherits it.
        """
        import json
        import re
        from pathlib import Path
        mf = Path(project.folder).parent / 'release' / 'embody-release.json'
        if not mf.is_file():
            self.skipTest('no release manifest in this checkout')
        declared = json.loads(mf.read_text(encoding='utf-8')).get(
            'custom_pars') or []
        headers = [seq.name for seq in op.Embody.seq if seq is not None]
        self.assertTrue(headers, 'precondition: Embody carries a sequence')
        pattern = re.compile(
            r'^(%s)\d' % '|'.join(re.escape(h) for h in headers))
        leaked = sorted(n for n in declared if pattern.match(n))
        self.assertEqual(
            [], leaked,
            'the manifest is shipping this machine\'s live sequence blocks '
            'as a parameter contract; every user whose sequence is longer '
            'loses the surplus on their next update: %r' % (leaked,))

    def test_the_exporter_declares_no_sequence_block_from_the_live_comp(self):
        """The producer itself, run against the real Embody COMP -- not the
        artifact it wrote last time. This is what proves the fix before the
        next release export, and it is the half a manifest assertion cannot
        see: the manifest on disk is only as new as the last project.save().
        """
        # The source-control execute DAT is Embody's SIBLING (it watches the
        # project, not the component), so it is reached through the parent.
        dat = op.Embody.op('../execute_src_ctrl')
        if dat is None:
            self.skipTest('execute_src_ctrl not resolvable from Embody')
        declared = dat.module._declaredCustomPars(op.Embody)
        leaked = sorted(n for n in declared
                        if n.lower().startswith('convoynodes')
                        and n.lower() != 'convoynodes')
        self.assertEqual([], leaked,
                         'the exporter is still declaring live sequence '
                         'blocks: %r' % (leaked,))
        self.assertIn('Convoynodes', declared, 'the header is a declaration')
        self.assertIn('Version', declared)
        self.assertEqual(sorted(set(declared)), declared,
                         'the manifest list must be sorted and duplicate-free')

    def test_the_exporter_and_the_pruner_use_one_predicate(self):
        """Two copies of 'is this a sequence block?' is how the producer and
        the consumer drift apart."""
        from pathlib import Path
        src = (Path(project.folder) / 'embody'
               / 'execute_src_ctrl.py').read_text(encoding='utf-8')
        body = src.split('def _declaredCustomPars', 1)[1]
        body = body.split('\ndef ', 1)[0]
        self.assertIn('_isSequenceBlockPar', body,
                      'the exporter must borrow UpdaterExt\'s predicate, '
                      'not restate it')

    def test_the_manifest_still_declares_the_sequence_header(self):
        """A filter that strips too much silently stops pruning."""
        import json
        from pathlib import Path
        mf = Path(project.folder).parent / 'release' / 'embody-release.json'
        if not mf.is_file():
            self.skipTest('no release manifest in this checkout')
        declared = json.loads(mf.read_text(encoding='utf-8')).get(
            'custom_pars') or []
        for name in ('Convoynodes', 'Version', 'Autoupdate'):
            self.assertIn(name, declared)


class _StartupStub(_StubUpdater):
    """Enough host for StartupCheck to run for real.

    Everything the gate DECIDES on is the real method; only the effects
    (rollback, re-arm, the auto check) are recorded instead of performed.
    """

    def __init__(self, embody, sentinel=None, choice=1):
        super().__init__(embody)
        self.sentinel = sentinel
        self.choice = choice
        self.rollbacks = []
        self.rearmed = []
        self.cleared = 0
        self.checked = []
        self.status = []
        self._rearmed = False

    def _staleInstance(self):
        return False

    def _readSentinel(self):
        return self.sentinel

    def _clearSentinel(self):
        self.cleared += 1
        self.sentinel = None

    def _validBackup(self, sentinel):
        return sentinel.get('backup_path'), None

    def _dialog(self, title, body, buttons):
        self.dialogs.append(body)
        return self.choice

    def _rollback(self, sentinel, why):
        self.rollbacks.append(why)

    def _resumeVerifyIfOrphaned(self, sentinel):
        # The REAL decision (same-session skip, one-shot latch); the stub
        # only observes whether it fired.
        before = self._rearmed
        _updater_cls()._resumeVerifyIfOrphaned(self, sentinel)
        if self._rearmed and not before:
            self.rearmed.append(sentinel)

    def _status(self, text):
        self.status.append(text)

    def isDevCheckout(self):
        return False

    def CheckForUpdate(self, interactive=True, auto_install=False):
        self.checked.append((interactive, auto_install))


class TestAnInFlightUpdateIsNotAnInterruptedOne(EmbodyTestCase):
    """The false 'An update to v6.0.246 did not complete' prompt.

    An in-place tox reload does NOT restart TouchDesigner. The rebuilt
    component runs Embody's ordinary boot chain in the SAME process, and
    that chain schedules this crash sweep on the same frame budget the
    update's own verifier uses -- so the sweep meets the live sentinel of
    the update it IS. The reporter was asked to roll back an install that
    succeeded four seconds later, between the prune dialog and the success
    dialog: three modals for one update.

    'Keep Current State' was the more damaging answer: it deletes the
    sentinel out from under VerifyUpdate, which then stamps nothing --
    par.Version keeps naming the old release and externaltox is left
    pointing into .embody/updates.
    """

    def _comp(self):
        return self.sandbox.create(containerCOMP, 'upd_flight')

    def _sentinel(self, comp, session=True, tox='/p/.embody/updates/n.tox'):
        s = {'tag': 'v6.0.246', 'tox_path': tox, 'from_version': '6.0.241',
             'backup_path': '/p/.embody/updates/backup-v6.0.241.tox'}
        if session:
            s['session'] = _updater_cls()._sessionMark()
        return s

    # --- the two witnesses ---

    def test_this_process_recognises_its_own_update(self):
        stub = _StubUpdater(self._comp())
        self.assertTrue(stub._sameSession(self._sentinel(None)))

    def test_another_process_is_not_this_one(self):
        import os
        stub = _StubUpdater(self._comp())
        alien = self._sentinel(None)
        alien['session'] = dict(alien['session'], pid=os.getpid() + 99991)
        self.assertFalse(stub._sameSession(alien),
                         'a crash in another process must still prompt')

    def test_a_restart_is_caught_by_the_start_time_even_on_a_reused_pid(self):
        stub = _StubUpdater(self._comp())
        alien = self._sentinel(None)
        alien['session'] = dict(alien['session'],
                                started=alien['session']['started'] - 3600)
        self.assertFalse(stub._sameSession(alien))

    def test_a_live_swap_is_recognised_without_any_session_stamp(self):
        """The witness that makes the FIRST update to ship this fix behave
        -- its sentinel was written by the older build, which stamped no
        owner at all."""
        comp = self._comp()
        stub = _StubUpdater(comp)
        sentinel = self._sentinel(comp, session=False,
                                  tox='/p/.embody/updates/n.tox')
        self.assertFalse(stub._sameSession(sentinel))
        self.assertFalse(stub._updateInFlight(sentinel),
                         'nothing is pointed at it yet')
        comp.par.externaltox = '/p/.embody/updates/n.tox'
        self.assertTrue(stub._swapStillPointed(sentinel))
        self.assertTrue(stub._updateInFlight(sentinel))

    def test_a_finished_swap_is_not_in_flight(self):
        """VerifyUpdate clears externaltox, so a sentinel that outlives it
        is a genuine crash artifact and must prompt."""
        comp = self._comp()
        stub = _StubUpdater(comp)
        comp.par.externaltox = ''
        sentinel = self._sentinel(comp, session=False)
        self.assertFalse(stub._updateInFlight(sentinel))

    # --- the gate ---

    def test_an_in_flight_update_is_never_offered_for_rollback(self):
        comp = self._comp()
        stub = _StartupStub(comp, self._sentinel(comp), choice=0)
        _updater_cls().StartupCheck(stub)
        self.assertEqual([], stub.dialogs,
                         'the update that is still running must not be '
                         'offered for recovery')
        self.assertEqual([], stub.rollbacks)
        self.assertEqual(0, stub.cleared,
                         'and its sentinel must survive for VerifyUpdate')

    def test_its_own_session_does_not_re_arm_a_second_verifier(self):
        comp = self._comp()
        stub = _StartupStub(comp, self._sentinel(comp))
        _updater_cls().StartupCheck(stub)
        self.assertEqual([], stub.rearmed,
                         '_applyPhase2 already armed one; a second would '
                         'race its own verifier')

    def test_a_live_swap_from_a_dead_session_is_finished_not_prompted(self):
        """Suppressing the prompt without this would trade a wrong dialog
        for a silent half-applied install."""
        comp = self._comp()
        comp.par.externaltox = '/p/.embody/updates/n.tox'
        stub = _StartupStub(comp, self._sentinel(comp, session=False))
        _updater_cls().StartupCheck(stub)
        self.assertEqual([], stub.dialogs)
        self.assertEqual(1, len(stub.rearmed),
                         'verification must be re-armed for a swap nothing '
                         'is left to finish')
        _updater_cls().StartupCheck(stub)
        self.assertEqual(1, len(stub.rearmed),
                         'once only -- a failing verifier must not loop')

    def test_a_genuinely_interrupted_update_still_prompts(self):
        """The crash sweep is the point of the sentinel; the gate must not
        disarm it."""
        comp = self._comp()
        comp.par.externaltox = ''
        stub = _StartupStub(comp, self._sentinel(comp, session=False),
                            choice=0)
        _updater_cls().StartupCheck(stub)
        self.assertEqual(1, len(stub.dialogs))
        self.assertIn('did not complete', stub.dialogs[0])
        self.assertEqual(1, len(stub.rollbacks))

    def test_a_dismissed_recovery_prompt_keeps_the_sentinel(self):
        comp = self._comp()
        comp.par.externaltox = ''
        stub = _StartupStub(comp, self._sentinel(comp, session=False),
                            choice=-1)
        _updater_cls().StartupCheck(stub)
        self.assertEqual(0, stub.cleared,
                         'a suppressed dialog must re-offer next open')


class TestVerifyUpdateClosesBeforeItSpeaks(EmbodyTestCase):
    """No modal may open while pending.json still exists.

    ui.messageBox is modal to its CALLER, not to TouchDesigner: run()
    callbacks keep firing while it is up (the field log shows Convoy's
    scheduled work landing between the prune dialog and the success one,
    two and a half minutes apart). So any dialog raised mid-verify parks
    the update in a half-applied state that a concurrent caller can act on.
    """

    def _verify_source(self):
        from pathlib import Path
        src = (Path(project.folder) / 'embody' / 'Embody' / 'updater'
               / 'UpdaterExt.py').read_text(encoding='utf-8')
        body = src.split('def VerifyUpdate', 1)[1]
        return body.split('\n    def ', 1)[0]

    def test_the_sentinel_is_cleared_before_any_dialog(self):
        body = self._verify_source()
        cleared = body.find('_clearSentinel()')
        dialog = body.find('self._dialog(')
        self.assertGreater(cleared, 0, 'VerifyUpdate must clear the sentinel')
        self.assertGreater(dialog, 0, 'and still report to the user')
        self.assertLess(cleared, dialog,
                        'a dialog opened before _clearSentinel leaves the '
                        'sentinel live for the startup sweep to find -- the '
                        'exact v6.0.246 field failure')

    def test_externaltox_is_detached_before_any_dialog(self):
        body = self._verify_source()
        self.assertLess(body.find('_clearExternalTox()'),
                        body.find('self._dialog('),
                        'a parked dialog must never leave the component '
                        'pointed at a file in .embody/updates')

    def test_no_callee_in_the_critical_section_may_raise_a_dialog(self):
        """The guard that actually catches the shipped bug.

        Scanning VerifyUpdate's own body is not enough: the v6.0.246 modal
        was raised by _pruneRetiredPars, a CALLEE, which a body scan cannot
        see -- so the ordering assertions above pass even on the unfixed
        code (panel finding, 2026-08-16). The invariant is about the whole
        critical section, so it is tested that way: nothing called between
        the sentinel being read and being cleared may block on a user.
        """
        from pathlib import Path
        src = (Path(project.folder) / 'embody' / 'Embody' / 'updater'
               / 'UpdaterExt.py').read_text(encoding='utf-8')
        for callee in ('_stampAboutPars', '_applyBuildOwnedPars',
                       '_pruneRetiredPars', '_clearExternalTox',
                       '_cleanupFiles'):
            body = src.split('def %s' % callee, 1)[1]
            body = body.split('\n    def ', 1)[0]
            for blocker in ('self._dialog(', 'ui.messageBox'):
                self.assertNotIn(
                    blocker, body,
                    '%s runs while the sentinel is still on disk; a modal '
                    'there parks the update half-applied and the startup '
                    'sweep offers to roll back a healthy install' % callee)

    def test_removals_ride_the_update_dialog_rather_than_their_own(self):
        body = self._verify_source()
        self.assertEqual(1, body.count('self._dialog('),
                         'one update, one dialog')
        self.assertIn('no longer exist in this', body,
                      'removals are still reported, inside that one dialog')
        self.assertLess(body.find('retired = self._pruneRetiredPars'),
                        body.find('_clearSentinel()'),
                        'the prune still runs INSIDE the critical section; '
                        'only its reporting moved out')


class TestManifestKeysThatDriveDestruction(EmbodyTestCase):
    """custom_pars and builtin_pars are unhashed, mutable release data that
    drive destructive reconciliation, so their TYPES are a gate."""

    def test_a_string_custom_pars_is_refused(self):
        """set('Version') is {'V','e','r',...} -- every real par would read
        as undeclared and the prune would destroy the entire component."""
        m = _valid_manifest()
        m['custom_pars'] = 'Version'
        self.assertIsNotNone(_updater_cls().validateManifest(m))

    def test_non_string_entries_are_refused(self):
        m = _valid_manifest()
        m['custom_pars'] = ['Version', 7]
        self.assertIsNotNone(_updater_cls().validateManifest(m))

    def test_a_non_object_builtin_pars_is_refused(self):
        m = _valid_manifest()
        m['builtin_pars'] = []
        self.assertIsNotNone(_updater_cls().validateManifest(m))

    def test_the_well_formed_pair_passes(self):
        m = _valid_manifest()
        m['custom_pars'] = ['Version', 'Autoupdate']
        m['builtin_pars'] = {'nodeview': {'mode': 'CONSTANT',
                                          'value': 'opviewer'}}
        self.assertIsNone(_updater_cls().validateManifest(m))

    def test_both_keys_stay_optional(self):
        """Pre-6.0.245 manifests carry neither and must still install."""
        self.assertIsNone(_updater_cls().validateManifest(_valid_manifest()))


# ======================================================================
# Stall control: a stuck download must never own the updater forever
# ======================================================================

def _stall_harness(now=1000.0):
    """Instance wired to a FAKE clock, no network, no TD, no real run().

    Every deferred callback and every network phase is recorded instead of
    executed, so the retry ladder and the deadlines are exercised
    deterministically (CI runners stall; real-clock deadlines flake).
    """
    U = _updater_cls()

    class Harness(U):
        def __init__(self):
            self.now = now
            self.scheduled = []
            self.logs = []
            self.status = []
            self.dialogs = []
            self.answers = []
            self.began = []
            self.rollbacks = []
            self.sentinel = None
            self.inflight = False
            self.backup = (None, 'backup file is missing')
            self._check_result = None
            self._download_result = None
            self._check_gen = 0
            self._download_gen = 0
            self._busy = False
            self._busy_phase = ''
            self._busy_deadline = 0.0
            self._net_attempt = 0
            self._retry_spec = None
            self._pending = None
            self._rearmed = False

        # -- injected seams ------------------------------------------
        def _clock(self):
            return self.now

        def _schedule(self, script, *args, **kwargs):
            self.scheduled.append((script, args, kwargs))

        def _log(self, msg, level='INFO'):
            self.logs.append((level, msg))

        def _status(self, text):
            self.status.append(str(text))

        def _dialog(self, title, message, buttons):
            self.dialogs.append(message)
            return self.answers.pop(0) if self.answers else -1

        def _staleInstance(self):
            return False

        def _readSentinel(self):
            return self.sentinel

        def _clearSentinel(self):
            self.sentinel = None

        def _updateInFlight(self, sentinel):
            return self.inflight

        def _validBackup(self, sentinel):
            return self.backup

        def _rollback(self, sentinel, why):
            self.rollbacks.append(why)

        # -- network phases: recorded, never run ---------------------
        def _beginCheck(self, interactive, auto_install):
            self.began.append(('check', interactive, auto_install))
            return {'status': 'checking'}

        def _beginDownload(self, interactive, apply_after):
            self.began.append(('download', interactive, apply_after))
            return {'status': 'downloading'}

    return Harness()


class TestUpdaterBusyLatch(EmbodyTestCase):
    """The latch used to be cleared ONLY by the poll chain that set it, so a
    chain that died (a raise, a swapped instance, a stopped frame clock)
    answered 'an update is already running' for the rest of the session --
    the field report this tier exists for."""

    def test_a_live_phase_still_refuses(self):
        h = _stall_harness()
        h._setBusy('download')
        h.now += 5
        blocked = h._busyBlocks(interactive=True)
        self.assertIsNotNone(blocked)
        self.assertIn('error', blocked)
        self.assertTrue(h._busy, 'a running phase must NOT be cleared')

    def test_the_refusal_says_it_recovers_by_itself(self):
        h = _stall_harness()
        h._setBusy('download')
        h._busyBlocks(interactive=True)
        self.assertTrue(h.dialogs)
        self.assertIn('retries by itself', h.dialogs[-1])

    def test_a_stalled_latch_is_cleared_by_the_next_attempt(self):
        h = _stall_harness()
        h._setBusy('download')
        h.now += h.BUSY_CEILING_S['download'] + 1
        self.assertIsNone(h._busyBlocks(interactive=True),
                          'past its ceiling the phase cannot be running')
        self.assertFalse(h._busy)
        self.assertEqual([], h.dialogs, 'a dead latch is not a user error')

    def test_clearing_a_stalled_latch_retires_its_generations(self):
        """An orphaned worker or poll must not land on the fresh attempt."""
        h = _stall_harness()
        h._setBusy('check')
        h._check_result = {'_gen': 0}
        gen_before = h._check_gen
        h.now += h.BUSY_CEILING_S['check'] + 1
        h._busyBlocks(interactive=False)
        self.assertGreater(h._check_gen, gen_before)
        self.assertIsNone(h._check_result)

    def test_an_unstamped_latch_reads_as_stalled(self):
        """A latch left by an older build carries no deadline; it must not
        read as an eternally running phase."""
        h = _stall_harness()
        h._busy = True
        h._busy_deadline = 0.0
        self.assertIsNone(h._busyBlocks(interactive=False))
        self.assertFalse(h._busy)

    def test_ceilings_outlast_the_phases_they_guard(self):
        """The backstop must never fire on a healthy phase."""
        h = _stall_harness()
        self.assertGreater(h.BUSY_CEILING_S['check'], h.CHECK_DEADLINE_S)
        self.assertGreater(h.BUSY_CEILING_S['download'],
                           h.DOWNLOAD_DEADLINE_S)


class TestUpdaterRetryLadder(EmbodyTestCase):
    """A failed network phase retries itself, then gives up loudly and
    leaves NOTHING behind that could refuse the next attempt."""

    CHECK = {'phase': 'check', 'interactive': True, 'auto_install': False}
    DL = {'phase': 'download', 'interactive': True, 'apply_after': True}

    def test_first_failure_retries_the_same_phase(self):
        h = _stall_harness()
        h._setBusy('check')
        out = h._networkFailed('update check', 'boom', True, self.CHECK)
        self.assertEqual('retrying', out.get('status'))
        self.assertFalse(h._busy, 'the latch is released between tries')
        self.assertEqual([], h.dialogs, 'a retry is not an alert')
        self.assertTrue(h.scheduled, 'the retry must be armed')
        h._retryNetworkPhase()
        self.assertEqual([('check', True, False)], h.began)

    def test_retry_status_is_visible_and_reads_as_running(self):
        h = _stall_harness()
        h._networkFailed('download', 'boom', True, self.DL)
        self.assertTrue(h.status[-1].lower().startswith('retrying'),
                        'startup_progress classifies this prefix as RUNNING')
        self.assertIn('2/3', h.status[-1])

    def test_third_failure_alerts_and_cancels(self):
        h = _stall_harness()
        h._pending = {'tag': 'v9.9.9'}
        for _ in range(2):
            h._setBusy('download')
            h._networkFailed('download', 'boom', True, self.DL)
            h._retryNetworkPhase()
        h._setBusy('download')
        out = h._networkFailed('download', 'boom', True, self.DL)
        self.assertIn('error', out)
        self.assertEqual(2, len(h.began), 'three tries total, not more')
        self.assertTrue(h.dialogs, 'the user is told, once, at the end')
        self.assertIn('internet connection', h.dialogs[-1])
        self.assertIn('check your internet', h.status[-1].lower())
        self.assertIn('failed', h.status[-1].lower(),
                      'the status readout grades this as FAILED')

    def test_giving_up_blocks_nothing_afterwards(self):
        """The whole point: the user can try again immediately."""
        h = _stall_harness()
        h._pending = {'tag': 'v9.9.9'}
        for _ in range(3):
            h._setBusy('download')
            h._networkFailed('download', 'boom', True, self.DL)
        self.assertFalse(h._busy)
        self.assertIsNone(h._pending)
        self.assertIsNone(h._retry_spec)
        self.assertEqual(0, h._net_attempt)
        self.assertIsNone(h._busyBlocks(interactive=True))
        h._retryNetworkPhase()
        self.assertEqual([], h.began, 'no fourth try was armed')

    def test_an_armed_retry_stands_down_for_a_newer_attempt(self):
        """A user who clicks again owns the phase; two chains would race."""
        h = _stall_harness()
        h._setBusy('check')
        h._networkFailed('update check', 'boom', True, self.CHECK)
        h._setBusy('check')  # a fresh attempt started meanwhile
        h._retryNetworkPhase()
        self.assertEqual([], h.began)
        self.assertIsNone(h._retry_spec)

    def test_the_unattended_path_gives_up_quietly(self):
        """Nobody wants a modal every launch because GitHub was down."""
        h = _stall_harness()
        spec = dict(self.CHECK, interactive=False)
        for _ in range(3):
            h._networkFailed('update check', 'boom', False, spec)
        self.assertEqual([], h.dialogs)
        self.assertIn('check your internet', h.status[-1].lower())
        self.assertTrue(any(level == 'ERROR' for level, _ in h.logs))


class TestUpdaterPollDeadlines(EmbodyTestCase):
    """Poll chains are bounded by wall clock and stand down when superseded."""

    def test_poll_rearms_while_inside_its_deadline(self):
        h = _stall_harness()
        h._check_gen = 1
        h._setBusy('check')
        h._pollCheck(True, False, 1, h.now + 10)
        self.assertEqual(1, len(h.scheduled))
        self.assertIn('delayMilliSeconds', h.scheduled[0][2],
                      'real time, not frames -- a stalled frame clock must '
                      'not extend a network deadline')

    def test_poll_past_its_deadline_enters_the_retry_ladder(self):
        h = _stall_harness()
        h._check_gen = 1
        h._setBusy('check')
        h._pollCheck(True, False, 1, h.now - 1)
        self.assertEqual(1, h._net_attempt)
        self.assertFalse(h._busy)
        h._retryNetworkPhase()
        self.assertEqual([('check', True, False)], h.began)

    def test_a_superseded_poll_stands_down(self):
        h = _stall_harness()
        h._check_gen = 5
        h._setBusy('check')
        h._pollCheck(True, False, 4, h.now - 1)
        self.assertEqual([], h.scheduled)
        self.assertEqual(0, h._net_attempt,
                         'an old chain must not fail the phase that '
                         'replaced it')
        self.assertTrue(h._busy)

    def test_download_poll_outlasts_the_workers_own_budget(self):
        """The worker names its stall; the poll must not time out first."""
        h = _stall_harness()
        h._download_gen = 1
        h._setBusy('download')
        h._pollDownload(True, True, 1, h.now + h.DOWNLOAD_DEADLINE_S)
        self.assertEqual(1, len(h.scheduled))

    def test_download_error_enters_the_retry_ladder(self):
        h = _stall_harness()
        h._download_gen = 1
        h._setBusy('download')
        h._download_result = {'_gen': 1, 'error': 'TimeoutError: stalled'}
        h._pollDownload(True, True, 1, h.now + 10)
        self.assertEqual(1, h._net_attempt)
        self.assertTrue(h._retry_spec)


class TestUpdaterLeftoverSentinel(EmbodyTestCase):
    """A sentinel refuses only while the swap is REALLY in flight. A
    leftover (a failed rollback writes one that nothing ever removes) used
    to refuse every later check with 'restart TouchDesigner' -- advice that
    cannot work, since the file outlives the process."""

    def _left(self, h):
        h.sentinel = {'tag': 'v9.9.9', 'phase': 'rollback_failed',
                      'backup_path': '/nope/backup.tox'}
        h.inflight = False

    def test_an_in_flight_swap_still_refuses(self):
        h = _stall_harness()
        self._left(h)
        h.inflight = True
        blocked = h._sentinelBlocks(interactive=True)
        self.assertIn('error', blocked)
        self.assertIsNotNone(h.sentinel, 'a live swap keeps its sentinel')

    def test_a_leftover_without_a_backup_is_discarded_and_work_continues(self):
        h = _stall_harness()
        self._left(h)
        self.assertIsNone(h._sentinelBlocks(interactive=True))
        self.assertIsNone(h.sentinel)

    def test_a_leftover_with_a_backup_offers_recovery_first(self):
        h = _stall_harness()
        self._left(h)
        h.backup = ('/updates/backup.tox', None)
        h.answers = [0]  # Restore Backup
        out = h._sentinelBlocks(interactive=True)
        self.assertEqual('rolled_back', out.get('status'))
        self.assertEqual(1, len(h.rollbacks))

    def test_discarding_the_offer_continues_instead_of_blocking(self):
        h = _stall_harness()
        self._left(h)
        h.backup = ('/updates/backup.tox', None)
        h.answers = [1]  # Discard and Continue
        self.assertIsNone(h._sentinelBlocks(interactive=True))
        self.assertIsNone(h.sentinel)

    def test_a_suppressed_dialog_does_not_re_arm_the_dead_end(self):
        """-1 (suppressed) on an explicit user action must still unblock."""
        h = _stall_harness()
        self._left(h)
        h.backup = ('/updates/backup.tox', None)
        h.answers = []  # -> -1
        self.assertIsNone(h._sentinelBlocks(interactive=True))
        self.assertIsNone(h.sentinel)

    def test_the_unattended_path_leaves_the_evidence_alone(self):
        """StartupCheck owns unattended recovery -- it can offer the backup."""
        h = _stall_harness()
        self._left(h)
        blocked = h._sentinelBlocks(interactive=False)
        self.assertIn('error', blocked)
        self.assertIsNotNone(h.sentinel)


class TestUpdaterCappedRead(EmbodyTestCase):
    """The transfer owns a deadline of its own. urlopen's timeout bounds one
    recv, so a trickling connection never times out and never finishes --
    the shape of the download that hung with no way back (field 2026-08-25).
    """

    class _Feed:
        """A response that hands out fixed chunks; `stall` advances the
        fake clock per read, so a slow line needs no real time."""

        def __init__(self, chunks, clock, stall=0.0):
            self.chunks = list(chunks)
            self.clock = clock
            self.stall = stall
            self.reads = 0

        def read(self, n):
            self.reads += 1
            self.clock[0] += self.stall
            if not self.chunks:
                return b''
            chunk = self.chunks.pop(0)
            if len(chunk) > n:  # a real reader honors the requested size
                self.chunks.insert(0, chunk[n:])
                chunk = chunk[:n]
            return chunk

    def _clock(self):
        now = [0.0]
        return now, (lambda: now[0])

    def test_a_normal_transfer_returns_every_byte(self):
        now, clock = self._clock()
        feed = self._Feed([b'ab', b'cd', b'ef'], now)
        got = _updater_cls()._readCapped(feed, 10, 300, clock=clock)
        self.assertEqual(b'abcdef', got)

    def test_a_stalled_transfer_raises_instead_of_hanging(self):
        now, clock = self._clock()
        feed = self._Feed([b'a'] * 1000, now, stall=40.0)
        with self.assertRaises(TimeoutError):
            _updater_cls()._readCapped(feed, 1000, 300, clock=clock)
        self.assertLess(feed.reads, 20, 'it must give up early, not read on')

    def test_the_cap_still_holds(self):
        """An overrun is detected, never buffered without bound."""
        now, clock = self._clock()
        feed = self._Feed([b'x' * 64] * 100, now)
        got = _updater_cls()._readCapped(feed, 100, 300, clock=clock)
        self.assertEqual(100, len(got))


class TestUpdateCheckIsQuiet(EmbodyTestCase):
    """A check that finds nothing must not open a modal.

    Its result already lands in the Update Status par, so a dialog states the
    same sentence twice and interrupts the user to report that nothing
    happened. The post-INSTALL confirmation is the opposite case -- it reports
    a change the user cannot otherwise see -- and must survive this.
    """

    def _checker(self, local='6.1.8'):
        U = _updater_cls()

        class FakePar:
            def __init__(self, val):
                self.val = val
                self.readOnly = True

            def eval(self):
                return self.val

        class FakePars:
            pass

        pars = FakePars()
        pars.Version = FakePar(local)

        class FakeEmbody:
            par = pars

        class Harness(U):
            _embody = FakeEmbody()   # shadow the property with an attribute

            def __init__(self):
                self.dialogs = []
                self.statuses = []
                self.logs = []

            def _status(self, text, *a, **k):
                self.statuses.append(text)

            def _log(self, message, level='INFO'):
                self.logs.append(message)

            def _dialog(self, title, body, buttons):
                self.dialogs.append(body)
                return buttons[0]

        return Harness()

    def test_an_up_to_date_check_raises_no_dialog(self):
        c = self._checker(local='6.1.8')
        c._finishCheck({'tag': 'v6.1.8'}, interactive=True, auto_install=False)
        self.assertEqual(
            [], c.dialogs,
            'an interactive check that finds nothing must not open a modal -- '
            'the Update Status par already carries this exact result')

    def test_an_older_remote_also_raises_no_dialog(self):
        """releases/latest is commit-date ordered, so a LOWER remote reaches
        the same branch -- it must be equally quiet."""
        c = self._checker(local='6.1.8')
        c._finishCheck({'tag': 'v6.1.7'}, interactive=True, auto_install=False)
        self.assertEqual([], c.dialogs)

    def test_removing_the_dialog_did_not_remove_the_report(self):
        """The par is now the ONLY report of a clean check; if this regresses,
        an interactive check becomes completely silent."""
        c = self._checker(local='6.1.8')
        c._finishCheck({'tag': 'v6.1.8'}, interactive=True, auto_install=False)
        self.assertTrue(
            any('Up to date' in s for s in c.statuses),
            'the status par must still report the check: %r' % (c.statuses,))
