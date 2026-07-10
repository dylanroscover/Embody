"""
Test suite: version / minimum-build doc sync.

Guards the user-facing version statements that onProjectPreSave
(execute_src_ctrl.updateVersionDocs) keeps in lock-step on every save:
the README version badge must match par.Version, and the minimum-TD-build
statements in README.md, docs/index.md, and CONTRIBUTING.md must agree
with each other and with par.Touchbuild (the build of the last save --
which IS the support floor, since TD files do not open in older builds).

Also drives the pure line-rewriter directly so the anchored-substitution
logic is covered without touching files.
"""

import re
from pathlib import Path


runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


BUILD_RE = re.compile(r'\b\d{4}\.\d{3,6}\b')

README_ANCHOR = '**Requirements:** TouchDesigner'
DOCS_ANCHOR = '- **TouchDesigner '
CONTRIB_ANCHOR = '- **TouchDesigner '


class TestVersionSync(EmbodyTestCase):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _repo_root(self):
        return Path(project.folder).resolve().parent

    def _read(self, *parts):
        path = self._repo_root().joinpath(*parts)
        self.assertTrue(path.is_file(), f'missing doc file: {path}')
        return path.read_text(encoding='utf-8')

    def _min_build(self, text, anchor, label):
        for line in text.splitlines():
            if line.startswith(anchor):
                match = BUILD_RE.search(line)
                self.assertIsNotNone(
                    match, f'{label}: anchored line has no build number: {line!r}')
                return match.group(0)
        self.fail(f'{label}: anchor line not found: {anchor!r}')

    def _src_ctrl(self):
        return opex('/embody/execute_src_ctrl').module

    # ------------------------------------------------------------------
    # Doc consistency (the tripwire)
    # ------------------------------------------------------------------

    def test_readme_badge_matches_par_version(self):
        text = self._read('README.md')
        match = re.search(r'badge/version-([0-9][0-9.]*)-', text)
        self.assertIsNotNone(match, 'version badge missing from README')
        self.assertEqual(
            match.group(1), op.Embody.par.Version.eval(),
            'README version badge out of sync with par.Version -- '
            'updateVersionDocs should rewrite it on every save')

    def test_min_build_consistent_across_docs(self):
        readme = self._min_build(self._read('README.md'), README_ANCHOR, 'README.md')
        docs = self._min_build(self._read('docs', 'index.md'), DOCS_ANCHOR, 'docs/index.md')
        contrib = self._min_build(self._read('CONTRIBUTING.md'), CONTRIB_ANCHOR, 'CONTRIBUTING.md')
        self.assertEqual(readme, docs, 'README vs docs/index.md minimum build drift')
        self.assertEqual(readme, contrib, 'README vs CONTRIBUTING.md minimum build drift')

    def test_min_build_matches_touchbuild(self):
        readme = self._min_build(self._read('README.md'), README_ANCHOR, 'README.md')
        self.assertEqual(
            readme, op.Embody.par.Touchbuild.eval(),
            'documented minimum build out of sync with par.Touchbuild '
            '(the build of the last save is the support floor)')

    # ------------------------------------------------------------------
    # Rewriter unit coverage (pure, no file writes)
    # ------------------------------------------------------------------

    def test_rewriter_updates_anchored_line_only(self):
        m = self._src_ctrl()
        text = (
            '**Requirements:** TouchDesigner **2025.11111 or later** blah\n'
            'history mentions TD 2025.22222 and must not change\n'
        )
        transforms = [(README_ANCHOR, lambda l: m.BUILD_RE.sub('2025.33333', l, count=1))]
        new_text, changed = m._rewriteText(text, transforms)
        self.assertTrue(changed)
        self.assertIn('2025.33333', new_text)
        self.assertIn('2025.22222', new_text,
                      'non-anchored line must never be rewritten')
        self.assertNotIn('2025.11111', new_text)

    def test_rewriter_reports_unchanged(self):
        m = self._src_ctrl()
        text = 'no anchored lines here\n'
        transforms = [(README_ANCHOR, lambda l: 'REWRITTEN\n')]
        new_text, changed = m._rewriteText(text, transforms)
        self.assertFalse(changed)
        self.assertEqual(new_text, text)

    def test_badge_regexes_hit_current_readme(self):
        # The anchors/regexes in updateVersionDocs must keep matching the
        # real README -- if the badge markup is ever restyled, this fails
        # instead of the bumper silently no-oping (the pre-2026 failure
        # mode this suite exists to prevent).
        text = self._read('README.md')
        lines = text.splitlines()
        self.assertTrue(
            any(l.startswith('[![Version](https://img.shields.io/badge/version-')
                for l in lines),
            'README version badge anchor no longer matches')
        self.assertTrue(
            any(l.startswith(README_ANCHOR) for l in lines),
            'README requirements anchor no longer matches')
