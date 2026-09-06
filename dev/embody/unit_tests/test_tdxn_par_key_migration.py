"""Config.json key migration for the TDN -> TDXN parameter rename.

config.json is keyed by PARAMETER NAME. Renaming a parameter without
mapping its stored key drops that setting back to its default on every
existing install -- silently, because a missing key is indistinguishable
from "never set". This suite pins that mapping against a REAL legacy
config captured before the rename (dev/release_testing/.embody/config.json,
13 TDN-era keys).

Pure-Python: runs under pytest/CI, skipped in TD.
"""
import json
import os
import unittest

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..'))
LEGACY_CONFIG = os.path.join(
    REPO_ROOT, 'dev', 'release_testing', '.embody', 'config.json')

# Mirrors EmbodyExt._TDXN_PAR_RENAMES. A test asserts the two agree when
# the extension source is readable, so this cannot drift silently.
RENAMES = {
    'Tdntags': 'Tdxntags', 'Tdntagcolor': 'Tdxntagcolor', 'Tdntag': 'Tdxntag',
    'Tdnexcludetag': 'Tdxnexcludetag', 'Tdnmode': 'Tdxnmode',
    'Embeddatsintdns': 'Embeddatsintdxns',
    'Embedstorageintdns': 'Embedstorageintdxns',
    'Tdncascade': 'Tdxncascade', 'Tdncascadewarn': 'Tdxncascadewarn',
    'Tdnlockedwarn': 'Tdxnlockedwarn', 'Tdncreateonstart': 'Tdxncreateonstart',
    'Tdnstriponsave': 'Tdxnstriponsave',
    'Tdnpalettehandling': 'Tdxnpalettehandling', 'Tdnfile': 'Tdxnfile',
    'Importtdn': 'Importtdxn', 'Tdnsavedcolor': 'Tdxnsavedcolor',
    'Shortcutcopytdn': 'Shortcutcopytdxn', 'Recordcopytdn': 'Recordcopytdxn',
    'Tdndatsafety': 'Tdxndatsafety',
}


def _normalize(params, renames):
    """Copy of embody_admin.normalize_legacy_par_keys, importable without TD."""
    if not renames:
        return params
    out = {}
    for key, entry in params.items():
        base, suffix = key, ''
        if (key not in renames
                and key[-1:] in ('r', 'g', 'b', 'a')
                and key[:-1] in renames):
            base, suffix = key[:-1], key[-1]
        out[renames.get(base, base) + suffix] = entry
    return out


class TestTdxnParKeyMigration(unittest.TestCase):

    def test_the_copy_here_matches_the_extension_source(self):
        """RENAMES must not drift from EmbodyExt._TDXN_PAR_RENAMES."""
        import ast
        src_path = os.path.join(REPO_ROOT, 'dev', 'embody', 'Embody',
                                'EmbodyExt.py')
        if not os.path.exists(src_path):
            self.skipTest('EmbodyExt.py not readable here')
        with open(src_path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == '_TDXN_PAR_RENAMES':
                        found = ast.literal_eval(node.value)
        self.assertIsNotNone(found, '_TDXN_PAR_RENAMES not found in EmbodyExt')
        self.assertEqual(found, RENAMES)

    def test_no_entry_is_a_no_op(self):
        """A key that maps to itself means the migration never fires."""
        noop = [k for k, v in RENAMES.items() if k == v]
        self.assertEqual(noop, [], 'identity entries neuter the migration')

    def test_tdnenable_is_never_renamed(self):
        """Pre-6.1 key the mode-migration nudge still reads."""
        self.assertNotIn('Tdnenable', RENAMES)
        out = _normalize({'Tdnenable': {'val': True}}, RENAMES)
        self.assertEqual(list(out), ['Tdnenable'])

    def test_a_real_legacy_config_migrates_completely(self):
        """The captured pre-rename config must carry every setting over."""
        if not os.path.exists(LEGACY_CONFIG):
            self.skipTest('legacy config fixture not present')
        with open(LEGACY_CONFIG, encoding='utf-8') as fh:
            params = json.load(fh).get('params', {})
        legacy_keys = [k for k in params
                       if k in RENAMES
                       or (k[-1:] in 'rgba' and k[:-1] in RENAMES)]
        self.assertTrue(legacy_keys, 'fixture has no TDN-era keys to migrate')

        out = _normalize(params, RENAMES)

        self.assertEqual(len(out), len(params), 'a setting was lost or merged')
        for k in legacy_keys:
            self.assertNotIn(k, out, f'{k} was not migrated')
        for k in params:
            if k not in legacy_keys:
                self.assertIn(k, out, f'{k} should have passed through')

    def test_values_survive_the_key_rename(self):
        params = {
            'Tdnmode': {'val': 'full'},
            'Tdntag': {'val': 'tdn'},
            'Tdntagcolorr': {'val': 0.3},
            'Tdntagcolorg': {'val': 0.5},
            'Folder': {'val': 'embody'},
        }
        out = _normalize(params, RENAMES)
        self.assertEqual(out['Tdxnmode']['val'], 'full')
        self.assertEqual(out['Tdxntag']['val'], 'tdn')
        self.assertEqual(out['Tdxntagcolorr']['val'], 0.3)
        self.assertEqual(out['Tdxntagcolorg']['val'], 0.5)
        self.assertEqual(out['Folder']['val'], 'embody')

    def test_migrating_twice_is_stable(self):
        params = {'Tdnmode': {'val': 'export'}, 'Tdntagcolorb': {'val': 0.9}}
        once = _normalize(params, RENAMES)
        twice = _normalize(once, RENAMES)
        self.assertEqual(once, twice)


if __name__ == '__main__':
    unittest.main()
