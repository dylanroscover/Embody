"""
Test suite: File management methods in EmbodyExt.

Tests safeDeleteFile, isTrackedFile, getTrackedFilePaths.
"""

import os
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestFileManagement(EmbodyTestCase):

    # --- getTrackedFilePaths ---

    def test_getTrackedFilePaths_returns_set(self):
        result = self.embody_ext.getTrackedFilePaths()
        self.assertIsInstance(result, set)

    def test_getTrackedFilePaths_has_entries(self):
        result = self.embody_ext.getTrackedFilePaths()
        self.assertGreater(len(result), 0)

    def test_getTrackedFilePaths_entries_are_paths(self):
        result = self.embody_ext.getTrackedFilePaths()
        for p in result:
            self.assertIsInstance(p, Path)

    # --- isTrackedFile ---

    def test_isTrackedFile_known_tracked_file(self):
        tracked = self.embody_ext.getTrackedFilePaths()
        if tracked:
            first = next(iter(tracked))
            self.assertTrue(self.embody_ext.isTrackedFile(str(first)))

    def test_isTrackedFile_untracked_path(self):
        self.assertFalse(self.embody_ext.isTrackedFile('/nonexistent/fake/file.tox'))

    # --- safeDeleteFile ---

    def test_safeDeleteFile_nonexistent_returns_false(self):
        # Short-circuits at is_file() before the tracked-file guard.
        result = self.embody_ext.safeDeleteFile('/nonexistent/fake/file.tox')
        self.assertFalse(result)

    def test_safeDeleteFile_existing_untracked_is_refused(self):
        """The actual data-loss guard: an EXISTING file that Embody does not
        track must NOT be deleted without force, and must survive the call.
        (The nonexistent-path test never reaches this branch.)"""
        temp_dir = Path(project.folder) / 'embody' / 'unit_tests' / '_test_temp'
        temp_dir.mkdir(parents=True, exist_ok=True)
        untracked = temp_dir / 'untracked_keepme.txt'
        untracked.write_text('not tracked by embody')
        try:
            self.assertFalse(self.embody_ext.isTrackedFile(str(untracked)))
            result = self.embody_ext.safeDeleteFile(str(untracked))  # no force
            self.assertFalse(result, 'must refuse to delete an untracked file')
            self.assertTrue(untracked.exists(),
                'untracked file must survive a non-forced safeDeleteFile')
        finally:
            if untracked.exists():
                untracked.unlink()
            try:
                temp_dir.rmdir()
            except OSError:
                pass

    def test_safeDeleteFile_force_on_temp_file(self):
        # Create a temp file and force-delete it
        temp_dir = Path(project.folder) / 'embody' / 'unit_tests' / '_test_temp'
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / 'delete_me.txt'
        temp_file.write_text('test')
        self.assertTrue(temp_file.exists())

        result = self.embody_ext.safeDeleteFile(str(temp_file), force=True)
        self.assertTrue(result)
        self.assertFalse(temp_file.exists())

        # Cleanup dir
        try:
            temp_dir.rmdir()
        except OSError:
            pass
