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
        """git is last-match-wins: the negation must remain BELOW the
        ignore it negates, or the committed td_build pin stops being
        tracked."""
        self._configure()
        lines = [ln.strip() for ln in self._lines()]
        self.assertIn('**/.embody/*', lines)
        self.assertIn('!**/.embody/project.json', lines)
        self.assertLess(
            lines.index('**/.embody/*'),
            lines.index('!**/.embody/project.json'),
            f'negation must follow the ignore it negates: {lines}')

    def _setup_mod(self):
        return op.Embody.op('envoy_setup').module

    def test_a_deliberate_user_ignore_withholds_the_negation(self):
        """THE OWLETTE FLEET COMPLAINT (2026-08-19): a repo deliberately
        ignoring .embody/project.json got the negation re-appended and
        carried a permanent conflict. When git names a NON-managed rule
        as decider, withhold the negation and warn."""
        setup = self._setup_mod()
        real = setup._project_json_ignore_decider
        setup._project_json_ignore_decider = (
            lambda root: ('.embody/project.json', '.gitignore'))
        try:
            self._configure()
        finally:
            setup._project_json_ignore_decider = real
        self.assertNotIn('!**/.embody/project.json', self._text())
        self.assertNotIn('**/.embody/*', self._text(),
                         'the ignore half is withheld TOO: adding it would '
                         'make our rule the last file-level match on the '
                         'next startup, flip the decider to managed, and '
                         're-add the negation -- the withhold must be '
                         'stable across runs')

    def test_the_withhold_is_stable_across_repeated_runs(self):
        """Run twice with a user rule deciding: neither run may add
        either half of the managed pair."""
        setup = self._setup_mod()
        real = setup._project_json_ignore_decider
        setup._project_json_ignore_decider = (
            lambda root: ('.embody/project.json', '.gitignore'))
        try:
            self._configure()
            self._configure()
        finally:
            setup._project_json_ignore_decider = real
        self.assertNotIn('!**/.embody/project.json', self._text())
        self.assertNotIn('**/.embody/*', self._text())

    def test_our_own_rule_as_decider_still_appends_the_pair(self):
        """A .embody ignore deciding is OUR half of the pair mid-append,
        never a user opt-out. BOTH the current '**/' form and the legacy
        anchored one count: v6.1.6 changed the shipped entry, and a repo
        still carrying '.embody/*' (ours until then, and hand-copied into
        plenty of .gitignores) must not be read as a deliberate opt-out --
        that withholds the negation and untracks project.json."""
        for rule in ('**/.embody/*', '.embody/*'):
            with self.subTest(rule=rule):
                self.gitignore.unlink(missing_ok=True)
                setup = self._setup_mod()
                real = setup._project_json_ignore_decider
                setup._project_json_ignore_decider = (
                    lambda root, _r=rule: (_r, '.gitignore'))
                try:
                    self._configure()
                finally:
                    setup._project_json_ignore_decider = real
                self.assertIn('!**/.embody/project.json', self._text())

    def test_probe_failure_fails_open(self):
        """No repo / no git -> None -> exactly the old behavior. The
        REAL probe runs here: self.tmp is not a git repository."""
        self._configure()
        self.assertIn('!**/.embody/project.json', self._text())

    def test_wrong_order_is_repaired_even_with_nothing_missing(self):
        """Both lines present but negation ABOVE the ignore used to
        return early -- the committed metadata stayed silently
        untracked."""
        self._configure()
        lines = [ln for ln in self._lines()
                 if ln.strip() != '!**/.embody/project.json']
        idx = next(i for i, ln in enumerate(lines)
                   if ln.strip() == '**/.embody/*')
        lines.insert(idx, '!**/.embody/project.json')
        self.gitignore.write_text('\n'.join(lines) + '\n',
                                  encoding='utf-8')
        self._configure()
        s = [ln.strip() for ln in self._lines()]
        self.assertLess(s.index('**/.embody/*'),
                        s.index('!**/.embody/project.json'),
                        'negation must be re-seated BELOW the ignore')

    def test_stale_line_outside_a_managed_block_is_user_content(self):
        """A user-authored '.embody/' outside any Embody header survives
        migration; the same line INSIDE a managed block is our legacy
        and is stripped (Owlette fleet lesson, 2026-08-19)."""
        self.gitignore.write_text(
            '# mine\n.embody/\n\n'
            '# Embody / Envoy (auto-managed)\n.embody/\n.venv/\n',
            encoding='utf-8')
        self._configure()
        lines = self._lines()
        self.assertEqual('.embody/', lines[1].strip(),
                         'user-authored line survives')
        hdr = next(i for i, ln in enumerate(lines)
                   if ln.strip().startswith(HEADER))
        block = []
        j = hdr + 1
        while j < len(lines) and lines[j].strip():
            block.append(lines[j].strip())
            j += 1
        self.assertNotIn('.embody/', block,
                         'legacy in-block entry is migrated away')

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
        self.assertIn('!**/.embody/project.json', lines)
        self.assertIn('.embody/*', lines)
        # The planted rule is the LEGACY anchored form on purpose: git is
        # last-match-wins, so a pre-v6.1.6 line below the managed block
        # re-ignores project.json unless the negation is seated under IT,
        # not merely under our own current entry.
        self.assertLess(
            lines.index('.embody/*'),
            lines.index('!**/.embody/project.json'),
            f'negation must end up BELOW the last .embody ignore: {lines}')

    def test_user_content_is_preserved(self):
        """The writer must never disturb lines the user owns."""
        self.gitignore.write_text(
            '# my own rules\nbuild/\n*.bak\n', encoding='utf-8')
        self._configure()
        text = self._text()
        for own in ('# my own rules', 'build/', '*.bak'):
            self.assertIn(own, text)
        self.assertEqual(1, self._header_count())

    def test_backup_dirs_are_ignored(self):
        """Issue #85: the rotated .bak/.bak2 crash-recovery copies of every
        externalized network file are machine-local scratch git must not
        track. The changelog claimed this from v5.0.227, but the entry only
        ever existed in Embody's own hand-written .gitignore; generated
        projects never got it and picked the backups up as untracked files.

        BOTH names ship (v6.1.6). Writes land in `.embody_backup/`, but
        nothing moves or deletes a pre-v6.1.6 `.tdn_backup/`, so retiring
        its rule would un-ignore every legacy backup on upgrade.

        Existing projects get entries through the same backfill path, so
        this asserts a fresh write AND a second run against a file that
        predates the entry.

        EXACTLY ONCE, not merely present. `assertIn` is true of a writer
        that appends on every run, so it cannot tell the backfill from a
        file that grows an identical line each time Embody starts -- and
        the third run below is the one that catches it, because that is the
        ordinary case: the entry is there and nothing should be written.
        """
        for entry in ('.embody_backup/', '.tdn_backup/'):
            with self.subTest(entry=entry):
                self.gitignore.unlink(missing_ok=True)

                def occurrences():
                    return [ln.strip() for ln in self._lines()].count(entry)

                self._configure()
                self.assertEqual(1, occurrences(),
                                 f'a fresh write must produce one {entry}: '
                                 f'{self._lines()}')

                lines = [ln for ln in self._lines() if ln.strip() != entry]
                self.gitignore.write_text(
                    chr(10).join(lines) + chr(10), encoding='utf-8')
                self._configure()
                self.assertEqual(
                    1, occurrences(),
                    f'a project predating {entry} must be backfilled, '
                    f'once: {self._lines()}')
                self.assertEqual(1, self._header_count())

                # The run that changes nothing. A writer that re-appends
                # what is present stays green on every assertion above.
                self._configure()
                self.assertEqual(
                    1, occurrences(),
                    f'a second run duplicated {entry}: {self._lines()}')
                self.assertEqual(1, self._header_count())

    def test_embody_ignore_pair_is_depth_agnostic(self):
        """v6.1.6: the .embody/ pair must carry a `**/` prefix.

        The .gitignore is ALWAYS written at the git root, but the .toe may
        sit in a subfolder. Any pattern with an interior slash anchors to
        the .gitignore's directory, so the old anchored `.embody/*` left
        `<subdir>/.embody/` untracked-and-visible AND inside a plain
        `git clean -fd`'s reach -- issue #85's exact shape for a nested
        project. Asserted on the SHIPPED entries, because this repo's own
        hand-written .gitignore already used the `**/` form and so could
        never have caught the drift.
        """
        self._configure()
        stripped = [ln.strip() for ln in self._lines()]
        self.assertIn('**/.embody/*', stripped)
        self.assertIn('!**/.embody/project.json', stripped)
        self.assertNotIn('.embody/*', stripped,
                         'the anchored form must not ship alongside')
        self.assertNotIn('!.embody/project.json', stripped)
        # git is last-match-wins: the negation must sit BELOW the ignore
        # or the committed project.json is silently untracked.
        self.assertLess(stripped.index('**/.embody/*'),
                        stripped.index('!**/.embody/project.json'))

    def test_anchored_embody_pair_is_migrated(self):
        """A project written by an older Embody carries the anchored pair
        inside the managed block. It must be swapped for the `**/` forms in
        one pass -- not left to sit as a second, contradicting rule.
        """
        self._configure()
        # Rewrite the managed block the way a pre-v6.1.6 Embody left it.
        lines = []
        for ln in self._lines():
            s = ln.strip()
            if s == '**/.embody/*':
                lines.append('.embody/*')
            elif s == '!**/.embody/project.json':
                lines.append('!.embody/project.json')
            else:
                lines.append(ln)
        self.gitignore.write_text(chr(10).join(lines) + chr(10),
                                  encoding='utf-8')

        self._configure()
        stripped = [ln.strip() for ln in self._lines()]
        self.assertNotIn('.embody/*', stripped,
                         'the stale anchored entry must be removed')
        self.assertNotIn('!.embody/project.json', stripped)
        self.assertEqual(1, stripped.count('**/.embody/*'))
        self.assertEqual(1, stripped.count('!**/.embody/project.json'))
        self.assertLess(stripped.index('**/.embody/*'),
                        stripped.index('!**/.embody/project.json'))
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


