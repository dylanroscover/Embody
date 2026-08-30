"""
Test suite: every committed TDXN document validates against the shipped schema.

docs/tdn.schema.yaml is contract C7 -- docs/tdxn/schema.md tells users to wire
it into their editor. Until 2026-08-30 nothing ever ran it: the exporter had
grown annotation `backAlpha`/`titleHeight`/`bodyFontSize` and custom-par
`sequence` fields that the schema rejected (additionalProperties: false), so
Embody's own receipts failed Embody's own schema (TDXN review 2026-08-30).

Pure Python (needs `jsonschema` + `pyyaml` from requirements-test.txt); runs
on both CI legs, skips in-TD.
"""

import io
import os
import sys

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_IN_TD = 'td' in sys.modules
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_SCHEMA = os.path.join(_REPO, 'docs', 'tdn.schema.yaml')
_ROOTS = (os.path.join(_REPO, 'dev'), os.path.join(_REPO, 'specimens'))


def _documents():
    for root in _ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            # never wander into venvs, backups or node trees
            dirnames[:] = [d for d in dirnames
                           if not d.startswith('.') and d != 'node_modules']
            for name in filenames:
                if name.endswith(('.tdn', '.tdxn')):
                    yield os.path.join(dirpath, name)


class TestTDXNSchema(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        if _IN_TD:
            self.skipTest('pure-Python suite -- runs under pytest/CI only')
        import yaml
        import jsonschema
        with io.open(_SCHEMA, encoding='utf-8') as fh:
            self.schema = yaml.safe_load(fh)
        self.validator = jsonschema.Draft202012Validator(self.schema)
        self.yaml = yaml

    def test_the_schema_itself_is_valid(self):
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(self.schema)

    def test_every_committed_document_validates(self):
        docs = list(_documents())
        self.assertGreaterEqual(len(docs), 20, 'the corpus lost files')
        failures = []
        for path in docs:
            with io.open(path, encoding='utf-8') as fh:
                doc = self.yaml.safe_load(fh)
            errs = sorted(self.validator.iter_errors(doc), key=lambda e: list(e.path))
            for e in errs[:3]:
                where = '/'.join(str(p) for p in e.path) or '<root>'
                failures.append('%s @ %s: %s' % (
                    os.path.relpath(path, _REPO), where, e.message[:120]))
        self.assertEqual([], failures,
                         'documents Embody wrote fail the schema Embody ships:\n'
                         + '\n'.join(failures))
