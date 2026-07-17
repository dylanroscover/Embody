"""
Tests for the tool-permissions posture writer (EnvoyExt).

The setup wizard lets the user choose how much Claude Code auto-approves Envoy
MCP tool calls in .claude/settings.local.json. The choice is stored on the
Toolpermissions param (all | some | prompt | leave) and applied by
EnvoyExt._deploySettingsLocal, which builds the file via _composeSettings /
_settingsSatisfies:

  all    -> permissions.allow gets the wildcard 'mcp__envoy' (all tools)
  some   -> allow gets only READ_ONLY_TOOLS ('mcp__envoy__get_*' etc.)
  prompt -> no Envoy tool entries (every tool prompts)
  leave  -> the file is never created or modified

Every written posture also whitelists the OS temp dir (tempfile.gettempdir())
in additionalDirectories so a captured TOP (saved there by capture_top) can be
Read without a prompt, pre-authorizes the sibling '<repo>-wt-*' worktree
directories (Read/Edit rules from _worktreePermissionRules, so the
isolated-worktree workflow never triggers per-file access prompts), and merges
into an existing file preserving all non-Envoy keys, idempotently.

SAFETY: not destructive. _composeSettings / _settingsSatisfies are pure. The
_deploySettingsLocal cases write ONLY into throwaway temp dirs, save/restore the
Toolpermissions param, set _consent_bulk so the guard applies silently, and stub
the install-manifest recorders so the real manifest is never touched.
"""

