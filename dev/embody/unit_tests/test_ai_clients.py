"""
Test suite: the ai_clients registry and the per-client MCP config writers.

The registry is the single source of truth for every client-specific fact
(launch spec, MCP config file, rules/skills dirs, restore probe, uninstall
footprint). These tests pin the schema so a new client row cannot ship
half-filled, and cover the writers that finally give Cursor, VS Code,
Copilot, Gemini, and Codex an actual Envoy connection -- none of them
reads the root .mcp.json.
"""

import json
import shutil
import tempfile
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

BRIDGE_CMD = [
	'C:/proj/.venv/Scripts/python.exe', '-u', 'C:/proj/.embody/envoy-bridge.py',
	'--port', '9870', '--config', 'C:/proj/.embody/envoy.json',
]


class TestAiClients(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self._temp_dir = Path(tempfile.mkdtemp(prefix='ai_clients_test_'))
		self.reg = op.Embody.op('ai_clients').module
		self.setup = op.Embody.op('envoy_setup').module
		self.envoy = op.Embody.ext.Envoy

	def tearDown(self):
		try:
			shutil.rmtree(self._temp_dir)
		except Exception:
			pass
		super().tearDown()

	def _write_all(self, target=None):
		"""Run every registry-driven MCP writer against a temp root."""
		target = target or self._temp_dir
		for token in self.reg.tokens():
			for mcp in self.reg.mcp_targets(token):
				path = target / mcp['path']
				if mcp['style'] == 'toml':
					self.setup.write_toml_mcp_config(
						self.envoy, target, token, mcp, path, BRIDGE_CMD)
				else:
					self.setup.write_json_mcp_config(
						self.envoy, target, token, mcp, path, BRIDGE_CMD)

	# ------------------------------------------------------------------
	# Group A: registry schema integrity
	# ------------------------------------------------------------------

	def test_A01_every_client_has_a_label(self):
		"""Every row needs a human label -- it reaches the UI and logs."""
		for token in self.reg.tokens():
			self.assertTrue(self.reg.label(token),
				f'{token} has no label')

	def test_A02_mcp_blocks_are_complete(self):
		"""An mcp block must carry path, key, style, and scope.

		A half-filled block is the failure this registry exists to
		prevent: the writer would raise mid-deploy on a KeyError.
		"""
		for token in self.reg.tokens():
			mcp = self.reg.spec(token).get('mcp')
			if not mcp:
				continue
			for field in ('path', 'key', 'style', 'scope'):
				self.assertIn(field, mcp, f'{token}.mcp missing {field}')
			self.assertIn(mcp['scope'], ('project', 'user'))

	def test_A03_rules_blocks_are_complete(self):
		"""A rules block must carry dir, ext, and a known style."""
		known = ('strip', 'raw', 'cursor', 'copilot')
		for token in self.reg.tokens():
			block = self.reg.spec(token).get('rules')
			if not block:
				continue
			for field in ('dir', 'ext', 'style'):
				self.assertIn(field, block, f'{token}.rules missing {field}')
			self.assertIn(block['style'], known)

	def test_A04_probe_paths_are_relative(self):
		"""Probe paths are joined onto the project root -- never absolute."""
		for token in self.reg.tokens():
			for group in self.reg.probe_groups(token):
				for rel in group:
					self.assertFalse(rel.startswith('/'),
						f'{token} probe path {rel} is absolute')

	def test_A05_copilot_borrows_the_vscode_launcher(self):
		"""Copilot is an extension inside VS Code, not an app of its own.

		It owns no launch spec, but must still resolve to the SAME spec
		object VS Code uses -- pressing Launch has to open VS Code.
		"""
		self.assertIsNone(self.reg.spec('copilot').get('launch'))
		table = self.reg.launch_table()
		self.assertIs(table['copilot'], self.reg.VSCODE_LAUNCH)
		self.assertIs(table['vscode'], self.reg.VSCODE_LAUNCH)

	def test_A06_cleanup_dirs_are_deepest_first(self):
		"""rmdir only succeeds bottom-up: a leaf must precede its parent."""
		dirs = self.reg.cleanup_dirs()
		for i, d in enumerate(dirs):
			for parent in dirs[i + 1:]:
				self.assertFalse(parent.startswith(d + '/'),
					f'{parent} must be pruned before its parent {d}')

	def test_A07_sweep_dirs_exclude_bare_parents(self):
		"""Marker sweeps walk only leaf dirs Embody fills.

		Sweeping a parent like .claude would walk settings.local.json and
		any user content living beside the generated files.
		"""
		for d in self.reg.cleanup_sweep_dirs():
			self.assertIn('/', d, f'{d} is a bare parent, not a leaf')

	# ------------------------------------------------------------------
	# Group B: which clients the generic MCP writer owns
	# ------------------------------------------------------------------

	def test_B01_baseline_and_bespoke_clients_are_skipped(self):
		"""Claude Code (.mcp.json baseline) and OpenCode (bespoke writer)
		must not be written twice by the generic path."""
		self.assertListEqual(self.reg.mcp_targets('claudecode'), [])
		self.assertListEqual(self.reg.mcp_targets('opencode'), [])

	def test_B02_windsurf_is_never_auto_written(self):
		"""Windsurf's only MCP config is user-global, shared by every
		project the user opens -- writing it would point all of them at
		THIS project's bridge."""
		self.assertListEqual(self.reg.mcp_targets('windsurf'), [])
		self.assertEqual(
			self.reg.spec('windsurf')['mcp']['owner'], 'manual')

	def test_B03_generic_writer_covers_the_gap_clients(self):
		"""Cursor, VS Code, Copilot, Gemini, and Codex each get a target."""
		for token in ('cursor', 'vscode', 'copilot', 'gemini', 'codex'):
			self.assertLen(self.reg.mcp_targets(token), 1,
				f'{token} has no MCP config target')

	def test_B04_config_files_never_lists_an_unwritten_file(self):
		"""The consent dialog must not name a file Embody will not touch."""
		self.assertNotIn(
			'~/.codeium/windsurf/mcp_config.json',
			self.reg.config_files('windsurf'))

	# ------------------------------------------------------------------
	# Group C: the writers produce each client's dialect
	# ------------------------------------------------------------------

	def test_C01_cursor_uses_mcpservers_without_type(self):
		self._write_all()
		cfg = json.loads(
			(self._temp_dir / '.cursor' / 'mcp.json').read_text(encoding='utf-8'))
		entry = cfg['mcpServers']['envoy']
		self.assertEqual(entry['command'], BRIDGE_CMD[0])
		self.assertListEqual(entry['args'], BRIDGE_CMD[1:])
		self.assertNotIn('type', entry)

	def test_C02_vscode_uses_servers_root_key(self):
		"""VS Code is the one client keyed 'servers', not 'mcpServers' --
		its config is NOT interchangeable with Cursor's."""
		self._write_all()
		cfg = json.loads(
			(self._temp_dir / '.vscode' / 'mcp.json').read_text(encoding='utf-8'))
		self.assertNotIn('mcpServers', cfg)
		self.assertEqual(cfg['servers']['envoy']['type'], 'stdio')

	def test_C03_gemini_settings_json(self):
		self._write_all()
		cfg = json.loads(
			(self._temp_dir / '.gemini' / 'settings.json').read_text(encoding='utf-8'))
		self.assertIn('envoy', cfg['mcpServers'])

	def test_C04_codex_writes_a_toml_table(self):
		self._write_all()
		body = (self._temp_dir / '.codex' / 'config.toml').read_text(encoding='utf-8')
		self.assertIn('[mcp_servers.envoy]', body)
		self.assertIn('command = "' + BRIDGE_CMD[0] + '"', body)

	def test_C05_copilot_shares_the_vscode_file(self):
		"""Copilot reads VS Code's MCP config -- one file, not two."""
		self.assertEqual(self.reg.spec('copilot')['mcp']['path'],
			self.reg.spec('vscode')['mcp']['path'])

	# ------------------------------------------------------------------
	# Group D: merging into files the user already owns
	# ------------------------------------------------------------------

	def test_D01_existing_settings_and_servers_survive(self):
		"""Merging must preserve every unrelated key and server."""
		(self._temp_dir / '.gemini').mkdir(parents=True)
		(self._temp_dir / '.gemini' / 'settings.json').write_text(
			json.dumps({'theme': 'GitHub',
				'mcpServers': {'other': {'command': 'x'}}}),
			encoding='utf-8')
		self._write_all()
		cfg = json.loads(
			(self._temp_dir / '.gemini' / 'settings.json').read_text(encoding='utf-8'))
		self.assertEqual(cfg['theme'], 'GitHub')
		self.assertIn('other', cfg['mcpServers'])
		self.assertIn('envoy', cfg['mcpServers'])

	def test_D02_unparseable_jsonc_is_left_untouched(self):
		"""Cursor and VS Code both accept JSONC, which json.loads rejects.

		Never clobber a hand-authored config to add our entry.
		"""
		(self._temp_dir / '.cursor').mkdir(parents=True)
		original = '{ /* hand-written */ }'
		target = self._temp_dir / '.cursor' / 'mcp.json'
		target.write_text(original, encoding='utf-8')
		self._write_all()
		self.assertEqual(target.read_text(encoding='utf-8'), original)

	def test_D03_writers_are_idempotent(self):
		"""A second deploy with the same bridge command changes nothing."""
		self._write_all()
		first = {rel: (self._temp_dir / rel).read_text(encoding='utf-8')
			for rel in ('.cursor/mcp.json', '.vscode/mcp.json',
				'.gemini/settings.json', '.codex/config.toml')}
		self._write_all()
		for rel, body in first.items():
			self.assertEqual(
				(self._temp_dir / rel).read_text(encoding='utf-8'), body,
				f'{rel} was rewritten on an unchanged second pass')

	def test_D04_toml_replace_keeps_other_tables(self):
		"""A port change rewrites only [mcp_servers.envoy]."""
		mcp = self.reg.spec('codex')['mcp']
		path = self._temp_dir / mcp['path']
		path.parent.mkdir(parents=True)
		path.write_text(
			'model = "gpt-5"\n\n[mcp_servers.envoy]\ncommand = "OLD.exe"\n'
			'\n[mcp_servers.other]\ncommand = "keepme"\n', encoding='utf-8')
		self.setup.write_toml_mcp_config(
			self.envoy, self._temp_dir, 'codex', mcp, path, BRIDGE_CMD)
		body = path.read_text(encoding='utf-8')
		self.assertNotIn('OLD.exe', body)
		self.assertIn('model = "gpt-5"', body)
		self.assertIn('keepme', body)
		# The replaced range must not swallow the blank line that
		# separated this table from the next.
		self.assertIn('\n\n[mcp_servers.other]', body)

	def test_D05_unselected_client_with_an_entry_stays_current(self):
		"""A config already holding an envoy entry keeps tracking bridge
		and port changes even when that client is not selected."""
		mcp = self.reg.spec('cursor')['mcp']
		path = self._temp_dir / mcp['path']
		path.parent.mkdir(parents=True)
		path.write_text(json.dumps(
			{'mcpServers': {'envoy': {'command': 'stale.exe', 'args': []}}}),
			encoding='utf-8')
		self.assertTrue(self.setup.mcp_entry_present(mcp, path))
		self.setup.write_json_mcp_config(
			self.envoy, self._temp_dir, 'cursor', mcp, path, BRIDGE_CMD)
		cfg = json.loads(path.read_text(encoding='utf-8'))
		self.assertEqual(cfg['mcpServers']['envoy']['command'], BRIDGE_CMD[0])

	def test_D06_absent_file_is_not_reported_as_present(self):
		mcp = self.reg.spec('vscode')['mcp']
		self.assertFalse(self.setup.mcp_entry_present(
			mcp, self._temp_dir / mcp['path']))

	# ------------------------------------------------------------------
	# Group E: two menus -- what gets written vs what gets opened
	# ------------------------------------------------------------------

	def _fake_ext(self, configclient=None, aiclient='claudecode',
			has_configclient=True):
		"""Stand-in with just the pars selected_clients reads.

		Flipping the live menus would trigger a real config deploy into
		this repo. has_configclient=False models a project saved before
		the second menu existed.
		"""
		class FakePar:
			def __init__(self, value):
				self.val = value

			def eval(self):
				return self.val

		class FakePars:
			pass

		class FakeMy:
			par = FakePars()

		class FakeExt:
			my = FakeMy()

			def Log(self, *a, **k):
				pass

		ext = FakeExt()
		ext.my.par.Aiclient = FakePar(aiclient)
		if has_configclient:
			ext.my.par.Configclient = FakePar(configclient)
		return op.Embody.op('embody_git').module, ext

	def test_E01_configure_menu_decides_what_is_written(self):
		git, ext = self._fake_ext(configclient='cursor', aiclient='claudecode')
		self.assertListEqual(git.selected_clients(ext), ['cursor'])

	def test_E02_launch_menu_alone_writes_nothing(self):
		"""The two axes are independent: configuring Cursor while
		launching a terminal for Claude Code is a normal setup."""
		git, ext = self._fake_ext(configclient='none', aiclient='claudecode')
		self.assertListEqual(git.selected_clients(ext), [])

	def test_E03_none_generates_no_client_config(self):
		"""The Convoy-only posture: Envoy's relay without any AI client."""
		git, ext = self._fake_ext(configclient='none', aiclient='none')
		self.assertListEqual(git.selected_clients(ext), [])

	def test_E04_pre_split_project_falls_back_to_the_launch_menu(self):
		"""A project saved before Configclient existed has only Aiclient;
		its behavior must be unchanged."""
		git, ext = self._fake_ext(aiclient='cursor', has_configclient=False)
		self.assertListEqual(git.selected_clients(ext), ['cursor'])

	def test_E05_an_unset_configure_menu_falls_back_too(self):
		git, ext = self._fake_ext(configclient='', aiclient='gemini')
		self.assertListEqual(git.selected_clients(ext), ['gemini'])

	def test_E06_selecting_a_second_client_keeps_the_first(self):
		"""Why neither menu needs to be a multi-select.

		Generation is additive and never removes, so picking Cursor and
		later Claude Code leaves BOTH configured.
		"""
		git = op.Embody.op('embody_git').module
		git.write_client_config(self.embody_ext, self._temp_dir, 'cursor')
		git.write_client_config(self.embody_ext, self._temp_dir, 'claudecode')
		self.assertTrue((self._temp_dir / '.cursor' / 'rules').exists(),
			'the first client must survive selecting a second')
		self.assertTrue((self._temp_dir / '.claude' / 'rules').exists())

	def test_E07_an_earlier_clients_mcp_config_keeps_tracking_changes(self):
		"""The other half: a config already carrying an Envoy entry is
		refreshed on every deploy whatever the menu now says, so the first
		client does not silently rot at an old port."""
		mcp = self.reg.spec('cursor')['mcp']
		path = self._temp_dir / mcp['path']
		path.parent.mkdir(parents=True, exist_ok=True)
		self.setup.write_json_mcp_config(
			self.envoy, self._temp_dir, 'cursor', mcp, path, BRIDGE_CMD)
		moved = ['py.exe', '-u', 'bridge.py', '--port', '9999']
		self.setup.write_client_mcp_configs(
			self.envoy, self._temp_dir, 9999, moved)
		cfg = json.loads(path.read_text(encoding='utf-8'))
		self.assertIn('9999', json.dumps(cfg['mcpServers']['envoy']))

	def test_E08_multi_client_write_produces_every_footprint(self):
		git = op.Embody.op('embody_git').module
		for token in ('claudecode', 'cursor', 'copilot'):
			git.write_client_config(self.embody_ext, self._temp_dir, token)
		for rel in ('CLAUDE.md', '.claude/rules', '.claude/skills',
				'.cursor/rules', '.github/copilot-instructions.md',
				'.github/instructions'):
			self.assertTrue((self._temp_dir / rel).exists(),
				f'{rel} missing after a multi-client write')

	def test_E09_both_menus_offer_the_same_clients(self):
		"""They answer different questions about the same client list; a
		client offered by one and not the other is a drift bug."""
		cfg = op.Embody.par.Configclient
		launch = op.Embody.par.Aiclient
		self.assertListEqual(list(cfg.menuNames), list(launch.menuNames))
		self.assertListEqual(list(cfg.menuLabels), list(launch.menuLabels))

	def test_E10_both_menus_are_persisted_and_frozen(self):
		"""Either one lost across an upgrade silently changes what Embody
		writes; either one editable in Perform Mode defeats the freeze."""
		for name in ('Aiclient', 'Configclient'):
			self.assertIn(name, self.embody_ext._PERSISTED_PARAMS)
			self.assertIn(name, self.embody_ext._envoyParamNames())

	def test_E11_no_per_client_toggle_parameters_remain(self):
		"""The nine Configure For toggles are gone -- a menu replaced them."""
		for token in self.reg.tokens():
			self.assertIsNone(getattr(op.Embody.par, 'Config' + token, None),
				f'Config{token} should no longer exist')

	# ------------------------------------------------------------------
	# Group F: Uninstall reverses every client's MCP footprint
	# ------------------------------------------------------------------

	def _admin(self):
		return op.Embody.op('embody_admin').module

	def test_F01_every_written_config_is_strippable(self):
		"""Whatever the writers can create, Uninstall must be able to
		reverse -- otherwise widening client support quietly leaves
		orphaned envoy entries behind in the user's configs."""
		written = set()
		for token in self.reg.tokens():
			for mcp in self.reg.mcp_targets(token):
				written.add(mcp['path'])
		strippable = {s['path'] for s in self.reg.project_mcp_specs()}
		self.assertTrue(written.issubset(strippable),
			f'not reversible by Uninstall: {written - strippable}')

	def test_F02_spec_lookup_distinguishes_cursor_from_vscode(self):
		"""Both are named mcp.json but key their servers differently --
		a basename match would strip the wrong key and leave the entry."""
		admin = self._admin()
		cursor = admin.mcp_spec_for(Path('/repo/.cursor/mcp.json'))
		vscode = admin.mcp_spec_for(Path('/repo/.vscode/mcp.json'))
		self.assertEqual(cursor['key'], 'mcpServers')
		self.assertEqual(vscode['key'], 'servers')

	def test_F03_strip_preserves_other_servers_in_each_dialect(self):
		"""Strip the envoy entry from every dialect, keep the user's."""
		self._write_all()
		for rel in ('.cursor/mcp.json', '.vscode/mcp.json',
				'.gemini/settings.json'):
			path = self._temp_dir / rel
			cfg = json.loads(path.read_text(encoding='utf-8'))
			key = self._admin().mcp_spec_for(path)['key']
			cfg[key]['mine'] = {'command': 'keepme'}
			path.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
			self._admin().strip_mcp_envoy(self.embody_ext, path)
			after = json.loads(path.read_text(encoding='utf-8'))
			self.assertNotIn('envoy', after[key], f'{rel} kept envoy')
			self.assertIn('mine', after[key], f'{rel} lost a user server')

	def test_F04_strip_removes_an_embody_only_file(self):
		"""A config holding nothing but Embody's entry is removed."""
		self._write_all()
		path = self._temp_dir / '.cursor' / 'mcp.json'
		self._admin().strip_mcp_envoy(self.embody_ext, path)
		self.assertFalse(path.exists())

	def test_F05_strip_toml_keeps_the_rest_of_config(self):
		self._write_all()
		path = self._temp_dir / '.codex' / 'config.toml'
		path.write_text('model = "gpt-5"\n\n'
			+ path.read_text(encoding='utf-8')
			+ '\n[mcp_servers.other]\ncommand = "keepme"\n',
			encoding='utf-8')
		self._admin().strip_mcp_envoy(self.embody_ext, path)
		body = path.read_text(encoding='utf-8')
		self.assertNotIn('[mcp_servers.envoy]', body)
		self.assertIn('model = "gpt-5"', body)
		self.assertIn('keepme', body)

	def test_F06_strip_toml_removes_an_embody_only_file(self):
		self._write_all()
		path = self._temp_dir / '.codex' / 'config.toml'
		self._admin().strip_mcp_envoy(self.embody_ext, path)
		self.assertFalse(path.exists())

	def test_F07_uninstall_plan_covers_every_client_config(self):
		"""The preview must NAME every config it will strip -- a client
		whose file is missing from the plan is a silent leftover."""
		self._write_all()
		plan = self._admin().compute_uninstall_plan(
			self.embody_ext, target_dir=str(self._temp_dir))
		planned = {entry['path'] for entry in plan['strip']}
		for rel in ('.cursor/mcp.json', '.vscode/mcp.json',
				'.gemini/settings.json', '.codex/config.toml'):
			self.assertIn(rel, planned,
				f'{rel} is written but never planned for stripping')

	# ------------------------------------------------------------------
	# Group G: Antigravity launch identity (verified against a real install)
	# ------------------------------------------------------------------

	def test_G01_antigravity_launch_identity_is_the_verified_one(self):
		"""These values came off a real 2.5.5 install's product.json, not
		a guess -- the first guess had every Windows path wrong.

		The traps: the folder AND the exe are both 'Antigravity IDE'
		(spaces), it installs per-user under LOCALAPPDATA rather than
		Program Files, and the CLI/shim is 'antigravity-ide', not
		'antigravity'.
		"""
		spec = self.reg.launch_table()['antigravity']
		self.assertEqual(spec['kind'], 'editor')
		self.assertEqual(spec['app'], 'Antigravity IDE')
		self.assertEqual(spec['bundle'], 'com.google.antigravity-ide')
		self.assertEqual(spec['win_shim'], 'antigravity-ide')
		self.assertTrue(
			any('Antigravity IDE.exe' in c for c in spec['win_exe']),
			'the executable name carries a space')
		self.assertTrue(
			any('%LOCALAPPDATA%' in c for c in spec['win_exe']),
			'Antigravity is a per-user install, not Program Files')
		self.assertIn('antigravity-ide', spec['mac_cli'])

	def test_G02_antigravity_mcp_entry_omits_the_type_key(self):
		"""Antigravity validates mcp_config.json against a schema with
		additionalProperties:false, so a VS Code style 'type': 'stdio'
		entry would be REJECTED."""
		setup = self.setup
		mcp = self.reg.spec('antigravity')['mcp']
		entry = setup.mcp_server_entry(mcp['style'], BRIDGE_CMD)
		self.assertNotIn('type', entry)
		self.assertEqual(sorted(entry), ['args', 'command'])

	def test_G03_antigravity_uses_the_agents_customization_root(self):
		"""'.agents/rules' and '.agents/skills' are literal strings in the
		shipped language server; the MCP config sits beside them."""
		row = self.reg.spec('antigravity')
		self.assertEqual(row['rules']['dir'], '.agents/rules')
		self.assertEqual(row['skills']['dir'], '.agents/skills')
		self.assertEqual(row['mcp']['path'], '.agents/mcp_config.json')

	def test_G04_agents_skills_is_shared_by_every_client_that_reads_it(self):
		"""Codex, Gemini CLI, Cursor and Antigravity all discover
		.agents/skills (verified against each vendor's docs 2026-08-30), so
		one copy on disk serves all four; every row that writes it must also
		sweep it on Uninstall."""
		for token in ('codex', 'gemini', 'cursor', 'antigravity'):
			row = self.reg.spec(token)
			self.assertEqual(row['skills']['dir'], '.agents/skills', token)
			self.assertIn('.agents/skills', row['cleanup_dirs'], token)
		for token in ('vscode', 'copilot', 'windsurf'):
			self.assertIsNone(self.reg.spec(token).get('skills'), token)

	# ------------------------------------------------------------------
	# Group H: uninstall through the REAL plan -> execute path
	# ------------------------------------------------------------------

	def test_H01_every_client_config_strips_via_plan_and_execute(self):
		"""End to end, not via the stripper directly.

		Group F called strip_mcp_envoy() straight, which bypasses the kind
		routing in execute_uninstall_plan -- and that is exactly where the
		bug was: Codex's config was recorded as kind 'toml_table', which
		fell through to strip_marked_block (a '#'-header line scanner
		built for .gitignore) and silently left the envoy entry in the
		file forever. Planned for stripping, then not stripped. Only a
		test that runs the real path can catch that class.
		"""
		admin = self._admin()
		seeded = {}
		for token in self.reg.tokens():
			for mcp in self.reg.mcp_targets(token):
				path = self._temp_dir / mcp['path']
				if path in seeded:
					continue
				path.parent.mkdir(parents=True, exist_ok=True)
				if mcp['style'] == 'toml':
					path.write_text(
						'model = "x"\n\n[mcp_servers.mine]\ncommand = "keepme"\n',
						encoding='utf-8')
					self.setup.write_toml_mcp_config(
						self.envoy, self._temp_dir, token, mcp, path, BRIDGE_CMD)
				else:
					path.write_text(
						json.dumps({mcp['key']: {'mine': {'command': 'keepme'}}}),
						encoding='utf-8')
					self.setup.write_json_mcp_config(
						self.envoy, self._temp_dir, token, mcp, path, BRIDGE_CMD)
				seeded[path] = mcp['path']

		self.assertTrue(seeded, 'no client MCP configs were written')
		plan = admin.compute_uninstall_plan(
			self.embody_ext, target_dir=str(self._temp_dir))
		admin.execute_uninstall_plan(self.embody_ext, plan)

		for path, rel in seeded.items():
			self.assertTrue(path.exists(),
				f'{rel} was deleted -- the user server went with it')
			body = path.read_text(encoding='utf-8')
			self.assertNotIn('envoy', body,
				f'{rel} still carries the Envoy entry after Uninstall')
			self.assertIn('keepme', body,
				f'{rel} lost the user own server')

	def test_H02_every_recorded_kind_has_a_handler(self):
		"""A manifest kind nothing routes is a silent no-op at uninstall."""
		src = op.Embody.op('embody_admin').text
		routed = src[src.index('for a in plan[\'strip\']:'):]
		for kind in ('mcp_config', 'json_key', 'toml_table', 'md_section'):
			self.assertIn(f"'{kind}'", routed,
				f'uninstall has no handler for the {kind!r} manifest kind')

	def _build_footprint(self, tokens):
		git = op.Embody.op('embody_git').module
		for token in tokens:
			git.write_client_config(self.embody_ext, self._temp_dir, token)
			for mcp in self.reg.mcp_targets(token):
				self.setup.write_json_mcp_config(
					self.envoy, self._temp_dir, token, mcp,
					self._temp_dir / mcp['path'], BRIDGE_CMD)

	def _uninstall(self):
		admin = self._admin()
		plan = admin.compute_uninstall_plan(
			self.embody_ext, target_dir=str(self._temp_dir))
		admin.execute_uninstall_plan(self.embody_ext, plan)
		return plan

	def test_H03_uninstall_leaves_no_empty_directory_skeleton(self):
		"""Embody creates .agents/, .vscode/, .codex/, .gemini/, .cursor/
		purely to hold its own files. Removing the files and leaving the
		directories is not a reversal."""
		self._build_footprint(('antigravity', 'claudecode', 'cursor',
			'vscode', 'gemini'))
		created = [q for q in self._temp_dir.rglob('*')
			if q.is_dir() and '.embody' not in str(q)]
		self.assertTrue(created, 'precondition: directories were created')
		self._uninstall()
		left = [str(q.relative_to(self._temp_dir))
			for q in self._temp_dir.rglob('*') if '.embody' not in str(q)]
		self.assertListEqual(left, [],
			f'uninstall left a skeleton behind: {left}')

	def test_H04_a_directory_holding_user_files_is_kept(self):
		"""The bare rmdir is the guard: it fails on a non-empty directory,
		so anything of the user's inside keeps the directory alive."""
		self._build_footprint(('antigravity', 'cursor'))
		mine = self._temp_dir / '.cursor' / 'rules' / 'my-rule.mdc'
		mine.write_text('mine\n', encoding='utf-8')
		self._uninstall()
		self.assertTrue(mine.exists(), 'the user rule was deleted')
		self.assertTrue(mine.parent.is_dir(), 'its directory was removed')
		self.assertFalse((self._temp_dir / '.agents').exists(),
			'a directory with nothing of the user in it should still go')

	def test_H05_directories_are_named_in_the_preview(self):
		"""Uninstall is preview-then-confirm; a directory removed without
		appearing in the preview was never actually consented to."""
		self._build_footprint(('antigravity',))
		admin = self._admin()
		plan = admin.compute_uninstall_plan(
			self.embody_ext, target_dir=str(self._temp_dir))
		planned = {e['path'] for e in plan['delete']
			if e.get('kind') == 'emptydir'}
		for rel in ('.agents', '.agents/rules', '.agents/skills'):
			self.assertIn(rel, planned,
				f'{rel} is removed but never shown in the preview')

	def test_H06_root_move_prunes_every_clients_mcp_config(self):
		"""An abandoned root must not keep a live Envoy entry spawning the
		bridge -- and its dot-dirs cannot empty while one remains."""
		self._build_footprint(('cursor', 'vscode', 'gemini', 'antigravity'))
		self.embody_ext._cleanupOldRootFiles(self._temp_dir)
		for token in ('cursor', 'vscode', 'gemini', 'antigravity'):
			for mcp in self.reg.mcp_targets(token):
				path = self._temp_dir / mcp['path']
				if path.exists():
					self.assertNotIn('envoy',
						path.read_text(encoding='utf-8'),
						f'{mcp["path"]} kept its Envoy entry at the old root')

	# ------------------------------------------------------------------
	# Group I: gaps the review panel found -- untested surfaces
	# ------------------------------------------------------------------

	def test_I01_every_client_writes_its_declared_footprint(self):
		"""Antigravity had NO write coverage at all -- only assertions
		about its registry row. Loop every client instead of the three
		that happened to be picked."""
		git = op.Embody.op('embody_git').module
		for token in self.reg.tokens():
			root = Path(tempfile.mkdtemp(prefix=f'fp_{token}_'))
			try:
				git.write_client_config(self.embody_ext, root, token)
				row = self.reg.spec(token)
				for section in ('rules', 'skills'):
					block = row.get(section)
					if not block:
						continue
					d = root / block['dir']
					self.assertTrue(d.is_dir(),
						f'{token}: {block["dir"]} was never created')
					self.assertTrue(any(d.rglob('*.md*')),
						f'{token}: {block["dir"]} is empty')
			finally:
				shutil.rmtree(root, ignore_errors=True)

	def test_I02_antigravity_skill_frontmatter_survives(self):
		"""Antigravity validates SKILL.md frontmatter and requires the
		name to match the folder slug."""
		git = op.Embody.op('embody_git').module
		git.write_client_config(self.embody_ext, self._temp_dir, 'antigravity')
		skills = sorted((self._temp_dir / '.agents' / 'skills').iterdir())
		self.assertTrue(skills, 'no skills were written')
		for folder in skills:
			body = (folder / 'SKILL.md').read_text(encoding='utf-8')
			self.assertTrue(body.lstrip().startswith('---'),
				f'{folder.name}/SKILL.md lost its frontmatter')
			self.assertIn(f'name: {folder.name}', body,
				f'{folder.name}/SKILL.md name must match its folder')

	def test_I03_the_real_entry_point_writes_the_selected_client(self):
		"""Every other test reaches for a per-client writer. This drives
		extract_ai_config -- the public path, including its consent
		layer -- so that layer is exercised at least once."""
		emb = self.embody_ext
		saved_root = emb._findProjectRoot
		saved_bulk = getattr(emb, '_consent_bulk', None)
		try:
			emb._findProjectRoot = lambda: self._temp_dir
			emb._consent_bulk = True
			git = op.Embody.op('embody_git').module
			git.extract_ai_config(emb)
		finally:
			emb._findProjectRoot = saved_root
			if saved_bulk is None:
				try:
					del emb._consent_bulk
				except Exception:
					pass
			else:
				emb._consent_bulk = saved_bulk
		self.assertTrue((self._temp_dir / 'AGENTS.md').exists(),
			'AGENTS.md is written for every project')

	def test_I04_a_bom_does_not_stop_a_client_being_configured(self):
		"""json.loads rejects a leading BOM outright, and the failure
		path is "leave it alone" -- so one invisible byte meant that
		client silently never connected, and nothing ever retried."""
		mcp = self.reg.spec('cursor')['mcp']
		path = self._temp_dir / mcp['path']
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(
			'﻿'.encode('utf-8')
			+ json.dumps({'mcpServers': {'mine': {'command': 'keep'}}}).encode())
		self.setup.write_json_mcp_config(
			self.envoy, self._temp_dir, 'cursor', mcp, path, BRIDGE_CMD)
		cfg = json.loads(path.read_text(encoding='utf-8-sig'))
		self.assertIn('envoy', cfg['mcpServers'], 'the BOM blocked the write')
		self.assertIn('mine', cfg['mcpServers'])

	def test_I05_a_bom_config_still_strips_on_uninstall(self):
		mcp = self.reg.spec('cursor')['mcp']
		path = self._temp_dir / mcp['path']
		path.parent.mkdir(parents=True, exist_ok=True)
		self.setup.write_json_mcp_config(
			self.envoy, self._temp_dir, 'cursor', mcp, path, BRIDGE_CMD)
		body = path.read_text(encoding='utf-8')
		path.write_bytes('﻿'.encode('utf-8') + body.encode('utf-8'))
		self._admin().strip_mcp_envoy(self.embody_ext, path)
		self.assertNotIn('envoy',
			path.read_text(encoding='utf-8-sig') if path.exists() else '')

	def test_I06_an_undecodable_file_does_not_abort_the_deploy(self):
		"""UnicodeDecodeError is a ValueError, not an OSError -- it used
		to escape every guard and kill config generation partway, leaving
		some clients written and others not."""
		git = op.Embody.op('embody_git').module
		(self._temp_dir / 'AGENTS.md').write_bytes(
			('# Caf' + chr(0xE9) + '\n').encode('latin-1'))
		git.write_agents_md(self.embody_ext, self._temp_dir)
		git.write_client_config(self.embody_ext, self._temp_dir, 'claudecode')
		self.assertTrue((self._temp_dir / '.claude' / 'rules').exists(),
			'the deploy stopped at the unreadable file')
		self.assertEqual(
			(self._temp_dir / 'AGENTS.md').read_bytes(),
			('# Caf' + chr(0xE9) + '\n').encode('latin-1'),
			'the unreadable file must be left byte-for-byte alone')
