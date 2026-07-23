"""
Tests for the OpenCode client config writer (envoy_setup.write_opencode_config
and friends).

OpenCode (opencode.ai) never reads .mcp.json -- Embody generates opencode.json
instead when the Aiclient parameter selects opencode:

  mcp.envoy      -> spawns the same STDIO bridge as .mcp.json ('local' entry
                    with the full argv), or a 'remote' 127.0.0.1 URL when no
                    bridge command is available
  instructions   -> ['.claude/rules/*.md'] created when absent; a user-owned
                    list gets the glob appended (never replaced)
  permission     -> Toolpermissions posture block, ONLY on fresh file creation
  $schema        -> only on fresh file creation

maybe_write_opencode_config gates on Aiclient == 'opencode' OR an existing
opencode.json that already carries an envoy entry (dual-client refresh).
ensure_opencode_config (the parexec/_extractAIConfig path) reuses the bridge
argv from .mcp.json and the port from the .embody/envoy.json registry.

SAFETY: not destructive. All writes go to throwaway temp dirs; the
Toolpermissions param is saved/restored; _consent_bulk silences the Advanced
guard so writes happen inline; manifest recorders are stubbed so the real
install manifest is never touched. The Aiclient param itself is NEVER flipped
here -- parexec would regenerate real config at the project root.
"""

import json
import tempfile
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

RULES_GLOB = '.claude/rules/*.md'


