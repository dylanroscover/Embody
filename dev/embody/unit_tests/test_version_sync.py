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

import os
import re
from pathlib import Path


runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


BUILD_RE = re.compile(r'\b\d{4}\.\d{3,6}\b')

README_ANCHOR = '**Requirements:** TouchDesigner'
DOCS_ANCHOR = '- **TouchDesigner '
CONTRIB_ANCHOR = '- **TouchDesigner '

# embody.tools states the support floor too. It is a single exported
# constant so ONE anchored line keeps the whole site in sync.
SITE_ANCHOR = 'export const MIN_TD_BUILD'
SITE_BUILD_PARTS = ('platform', 'apps', 'web', 'src', 'lib', 'tdBuild.ts')
SITE_SRC_PARTS = ('platform', 'apps', 'web', 'src')

# Hand-maintained machine files served by the site. They are not rewritten by
# the save hook (they are prose/JSON audited at release), so they get a loud
# tripwire instead of silent drift.
SITE_MACHINE_FILES = (
    ('platform', 'apps', 'web', 'public', 'for-ai.json'),
    ('platform', 'apps', 'web', 'public', 'for-ai', 'index.html'),
    ('platform', 'apps', 'web', 'public', 'llms-full.txt'),
)

# A requirement statement that hard-codes a build, e.g.
# 'TouchDesigner 2025.32280 or later'. Anywhere but tdBuild.ts this is drift.
SITE_LITERAL_RE = re.compile(r'TouchDesigner\s+\d{4}\.\d{3,6}')


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
        would be a far larger write than this warrants.

        Drives the REAL syncVersionIntoTDN and records what it asks to be
        written. Re-implementing the row filter here instead would pass
        even if the function were deleted, which is what it used to do.
        """
        embody_path = self.embody.path
        ext = self.embody_ext
        calls = []
        # Shadow on the INSTANCE, restored by `del` (the class attribute is
        # untouched). buildAbsolutePath is shadowed too so the idempotency
        # skip cannot fire: the live .tdn files are already stamped with the
        # current version, so every row would otherwise be skipped and this
        # would assert on an empty list.
        ext.SaveTDN = lambda path, bump_build=True: calls.append(
            (path, bump_build))
        ext.buildAbsolutePath = lambda rel: Path('/no/such/tdn/file')
        try:
            op('/embody/execute_src_ctrl').module.syncVersionIntoTDN()
        finally:
            del ext.SaveTDN
            del ext.buildAbsolutePath
        picked = [path for path, _bump in calls]
        self.assertIn(embody_path, picked,
                      'the row holding the Embody COMP must be re-exported')
        self.assertNotIn('/', picked, "'/' must never be re-exported here")
        for path in picked:
            self.assertTrue(
                path == embody_path or embody_path.startswith(path + '/'),
                f'{path} does not contain the Embody COMP')

    def test_the_version_sync_never_bumps_the_build(self):
        """The sync runs AFTER the release manifest recorded par.Build, so
        a second bump would leave the manifest one behind the .tdn -- the
        same drift, one size smaller."""
        ext = self.embody_ext
        calls = []
        ext.SaveTDN = lambda path, bump_build=True: calls.append(
            (path, bump_build))
        ext.buildAbsolutePath = lambda rel: Path('/no/such/tdn/file')
        try:
            op('/embody/execute_src_ctrl').module.syncVersionIntoTDN()
        finally:
            del ext.SaveTDN
            del ext.buildAbsolutePath
        self.assertTrue(calls, 'the sync re-exported nothing to assert on')
        for path, bump in calls:
            self.assertIs(bump, False,
                          f'{path} was re-exported with the Build bump on')

    def test_save_tdn_honours_bump_build_on_a_comp_that_has_one(self):
        """The flag has to reach par.Build, not merely exist in the signature.

        A sandbox COMP with its own Build par is the only way to observe
        this without moving the real Embody build number.
        """
        ext = self.embody_ext
        comp = self.sandbox.create(baseCOMP, 'bumpguard')
        comp.create(noiseTOP, 'n1')
        page = comp.appendCustomPage('Test')
        page.appendInt('Build')[0].val = 5
        ext.applyTagToOperator(comp, 'tdn')
        ext.ExternalizeImmediate(comp)
        rel = ext._getStrategyFilePath(comp.path, 'tdn')
        abs_tdn = str(ext.buildAbsolutePath(rel)) if rel else None
        try:
            # Baseline AFTER externalization: the first export writes the file
            # and advances the counter itself, so the starting value is
            # whatever that left behind, not the 5 seeded above.
            base = comp.par.Build.eval()
            ext.SaveTDN(comp.path, bump_build=False)
            self.assertEqual(comp.par.Build.eval(), base,
                             'bump_build=False still advanced par.Build')
            ext.SaveTDN(comp.path, bump_build=True)
            self.assertEqual(comp.par.Build.eval(), base + 1,
                             'the default must still bump par.Build')
        finally:
            try:
                ext._removeTDNStrategy(comp.path, delete_file=True)
            except Exception:
                pass
            if abs_tdn and os.path.isfile(abs_tdn):
                try:
                    os.remove(abs_tdn)
                except OSError:
                    pass

    def test_version_target_is_reached_or_empty(self):
        """A VERSION_TARGET left armed BELOW the current version would jump
        the version on some later, unrelated save. Once reached it is inert,
        so 'already reached' is the safe resting state -- not 'must be
        cleared'."""
        target = getattr(self._src_ctrl(), 'VERSION_TARGET', '') or ''
        target = str(target).strip()
        if not target:
            return
        to_tuple = self._src_ctrl()._versionTuple
        target_t = to_tuple(target)
        self.assertIsNotNone(
            target_t,
            f'VERSION_TARGET {target!r} is not X.Y.Z -- it would be silently '
            'ignored, and if ever stamped the updater could not order it')
        current_t = to_tuple(op.Embody.par.Version.eval())
        self.assertIsNotNone(current_t, 'par.Version is not X.Y.Z')
        self.assertGreaterEqual(
            current_t, target_t,
            f'VERSION_TARGET {target!r} is still ARMED above the current '
            f'version {op.Embody.par.Version.eval()!r} -- the next save will '
            'jump straight to it. Clear it, or intend that jump.')

    def test_version_target_never_oscillates(self):
        """Pure-logic guard on the bump rule itself: the target must fire
        only while STRICTLY below it. Comparing for inequality instead sent
        6.1.0 -> 6.1.1 -> back to 6.1.0 forever (caught 2026-08-26)."""
        to_tuple = self._src_ctrl()._versionTuple
        self.assertIsNone(to_tuple('6.1.-1'),
                          "'6.1.-1' must not parse -- int() accepts it but "
                          "the updater's \\d+ regex does not")
        self.assertIsNone(to_tuple('6.1'))
        self.assertIsNone(to_tuple('garbage'))
        self.assertEqual(to_tuple('v6.1.0'), (6, 1, 0))
        self.assertLess(to_tuple('6.0.280'), to_tuple('6.1.0'))
        # Once at or past the target the rule must be inert.
        self.assertFalse(to_tuple('6.1.1') < to_tuple('6.1.0'))
        self.assertFalse(to_tuple('6.1.0') < to_tuple('6.1.0'))

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
    # embody.tools (the site is a separate deploy -- it drifted silently)
    # ------------------------------------------------------------------

    def test_site_min_build_matches_docs(self):
        """The landing page restates the support floor.

        It sat at 2025.32280 while every doc said 2025.33070 -- the site
        hard-coded the literal and nothing was watching. It now reads one
        constant that updateVersionDocs rewrites on every save.
        """
        readme = self._min_build(self._read('README.md'), README_ANCHOR, 'README.md')
        site = self._min_build(
            self._read(*SITE_BUILD_PARTS), SITE_ANCHOR, 'web/src/lib/tdBuild.ts')
        self.assertEqual(
            readme, site,
            'README vs embody.tools minimum build drift -- updateVersionDocs '
            'should rewrite the MIN_TD_BUILD constant on every save')

    def test_site_states_no_hard_coded_build(self):
        """No site source may restate the build as a literal.

        That is exactly how the landing page went stale: a second copy of
        the number with no sync path. Every requirement statement must
        interpolate MIN_TD_BUILD instead.
        """
        src = self._repo_root().joinpath(*SITE_SRC_PARTS)
        self.assertTrue(src.is_dir(), f'missing site source dir: {src}')
        allowed = self._repo_root().joinpath(*SITE_BUILD_PARTS)
        offenders = []
        for path in src.rglob('*'):
            if not path.is_file() or path.suffix not in (
                    '.astro', '.ts', '.tsx', '.js', '.jsx', '.json', '.md'):
                continue
            if path == allowed:
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if SITE_LITERAL_RE.search(line):
                    rel = path.relative_to(self._repo_root()).as_posix()
                    offenders.append(f'{rel}:{i}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'hard-coded TouchDesigner build in site source -- import '
            'MIN_TD_BUILD from lib/tdBuild.ts instead:\n' + '\n'.join(offenders))

    def test_site_machine_files_state_the_current_floor(self):
        """The served machine files (for-ai.*, llms-full.txt) are audited by
        hand, not rewritten by the save hook -- so assert every requirement
        statement in them names the current floor rather than trusting the
        audit to catch it."""
        readme = self._min_build(self._read('README.md'), README_ANCHOR, 'README.md')
        stale = []
        for parts in SITE_MACHINE_FILES:
            text = self._read(*parts)
            label = '/'.join(parts)
            for i, line in enumerate(text.splitlines(), 1):
                for match in SITE_LITERAL_RE.finditer(line):
                    build = BUILD_RE.search(match.group(0)).group(0)
                    if build != readme:
                        stale.append(f'{label}:{i}: {line.strip()}')
        self.assertEqual(
            stale, [],
            f'machine file states a stale minimum build (current {readme}):\n'
            + '\n'.join(stale))

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

    # ------------------------------------------------------------------
    # Local pytest must run on TouchDesigner's own Python
    # ------------------------------------------------------------------

    def _conftest_target(self):
        """The TD_PYTHON_TARGET tuple declared in the pytest shim."""
        text = self._read('dev', 'embody', 'unit_tests', 'conftest.py')
        m = re.search(r'TD_PYTHON_TARGET\s*=\s*\((\d+),\s*(\d+)\)', text)
        self.assertIsNotNone(
            m, 'conftest.py no longer declares TD_PYTHON_TARGET -- the local '
               'pytest interpreter guard has been removed or renamed')
        return (int(m.group(1)), int(m.group(2)))

    def test_pytest_target_matches_this_touchdesigner(self):
        """The declared target must equal the Python TD actually ships.

        This is the test that keeps the pin honest: the moment a TD upgrade
        moves the interpreter, this fails and names both halves, instead of
        local pytest quietly drifting onto a version nothing else uses.
        """
        import sys as _sys
        live = _sys.version_info[:2]
        declared = self._conftest_target()
        self.assertEqual(
            declared, live,
            'conftest.TD_PYTHON_TARGET is %d.%d but this TouchDesigner runs '
            'Python %d.%d -- update TD_PYTHON_TARGET and the python-version '
            'in .github/workflows/bridge-tests.yml together'
            % (declared + live))

    def test_ci_python_matches_the_pytest_target(self):
        """CI pins an interpreter too; it must be the same one."""
        workflow = self._read('.github', 'workflows', 'bridge-tests.yml')
        found = re.findall(r"python-version:\s*'([\d.]+)'", workflow)
        self.assertTrue(found, 'bridge-tests.yml pins no python-version')
        declared = '%d.%d' % self._conftest_target()
        for pinned in found:
            self.assertEqual(
                pinned, declared,
                'CI pins Python %s but conftest.TD_PYTHON_TARGET is %s -- '
                'local, CI and TD must all agree' % (pinned, declared))

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
