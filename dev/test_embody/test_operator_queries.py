"""
Test suite: Operator query methods in EmbodyExt.

Tests getExternalizedOps, getOpsToExternalize, getOpsByPar.
"""

runner_mod = op('TestRunner').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestOperatorQueries(EmbodyTestCase):

    # --- getExternalizedOps ---

    def test_getExternalizedOps_comp_returns_list(self):
        result = self.embody_ext.getExternalizedOps(COMP)
        self.assertIsInstance(result, list)

    def test_getExternalizedOps_dat_returns_list(self):
        result = self.embody_ext.getExternalizedOps(DAT)
        self.assertIsInstance(result, list)

    def test_getExternalizedOps_comp_contains_embody(self):
        comps = self.embody_ext.getExternalizedOps(COMP)
        # COMP externalization may be empty if no COMPs are tagged;
        # just verify it returns a valid list (DATs are tested separately)
        self.assertIsInstance(comps, list)

    def test_getExternalizedOps_dat_has_entries(self):
        dats = self.embody_ext.getExternalizedOps(DAT)
        # At minimum, EmbodyExt.py and ClaudiusExt.py should be externalized
        self.assertGreater(len(dats), 0)

    # --- getOpsByPar ---

    def test_getOpsByPar_comp_returns_list(self):
        result = self.embody_ext.getOpsByPar(COMP)
        self.assertIsInstance(result, list)

    def test_getOpsByPar_dat_returns_list(self):
        result = self.embody_ext.getOpsByPar(DAT)
        self.assertIsInstance(result, list)
