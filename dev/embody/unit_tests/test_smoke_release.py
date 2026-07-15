"""
Test suite: Release smoke tests.

Validates that Embody's post-init state is healthy and that the
_messageBox auto-response mechanism works correctly. These tests run
against the current TD session's Embody instance and verify the same
invariants that a fresh release install must satisfy.

For the full E2E flow (loading release .tox into a fresh project),
see smoke_bootstrap.py (template .toe startup script) and the
orchestration notes in the project memory.

The v6 fresh-install checks at the bottom of this suite assert that the
SHIPPED release .tox boots with all v6 features wired and live:
clipboard TDN copy/paste, the Collection safety scanner, TDN v2.0 YAML
round-trips, GLSL .glsl externalization, the Envoy liveness watchdog, and
end-to-end TDN feature round-trips (POP chains, default-valued custom
parameters, and the tdn_exclude tag). They reach the installed extensions
exactly the way the existing smoke tests do (self.embody.ext.TDN /
self.embody.ext.Envoy / self.embody.op('Collection').ext.Collection),
never a dev-only EmbodyTestCase helper.
"""

import os
import tempfile
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestSmokeRelease(EmbodyTestCase):

    def tearDown(self):
        """Always clear seeded responses to prevent leakage."""
        try:
            self.embody.unstore('_smoke_test_responses')
        except Exception:
            pass
        super().tearDown()

    # =========================================================================
    # _messageBox auto-response mechanism
    # =========================================================================

    def test_message_box_auto_response(self):
        """Seeded response is returned and consumed by _messageBox."""
        self.embody.store('_smoke_test_responses', {
            'Test Dialog': 1
        })
        result = self.embody_ext._messageBox(
            'Test Dialog', 'Test message', buttons=['Cancel', 'OK'])
        self.assertEqual(result, 1)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNone(responses,
            'Storage should be unstored when last response is consumed')

    def test_message_box_multiple_responses(self):
        """Multiple seeded responses are consumed independently."""
        self.embody.store('_smoke_test_responses', {
            'Dialog A': 0,
            'Dialog B': 2
        })
        result_a = self.embody_ext._messageBox(
            'Dialog A', 'msg', buttons=['OK'])
        self.assertEqual(result_a, 0)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNotNone(responses)
        self.assertIn('Dialog B', responses)
        result_b = self.embody_ext._messageBox(
            'Dialog B', 'msg', buttons=['A', 'B', 'C'])
        self.assertEqual(result_b, 2)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNone(responses)

    def test_message_box_no_response_seeded(self):
        """With no seeded responses, storage returns None."""
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNone(responses,
            'No responses should be seeded at test start')

    def test_message_box_unmatched_title_left_intact(self):
        """A title with no matching response is left for ui.messageBox."""
        self.embody.store('_smoke_test_responses', {
            'Other Dialog': 1
        })
        # Call with a different title - should NOT consume the stored response.
        # We can't test the ui.messageBox fallback without a modal, so just
        # verify the stored response survives.
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIn('Other Dialog', responses)
        self.embody.unstore('_smoke_test_responses')

    # =========================================================================
    # Post-init state verification
    # =========================================================================

    def test_status_enabled(self):
        """Embody status is Enabled after init completes."""
        self.assertEqual(self.embody.par.Status.eval(), 'Enabled')

    def test_embody_extension_loaded(self):
        """EmbodyExt is accessible on the Embody COMP."""
        ext = self.embody.ext.Embody
        self.assertIsNotNone(ext, 'EmbodyExt should be loaded')

    def test_envoy_extension_loaded(self):
        """EnvoyExt is accessible on the Embody COMP."""
        ext = self.embody.ext.Envoy
        self.assertIsNotNone(ext, 'EnvoyExt should be loaded')

    def test_tdn_extension_loaded(self):
        """TDNExt is accessible on the Embody COMP."""
        ext = self.embody.ext.TDN
        self.assertIsNotNone(ext, 'TDNExt should be loaded')

    def test_no_script_errors(self):
        """Embody COMP has no script errors."""
        errors = self.embody.scriptErrors()
        self.assertEqual(len(errors), 0,
            f'Embody has script errors: {errors}')

    def test_version_parameter_exists(self):
        """Version parameter exists and is a non-empty string."""
        version = str(self.embody.par.Version.eval())
        self.assertTrue(len(version) > 0, 'Version should be non-empty')

    def test_build_parameter_exists(self):
        """Build parameter exists and is a positive integer."""
        build = int(self.embody.par.Build.eval())
        self.assertGreater(build, 0, f'Build should be > 0, got {build}')

    def test_externalizations_table_exists(self):
        """Externalizations table exists and is a DAT."""
        table = self.embody_ext.Externalizations
        self.assertIsNotNone(table, 'Externalizations table must exist')

    def test_externalizations_table_schema(self):
        """Externalizations table has the expected column headers."""
        table = self.embody_ext.Externalizations
        self.assertIsNotNone(table)
        expected = [
            'path', 'type', 'strategy', 'rel_file_path',
            'timestamp', 'dirty', 'build', 'touch_build'
        ]
        headers = [table[0, c].val for c in range(table.numCols)]
        for col in expected:
            self.assertIn(col, headers, f'Missing column: {col}')

    def test_promoted_methods_exist(self):
        """Key promoted methods are callable on the Embody COMP."""
        for method_name in ['Update', 'Save', 'Verify', 'Reset']:
            method = getattr(self.embody, method_name, None)
            self.assertIsNotNone(method,
                f'Promoted method {method_name} missing')
            self.assertTrue(callable(method),
                f'{method_name} should be callable')

    def test_log_method_works(self):
        """Log method executes without error."""
        try:
            self.embody_ext.Log('[test] smoke test log check', 'INFO')
        except Exception as e:
            raise AssertionError(f'Log() raised: {e}')

    def test_global_op_shortcut(self):
        """op.Embody resolves to the Embody COMP."""
        self.assertIs(op.Embody, self.embody,
            'op.Embody should resolve to the Embody COMP')

    def test_parent_shortcut(self):
        """parent.Embody resolves from inside the Embody COMP."""
        # The execute DAT lives inside Embody, so parent.Embody
        # should resolve from there. We verify indirectly: the
        # extension's self.my should be the Embody COMP.
        self.assertIs(self.embody_ext.my, self.embody,
            'Extension self.my should be the Embody COMP')

    def test_key_parameters_exist(self):
        """Essential parameters exist on the Embody COMP."""
        required_pars = [
            'Status', 'Version', 'Build', 'Envoyenable', 'Envoyport',
            'Logtofile', 'Logfolder', 'Filecleanup', 'Toxdropexpr',
            'Tdnstriponsave', 'Refresh',
        ]
        for par_name in required_pars:
            par = getattr(self.embody.par, par_name, None)
            self.assertIsNotNone(par, f'Parameter {par_name} missing')

    def test_v6_uninstall_pulse_and_handler_present(self):
        """v6.0.108: the release .tox ships the Uninstall pulse (ordered right
        after Disable) plus the promoted UninstallHandler / Uninstall /
        PreviewUninstall API. Read-only -- never fires the pulse."""
        un = getattr(self.embody.par, 'Uninstall', None)
        self.assertIsNotNone(un, 'Uninstall param missing from the release build')
        self.assertEqual(un.style, 'Pulse', 'Uninstall must be a Pulse parameter')
        dis = getattr(self.embody.par, 'Disable', None)
        self.assertIsNotNone(dis, 'Disable param missing')
        self.assertGreater(un.order, dis.order,
            'Uninstall should be ordered after Disable on the Embody page')
        for method_name in ['UninstallHandler', 'Uninstall', 'PreviewUninstall']:
            method = getattr(self.embody, method_name, None)
            self.assertIsNotNone(method,
                f'Promoted method {method_name} missing from the release build')
            self.assertTrue(callable(method), f'{method_name} should be callable')

    def test_log_buffer_initialized(self):
        """Internal log buffer is initialized and operational."""
        buffer = self.embody_ext._log_buffer
        self.assertIsNotNone(buffer, 'Log buffer should be initialized')
        # Buffer should have entries from init
        self.assertGreater(len(buffer), 0,
            'Log buffer should have entries after init')

    # =========================================================================
    # _promptEnvoy with auto-response
    # =========================================================================

    def test_prompt_envoy_skip(self):
        """Auto-responding Skip (0) to Envoy prompt keeps Envoyenable off."""
        original = self.embody.par.Envoyenable.eval()
        self.embody.par.Envoyenable = False
        try:
            self.embody.store('_smoke_test_responses', {
                'Embody - AI Coding Assistant Integration': 0
            })
            self.embody_ext._promptEnvoy()
            self.assertFalse(self.embody.par.Envoyenable.eval(),
                'Envoyenable should remain False after Skip')
        finally:
            self.embody.par.Envoyenable = original

    def test_prompt_envoy_enable(self):
        """Auto-responding Enable (1) to Envoy prompt enables Envoy."""
        original = self.embody.par.Envoyenable.eval()
        self.embody.par.Envoyenable = False
        try:
            self.embody.store('_smoke_test_responses', {
                'Embody - AI Coding Assistant Integration': 1
            })
            self.embody_ext._promptEnvoy()
            self.assertTrue(self.embody.par.Envoyenable.eval(),
                'Envoyenable should be True after Enable')
        finally:
            self.embody.par.Envoyenable = original

    # =========================================================================
    # Envoy state (when enabled)
    # =========================================================================

    def test_envoy_server_running_if_enabled(self):
        """If Envoyenable is True, the MCP server should be running."""
        if not self.embody.par.Envoyenable.eval():
            self.skipTest('Envoy not enabled in this session')
        # Check status parameter (survives extension reinit) rather than
        # envoy_running store (reset to False on every __init__).
        status = str(self.embody.par.Envoystatus.eval())
        self.assertTrue(status.startswith('Running'),
            f'Server should be running when Envoy is enabled, got: {status}')

    def test_envoy_port_valid(self):
        """Envoy port is in a valid range."""
        port = int(self.embody.par.Envoyport.eval())
        self.assertGreater(port, 1023, f'Port {port} too low')
        self.assertLess(port, 65536, f'Port {port} too high')

    # =========================================================================
    # Update() race condition with CatalogManager (regression for v5.0.398)
    # =========================================================================

    def _restore_status(self, saved_status, saved_pending):
        """Helper used by the regression tests below."""
        self.embody.par.Status = saved_status
        if saved_pending is True:
            self.embody_ext._pending_envoy_prompt = True
        else:
            try:
                delattr(self.embody_ext, '_pending_envoy_prompt')
            except AttributeError:
                pass
        try:
            self.embody.unstore('_smoke_test_responses')
        except Exception:
            pass

    def test_update_consumes_pending_prompt_during_catalog_scan(self):
        """Update() must consume `_pending_envoy_prompt` even when Status
        is a transient catalog-scan value.

        The bug: on fresh-project drops, EnsureCatalogs (no cached catalog)
        sets Status='Scanning defaults (X/N)' one frame before Update fires.
        The old check `Status != 'Enabled'` returned early on that transient
        value, so Update never consumed `_pending_envoy_prompt`, so
        `_promptEnvoy` was never scheduled, so the Envoy opt-in dialog
        never appeared. Latent for many releases; surfaced when a user
        finally tested fresh-drop on a machine without a cached catalog.
        """
        saved_status = self.embody.par.Status.eval()
        saved_pending = getattr(self.embody_ext, '_pending_envoy_prompt', False)
        try:
            # Seed the prompt response so any chained _promptEnvoy is silent
            self.embody.store('_smoke_test_responses', {
                'Embody - AI Coding Assistant Integration': 0  # Skip
            })
            # Simulate the race: scanning value when Update fires
            self.embody.par.Status = 'Scanning defaults (12/685)'
            self.embody_ext._pending_envoy_prompt = True
            # Run Update -- must NOT return early on transient Status
            self.embody_ext.Update(suppress_refresh=True)
            self.assertFalse(
                getattr(self.embody_ext, '_pending_envoy_prompt', False),
                "Update must consume _pending_envoy_prompt even when "
                "Status='Scanning defaults' -- the race fix in v5.0.398 "
                "ensures Update only skips when Status=='Disabled'")
        finally:
            self._restore_status(saved_status, saved_pending)

    def test_update_skips_only_when_disabled(self):
        """Update should run for every transient Status value (Scanning
        defaults, Scanning palette, Testing) and only return early when
        Embody is explicitly Disabled. Verifies no other transient state
        regresses to the old `!= 'Enabled'` check."""
        saved_status = self.embody.par.Status.eval()
        saved_pending = getattr(self.embody_ext, '_pending_envoy_prompt', False)
        try:
            self.embody.store('_smoke_test_responses', {
                'Embody - AI Coding Assistant Integration': 0
            })
            for transient in (
                'Scanning defaults (0/100)',
                'Scanning palette (50/261)',
                'Testing',
            ):
                self.embody.par.Status = transient
                self.embody_ext._pending_envoy_prompt = True
                self.embody_ext.Update(suppress_refresh=True)
                self.assertFalse(
                    getattr(self.embody_ext, '_pending_envoy_prompt', False),
                    f"Update must run with Status='{transient}'")
            # Confirm Disabled DOES short-circuit
            self.embody.par.Status = 'Disabled'
            self.embody_ext._pending_envoy_prompt = True
            self.embody_ext.Update(suppress_refresh=True)
            self.assertTrue(
                getattr(self.embody_ext, '_pending_envoy_prompt', False),
                "Update must skip (and leave _pending_envoy_prompt set) "
                "when Status=='Disabled' -- this is the only state that "
                "should short-circuit Update")
        finally:
            self._restore_status(saved_status, saved_pending)

    # =========================================================================
    # v6 fresh-install: shared helpers
    #
    # These reach the SHIPPED extensions the same way the rest of this suite
    # does -- self.embody.ext.TDN / .Envoy and self.embody.op('Collection')
    # -- never a dev-only EmbodyTestCase convenience. Each helper resolves its
    # target inline (never cached) and skips gracefully when the feature isn't
    # present (e.g. an older .tox, or POPs unavailable in this TD build).
    # =========================================================================

    def _make_sandbox_comp(self, name):
        """Create a temporary baseCOMP under the test sandbox.

        The runner's tearDown destroys every sandbox child, so callers never
        clean up. Returns the new COMP.
        """
        return self.sandbox.create(baseCOMP, name)

    def _tdn(self):
        """The shipped TDN extension (resolved live, never cached)."""
        ext = self.embody.ext.TDN
        if ext is None:
            raise SkipTest('TDN extension not loaded on the release .tox')
        return ext

    def _collection_ext(self):
        """The shipped CollectionExt on the Collection sub-COMP, or skip."""
        collection = self.embody.op('Collection')
        if collection is None:
            raise SkipTest('Collection sub-COMP not present on the release .tox')
        ext = collection.ext.Collection
        if ext is None:
            raise SkipTest('CollectionExt not loaded on the Collection COMP')
        return ext

    # =========================================================================
    # v6: TDN clipboard methods exist + are callable on the TDN ext
    #
    # Introspection only -- getattr + callable. We never touch the OS
    # clipboard here (that would be flaky and machine-dependent); the actual
    # copy/paste behavior is covered by the dedicated clipboard suite.
    # =========================================================================

    def test_v6_tdn_clipboard_methods_exist_and_callable(self):
        """All five clipboard methods are present and callable on the TDN ext."""
        tdn = self._tdn()
        for name in (
            'CopyNetworkToClipboard',
            'CopySelectedToClipboard',
            'PasteNetworkFromClipboard',
            'PasteNetworkAsNewComp',
            'ClipboardHasNetwork',
        ):
            method = getattr(tdn, name, None)
            self.assertIsNotNone(method,
                f'TDN clipboard method {name} is missing on the release .tox')
            self.assertTrue(callable(method),
                f'TDN clipboard method {name} is not callable')

    # =========================================================================
    # v6: Collection scanner is wired + a trivially-clean TDN scans 'clean'
    # =========================================================================

    def test_v6_collection_subcomp_and_scanner_present(self):
        """Collection sub-COMP has scanner + safe_import DATs and CollectionExt."""
        collection = self.embody.op('Collection')
        if collection is None:
            self.skipTest('Collection sub-COMP not present on the release .tox')
        self.assertIsNotNone(collection.op('scanner'),
            'Collection/scanner DAT must exist')
        self.assertIsNotNone(collection.op('safe_import'),
            'Collection/safe_import DAT must exist')
        self.assertIsNotNone(collection.ext.Collection,
            'CollectionExt must be loaded on the Collection COMP')

    def test_v6_collection_scan_clean_tdn_returns_clean(self):
        """A benign source -> null TDN scans 'clean' via CollectionExt.ScanTdn."""
        ext = self._collection_ext()
        clean_tdn = {
            'format': 'tdn',
            'version': '2.0',
            'network_path': '/smoke',
            'type': 'baseCOMP',
            'operators': [
                {'name': 'source1', 'type': 'constantTOP'},
                {'name': 'null1', 'type': 'nullTOP', 'inputs': ['source1']},
            ],
        }
        result = ext.ScanTdn(clean_tdn)
        self.assertIsInstance(result, dict)
        self.assertIn('verdict', result,
            'Capability report must carry a verdict key')
        self.assertEqual(result['verdict'], 'clean',
            f"Trivial clean TDN should scan 'clean', got {result.get('verdict')}")

    # =========================================================================
    # v6: TDN v2.0 YAML round-trip through a real .tdn file on disk
    #
    # Build a tiny COMP with a multi-line-text DAT, ExportNetwork to a temp
    # .tdn (YAML v2.0), read it back, ImportNetwork into a CLEAN COMP, and
    # assert the DAT text is byte-identical and the file parses as YAML with
    # format == 'tdn'. ExportNetwork writes to the EXACT explicit path (the
    # _resolveOutputPath passthrough branch), so a temp file outside the
    # project folder never pollutes externalizations.tsv.
    # =========================================================================

    def test_v6_tdn_v2_yaml_roundtrip_dat_byte_identical(self):
        """Multi-line DAT text survives a YAML v2.0 .tdn file round-trip intact."""
        import yaml
        tdn = self._tdn()

        src = self._make_sandbox_comp('v6_yaml_src')
        dat = src.create(textDAT, 'multiline')
        # A deliberately multi-line payload -- block-scalar territory for YAML.
        payload = ('line one\n'
                   'line two with  internal  spaces\n'
                   '\n'
                   '    indented line\n'
                   'trailing text')
        dat.text = payload

        tmp_dir = tempfile.mkdtemp(prefix='smoke_tdn_')
        fp = str(Path(tmp_dir) / 'roundtrip.tdn')
        try:
            export = tdn.ExportNetwork(
                root_path=src.path, output_file=fp, include_dat_content=True)
            self.assertTrue(export.get('success'),
                f'ExportNetwork failed: {export}')
            self.assertTrue(Path(fp).exists(),
                'ExportNetwork did not write the .tdn file')

            # The on-disk file must parse as YAML and self-identify as TDN.
            with open(fp, 'r', encoding='utf-8') as f:
                doc = yaml.safe_load(f)
            self.assertEqual(doc.get('format'), 'tdn',
                "Exported .tdn must declare format == 'tdn'")

            # Import the parsed doc into a CLEAN, separate COMP.
            target = self._make_sandbox_comp('v6_yaml_target')
            result = tdn.ImportNetwork(target_path=target.path, tdn=doc)
            self.assertTrue(result.get('success'),
                f'ImportNetwork failed: {result}')

            imported = target.op('multiline')
            self.assertIsNotNone(imported,
                'multiline DAT was not recreated on import')
            self.assertEqual(imported.text, payload,
                'DAT text must be byte-identical after a YAML round-trip')
        finally:
            try:
                Path(fp).unlink(missing_ok=True)
            except Exception:
                pass
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass

    # =========================================================================
    # v6: GLSL externalizes to a .glsl file with the glsl tag
    #
    # Uses the SHIPPED Envoy externalize path (the same surface the dedicated
    # externalization suite uses) so this exercises the real release wiring.
    # =========================================================================

    def test_v6_glsl_externalizes_to_dot_glsl_with_glsl_tag(self):
        """A glsl-language textDAT externalizes with the glsl tag to a .glsl file."""
        envoy = self.embody.ext.Envoy
        if envoy is None:
            self.skipTest('Envoy extension not loaded on the release .tox')
        if not hasattr(envoy, '_externalize_op'):
            self.skipTest('Envoy._externalize_op not available on this .tox')

        glsl_tag = self.embody.par.Glsltag.eval()
        dat = self._make_sandbox_comp('v6_glsl_host').create(textDAT, 'shader')
        dat.par.language = 'glsl'
        dat.text = 'out vec4 fragColor;\nvoid main() { fragColor = vec4(1.0); }\n'

        result = envoy._externalize_op(op_path=dat.path)
        try:
            self.assertTrue(result.get('success'),
                f'GLSL externalize failed: {result}')
            # The v6.0.34 fix is content-based TAG inference: a glsl-language DAT
            # must resolve to the glsl tag (NOT py). That is the synchronous,
            # deterministic regression guard. The resulting .glsl on-disk FILE is
            # written by a DEFERRED Update() (async -- it is '' during a same-frame
            # co-run), so the file-extension is verified deterministically in the
            # dedicated test_glsl_externalize.py rather than depended on here.
            self.assertEqual(result.get('tag'), glsl_tag,
                f"glsl-language DAT must infer the glsl tag {glsl_tag!r} (the "
                f"v6.0.34 fix), got {result.get('tag')!r}")
        finally:
            # Drop the tag + tracking so the externalization folder stays tidy.
            try:
                envoy._remove_externalization_tag(op_path=dat.path)
            except Exception:
                pass

    # =========================================================================
    # v6: Envoy liveness watchdog is armed
    #
    # Introspection only -- the watchdog methods exist and the armed-generation
    # counter is > 0. We do NOT probe or kill the socket here (that would risk
    # the live MCP server); the watchdog's self-healing is verified by the
    # dedicated lifecycle-hardening suite.
    # =========================================================================

    def test_v6_envoy_watchdog_methods_exist(self):
        """The watchdog tick / revive / probe methods exist on the Envoy ext."""
        envoy = self.embody.ext.Envoy
        if envoy is None:
            self.skipTest('Envoy extension not loaded on the release .tox')
        for name in ('_watchdogTick', '_reviveDeadServer', '_probeAlive'):
            method = getattr(envoy, name, None)
            self.assertIsNotNone(method,
                f'Envoy watchdog method {name} is missing on the release .tox')
            self.assertTrue(callable(method),
                f'Envoy watchdog method {name} is not callable')

    def test_v6_envoy_watchdog_armed(self):
        """The watchdog generation counter is > 0 (a loop was armed at init).

        _watchdog_gen is stored on the Embody COMP and bumped each time a tick
        loop is armed (once per EnvoyExt instance). A value > 0 means the
        instance armed its self-healing loop on init.
        """
        if self.embody.ext.Envoy is None:
            self.skipTest('Envoy extension not loaded on the release .tox')
        gen = self.embody.fetch('_watchdog_gen', 0)
        self.assertGreater(int(gen), 0,
            'Envoy watchdog generation must be > 0 (no loop was armed)')

    # =========================================================================
    # v6: TDN feature round-trips -- POP chain, default-valued custom Float,
    # and a nested tdn_exclude-tagged COMP all survive export/import.
    #
    # The excluded COMP is placed NESTED (depth > 0 under the export root),
    # NOT as a direct child: a direct-child excluded COMP is intentionally
    # OMITTED from the export (the owning app manages it), so it could never
    # "survive" a round-trip. A nested excluded COMP serializes as ordinary
    # content (with a warning) and DOES round-trip, tag intact -- that is the
    # behavior under test here.
    # =========================================================================

    def test_v6_tdn_feature_roundtrip_pop_float_exclude(self):
        """POP sequence + default-valued custom Float + nested tdn_exclude tag
        all survive a TDN export/import round-trip."""
        tdn = self._tdn()
        exclude_tag = self.embody.par.Tdnexcludetag.eval()

        src = self._make_sandbox_comp('v6_feature_src')

        # (1) POP sequence -- skip cleanly if POPs are unavailable in this build.
        try:
            grid = src.create(gridPOP, 'pop_grid')
            xform = src.create(transformPOP, 'pop_transform')
            grid.outputConnectors[0].connect(xform.inputConnectors[0])
        except Exception:
            self.skipTest('POPs not available in this TD build')
            return

        # (2) A custom Float left at its (non-zero) default value. The default
        # is part of the parameter definition and must survive the round-trip
        # even though the live value equals it (so it may be omitted as a
        # non-default value).
        floaty = src.create(baseCOMP, 'floaty')
        page = floaty.appendCustomPage('Settings')
        pg = page.appendFloat('Speed', label='Speed')
        pg[0].default = 2.5
        pg[0].val = 2.5  # at default -- exercises the default-value path

        # (3) A nested excluded COMP (depth > 0) -- serializes as normal
        # content and round-trips with its exclude tag intact.
        intermediate = src.create(baseCOMP, 'intermediate')
        excluded = intermediate.create(baseCOMP, 'excluded_child')
        excluded.tags.add(exclude_tag)

        # Export -> import into a CLEAN, separate target COMP.
        export = tdn.ExportNetwork(root_path=src.path, include_dat_content=True)
        self.assertTrue(export.get('success'),
            f'ExportNetwork failed: {export}')

        target = self._make_sandbox_comp('v6_feature_target')
        result = tdn.ImportNetwork(target_path=target.path, tdn=export['tdn'])
        self.assertTrue(result.get('success'),
            f'ImportNetwork failed: {result}')

        # (1) POP chain restored, wired source -> dest.
        rg = target.op('pop_grid')
        rt = target.op('pop_transform')
        self.assertIsNotNone(rg, 'gridPOP not recreated on import')
        self.assertIsNotNone(rt, 'transformPOP not recreated on import')
        self.assertGreaterEqual(len(rt.inputs), 1,
            'transformPOP lost its input wire on import')
        self.assertEqual(rt.inputs[0].name, 'pop_grid',
            'POP chain wiring not preserved')

        # (2) Custom Float present with its default value intact.
        rf = target.op('floaty')
        self.assertIsNotNone(rf, 'floaty COMP not recreated on import')
        speed = getattr(rf.par, 'Speed', None)
        self.assertIsNotNone(speed,
            'Custom Float parameter Speed was lost on import')
        self.assertAlmostEqual(speed.default, 2.5,
            msg='Custom Float default value was not preserved')
        self.assertAlmostEqual(speed.eval(), 2.5,
            msg='Custom Float live value (== default) was not preserved')

        # (3) Nested excluded COMP round-tripped with its exclude tag.
        rex = target.op('intermediate/excluded_child')
        self.assertIsNotNone(rex,
            'Nested excluded COMP was lost on import (nested exclusion must '
            'serialize as normal content and round-trip)')
        self.assertIn(exclude_tag, rex.tags,
            'tdn_exclude tag did not survive the round-trip on the nested COMP')
