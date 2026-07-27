"""
Test suite: config writers across a VERSION BUMP (the migration axis).

Embody's repo-config writers (.gitignore, .gitattributes) are idempotent
within a single run, and every existing test exercised exactly that: write
once, assert the result. The defect class they cannot see is what happens
on the SECOND run, after a release adds a new managed entry -- which is how
a user actually experiences these functions.

That gap shipped the v6.0.157 duplicate-header bug: configure_gitignore
computed only the MISSING entries but wrote a fresh
`# Embody / Envoy (auto-managed)` header unconditionally, so every release
that added an entry appended another identical header block. Embody's own
repo accumulated three.

These tests run the writers repeatedly against a throwaway directory and
assert properties that only a multi-run sequence can check.
"""

import shutil
import tempfile
from pathlib import Path

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


HEADER = '# Embody / Envoy (auto-managed)'


class TestConfigMigration(EmbodyTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='embody_cfgmig_'))
        self.gitignore = self.tmp / '.gitignore'
        # _guardFileWrite applies silently for a consented batch; without
        # this an Advanced-mode project would DEFER the write and these
        # tests would assert against an unwritten file.
        self._prev_consent = self.embody_ext._consent_bulk
        self.embody_ext._consent_bulk = True
        # The config writers record every file they append to in the LIVE
        # project's uninstall manifest, keyed off _findProjectRoot() --
        # which is the REAL project, not our temp dir. Unstubbed, each run
        # permanently added an absolute temp path to that manifest (21 dead
        # entries had accumulated before this stub was added). Uninstall
        # walks that list, so polluting it corrupts the uninstall contract.
        self._prev_manifest = self.embody_ext._manifestRecordAppendedFile
        self.embody_ext._manifestRecordAppendedFile = (
            lambda *a, **k: None)

    def tearDown(self):
        # Restore unconditionally -- a failing assertion must never leave
        # the live project permanently consented or unmanifested.
        try:
            self.embody_ext._consent_bulk = self._prev_consent
            self.embody_ext._manifestRecordAppendedFile = self._prev_manifest
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)
            super().tearDown()

    # --- helpers ---------------------------------------------------------

    def _configure(self):
        # Reach the module through the Embody COMP: bare `mod.envoy_setup`
        # resolves relative to the TEST DAT, which cannot see Embody's
        # internal DATs.
        setup = op.Embody.op('envoy_setup').module
        setup.configure_gitignore(self.embody.ext.Envoy, self.tmp)

    def _text(self):
        return self.gitignore.read_text(encoding='utf-8')

    def _lines(self):
        return self._text().splitlines()

    def _header_count(self):
        return sum(1 for ln in self._lines() if ln.strip().startswith(HEADER))

    # --- the migration property ------------------------------------------

    def test_repeated_runs_never_add_a_second_header(self):
        """THE REGRESSION: a later run that has new entries to add must
        extend the existing block, not start another one."""
        self._configure()
        self.assertEqual(1, self._header_count(),
                         'first run writes exactly one header')
        first = self._text()

        self._configure()
        self.assertEqual(first, self._text(),
                         'a second run with nothing to do must not touch '
                         'the file at all')
        self.assertEqual(1, self._header_count())

        # Simulate the reported sequence: a release adds a managed entry
        # the user's file does not have yet (here, by removing one).
        lines = [ln for ln in self._lines() if ln.strip() != 'opencode.json']
        self.gitignore.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.assertNotIn('opencode.json', self._text())

        self._configure()
        self.assertIn('opencode.json', self._text(),
                      'the newly managed entry is restored')
        self.assertEqual(
            1, self._header_count(),
            'restoring an entry must NOT emit a second managed header '
            f'(got {self._header_count()}):\n{self._text()}')

    def test_restored_entry_lands_inside_the_managed_block(self):
        """An entry appended outside the header block would survive an
        Uninstall, which reclaims only the lines under the marker."""
        self._configure()
        lines = [ln for ln in self._lines() if ln.strip() != 'briefs/']
        self.gitignore.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self._configure()

        lines = self._lines()
        hdr = next(i for i, ln in enumerate(lines)
                   if ln.strip().startswith(HEADER))
        end = hdr + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        block = [ln.strip() for ln in lines[hdr + 1:end]]
        self.assertIn('briefs/', block,
                      f'entry must sit inside the managed block: {lines}')

    def test_negation_stays_below_its_ignore(self):
        """git is last-match-wins: '!.embody/project.json' must remain
        BELOW '.embody/*' or the committed td_build pin stops being
        tracked."""
        self._configure()
        lines = [ln.strip() for ln in self._lines()]
        self.assertIn('.embody/*', lines)
        self.assertIn('!.embody/project.json', lines)
        self.assertLess(
            lines.index('.embody/*'), lines.index('!.embody/project.json'),
            f'negation must follow the ignore it negates: {lines}')

    def test_duplicate_headers_are_consolidated(self):
        """Repos already carrying duplicate headers (Embody's own had 3)
        must get healed, not merely stop accumulating more."""
        self.gitignore.write_text(
            '# Embody / Envoy (auto-managed)\n'
            'Backup/\n'
            'logs/\n'
            '\n'
            '# Embody / Envoy (auto-managed)\n'
            'briefs/\n'
            '\n'
            '# Embody / Envoy (auto-managed)\n'
            'opencode.json\n',
            encoding='utf-8')
        self._configure()
        self.assertEqual(
            1, self._header_count(),
            f'all-managed duplicate blocks must merge into one: '
            f'{self._lines()}')
        for entry in ('Backup/', 'logs/', 'briefs/', 'opencode.json'):
            self.assertIn(entry, [ln.strip() for ln in self._lines()])

    def test_consolidation_never_swallows_user_content(self):
        """A managed header whose block holds a USER line is left alone --
        merging it would widen what Uninstall's strip_marked_block
        deletes."""
        self.gitignore.write_text(
            '# Embody / Envoy (auto-managed)\n'
            'Backup/\n'
            '\n'
            '# Embody / Envoy (auto-managed)\n'
            'briefs/\n'
            'my_precious_user_rule/\n',
            encoding='utf-8')
        self._configure()
        text = self._text()
        self.assertIn('my_precious_user_rule/', text)
        self.assertEqual(
            2, self._header_count(),
            f'the block containing user content keeps its own header: '
            f'{self._lines()}')

    def test_negation_reordered_when_the_ignore_sits_below_it(self):
        """The hazard the block-insert introduced: if '.embody/*' already
        lives BELOW the managed block (or below a blank line that ends the
        block scan), a freshly inserted '!.embody/project.json' lands above
        it and git's last-match-wins re-ignores the td_build pin."""
        self.gitignore.write_text(
            '# Embody / Envoy (auto-managed)\n'
            'Backup/\n'
            '\n'
            '# user section\n'
            '.embody/*\n',
            encoding='utf-8')
        self._configure()
        lines = [ln.strip() for ln in self._lines()]
        self.assertIn('!.embody/project.json', lines)
        self.assertIn('.embody/*', lines)
        self.assertLess(
            lines.index('.embody/*'), lines.index('!.embody/project.json'),
            f'negation must end up BELOW the ignore: {lines}')

    def test_user_content_is_preserved(self):
        """The writer must never disturb lines the user owns."""
        self.gitignore.write_text(
            '# my own rules\nbuild/\n*.bak\n', encoding='utf-8')
        self._configure()
        text = self._text()
        for own in ('# my own rules', 'build/', '*.bak'):
            self.assertIn(own, text)
        self.assertEqual(1, self._header_count())

    # --- .gitattributes: same class ---------------------------------------

    def test_gitattributes_backfills_entries_added_by_later_releases(self):
        """configure_gitattributes returned on ANY existing marker, so a
        project installed before an attribute line was added never got it
        and kept churning CRLF for those file types."""
        setup = op.Embody.op('envoy_setup').module
        gitattr = self.tmp / '.gitattributes'
        setup.configure_gitattributes(self.embody.ext.Envoy, self.tmp)
        full = [ln.strip() for ln in
                gitattr.read_text(encoding='utf-8').splitlines()]
        self.assertIn('*.tox binary', full)

        # Simulate an older install: same marker, missing a later entry.
        trimmed = [ln for ln in gitattr.read_text(
            encoding='utf-8').splitlines() if ln.strip() != '*.tox binary']
        gitattr.write_text('\n'.join(trimmed) + '\n', encoding='utf-8')

        setup.configure_gitattributes(self.embody.ext.Envoy, self.tmp)
        after = [ln.strip() for ln in
                 gitattr.read_text(encoding='utf-8').splitlines()]
        self.assertIn(
            '*.tox binary', after,
            f'a later-added attribute must be backfilled: {after}')
        self.assertEqual(
            1, sum(1 for ln in after if ln.startswith('# Embody / Envoy')),
            f'and must not start a second managed block: {after}')

    # --- the discarded-migration bug -------------------------------------

    def test_stale_entry_removal_is_actually_written(self):
        """Stale-entry migration must reach disk even when no entries are
        missing.

        The old code computed the stale-stripped content, then returned
        early on `if not missing`, discarding it -- so a documented
        migration silently never ran for any project that was otherwise
        up to date.
        """
        self._configure()                      # bring the file fully current
        text = self._text()
        stripped = [ln.strip() for ln in self._lines()]
        # Whole-line comparison: '.claude/' is a PREFIX of the legitimate
        # managed entries '.claude/settings.local.json' and
        # '.claude/projects/', so a substring check would false-positive.
        self.assertNotIn('.claude/', stripped)

        # Re-introduce a stale entry from an older Embody, with nothing
        # else missing.
        self.gitignore.write_text(text + '.claude/\n', encoding='utf-8')
        self.assertIn('.claude/', [ln.strip() for ln in self._lines()])

        self._configure()
        self.assertNotIn(
            '.claude/', [ln.strip() for ln in self._lines()],
            'the stale entry must be removed from disk, not just from an '
            'in-memory copy that gets thrown away')
        # The specific entries it is a prefix of must survive.
        self.assertIn('.claude/settings.local.json',
                      [ln.strip() for ln in self._lines()])
