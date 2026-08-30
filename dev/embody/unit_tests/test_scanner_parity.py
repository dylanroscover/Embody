"""
Test suite: TDXN capability-scanner CROSS-IMPLEMENTATION parity (contract C8).

`platform/SCANNER-SPEC.md` freezes one requirement that had never been
executed: the scanner is implemented TWICE -- `packages/scanner-ts`
(server-side, on submit AND download) and `Embody/Collection/scanner.py`
(Embody-side, at import) -- and the two "MUST produce the SAME verdict +
counts on the shared fixtures". Neither fixture directory existed, so two
independent implementations of a security scanner had never been compared.

This suite is the Python half. `scanner-ts/src/parity.test.ts` is the other
half; both read the SAME corpus, so a drift on either side turns a test red.

The divergence ledger is deliberately fail-loud in BOTH directions: a NEW
disagreement fails, and so does FIXING a declared one (which then has to be
removed from the fixture). A known gap can therefore never go quiet.

Pure Python (TD-import-free) -- see test_embody_pyenv.py for the same
pytest-only convention.
"""

import importlib.util
import io
import json
import os
import sys

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_IN_TD = 'td' in sys.modules

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_FIXTURES = os.path.join(_HERE, 'fixtures')
_TS_FIXTURES = os.path.join(_REPO, 'platform', 'packages', 'scanner-ts', 'fixtures')

_SCANNER_PATH = os.path.join(os.path.dirname(_HERE), 'Embody', 'Collection', 'scanner.py')
_spec = importlib.util.spec_from_file_location('scanner_under_parity', _SCANNER_PATH)
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)

COUNT_KEYS = ('execute_dats', 'file_read_exprs', 'web_ops', 'extensions',
              'storage_payloads', 'denylisted_types', 'traversal_paths',
              'external_refs')


def _load_fixtures():
    out = []
    for name in sorted(os.listdir(_FIXTURES)):
        if name.endswith('.json'):
            with io.open(os.path.join(_FIXTURES, name), encoding='utf-8') as fh:
                out.append(json.load(fh))
    return out


def _counts(result):
    return {k: result['counts'].get(k, 0) for k in COUNT_KEYS}


class TestScannerParityCorpus(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only '
                          '(in-TD execution adds no coverage)')

    def test_the_corpus_is_not_empty(self):
        """A silently empty corpus would make every other test vacuous."""
        fixtures = _load_fixtures()
        self.assertGreaterEqual(len(fixtures), 20,
                                'the shared corpus lost fixtures')

    def test_every_surface_is_covered(self):
        """C8 names eight surfaces; a corpus missing one proves nothing there."""
        seen = set()
        for fx in _load_fixtures():
            seen.update(fx.get('surfaces') or [])
        missing = sorted(set(COUNT_KEYS) - seen)
        self.assertEqual([], missing,
                         'no fixture exercises: %s' % ', '.join(missing))

    def test_python_scanner_matches_its_recorded_expectation(self):
        for fx in _load_fixtures():
            with self.subTest(fixture=fx['name']):
                result = scanner.scan_tdn(fx['tdn'])
                self.assertEqual(fx['expect_py']['verdict'], result['verdict'],
                                 '%s: verdict drifted' % fx['name'])
                self.assertEqual(fx['expect_py']['counts'], _counts(result),
                                 '%s: counts drifted' % fx['name'])

    def test_the_divergence_ledger_is_exact(self):
        """Fail-loud both ways: a new divergence, or a fixed one left declared."""
        for fx in _load_fixtures():
            same = fx['expect_py'] == fx['expect_ts']
            declared = bool(fx.get('divergence'))
            if same and declared:
                self.fail('%s: expectations now AGREE but the fixture still '
                          'declares a divergence -- delete the `divergence` '
                          'field, the gap is closed.' % fx['name'])
            if not same and not declared:
                self.fail('%s: python and typescript expectations DISAGREE '
                          'with no `divergence` note. C8 requires identical '
                          'verdict+counts; either fix the scanner or declare '
                          'the gap explicitly.' % fx['name'])

    def test_the_mirrored_corpus_is_identical(self):
        """The two directories are one corpus; a one-sided edit is a silent hole."""
        for fx_name in sorted(os.listdir(_FIXTURES)):
            if not fx_name.endswith('.json'):
                continue
            with self.subTest(fixture=fx_name):
                # Bytes, not text: universal newlines would hide a CRLF drift.
                with open(os.path.join(_FIXTURES, fx_name), 'rb') as fa:
                    a = fa.read()
                with open(os.path.join(_TS_FIXTURES, fx_name), 'rb') as fb:
                    b = fb.read()
                self.assertEqual(a, b, '%s drifted between the two fixture '
                                       'directories' % fx_name)
        ours = {n for n in os.listdir(_FIXTURES) if n.endswith('.json')}
        theirs = {n for n in os.listdir(_TS_FIXTURES) if n.endswith('.json')}
        self.assertEqual(ours, theirs, 'fixture directories hold different files')