class TestOpencodeConfig(EmbodyTestCase):

    def setUp(self):
        self._env = op.Embody.ext.Envoy
        self._emb = op.Embody.ext.Embody
        self._setup = op.Embody.op('envoy_setup').module
        self._saved_posture = op.Embody.par.Toolpermissions.eval()
        self._prev_bulk = getattr(self._emb, '_consent_bulk', False)
        self._emb._consent_bulk = True
        self._emb._manifestRecordCreatedFile = lambda *a, **k: None
        self._emb._manifestRecordAppendedFile = lambda *a, **k: None

    def tearDown(self):
        op.Embody.par.Toolpermissions = self._saved_posture
        self._emb._consent_bulk = self._prev_bulk
        self._emb.__dict__.pop('_manifestRecordCreatedFile', None)
        self._emb.__dict__.pop('_manifestRecordAppendedFile', None)

    # ---- helpers ---------------------------------------------------------

    def _tmp(self):
        return Path(tempfile.mkdtemp())

    def _cmd(self):
        return ['C:/proj/.venv/Scripts/python.exe', '-u',
                'C:/proj/.embody/envoy-bridge.py',
                '--port', '9872', '--config', 'C:/proj/.embody/envoy.json']

    def _read(self, root):
        return json.loads((root / 'opencode.json').read_text(encoding='utf-8'))

    # ---- write_opencode_config: creation ---------------------------------

    def test_create_fresh_local_entry(self):
        op.Embody.par.Toolpermissions = 'all'
        root = self._tmp()
        self._setup.write_opencode_config(self._env, root, 9872, self._cmd())
        cfg = self._read(root)
        self.assertEqual(cfg.get('$schema'), 'https://opencode.ai/config.json',
                         'fresh file must carry the opencode schema')
        envoy = cfg['mcp']['envoy']
        self.assertEqual(envoy['type'], 'local')
        self.assertEqual(envoy['command'], self._cmd(),
                         'must spawn the SAME bridge argv as .mcp.json')
        self.assertTrue(envoy['enabled'])
        self.assertEqual(envoy['timeout'], 30000,
                         'tool-fetch timeout must exceed the 5s opencode default')
        self.assertEqual(envoy['environment'],
                         {'EMBODY_SESSION_LABEL': 'opencode'},
                         'sessions must be attributable as opencode')
        self.assertEqual(cfg['instructions'], [RULES_GLOB],
                         'rules glob must load .claude/rules for opencode')
        self.assertNotIn('permission', cfg,
                         "posture 'all' needs no block -- opencode allows by default")

    def test_remote_fallback_uses_loopback(self):
        op.Embody.par.Toolpermissions = 'all'
        root = self._tmp()
        self._setup.write_opencode_config(self._env, root, 9955, None)
        envoy = self._read(root)['mcp']['envoy']
        self.assertEqual(envoy['type'], 'remote')
        self.assertEqual(envoy['url'], 'http://127.0.0.1:9955/mcp',
                         '127.0.0.1, never localhost (issue #57)')

    # ---- write_opencode_config: merge behavior ---------------------------

    def test_merge_preserves_user_config(self):
        op.Embody.par.Toolpermissions = 'all'
        root = self._tmp()
        user = {'model': 'lmstudio/qwen3-coder-30b',
                'mcp': {'other': {'type': 'remote', 'url': 'http://x/'}},
                'instructions': ['MY_RULES.md']}
        (root / 'opencode.json').write_text(
            json.dumps(user), encoding='utf-8')
        self._setup.write_opencode_config(self._env, root, 9872, self._cmd())
        cfg = self._read(root)
        self.assertEqual(cfg['model'], 'lmstudio/qwen3-coder-30b',
                         'user keys must survive the merge')
        self.assertIn('other', cfg['mcp'],
                      'other MCP servers must survive the merge')
        self.assertEqual(cfg['mcp']['envoy']['type'], 'local')
        self.assertEqual(cfg['instructions'], ['MY_RULES.md', RULES_GLOB],
                         'user instructions list gets the glob APPENDED')
        self.assertNotIn('permission', cfg,
                         'permission is never merged into an existing file')
        self.assertNotIn('$schema', cfg,
                         '$schema is only added on fresh creation')

    def test_idempotent_skip(self):
        op.Embody.par.Toolpermissions = 'all'
        root = self._tmp()
        self._setup.write_opencode_config(self._env, root, 9872, self._cmd())
        path = root / 'opencode.json'
        before = path.stat().st_mtime_ns
        self._setup.write_opencode_config(self._env, root, 9872, self._cmd())
        self.assertEqual(path.stat().st_mtime_ns, before,
                         'an already-configured file must not be rewritten')

    def test_unparseable_file_left_untouched(self):
        root = self._tmp()
        raw = '// jsonc comment -- hand-authored\n{ "mcp": {} }\n'
        (root / 'opencode.json').write_text(raw, encoding='utf-8')
        self._setup.write_opencode_config(self._env, root, 9872, self._cmd())
        self.assertEqual((root / 'opencode.json').read_text(encoding='utf-8'),
                         raw, 'unparseable (JSONC) config must never be clobbered')

    # ---- permission posture ----------------------------------------------

    def test_permission_posture_some(self):
        op.Embody.par.Toolpermissions = 'some'
        root = self._tmp()
        self._setup.write_opencode_config(self._env, root, 9872, self._cmd())
        perm = self._read(root)['permission']
        keys = list(perm.keys())
        self.assertEqual(keys[0], 'envoy_*',
                         'catch-all must come FIRST (last matching rule wins)')
        self.assertEqual(perm['envoy_*'], 'ask')
        readonly = set(self._env.READ_ONLY_TOOLS)
        allows = {k[len('envoy_'):] for k, v in perm.items()
                  if v == 'allow'}
        self.assertEqual(allows, readonly,
                         "posture 'some' must allow exactly READ_ONLY_TOOLS")

    def test_permission_posture_prompt(self):
        op.Embody.par.Toolpermissions = 'prompt'
        root = self._tmp()
        self._setup.write_opencode_config(self._env, root, 9872, self._cmd())
        self.assertEqual(self._read(root)['permission'], {'envoy_*': 'ask'})

    # ---- maybe_write_opencode_config gate --------------------------------

    def test_maybe_write_refreshes_existing_entry(self):
        # Aiclient is NOT flipped: the existing-envoy-entry branch alone must
        # trigger the refresh (dual-client setups keep tracking port changes).
        root = self._tmp()
        stale = {'mcp': {'envoy': {'type': 'remote',
                                   'url': 'http://127.0.0.1:1111/mcp',
                                   'enabled': True}}}
        (root / 'opencode.json').write_text(json.dumps(stale),
                                            encoding='utf-8')
        self._setup.maybe_write_opencode_config(
            self._env, root, 9872, self._cmd())
        envoy = self._read(root)['mcp']['envoy']
        self.assertEqual(envoy['type'], 'local',
                         'existing entry must be upgraded to the bridge form')
        self.assertEqual(envoy['command'], self._cmd())

    def test_maybe_write_skips_when_not_in_play(self):
        if op.Embody.par.Aiclient.eval() == 'opencode':
            self.skipTest('dev project Aiclient is opencode; gate untestable')
        root = self._tmp()
        self._setup.maybe_write_opencode_config(
            self._env, root, 9872, self._cmd())
        self.assertFalse((root / 'opencode.json').exists(),
                         'no selection + no existing entry -> no file')

    # ---- ensure_opencode_config (parexec path) ---------------------------

    def test_ensure_reuses_bridge_argv_from_mcp_json(self):
        op.Embody.par.Toolpermissions = 'all'
        root = self._tmp()
        mcp = {'mcpServers': {'envoy': {
            'type': 'stdio',
            'command': 'C:/proj/.venv/Scripts/python.exe',
            'args': ['-u', 'B.py', '--port', '9872', '--config', 'C.json']}}}
        (root / '.mcp.json').write_text(json.dumps(mcp), encoding='utf-8')
        self._setup.ensure_opencode_config(self._env, root)
        envoy = self._read(root)['mcp']['envoy']
        self.assertEqual(envoy['type'], 'local')
        self.assertEqual(envoy['command'],
                         ['C:/proj/.venv/Scripts/python.exe',
                          '-u', 'B.py', '--port', '9872',
                          '--config', 'C.json'],
                         'the two configs must agree on the bridge argv')

    def test_ensure_falls_back_to_registry_port(self):
        op.Embody.par.Toolpermissions = 'all'
        root = self._tmp()
        (root / '.embody').mkdir()
        reg = {'instances': {'Proj-1': {'port': 9911}}, 'active': 'Proj-1'}
        (root / '.embody' / 'envoy.json').write_text(json.dumps(reg),
                                                     encoding='utf-8')
        self._setup.ensure_opencode_config(self._env, root)
        envoy = self._read(root)['mcp']['envoy']
        self.assertEqual(envoy['type'], 'remote',
                         'no .mcp.json bridge entry -> remote fallback')
        self.assertEqual(envoy['url'], 'http://127.0.0.1:9911/mcp',
                         'port must come from the instance registry')

    # ---- worktree mirroring ----------------------------------------------

    def test_mirror_includes_opencode_json(self):
        base = Path(tempfile.mkdtemp())
        root = base / 'Proj'
        root.mkdir()
        (root / 'opencode.json').write_text('{"mcp": {}}', encoding='utf-8')
        wt = base / 'Proj-wt-task'
        wt.mkdir()
        (wt / '.git').write_text('gitdir: elsewhere')
        self._setup.mirror_ai_config_to_worktrees(self._env, root)
        self.assertTrue((wt / 'opencode.json').is_file(),
                        'worktree must receive opencode.json (same as .mcp.json)')


