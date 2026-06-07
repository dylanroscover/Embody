"""Enforcement: no cached extension references anywhere in Embody source.

Hard project rule (.claude/rules/embody-code-conventions.md): never store a TD
extension in a variable; always reference it inline
(op.Embody.ext.Embody.method(), self.ownerComp.ext.TDN.method(), ...). Cached
refs go stale when TD reinitializes an extension (e.g. when its externalized
.py changes on disk), silently pointing at a dead instance.

This test scans the Embody production source and the test suite for the
anti-pattern and fails on any new occurrence. The single sanctioned exception
-- resolving an extension on the main thread to hand to a worker thread, where
inlining would be a thread conflict -- must carry a trailing `# ext-cache-ok`
marker on the assignment line.
"""

import os
import re

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# Matches an assignment whose entire right-hand side is a bare extension
# reference (X = a.b.ext.SomeExt) or a bound method of one
# (X = a.b.ext.SomeExt._method) -- i.e. the value IS the extension/its method,
# not a value returned by calling it. Inline calls like
#   result = self.ownerComp.ext.TDN.ExportNetwork(...)
# and DAT caches like
#   table = op.Embody.ext.Embody.Externalizations
# do NOT match (they have a call or a non-underscore attribute after the class).
_EXT_CACHE = re.compile(
    r"^\s*[\w.\[\]]+\s*=\s*[\w.]+\.ext\.[A-Z]\w*(?:\._\w+)?\s*(?:#.*)?$")

_SKIP_FILENAMES = {'test_no_ext_caching.py'}


class TestNoExtCaching(EmbodyTestCase):

    def _scan_dir(self, root):
        violations = []
        for dirpath, dirnames, filenames in os.walk(root):
            if '__pycache__' in dirpath:
                continue
            for fn in filenames:
                if not fn.endswith('.py') or fn in _SKIP_FILENAMES:
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        lines = f.read().splitlines()
                except Exception:
                    continue
                for i, line in enumerate(lines, 1):
                    if 'ext-cache-ok' in line:
                        continue
                    if _EXT_CACHE.match(line):
                        rel = os.path.relpath(path, root)
                        violations.append(f'{rel}:{i}: {line.strip()}')
        return violations

    def test_no_ext_caching_in_production(self):
        root = project.folder + '/embody/Embody'
        violations = self._scan_dir(root)
        self.assertEqual(
            violations, [],
            'Cached extension references found in production code. Reference '
            'extensions inline (op.Embody.ext.X.method()), never via a '
            'variable. If a main-thread->worker handoff genuinely requires it, '
            'add a trailing "# ext-cache-ok" marker.\n  ' +
            '\n  '.join(violations))

    def test_no_ext_caching_in_tests(self):
        root = project.folder + '/embody/unit_tests'
        violations = self._scan_dir(root)
        self.assertEqual(
            violations, [],
            'Cached extension references found in test code. Reference '
            'extensions inline (self.embody.ext.X.method()), never via a '
            'variable (no self.envoy = ..., emb = ..., etc.).\n  ' +
            '\n  '.join(violations))