import json
import copy
import tempfile
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestToolPermissions(EmbodyTestCase):

    def setUp(self):
        self._env = op.Embody.ext.Envoy
        self._emb = op.Embody.ext.Embody
        self._saved_posture = op.Embody.par.Toolpermissions.eval()
        self._prev_bulk = getattr(self._emb, '_consent_bulk', False)
        # Silence the Advanced-mode guard so _deploySettingsLocal writes inline,
        # and keep the real install manifest untouched.
        self._emb._consent_bulk = True
        self._emb._manifestRecordCreatedFile = lambda *a, **k: None
        self._emb._manifestRecordAppendedFile = lambda *a, **k: None

    def tearDown(self):
        op.Embody.par.Toolpermissions = self._saved_posture
        self._emb._consent_bulk = self._prev_bulk
        self._emb.__dict__.pop('_manifestRecordCreatedFile', None)
        self._emb.__dict__.pop('_manifestRecordAppendedFile', None)

    # ---- helpers ---------------------------------------------------------

    def _baseline(self):
        return {'permissions': {'allow': ['Bash', 'WebFetch'],
                                'additionalDirectories': ['/tmp']},
                'enabledMcpjsonServers': ['envoy'],
                'enableAllProjectMcpServers': True}

    def _envoy_entries(self, cfg):
        allow = cfg.get('permissions', {}).get('allow', [])
        return [a for a in allow if a == 'mcp__envoy' or a.startswith('mcp__envoy__')]

    def _deploy(self, posture, claude_dir):
        op.Embody.par.Toolpermissions = posture
        self._env._deploySettingsLocal(claude_dir)

    # ---- _composeSettings (pure) ----------------------------------------

    def test_compose_all_uses_wildcard(self):
        cfg = self._env._composeSettings(self._baseline(), 'all')
        self.assertEqual(self._envoy_entries(cfg), ['mcp__envoy'],
                         "posture 'all' must auto-approve via the mcp__envoy wildcard")

    def test_compose_some_is_readonly_only(self):
        cfg = self._env._composeSettings(self._baseline(), 'some')
        entries = set(self._envoy_entries(cfg))
        want = {f'mcp__envoy__{t}' for t in self._env.READ_ONLY_TOOLS}
        self.assertEqual(entries, want,
                         "posture 'some' must auto-approve exactly the read-only tools")
        self.assertNotIn('mcp__envoy', entries, "'some' must not include the wildcard")

    def test_compose_prompt_has_no_envoy_entries(self):
        cfg = self._env._composeSettings(self._baseline(), 'prompt')
        self.assertEqual(self._envoy_entries(cfg), [],
                         "posture 'prompt' must not pre-approve any Envoy tool")

    def test_compose_always_whitelists_os_tempdir(self):
        tmp = tempfile.gettempdir().replace('\\', '/')
        for posture in ('all', 'some', 'prompt'):
            cfg = self._env._composeSettings(self._baseline(), posture)
            add = cfg['permissions']['additionalDirectories']
            self.assertIn(tmp, add,
                          f"posture '{posture}' must whitelist the OS temp dir for capture_top reads")
            self.assertTrue(cfg.get('enableAllProjectMcpServers'),
                            "the Envoy MCP server must stay trusted")

    def test_readonly_tools_exclude_mutating_ops(self):
        # A guard against accidentally auto-approving a destructive tool under 'some'.
        forbidden = {'create_op', 'delete_op', 'execute_python', 'set_parameter',
                     'import_network', 'externalize_op', 'exec_op_method',
                     'connect_ops', 'set_dat_content', 'edit_dat_content',
                     'copy_op', 'rename_op', 'run_tests', 'restart_td'}
        self.assertFalse(forbidden & set(self._env.READ_ONLY_TOOLS),
                         "READ_ONLY_TOOLS must not contain any mutating tool")

    # ---- _worktreePermissionRules (pure) ---------------------------------

    def test_worktree_rules_shape(self):
        # POSIX root and Windows root both normalize to '//'-anchored globs.
        rules = self._env._worktreePermissionRules('/home/u/Git/Proj')
        self.assertEqual(rules, ['Read(//home/u/Git/Proj-wt-*/**)',
                                 'Edit(//home/u/Git/Proj-wt-*/**)'])
        rules = self._env._worktreePermissionRules('C:\\Users\\u\\Git\\Proj')
        self.assertEqual(rules, ['Read(//c/Users/u/Git/Proj-wt-*/**)',
                                 'Edit(//c/Users/u/Git/Proj-wt-*/**)'])

    def test_worktree_rules_no_root_degrades_to_empty(self):
        # Pure function: no root (None/''/no-git) -> no rules, never a
        # TD lookup, never a crash.
        for root in ('no-git', None, ''):
            self.assertEqual(self._env._worktreePermissionRules(root), [],
                             f"root={root!r} must yield no rules")

    def test_compose_adds_worktree_rules_every_posture(self):
        root = '/home/u/Git/Proj'
        want = set(self._env._worktreePermissionRules(root))
        self.assertTrue(want, 'sanity: rules must be computable for a real root')
        for posture in ('all', 'some', 'prompt'):
            cfg = self._env._composeSettings(self._baseline(), posture, root)
            allow = set(cfg['permissions']['allow'])
            self.assertTrue(want <= allow,
                            f"posture '{posture}' must pre-authorize sibling "
                            f"'-wt-*' worktree access (got {sorted(allow)})")

    # ---- _mirrorAiConfigToWorktrees (file I/O in a temp dir) -------------

    def test_mirror_ai_config_into_sibling_worktrees(self):
        base = Path(tempfile.mkdtemp())
        root = base / 'Proj'
        (root / '.claude').mkdir(parents=True)
        (root / '.mcp.json').write_text('{"mcpServers": {}}')
        (root / '.claude' / 'settings.local.json').write_text('{}')
        wt = base / 'Proj-wt-task'
        wt.mkdir()
        (wt / '.git').write_text('gitdir: elsewhere')  # worktree marker
        decoy = base / 'Proj-unrelated'
        decoy.mkdir()  # no -wt- prefix -> must be skipped
        stray = base / 'Proj-wt-notgit'
        stray.mkdir()  # -wt- name but no .git -> must be skipped
        self._env._mirrorAiConfigToWorktrees(root)
        self.assertTrue((wt / '.mcp.json').is_file(),
                        'worktree must receive .mcp.json')
        self.assertTrue((wt / '.claude' / 'settings.local.json').is_file(),
                        'worktree must receive settings.local.json')
        self.assertFalse((decoy / '.mcp.json').exists(),
                         'non-worktree sibling must not be touched')
        self.assertFalse((stray / '.mcp.json').exists(),
                         'folder without .git must not be touched')
        # Idempotent: identical content is not rewritten (mtime preserved)
        before = (wt / '.mcp.json').stat().st_mtime_ns
        self._env._mirrorAiConfigToWorktrees(root)
        self.assertEqual((wt / '.mcp.json').stat().st_mtime_ns, before,
                         'unchanged mirror must not rewrite the file')

    # ---- _settingsSatisfies (pure) --------------------------------------

    def test_satisfies_requires_worktree_rules(self):
        # A file written by a pre-worktree Embody must NOT satisfy, so the
        # next Envoy start upgrades it in place.
        root = '/home/u/Git/Proj'
        cfg = self._env._composeSettings(self._baseline(), 'all', root)
        self.assertTrue(self._env._settingsSatisfies(cfg, 'all', root))
        stale = copy.deepcopy(cfg)
        stale['permissions']['allow'] = [
            a for a in stale['permissions']['allow']
            if '-wt-*' not in a]
        self.assertFalse(self._env._settingsSatisfies(stale, 'all', root),
                         'missing worktree rules must trigger a rewrite')

    def test_satisfies_matches_own_posture_only(self):
        for posture in ('all', 'some', 'prompt'):
            cfg = self._env._composeSettings(self._baseline(), posture)
            self.assertTrue(self._env._settingsSatisfies(cfg, posture),
                            f"a freshly composed '{posture}' file must satisfy '{posture}'")
        some_cfg = self._env._composeSettings(self._baseline(), 'some')
        self.assertFalse(self._env._settingsSatisfies(some_cfg, 'all'),
                         "a 'some' file must NOT satisfy 'all'")
        prompt_cfg = self._env._composeSettings(self._baseline(), 'prompt')
        self.assertFalse(self._env._settingsSatisfies(prompt_cfg, 'some'),
                         "a 'prompt' file must NOT satisfy 'some'")

    # ---- _deploySettingsLocal (file I/O in a temp dir) ------------------

    def test_deploy_creates_per_posture(self):
        for posture, wild, n in (('all', True, 1), ('prompt', False, 0)):
            cdir = Path(tempfile.mkdtemp()) / '.claude'
            self._deploy(posture, cdir)
            cfg = json.loads((cdir / 'settings.local.json').read_text())
            entries = self._envoy_entries(cfg)
            self.assertEqual('mcp__envoy' in entries, wild)
            self.assertEqual(len(entries), n if posture == 'prompt' else len(entries))

    def test_deploy_leave_writes_nothing(self):
        cdir = Path(tempfile.mkdtemp()) / '.claude'
        self._deploy('leave', cdir)
        self.assertFalse((cdir / 'settings.local.json').exists(),
                         "posture 'leave' must not create settings.local.json")

    def test_deploy_merges_preserving_user_keys(self):
        cdir = Path(tempfile.mkdtemp()) / '.claude'
        cdir.mkdir(parents=True)
        user = {'permissions': {'allow': ['Bash', 'Edit(/x/**)', 'mcp__envoy__get_op'],
                                'additionalDirectories': ['/tmp']},
                'hooks': {'Stop': 'x'}, 'model': 'sonnet'}
        (cdir / 'settings.local.json').write_text(json.dumps(user, indent=2))
        self._deploy('all', cdir)
        merged = json.loads((cdir / 'settings.local.json').read_text())
        allow = merged['permissions']['allow']
        self.assertEqual(merged.get('model'), 'sonnet', 'user key must be preserved')
        self.assertIn('hooks', merged, 'user key must be preserved')
        self.assertIn('Edit(/x/**)', allow, 'non-Envoy allow entry must be preserved')
        self.assertNotIn('mcp__envoy__get_op', allow,
                         'the stale explicit Envoy entry must be replaced by the wildcard')
        self.assertIn('mcp__envoy', allow, 'the wildcard must be added')

    def test_deploy_is_idempotent(self):
        cdir = Path(tempfile.mkdtemp()) / '.claude'
        self._deploy('all', cdir)
        before = (cdir / 'settings.local.json').read_text()
        self._deploy('all', cdir)   # second identical call
        after = (cdir / 'settings.local.json').read_text()
        self.assertEqual(before, after,
                         "an already-satisfying file must not be rewritten (no churn)")
