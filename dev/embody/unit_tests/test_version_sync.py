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

    # ------------------------------------------------------------------
    # The .tdn version lag (fixed 2026-08-08)
    # ------------------------------------------------------------------

    def _tdn_head(self, rel):
        path = Path(project.folder) / rel
        with open(path, 'r', encoding='utf-8') as handle:
            return ''.join(handle.readline() for _ in range(14))

    def test_tdn_files_carry_the_current_version(self):
        """Both Embody-covering .tdn files must stamp the version that is
        actually shipping.

        A single project.save fires onProjectPreSave on two Execute DATs.
        The Embody COMP's own DAT exports every dirty TDN row FIRST,
        reading par.Version as it stands; only then does
        /embody/execute_src_ctrl bump it and bake the .tox. So every
        release shipped a .tdn one version behind its own .tox --
        `generator: Embody/6.0.222` beside `source_file:
        Embody-6.223.toe` -- and a COMP reconstructed from that .tdn came
        up a version behind. onProjectPostSave now re-exports those rows
        after the bump.
        """
        version = str(self.embody.par.Version.eval())
        for rel in ('embody/Embody.tdn', 'embody.tdn'):
            head = self._tdn_head(rel)
            self.assertIn(
                'generator: Embody/%s' % version, head,
                '%s is stamped with a stale version (the pre-save export '
                'ran before the bump): %r' % (rel, head[:200]))

    def test_the_post_save_version_sync_hook_is_enabled(self):
        """The fix reaches production through ONE toggle. Without it the
        sync never runs and every other assertion here still passes on a
        tree that happens to be current."""
        dat = op('/embody/execute_src_ctrl')
        self.assertIsNotNone(dat, 'the build/release Execute DAT is missing')
        self.assertTrue(
            bool(dat.par.projectpostsave.eval()),
            'projectpostsave is off, so syncVersionIntoTDN never fires')
        self.assertTrue(hasattr(dat.module, 'syncVersionIntoTDN'))

    def test_the_version_sync_selects_only_embody_covering_tdn_rows(self):
        """It must re-export the rows that CONTAIN the Embody COMP, and
        never '/' -- re-exporting the whole project root on every save
        would be a far larger write than this warrants."""
        embody_path = self.embody.path
        table = self.embody_ext.Externalizations
        headers = [table[0, c].val for c in range(table.numCols)]
        path_col = headers.index('path')
        strategy_col = headers.index('strategy')
        picked = [
            str(table[r, path_col].val)
            for r in range(1, table.numRows)
            if str(table[r, strategy_col].val or '') == 'tdn'
            and str(table[r, path_col].val or '') not in ('', '/')
            and (str(table[r, path_col].val) == embody_path
                 or embody_path.startswith(str(table[r, path_col].val) + '/'))
        ]
        self.assertIn(embody_path, picked)
        self.assertNotIn('/', picked, "'/' must never be re-exported here")

    def test_save_tdn_can_re_export_without_bumping_build(self):
        """The sync runs AFTER the release manifest recorded par.Build, so
        a second bump would leave the manifest one behind the .tdn -- the
        same drift, one size smaller."""
        # Read the DAT text: inspect.getsource cannot see an extension's
        # source inside TouchDesigner.
        dat = self.embody.op('EmbodyExt')
        self.assertIsNotNone(dat, 'EmbodyExt DAT is missing')
        source = dat.text
        self.assertIn('def SaveTDN(self, opPath: str, bump_build: bool = True)',
                      source, 'SaveTDN must accept bump_build')
        self.assertIn('if bump_build and hasattr(oper.par, ', source,
                      'the Build bump must be guarded by bump_build')

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

    # ------------------------------------------------------------------
    # MCP tool count (NOT automated by the save hook -- hence this guard)
    # ------------------------------------------------------------------

    def _registered_tool_count(self):
        """Count @self.mcp.tool() wrappers in EnvoyExt.py.

        Static, so it needs no worker-thread access (the MCPServer
        object is None on the main thread). Cross-checked against the
        bridge's on-disk tools cache when this guard was written:
        58 advertised = 54 TD-side + 4 bridge meta-tools.
        """
        import ast
        src = Path(project.folder) / 'embody' / 'Embody' / 'EnvoyExt.py'
        tree = ast.parse(src.read_text(encoding='utf-8'))
        count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in getattr(node, 'decorator_list', []):
                f = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(f, ast.Attribute) and f.attr == 'tool':
                    count += 1
                    break
        return count

    def test_mcp_tool_count_claims_match_reality(self):
        """The user-facing tool count must match the registered tools.

        Unlike the version badge, updateVersionDocs does NOT rewrite this
        number, and nothing asserted it -- so it silently drifted to 53
        while 54 tools were registered (found during the v6.0.158 triage).
        Counts the TD-side tools only; the 4 bridge meta-tools are
        documented separately under "Bridge Meta-Tools".
        """
        actual = self._registered_tool_count()
        self.assertGreater(actual, 40, 'sanity: tool collector found almost '
                                       'nothing, so the assert below would '
                                       'be meaningless')
        claims = []
        readme = self._read('README.md')
        m = re.search(r'badge/MCP_tools-(\d+)-', readme)
        self.assertIsNotNone(m, 'MCP tools badge missing from README')
        claims.append(('README badge', int(m.group(1))))
        # Every published surface that states a present-tense count. The
        # first version of this guard checked only README + docs/index.md
        # and therefore missed three live claims on the MkDocs site.
        for label, text in (
                ('README.md', readme),
                ('docs/index.md', self._read('docs', 'index.md')),
                ('docs/envoy/index.md',
                 self._read('docs', 'envoy', 'index.md')),
                ('docs/envoy/tools-reference.md',
                 self._read('docs', 'envoy', 'tools-reference.md'))):
            for line in text.splitlines():
                # Release-history bullets state the count as it was for
                # THAT version ("~34 tools" in the v6.0.145 entry) and must
                # never be rewritten -- only present-tense claims count.
                if line.lstrip().startswith('- **'):
                    continue
                for n in re.findall(r'\b(\d+) (?:MCP )?tools\b', line):
                    claims.append((f'{label} prose', int(n)))
        wrong = [(where, n) for where, n in claims if n != actual]
        self.assertEqual(
            [], wrong,
            f'{len(wrong)} user-facing tool-count claim(s) disagree with the '
            f'{actual} registered tools: {wrong}')

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