class TestProjectJsonStewardship(EmbodyTestCase):
    """A-14 (Convoy plan): td_build lives machine-locally in
    .embody/local.json; the COMMITTED project.json is stewarded with
    key-level ownership -- the retired td_build key removed once, foreign
    keys (a future Convoy key) preserved byte-for-byte, and unreadable
    JSON NEVER treated as empty-and-overwrite.

    Runs against a temp root via an instance-patched _findProjectRoot;
    Log is recorded, and _write_json_atomic is wrapped with a recorder so
    no-op paths are proven by WRITE COUNT and content, never by mtime
    (measured ~33% false-pass under Windows mtime granularity). setUp
    creates all plain state BEFORE the first live-ext patch, so a raising
    setUp cannot strand the live ext (the harness never calls tearDown
    when setUp raises); tearDown restores patches and sweeps temp state
    in one try/finally with super().tearDown() inside the finally.
    """

    def setUp(self):
        super().setUp()
        # Plain state first -- nothing above this line touches the live ext.
        self.tmp = Path(tempfile.mkdtemp(prefix='embody_pjson_'))
        (self.tmp / '.embody').mkdir()
        self.admin = self.embody.op('embody_admin').module
        self._logs = []
        self._writes = []
        self._patches = []
        self._mod_patches = []
        # Live-ext patches last, and nothing after them can raise.
        self._patch('_findProjectRoot', lambda: self.tmp)
        self._patch('Log',
                    lambda msg, level='INFO': self._logs.append((msg, level)))
        real_write = self.admin._write_json_atomic
        self._mod_patch('_write_json_atomic',
                        lambda path, data: (self._writes.append(str(path)),
                                            real_write(path, data))[1])

    def tearDown(self):
        try:
            while self._patches:
                name, old, sentinel = self._patches.pop()
                if old is sentinel:
                    try:
                        delattr(self.embody_ext, name)
                    except AttributeError:
                        pass
                else:
                    setattr(self.embody_ext, name, old)
            while self._mod_patches:
                name, old = self._mod_patches.pop()
                setattr(self.admin, name, old)
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)
            super().tearDown()

    def _patch(self, name, value):
        sentinel = object()
        old = self.embody_ext.__dict__.get(name, sentinel)
        setattr(self.embody_ext, name, value)
        self._patches.append((name, old, sentinel))

    def _mod_patch(self, name, value):
        self._mod_patches.append((name, getattr(self.admin, name)))
        setattr(self.admin, name, value)

    def _read(self, name):
        import json
        return json.loads(
            (self.tmp / '.embody' / name).read_text(encoding='utf-8'))

    def _warnings(self):
        return [m for m, lvl in self._logs if lvl == 'WARNING']

    # -- local.json pin ----------------------------------------------

    def test_local_pin_written_and_idempotent(self):
        self.admin.write_local_json(self.embody_ext)
        self.assertEqual(self._read('local.json'), {'td_build': app.build})
        writes = len(self._writes)
        self.admin.write_local_json(self.embody_ext)
        self.assertEqual(
            len(self._writes), writes,
            'a current pin must be a WRITE no-op (idempotent)')

    def test_local_pin_preserves_foreign_keys(self):
        import json
        (self.tmp / '.embody' / 'local.json').write_text(
            json.dumps({'td_build': '2020.10000',
                        'node_anchor': 'future-machine-key'}),
            encoding='utf-8')
        self.admin.write_local_json(self.embody_ext)
        data = self._read('local.json')
        self.assertEqual(data.get('td_build'), app.build)
        self.assertEqual(
            data.get('node_anchor'), 'future-machine-key',
            'readable foreign keys must survive the pin update')

    def test_corrupt_local_json_self_heals_with_warning(self):
        (self.tmp / '.embody' / 'local.json').write_text(
            'not json {{{', encoding='utf-8')
        self.admin.write_local_json(self.embody_ext)
        self.assertEqual(self._read('local.json'), {'td_build': app.build},
                         'a corrupt machine-local cache is recreated')
        self.assertTrue(
            any('unreadable' in w for w in self._warnings()),
            'the self-heal must be loud, never silent')

    def test_utf16_local_json_self_heals_not_raises(self):
        """UnicodeDecodeError is a ValueError, not a JSONDecodeError --
        a UTF-16 file must warn and heal, never propagate (panel
        finding: the raise silently aborted the project.json steward)."""
        (self.tmp / '.embody' / 'local.json').write_bytes(
            '{"td_build": "x"}'.encode('utf-16'))
        self.admin.write_local_json(self.embody_ext)
        self.assertEqual(self._read('local.json'), {'td_build': app.build})
        self.assertTrue(any('unreadable' in w for w in self._warnings()))

    # -- project.json stewardship ------------------------------------

    def test_absent_project_json_created_empty(self):
        self.admin.write_project_json(self.embody_ext)
        self.assertEqual(
            self._read('project.json'), {},
            'the committed placeholder is created as an empty object')

    def test_td_build_retired_foreign_keys_preserved_then_stable(self):
        """The migration axis this suite exists for: run the steward
        REPEATEDLY -- td_build is removed exactly once, foreign keys
        survive every run, and subsequent runs write nothing at all."""
        import json
        (self.tmp / '.embody' / 'project.json').write_text(
            json.dumps({'td_build': '2025.30000',
                        'convoy': {'id': 'future'},
                        'other': [1, 2]}),
            encoding='utf-8')
        self.admin.write_project_json(self.embody_ext)
        data = self._read('project.json')
        self.assertNotIn('td_build', data,
                         'the retired machine-specific key is removed')
        self.assertEqual(data.get('convoy'), {'id': 'future'},
                         "a co-writer's key must survive the steward")
        self.assertEqual(data.get('other'), [1, 2])

        writes = len(self._writes)
        for _ in range(3):
            self.admin.write_project_json(self.embody_ext)
        self.assertEqual(
            len(self._writes), writes,
            'runs after the retirement must be WRITE no-ops')
        self.assertEqual(
            self._read('project.json'),
            {'convoy': {'id': 'future'}, 'other': [1, 2]},
            'content must be byte-stable across repeat runs')

    def test_empty_project_json_healed(self):
        """A zero-byte file (interrupted write, bad checkout) holds
        nothing a co-writer could lose -- the one corrupt shape the
        steward heals instead of refusing forever."""
        path = self.tmp / '.embody' / 'project.json'
        path.write_text('', encoding='utf-8')
        self.admin.write_project_json(self.embody_ext)
        self.assertEqual(self._read('project.json'), {})

    def test_unreadable_project_json_never_overwritten(self):
        path = self.tmp / '.embody' / 'project.json'
        path.write_text('corrupt {{{ not json', encoding='utf-8')
        writes = len(self._writes)
        self.admin.write_project_json(self.embody_ext)
        self.assertEqual(
            path.read_text(encoding='utf-8'), 'corrupt {{{ not json',
            'unreadable JSON must be left byte-for-byte untouched -- '
            'never treated as empty-and-overwrite (A-14)')
        self.assertEqual(len(self._writes), writes)
        self.assertTrue(
            any('unreadable' in w for w in self._warnings()),
            'the refusal must warn loudly')

    def test_utf16_project_json_never_overwritten(self):
        path = self.tmp / '.embody' / 'project.json'
        payload = '{"td_build": "x"}'.encode('utf-16')
        path.write_bytes(payload)
        self.admin.write_project_json(self.embody_ext)
        self.assertEqual(
            path.read_bytes(), payload,
            'a UTF-16/binary file must be refused, not raised over or '
            'overwritten')
        self.assertTrue(any('unreadable' in w for w in self._warnings()))

    def test_non_dict_project_json_never_overwritten(self):
        import json
        path = self.tmp / '.embody' / 'project.json'
        path.write_text(json.dumps(['a', 'list']), encoding='utf-8')
        self.admin.write_project_json(self.embody_ext)
        self.assertEqual(self._read('project.json'), ['a', 'list'],
                         'a non-object file is not Embody-shaped -- '
                         'leave it alone')
        self.assertTrue(any('not a JSON object' in w
                            for w in self._warnings()))

    def test_failed_write_leaves_file_and_warns(self):
        import json
        path = self.tmp / '.embody' / 'project.json'
        original = json.dumps({'td_build': '2025.30000', 'keep': True})
        path.write_text(original, encoding='utf-8')

        def exploding_write(_path, _data):
            raise PermissionError('locked (test)')
        self._mod_patch('_write_json_atomic', exploding_write)

        self.admin.write_project_json(self.embody_ext)
        self.assertEqual(
            path.read_text(encoding='utf-8'), original,
            'a failed write must leave the existing file intact')
        self.assertTrue(
            any('Failed to write project.json' in w
                for w in self._warnings()))

    # -- the wrapper drives both -------------------------------------

    def test_wrapper_pins_locally_and_stewards_tracked(self):
        import json
        (self.tmp / '.embody' / 'project.json').write_text(
            json.dumps({'td_build': '2025.30000'}), encoding='utf-8')
        self.embody_ext._writeProjectJson()
        self.assertEqual(self._read('local.json'),
                         {'td_build': app.build},
                         'the wrapper must write the machine-local pin')
        self.assertEqual(self._read('project.json'), {},
                         'the wrapper must retire the committed pin')

    def test_wrapper_stewards_even_when_local_pin_raises(self):
        """A raising local-pin write must never block the tracked-file
        steward (panel finding)."""
        import json
        (self.tmp / '.embody' / 'project.json').write_text(
            json.dumps({'td_build': '2025.30000'}), encoding='utf-8')

        real = self.admin.write_local_json

        def exploding_local(ext):
            raise RuntimeError('local pin exploded (test)')
        self._mod_patch('write_local_json', exploding_local)

        self.embody_ext._writeProjectJson()
        self.assertEqual(self._read('project.json'), {},
                         'the steward must still run')
        self.assertTrue(
            any('local.json pin failed' in w for w in self._warnings()))

    # -- the load-bearing gitignore claim ----------------------------

    def test_local_json_is_gitignored(self):
        """A-14's whole point dies silently if local.json ever becomes
        tracked -- pin the ignore with git itself."""
        import subprocess
        try:
            proc = subprocess.run(
                ['git', 'check-ignore', '-q', '.embody/local.json'],
                cwd=str(project.folder + '/..'), capture_output=True,
                timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest('git unavailable for check-ignore')
        self.assertEqual(
            proc.returncode, 0,
            '.embody/local.json must be gitignored -- a tracked '
            'machine-local pin reintroduces the per-machine churn A-14 '
            'removed')
