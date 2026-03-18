"""
Test suite: Custom parameter behavior.

Tests the high-value, failure-prone custom parameters on the Embody COMP:
Folder change (full flow), Disable/Enable lifecycle, Update/Refresh,
TDN page controls, Logs toggles, and Envoy state verification.
"""

import os
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

class TestCustomParameters(EmbodyTestCase):

    # ==================================================================
    # LIFECYCLE HOOKS
    # ==================================================================

    def setUpSuite(self):
        """Snapshot non-readOnly, non-pulse custom parameter values."""
        self._par_snapshot = {}
        for page in self.embody.customPages:
            for p in page.pars:
                if not p.readOnly and not p.isPulse:
                    self._par_snapshot[p.name] = p.eval()

    def setUp(self):
        """Track which parameters this test modifies."""
        self._modified_pars = []

    def tearDown(self):
        """Restore modified parameters with parexec disabled."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        for par_name in self._modified_pars:
            if par_name in self._par_snapshot:
                try:
                    getattr(self.embody.par, par_name).val = self._par_snapshot[par_name]
                except Exception:
                    pass
        parexec.par.active = was_active
        self._modified_pars = []
        super().tearDown()

    def tearDownSuite(self):
        """Safety net: ensure Embody is operational after suite."""
        path = self.embody.path
        run(f"op('{path}').UpdateHandler()", delayFrames=5)
        run(f"op('{path}').Update()", delayFrames=10)

    # ==================================================================
    # HELPERS
    # ==================================================================

    def _set_and_track(self, par_name, value):
        """Set a parameter value and register for tearDown restoration."""
        self._modified_pars.append(par_name)
        getattr(self.embody.par, par_name).val = value

    def _get_log_id(self):
        """Get current log counter for checking new entries after an operation."""
        return self.embody_ext._log_counter

    def _get_logs_since(self, since_id):
        """Get log entries added after the given ID."""
        return [e for e in self.embody_ext._log_buffer if e['id'] > since_id]

    def _has_log_message(self, since_id, substring):
        """Check if any log entry since the given ID contains the substring."""
        for entry in self._get_logs_since(since_id):
            if substring in entry.get('message', ''):
                return True
        return False

    def _externalize_project_silent(self, use_tdn=False):
        """Replicate ExternalizeProject() without the ui.messageBox prompt.

        Tags all eligible COMPs and DATs, then calls UpdateHandler + Update.
        """
        ext = self.embody_ext
        root = ext.root

        # Find system COMPs to exclude (palette clones)
        sys_comps = root.findChildren(
            type=COMP, parName='clone',
            key=lambda x: any(
                s in (str(x.par.clone.expr) or '')
                for s in ['TDTox', 'TDBasicWidgets']
            ))
        paths_to_exclude = set()
        for sys_comp in sys_comps:
            paths_to_exclude.add(sys_comp.path)
            for desc in sys_comp.findChildren():
                paths_to_exclude.add(desc.path)

        # Tag eligible DATs
        for oper in root.findChildren(type=DAT, parName='file'):
            if ext._shouldSkipOp(oper, paths_to_exclude):
                continue
            if oper.type in ext.supported_dat_types:
                tag_value = ext._inferDATTagValue(oper)
                ext.applyTagToOperator(oper, tag_value)

        # Tag eligible COMPs
        if use_tdn:
            comp_tag = ext.my.par.Tdntag.val
            for oper in root.findChildren(type=COMP):
                if ext._shouldSkipOp(oper, paths_to_exclude):
                    continue
                ext.applyTagToOperator(oper, comp_tag)
        else:
            comp_tag = ext.my.par.Toxtag.val
            for oper in root.findChildren(type=COMP, parName='externaltox'):
                if ext._shouldSkipOp(oper, paths_to_exclude):
                    continue
                ext.applyTagToOperator(oper, comp_tag)

        # Run Update to write files and populate table
        ext.UpdateHandler()
        ext.Update()

    # ==================================================================
    # A. EMBODY CORE
    # ==================================================================

    def test_externalizations_par_points_to_valid_table(self):
        """Verify Externalizations par points to a valid tableDAT with expected columns."""
        table = self.embody_ext.Externalizations
        self.assertIsNotNone(table)
        self.assertTrue(table.valid)
        # Check expected columns exist
        headers = [table[0, c].val for c in range(table.numCols)]
        self.assertIn('path', headers)
        self.assertIn('rel_file_path', headers)
        self.assertIn('type', headers)

    def test_folder_is_string_and_getProjectFolder_works(self):
        """Verify Folder is a string and getProjectFolder constructs an absolute path."""
        folder_val = self.embody.par.Folder.eval()
        self.assertIsInstance(str(folder_val), str)
        abs_path = self.embody_ext.getProjectFolder()
        self.assertTrue(os.path.isabs(abs_path))
        # Should contain the folder value if non-empty
        if folder_val:
            self.assertIn(str(folder_val), abs_path)

    def test_update_pulse_sets_status_enabled(self):
        """Call UpdateHandler, verify Status is Enabled."""
        # Status should already be Enabled, but let's confirm UpdateHandler maintains it
        self.embody_ext.UpdateHandler()
        self.assertEqual(self.embody.par.Status.eval(), 'Enabled')

    def test_update_creates_externalization_folder(self):
        """After UpdateHandler, externalization folder exists on disk."""
        self.embody_ext.UpdateHandler()
        folder = self.embody_ext.getProjectFolder()
        self.assertTrue(os.path.isdir(folder))

    def test_disable_directly_with_keep_tags(self):
        """Call Disable(removeTags=False), verify Status=Disabled, tags preserved."""
        # Get a tagged op before disable
        tagged_ops = self.embody_ext.getExternalizedOps(COMP)
        tags_before = {}
        for oper in tagged_ops[:3]:  # Check up to 3 ops
            tags_before[oper.path] = set(oper.tags)

        # Disable parexec to prevent callback cascades
        parexec = self.embody.op('parexec')
        parexec.par.active = False

        try:
            self.embody_ext.Disable(removeTags=False)
            self.assertEqual(self.embody.par.Status.eval(), 'Disabled')

            # Tags should be preserved
            for path, tags in tags_before.items():
                oper = op(path)
                if oper:
                    for t in tags:
                        self.assertIn(t, oper.tags)

            # Re-enable
            self.embody_ext.UpdateHandler()
            self.assertEqual(self.embody.par.Status.eval(), 'Enabled')
            self.embody_ext.Update()
        finally:
            parexec.par.active = True

    def test_disable_directly_with_remove_tags(self):
        """Call Disable(removeTags=True), verify tags removed, then re-enable and restore."""
        tags = self.embody_ext.getTags()
        # Create a test comp in sandbox and tag it
        test_comp = self.sandbox.create(baseCOMP, 'disable_test')
        test_comp.tags.add(tags[0])  # Add first tag

        parexec = self.embody.op('parexec')
        parexec.par.active = False

        try:
            self.embody_ext.Disable(removeTags=True)
            self.assertEqual(self.embody.par.Status.eval(), 'Disabled')

            # Tags should be removed from ops
            # (sandbox comp will be destroyed in tearDown anyway)
            if test_comp.valid:
                for t in tags:
                    self.assertNotIn(t, test_comp.tags)

            # Re-enable
            self.embody_ext.UpdateHandler()
            self.assertEqual(self.embody.par.Status.eval(), 'Enabled')
            self.embody_ext.Update()
        finally:
            parexec.par.active = True

    def test_disable_z01_restore_tox(self):
        """After disable tests: Externalize full project (TOX mode), verify TOXes."""
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        try:
            self._externalize_project_silent(use_tdn=False)
        finally:
            parexec.par.active = True

        # Verify table is populated
        table = self.embody_ext.Externalizations
        self.assertGreater(table.numRows, 1,
                           'Externalizations table should have rows after TOX externalization')

        # Verify TOX files exist for tagged COMPs
        ext_folder = self.embody_ext.getProjectFolder()
        tox_count = 0
        for i in range(1, table.numRows):
            strategy = table[i, 'strategy'].val if 'strategy' in [table[0, c].val for c in range(table.numCols)] else ''
            rel_path = table[i, 'rel_file_path'].val
            if rel_path.endswith('.tox'):
                full_path = os.path.join(ext_folder, rel_path)
                self.assertTrue(os.path.isfile(full_path),
                                f'TOX file should exist: {rel_path}')
                tox_count += 1
        self.assertGreater(tox_count, 0, 'Should have at least one TOX file')

    def test_disable_z02_disable_again(self):
        """Disable with removeTags=True again after TOX restore."""
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        try:
            self.embody_ext.Disable(removeTags=True)
            self.assertEqual(self.embody.par.Status.eval(), 'Disabled')
        finally:
            parexec.par.active = True

    def test_disable_z03_restore_tdn(self):
        """Externalize full project (TDN mode), verify table is populated.

        Only checks that the table has rows — file existence and TDN/py counts
        are verified in z04, which runs one frame later after deferred Updates
        (TDN exports and DAT additions) have had time to settle.
        """
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        try:
            self._externalize_project_silent(use_tdn=True)
        finally:
            parexec.par.active = True

        # Verify table is populated — deferred Updates may still be in-flight,
        # so we only assert the table is non-empty here. Full file/count checks
        # happen in z04 (next frame).
        table = self.embody_ext.Externalizations
        self.assertGreater(table.numRows, 1,
                           'Externalizations table should have rows after TDN externalization')

    def test_disable_z04_verify_complete(self):
        """Final verification: all operators, files, TDN/py counts are intact.

        Runs one frame after z03 so that deferred Updates (TDN exports, DAT
        additions) have fully settled before asserting file existence and counts.
        """
        table = self.embody_ext.Externalizations
        ext_folder = self.embody_ext.getProjectFolder()

        # Check all table rows have valid operators and files on disk
        missing_ops = []
        missing_files = []
        tdn_count = 0
        py_count = 0
        for i in range(1, table.numRows):
            op_path = table[i, 'path'].val
            rel_path = table[i, 'rel_file_path'].val
            full_path = os.path.join(ext_folder, rel_path)

            if not op(op_path):
                missing_ops.append(op_path)
            if not os.path.isfile(full_path):
                missing_files.append(rel_path)
            if rel_path.endswith('.tdn'):
                tdn_count += 1
            elif rel_path.endswith('.py'):
                py_count += 1

        self.assertEqual(len(missing_ops), 0,
                         f'All operators should exist: missing {missing_ops[:5]}')
        self.assertEqual(len(missing_files), 0,
                         f'All files should exist: missing {missing_files[:5]}')
        self.assertGreater(tdn_count, 0, 'Should have at least one TDN file')
        self.assertGreater(py_count, 0, 'Should have at least one .py file')

        # Verify Embody is fully operational
        self.assertEqual(self.embody.par.Status.eval(), 'Enabled')

    def test_refresh_cleans_and_updates(self):
        """Call Refresh, verify no errors and list comp is valid."""
        log_id = self._get_log_id()
        self.embody_ext.Refresh()
        # Verify list comp is still valid
        list_comp = self.embody.op('list/list1')
        self.assertTrue(list_comp.valid)
        # No ERROR-level logs from Refresh
        for entry in self._get_logs_since(log_id):
            if entry['level'] == 'ERROR':
                # Allow timeline pause warning
                if 'TIMELINE' not in entry['message']:
                    self.assertTrue(False, f"Unexpected error during Refresh: {entry['message']}")

    def test_detectduplicatepaths_toggle(self):
        """Toggle Detectduplicatepaths off and on, verify value changes."""
        original = self.embody.par.Detectduplicatepaths.eval()
        self._set_and_track('Detectduplicatepaths', 0)
        self.assertFalse(self.embody.par.Detectduplicatepaths.eval())
        self._set_and_track('Detectduplicatepaths', 1)
        self.assertTrue(self.embody.par.Detectduplicatepaths.eval())

    # ==================================================================
    # B. TDN PAGE
    # ==================================================================

    def test_embeddatsintdns_toggle_triggers_reexport(self):
        """Toggle Embeddatsintdns, verify reexport is triggered via logs."""
        log_id = self._get_log_id()
        original = self.embody.par.Embeddatsintdns.eval()
        new_val = 0 if original else 1
        self._set_and_track('Embeddatsintdns', new_val)
        # parexec fires async at end-of-frame; call directly to test synchronously
        self.embody.ext.TDN.ReexportAllTDNs()
        # Check logs for reexport message
        has_reexport = self._has_log_message(log_id, 'Re-exporting')
        has_no_tdn = self._has_log_message(log_id, 'No TDN exports')
        self.assertTrue(has_reexport or has_no_tdn,
                        'Expected reexport log message after toggling Embeddatsintdns')

    def test_tdncreateonstart_toggle(self):
        """Toggle Tdncreateonstart off and on."""
        original = self.embody.par.Tdncreateonstart.eval()
        new_val = 0 if original else 1
        self._set_and_track('Tdncreateonstart', new_val)
        expected = bool(new_val)
        self.assertEqual(bool(self.embody.par.Tdncreateonstart.eval()), expected)

    def test_tdnfile_accepts_path(self):
        """Set Tdnfile to a path, verify accepted."""
        self._set_and_track('Tdnfile', '/tmp/test_file.tdn')
        self.assertEqual(self.embody.par.Tdnfile.eval(), '/tmp/test_file.tdn')

    def test_networkpath_accepts_path(self):
        """Set Networkpath to a path, verify accepted."""
        self._set_and_track('Networkpath', '/project1/test_comp')
        self.assertEqual(self.embody.par.Networkpath.val, '/project1/test_comp')

    def test_importtdn_with_invalid_file_logs_error(self):
        """Import with nonexistent file returns error without crashing."""
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        try:
            self._set_and_track('Tdnfile', '/nonexistent/path/fake.tdn')
            self._set_and_track('Networkpath', self.sandbox.path)
        finally:
            parexec.par.active = True

        # Directly call ImportNetworkFromFile
        result = self.embody.ext.TDN.ImportNetworkFromFile(
            '/nonexistent/path/fake.tdn', self.sandbox.path)
        # Should return an error dict, not crash
        self.assertIsNotNone(result, 'ImportNetworkFromFile should return a result')
        self.assertTrue(bool(result.get('error')),
                        'Expected error in result for nonexistent TDN file')

    # ==================================================================
    # C. ENVOY PAGE (state verification only)
    # ==================================================================

    def test_envoyenable_reflects_server_state(self):
        """If Envoyenable is True, Envoystatus should contain Running."""
        if self.embody.par.Envoyenable.eval():
            status = str(self.embody.par.Envoystatus.eval())
            # Skip if server is in a transitional state (port conflicts, startup)
            if any(s in status for s in ('Waiting', 'Starting', 'Stopping')):
                self.skip(f'Server in transitional state: {status}')
            self.assertIn('Running', status)
        else:
            # Server not running — just verify the par exists
            self.assertIsNotNone(self.embody.par.Envoystatus.eval())

    def test_envoyport_is_valid_range(self):
        """Verify port is in valid range."""
        port = int(self.embody.par.Envoyport.eval())
        self.assertGreaterEqual(port, 1024)
        self.assertLessEqual(port, 65535)

    # ==================================================================
    # D. LOGS PAGE
    # ==================================================================

    def test_verbose_toggle_affects_debug_output(self):
        """Verbose controls whether DEBUG messages go to FIFO."""
        fifo = self.embody_ext._fifo
        if not fifo:
            self.skip('FIFO DAT not available')

        # With Verbose OFF, Debug should NOT go to FIFO
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        self._set_and_track('Verbose', 0)
        parexec.par.active = True
        self.embody_ext.Debug('test_verbose_off_message')

        # With Verbose ON, Debug SHOULD go to FIFO
        parexec.par.active = False
        self._set_and_track('Verbose', 1)
        parexec.par.active = True
        self.embody_ext.Debug('test_verbose_on_message')

        # Check FIFO content (row count is unreliable when FIFO is at capacity)
        fifo_text = fifo.text
        self.assertNotIn('test_verbose_off_message', fifo_text,
                         'Debug message should NOT go to FIFO when Verbose is OFF')
        self.assertIn('test_verbose_on_message', fifo_text,
                      'Debug message SHOULD go to FIFO when Verbose is ON')

    def test_print_toggle(self):
        """Toggle Print parameter, verify value changes."""
        original = self.embody.par.Print.eval()
        new_val = 0 if original else 1
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        self._set_and_track('Print', new_val)
        parexec.par.active = True
        self.assertEqual(bool(self.embody.par.Print.eval()), bool(new_val))

    def test_logtofile_toggle(self):
        """Toggle Logtofile parameter, verify value changes."""
        original = self.embody.par.Logtofile.eval()
        new_val = 0 if original else 1
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        self._set_and_track('Logtofile', new_val)
        parexec.par.active = True
        self.assertEqual(bool(self.embody.par.Logtofile.eval()), bool(new_val))

    def test_logfolder_default(self):
        """Verify Logfolder defaults to 'logs'."""
        self.assertEqual(self.embody.par.Logfolder.eval(), 'logs')

    def test_logfolder_change_affects_log_path(self):
        """Changing Logfolder changes where log files are written."""
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        self._set_and_track('Logfolder', '_test_logs_temp')
        parexec.par.active = True
        log_path = self.embody_ext._get_log_file_path()
        if log_path:
            self.assertIn('_test_logs_temp', str(log_path))
        # Clean up created directory
        temp_dir = Path('_test_logs_temp')
        if temp_dir.is_dir():
            try:
                temp_dir.rmdir()
            except OSError:
                pass

    def test_enablekeyboardshortcuts_toggle(self):
        """Toggle Enablekeyboardshortcuts, verify value changes."""
        original = self.embody.par.Enablekeyboardshortcuts.eval()
        new_val = 0 if original else 1
        parexec = self.embody.op('parexec')
        parexec.par.active = False
        self._set_and_track('Enablekeyboardshortcuts', new_val)
        parexec.par.active = True
        self.assertEqual(bool(self.embody.par.Enablekeyboardshortcuts.eval()), bool(new_val))

    # ==================================================================
    # E. FOLDER CHANGE FULL FLOW (runs last due to zz_ prefix)
    # ==================================================================

    def test_zz_folder_01_change_to_temp(self):
        """Step 1: Change Folder to temp value, call Disable, verify Disabled."""
        # Save original state
        self.__class__._folder_original = self.embody.par.Folder.eval()
        self.__class__._folder_ext_folder = self.embody_ext.getProjectFolder()

        # Verify we have externalized content before the change
        table = self.embody_ext.Externalizations
        self.__class__._folder_had_rows = table.numRows > 1 if table else False

        # Disable parexec to prevent callback cascade + delayed run() scheduling
        parexec = self.embody.op('parexec')
        parexec.par.active = False

        try:
            prev = self.embody.par.Folder.eval()
            self.embody.par.Folder.val = '_test_pars_temp'
        finally:
            parexec.par.active = True

        # Manually call Disable (same as what parexec.onValueChange does)
        self.embody_ext.Disable(prev, removeTags=False)
        self.assertEqual(self.embody.par.Status.eval(), 'Disabled')

    def test_zz_folder_02_reenable_new(self):
        """Step 2: Call UpdateHandler, verify Enabled and new folder exists."""
        self.embody_ext.UpdateHandler()
        self.assertEqual(self.embody.par.Status.eval(), 'Enabled')

        new_folder = self.embody_ext.getProjectFolder()
        self.assertTrue(os.path.isdir(new_folder),
                        f'New folder should exist: {new_folder}')

    def test_zz_folder_03_update_new_folder(self):
        """Step 3: Run Update, verify table has rows in new folder."""
        self.embody_ext.Update()
        table = self.embody_ext.Externalizations
        if self.__class__._folder_had_rows:
            self.assertGreater(table.numRows, 1,
                               'Externalizations table should have rows after Update')

    def test_zz_folder_04_restore_original(self):
        """Step 4: Change Folder back to original, call Disable, verify Disabled."""
        parexec = self.embody.op('parexec')
        parexec.par.active = False

        try:
            prev = self.embody.par.Folder.eval()
            self.embody.par.Folder.val = self.__class__._folder_original
        finally:
            parexec.par.active = True

        # Disable with the temp folder as "previous"
        self.embody_ext.Disable(prev, removeTags=False)
        self.assertEqual(self.embody.par.Status.eval(), 'Disabled')

    def test_zz_folder_05_reenable_original(self):
        """Step 5: Re-enable with original folder, verify operational."""
        self.embody_ext.UpdateHandler()
        self.assertEqual(self.embody.par.Status.eval(), 'Enabled')
        self.embody_ext.Update()

        # Verify original folder exists
        folder = self.embody_ext.getProjectFolder()
        self.assertTrue(os.path.isdir(folder),
                        f'Original folder should exist: {folder}')

        # Verify table has rows
        table = self.embody_ext.Externalizations
        if self.__class__._folder_had_rows:
            self.assertGreater(table.numRows, 1,
                               'Table should have rows after restoring original folder')

        # Clean up temp folder if empty
        temp_folder = Path(project.folder) / '_test_pars_temp'
        if temp_folder.is_dir():
            try:
                temp_folder.rmdir()
            except OSError:
                pass  # Not empty, leave it