class TestOpencodeUninstall(EmbodyTestCase):
    """strip_mcp_envoy is shape-aware: opencode.json ('mcp' + instructions)
    vs .mcp.json ('mcpServers'). It must never write one shape's keys into
    the other's file, always preserve user content, and remove the file
    only when nothing but Embody's config remains."""

    def setUp(self):
        super().setUp()
        self._admin = op.Embody.op('embody_admin').module
        self._tmp = Path(tempfile.mkdtemp(prefix='oc_uninstall_'))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    def _write(self, name, cfg):
        p = self._tmp / name
        p.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
        return p

    def test_strip_opencode_merged_preserves_user(self):
        p = self._write('opencode.json', {
            '$schema': 'https://opencode.ai/config.json',
            'mcp': {'envoy': {'type': 'local'},
                    'mytools': {'type': 'remote', 'url': 'http://x'}},
            'instructions': [RULES_GLOB, 'MY_NOTES.md'],
            'theme': 'dark',
        })
        self._admin.strip_mcp_envoy(self.embody_ext, p)
        cfg = json.loads(p.read_text(encoding='utf-8'))
        self.assertNotIn('envoy', cfg['mcp'])
        self.assertIn('mytools', cfg['mcp'])
        self.assertEqual(cfg['instructions'], ['MY_NOTES.md'])
        self.assertEqual(cfg['theme'], 'dark')
        self.assertNotIn('mcpServers', cfg,
                         'must never inject .mcp.json keys into opencode.json')

    def test_strip_opencode_fresh_file_unlinked(self):
        p = self._write('opencode.json', {
            '$schema': 'https://opencode.ai/config.json',
            'mcp': {'envoy': {'type': 'local'}},
            'instructions': [RULES_GLOB],
            'permission': {'envoy_*': 'allow'},
        })
        self._admin.strip_mcp_envoy(self.embody_ext, p)
        self.assertFalse(p.exists(),
                         'an Embody-only opencode.json must be removed')

    def test_strip_opencode_user_keys_survive(self):
        p = self._write('opencode.json', {
            'mcp': {'envoy': {'type': 'local'}},
            'theme': 'dark',
        })
        self._admin.strip_mcp_envoy(self.embody_ext, p)
        cfg = json.loads(p.read_text(encoding='utf-8'))
        self.assertNotIn('mcp', cfg)
        self.assertEqual(cfg['theme'], 'dark')

    def test_strip_mcp_json_shape_unchanged(self):
        p = self._write('.mcp.json', {
            'mcpServers': {'envoy': {'command': 'x'},
                           'other': {'command': 'y'}},
        })
        self._admin.strip_mcp_envoy(self.embody_ext, p)
        cfg = json.loads(p.read_text(encoding='utf-8'))
        self.assertNotIn('envoy', cfg['mcpServers'])
        self.assertIn('other', cfg['mcpServers'])
        self.assertNotIn('mcp', cfg)

    def test_strip_mcp_json_only_envoy_unlinked(self):
        p = self._write('.mcp.json', {
            'mcpServers': {'envoy': {'command': 'x'}},
        })
        self._admin.strip_mcp_envoy(self.embody_ext, p)
        self.assertFalse(p.exists())
