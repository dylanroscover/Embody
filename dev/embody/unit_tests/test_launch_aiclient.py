"""
Test suite: Launchaiclient launcher in EmbodyExt.

Covers the _AICLIENT_LAUNCH table shape (including the per-OS install
dicts), _resolveCliAbs probing, _buildTerminalScript / Win content (the
missing-CLI install guards with their copy/paste command rendering and
shell escaping), and install_summary dialog text. Does NOT spawn real
terminals or editors -- only pure builders and _launchEditor's no-window
failure path are exercised live.
"""

import sys
import tempfile
import shutil
import subprocess
from pathlib import Path
from unittest import mock

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestLaunchAIClient(EmbodyTestCase):

	DIALOG_TITLE = 'Embody -- Launch AI Client'

	def setUp(self):
		super().setUp()
		self._temp_dir = Path(tempfile.mkdtemp(prefix='launch_test_'))

	def tearDown(self):
		try:
			shutil.rmtree(self._temp_dir)
		except Exception:
			pass
		super().tearDown()

	def _save_responses_and_seed_launch_dialog(self):
		saved = op.Embody.fetch('_smoke_test_responses', None, search=False)
		op.Embody.store('_smoke_test_responses', {self.DIALOG_TITLE: 0})
		return saved

	def _restore_responses(self, saved):
		if saved is not None:
			op.Embody.store('_smoke_test_responses', saved)
		else:
			op.Embody.unstore('_smoke_test_responses')

	def _set_aiclient_for_test(self, token):
		prev_restoring = getattr(self.embody_ext, '_restoring_settings', False)
		self.embody_ext._restoring_settings = True
		try:
			self.embody.par.Aiclient = token
		finally:
			self.embody_ext._restoring_settings = prev_restoring

	# ------------------------------------------------------------------
	# Group A: _AICLIENT_LAUNCH table shape
	# ------------------------------------------------------------------

	def test_A01_table_covers_all_launchable_tokens(self):
		"""Every launchable Aiclient token is in the table ('none' excluded)."""
		table = self.embody_ext._AICLIENT_LAUNCH
		for token in ('claudecode', 'codex', 'gemini',
					  'cursor', 'copilot', 'vscode', 'windsurf'):
			self.assertIn(token, table, f'{token} missing from launch table')

	def test_A02_none_has_no_launcher(self):
		"""'none' is intentionally absent -> LaunchAIClient logs 'no launcher'."""
		self.assertNotIn('none', self.embody_ext._AICLIENT_LAUNCH)

	def test_A03_every_entry_well_formed(self):
		"""kind is editor|terminal; terminals carry cli, editors carry app."""
		for token, spec in self.embody_ext._AICLIENT_LAUNCH.items():
			self.assertIn(spec['kind'], ('editor', 'terminal'), token)
			if spec['kind'] == 'terminal':
				self.assertIn('cli', spec, f'{token}: terminal needs cli')
			else:
				self.assertIn('app', spec, f'{token}: editor needs app')

	def test_A04_terminal_clis_are_the_three_tools(self):
		"""claude/codex/gemini map to terminal CLIs by those exact barewords."""
		table = self.embody_ext._AICLIENT_LAUNCH
		self.assertEqual(table['claudecode']['cli'], 'claude')
		self.assertEqual(table['codex']['cli'], 'codex')
		self.assertEqual(table['gemini']['cli'], 'gemini')

	def test_A05_editors_are_editors(self):
		"""cursor/copilot/vscode/windsurf are all editor-kind."""
		table = self.embody_ext._AICLIENT_LAUNCH
		for editor in ('cursor', 'copilot', 'vscode', 'windsurf'):
			self.assertEqual(table[editor]['kind'], 'editor', editor)

	def test_A06_copilot_opens_vscode(self):
		"""Copilot lives inside VS Code -> its launch spec is the VS Code app."""
		table = self.embody_ext._AICLIENT_LAUNCH
		self.assertEqual(table['copilot']['app'], 'Visual Studio Code')
		self.assertIs(table['copilot'], self.embody_ext._VSCODE_LAUNCH)

	def test_A07_vscode_uses_copilot_launcher(self):
		"""The explicit VS Code token shares the Copilot/VS Code launch object."""
		table = self.embody_ext._AICLIENT_LAUNCH
		self.assertIn('vscode', table)
		self.assertIs(table['vscode'], table['copilot'])
		self.assertIs(table['vscode'], self.embody_ext._VSCODE_LAUNCH)

	def test_A08_cli_names_are_barewords(self):
		"""CLI tokens must be single shell-safe barewords (no spaces/flags)."""
		for token, spec in self.embody_ext._AICLIENT_LAUNCH.items():
			if spec['kind'] == 'terminal':
				cli = spec['cli']
				self.assertTrue(cli and ' ' not in cli and '/' not in cli,
					f'{token}: cli {cli!r} must be a bareword')

	def test_A09_terminal_installs_are_per_os_dicts(self):
		"""Terminal CLIs carry per-OS install dicts with the required keys,
		so the missing-CLI guard can show the right copy/paste command on
		both platforms plus the official docs URL."""
		for token in ('claudecode', 'codex', 'gemini'):
			install = self.embody_ext._AICLIENT_LAUNCH[token]['install']
			self.assertIsInstance(install, dict, f'{token}: install must be a dict')
			for key in ('name', 'mac', 'win', 'docs'):
				self.assertTrue(install.get(key),
					f'{token}: install spec needs a non-empty {key!r}')

	# ------------------------------------------------------------------
	# Group B: _resolveCliAbs (pure, no subprocess)
	# ------------------------------------------------------------------

	def test_B01_bogus_cli_resolves_to_none(self):
		"""A CLI that cannot exist resolves to None."""
		self.assertIsNone(
			self.embody_ext._resolveCliAbs('no_such_cli_embody_xyz'))

	def test_B02_resolve_returns_abspath_or_none(self):
		"""_resolveCliAbs returns an absolute path string or None."""
		result = self.embody_ext._resolveCliAbs('claude')
		self.assertTrue(
			result is None
			or (isinstance(result, str) and Path(result).is_absolute()))

	# ------------------------------------------------------------------
	# Group C: _buildTerminalScript (macOS .command content, pure)
	# ------------------------------------------------------------------

	def test_C01_script_has_shebang_cd_and_exec(self):
		"""With an abs path the script cd's to cwd and execs that path directly."""
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'claude', '/abs/bin/claude')
		self.assertTrue(body.startswith('#!/bin/zsh -l\n'))
		# The generator normalizes to forward slashes (a zsh script must
		# never contain Windows separators), so expect the posix form.
		posix_dir = str(self._temp_dir).replace('\\', '/')
		self.assertIn(f"cd '{posix_dir}'", body)
		self.assertIn('exec /abs/bin/claude', body)

	def test_C02_absent_cli_gets_guard_and_login_shell(self):
		"""No abs path -> guard on command -v, fall back to a login-shell resolve."""
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'gemini', None)
		self.assertIn('command -v gemini', body)
		self.assertIn('exec "${SHELL:-/bin/zsh}" -ilc gemini', body)
		self.assertIn('not found on PATH', body)

	def test_C03_single_quote_in_path_is_escaped(self):
		"""A single quote in the path must be escaped so cd stays well-formed."""
		body = self.embody_ext._buildTerminalScript(
			Path("/tmp/it's a dir"), 'claude', '/abs/claude')
		self.assertIn("cd '/tmp/it'\\''s a dir'", body)

	def test_C04_install_hint_echoed_when_absent(self):
		"""A legacy string spec keeps the old one-line install hint."""
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'gemini', None,
			install='npm install -g @google/gemini-cli')
		self.assertIn('Install:', body)
		self.assertIn('npm install -g @google/gemini-cli', body)

	def test_C05_install_dict_renders_mac_command_on_own_line(self):
		"""A per-OS dict renders numbered steps with the macOS command on its
		own printf line, blank lines around it, alt + docs -- and never the
		Windows command. printf '%s\\n' (not echo): zsh's echo interprets
		backslash escapes and leading-dash flags."""
		install = {'name': 'Claude Code',
				   'mac': 'curl -fsSL https://claude.ai/install.sh | bash',
				   'mac_alt': 'brew install --cask claude-code',
				   'win': 'WIN_ONLY_TOKEN',
				   'docs': 'https://code.claude.com/docs/en/setup'}
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'claude', None, install=install)
		self.assertIn(
			"printf '%s\\n' '    curl -fsSL https://claude.ai/install.sh | bash'",
			body)
		self.assertIn("printf '%s\\n' ''", body)
		self.assertNotIn("echo '", body)
		self.assertIn('Copy the line below', body)
		self.assertIn('Claude Code (claude) is not installed', body)
		self.assertIn('brew install --cask claude-code', body)
		self.assertIn('Docs:', body)
		self.assertNotIn('WIN_ONLY_TOKEN', body)
		self.assertIn('command -v claude', body)
		self.assertIn('exec "${SHELL:-/bin/zsh}" -i', body)

	def test_C06_install_dict_quotes_cannot_escape_printf(self):
		"""Single quotes in dict values are escaped so text stays inert:
		stripping the '\\'' escape from every guard printf line must leave
		exactly four quotes (the format string's pair + the text's pair)."""
		install = {'name': "Evil'; rm -rf ~; echo 'x",
				   'mac': 'safe-cmd', 'docs': 'https://example.com'}
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'foo', None, install=install)
		guard_prints = [line.strip() for line in body.split('\n')
						if line.strip().startswith('printf')]
		self.assertTrue(guard_prints, 'guard must print through printf')
		for line in guard_prints:
			self.assertEqual(line.replace("'\\''", '').count("'"), 4,
				f'unescaped quote leaks from: {line}')

	def test_C07_install_dict_note_renders_under_command(self):
		"""A note (e.g. the Node.js prerequisite) renders as its own line."""
		install = {'name': 'Gemini CLI',
				   'mac': 'npm install -g @google/gemini-cli',
				   'note': 'needs Node.js 20 or newer -- https://nodejs.org',
				   'docs': 'https://example.com'}
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'gemini', None, install=install)
		self.assertIn('(needs Node.js 20 or newer -- https://nodejs.org)', body)

	def test_C08_install_dict_alt_note_renders_on_own_line(self):
		"""An alt caveat renders parenthesized under the alternative, so the
		alternative itself stays a pure pasteable command."""
		install = {'name': 'Codex CLI', 'mac': 'primary-cmd',
				   'mac_alt': 'npm install -g @openai/codex',
				   'mac_alt_note': 'needs Node.js -- https://nodejs.org',
				   'docs': 'https://example.com'}
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'codex', None, install=install)
		self.assertIn(' Alternative:  npm install -g @openai/codex', body)
		self.assertIn('(needs Node.js -- https://nodejs.org)', body)
		self.assertNotIn('npm install -g @openai/codex (needs', body)

	# ------------------------------------------------------------------
	# Group D: _launchEditor graceful failure (no window spawned)
	# ------------------------------------------------------------------

	def test_D01_missing_editor_returns_false(self):
		"""A bogus app (no bundle/exe/shim) fails gracefully: no launch, False.

		macOS `open -a <missing app>` prints an error and returns non-zero
		WITHOUT opening any window, so this is safe to run live. On Windows,
		empty win_exe_candidates + no win_shim also returns False without
		launching. This is the false-SUCCESS regression guard.
		"""
		result = self.embody_ext._launchEditor(
			self._temp_dir, 'NoSuchEditor_Embody_XYZ')
		self.assertFalse(result)

	# ------------------------------------------------------------------
	# Group E: _launchEnv sanitizes the environment (the Cursor fix)
	# ------------------------------------------------------------------

	def test_E01_launch_env_strips_td_injected_vars(self):
		"""_launchEnv drops the TD-injected vars that break launched apps.

		ELECTRON_RUN_AS_NODE makes a fresh Electron editor run headless-as-Node
		and quit instantly (Cursor "bounce then close"); TD sets it live.
		"""
		env = self.embody_ext._launchEnv()
		self.assertNotIn('ELECTRON_RUN_AS_NODE', env)
		self.assertNotIn('LD_LIBRARY_PATH', env)

	def test_E02_launch_env_strips_injected_dyld_and_node(self):
		"""_launchEnv strips DYLD_* and NODE_OPTIONS even when freshly injected."""
		import os
		added = [k for k in ('DYLD_INSERT_LIBRARIES', 'NODE_OPTIONS')
				 if k not in os.environ]
		for k in added:
			os.environ[k] = 'x'
		try:
			env = self.embody_ext._launchEnv()
			self.assertNotIn('DYLD_INSERT_LIBRARIES', env)
			self.assertNotIn('NODE_OPTIONS', env)
		finally:
			for k in added:
				os.environ.pop(k, None)

	def test_E03_launch_env_keeps_ordinary_vars(self):
		"""_launchEnv preserves a normal environment (HOME/PATH/USER survive)."""
		env = self.embody_ext._launchEnv()
		self.assertTrue(any(k in env for k in ('HOME', 'PATH', 'USER')))

	# ------------------------------------------------------------------
	# Group F: _launchTerminal Windows console (regression guard)
	# ------------------------------------------------------------------

	def _capture_win_terminal_popen(self, cli='claude'):
		"""Run _launchTerminal on the Windows branch with subprocess.Popen mocked;
		return (result, captured_kwargs, captured_args). Portable: patches
		sys.platform and injects CREATE_NEW_CONSOLE so it runs on any host."""
		new_console = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0x10)
		captured = {}

		def fake_popen(*args, **kwargs):
			captured['args'] = args
			captured['kwargs'] = kwargs
			return mock.Mock()

		with mock.patch.object(sys, 'platform', 'win32'), \
			 mock.patch.object(subprocess, 'CREATE_NEW_CONSOLE', new_console, create=True), \
			 mock.patch.object(subprocess, 'Popen', side_effect=fake_popen):
			result = self.embody_ext._launchTerminal(self._temp_dir, cli)
		return result, captured.get('kwargs', {}), captured.get('args', ())

	def test_F01_windows_terminal_never_redirects_stdin(self):
		"""Windows terminal launch must NOT redirect stdin.

		The CLIs are interactive Ink/Node TUIs that need a real console TTY on
		stdin (claude's OAuth especially). stdin=DEVNULL sets
		STARTF_USESTDHANDLES: stdin is pinned to NUL (Ink can't enter raw mode)
		and -- from a GUI parent with no console handles -- the child also gets
		bogus stdout/stderr. That is the "blank terminal, login browser flashes
		then closes" bug. This guards against re-adding the redirect.
		"""
		result, kwargs, _ = self._capture_win_terminal_popen()
		self.assertTrue(result)
		self.assertNotIn('stdin', kwargs,
			'Windows terminal launch must not redirect stdin (breaks the TUI TTY)')
		self.assertNotIn('stdout', kwargs)
		self.assertNotIn('stderr', kwargs)

	def test_F02_windows_terminal_allocates_new_console(self):
		"""The launch must open a fresh console (CREATE_NEW_CONSOLE) at cwd."""
		result, kwargs, args = self._capture_win_terminal_popen()
		self.assertTrue(result)
		self.assertEqual(kwargs.get('creationflags'),
			getattr(subprocess, 'CREATE_NEW_CONSOLE', 0x10))
		self.assertEqual(str(kwargs.get('cwd')), str(self._temp_dir))

	def test_F03_windows_terminal_uses_doubled_quote_cmd_k(self):
		"""cmd /K with the doubled-quote form keeps the .bat literally quoted."""
		_, _, args = self._capture_win_terminal_popen(cli='claude')
		self.assertTrue(args, 'Popen was not called')
		script = self._temp_dir / '.embody' / 'launch_claude.bat'
		self.assertEqual(args[0], f'cmd /K ""{script}""')
		self.assertTrue(script.exists(), 'Windows launcher must leave the .bat on disk')
		data = script.read_bytes()
		self.assertIn(b'\r\n', data)
		self.assertFalse(b'\n' in data.replace(b'\r\n', b''),
			'Windows launcher write must preserve CRLF without lone LF bytes')

	# ------------------------------------------------------------------
	# Group G: _buildTerminalScriptWin (Windows .bat content, pure)
	# ------------------------------------------------------------------

	def test_G01_windows_script_abs_cli_quotes_path_without_where_guard(self):
		"""With an abs path the .bat cd's to cwd and invokes that path directly."""
		body = self.embody_ext._buildTerminalScriptWin(
			Path("C:/Users/O'Neil/My Project"), 'claude',
			'C:/Program Files/Claude/claude.exe')
		self.assertTrue(body.startswith('@echo off\r\n'))
		self.assertIn('cd /d "C:/Users/O\'Neil/My Project"', body)
		self.assertIn('"C:/Program Files/Claude/claude.exe"', body)
		self.assertNotIn('where claude', body)

	def test_G02_windows_script_bare_cli_has_guard_labels_and_sanitized_hint(self):
		"""No abs path -> where guard, label flow, and cmd-safe install hint."""
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'gemini', None,
			'Use %PATH% & npm | bun ^ "quoted" `tick` $cash (ok)')
		self.assertIn('where gemini >nul 2>nul', body)
		self.assertIn('if errorlevel 1 goto :missing', body)
		self.assertIn(':missing\r\n', body)
		self.assertIn('gemini not found on PATH.', body)
		install_line = [line for line in body.split('\r\n')
						if line.startswith('echo Install:')][0]
		self.assertIn('(ok)', install_line)
		for bad in ('%', '&', '|', '^', '"', '`', '$'):
			self.assertNotIn(bad, install_line)

	def test_G03_windows_script_uses_crlf_exclusively(self):
		"""cmd label parsing relies on CRLF line endings."""
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'gemini', None)
		self.assertIn('\r\n', body)
		self.assertFalse('\n' in body.replace('\r\n', ''),
			'Windows .bat must not contain lone LF characters')
		self.assertTrue(body.endswith('\r\n'))

	def test_G04_windows_script_uses_label_flow_not_parenthesized_blocks(self):
		"""Install hints can contain parentheses, so the .bat must avoid if blocks."""
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'gemini', None,
			'npm install -g @google/gemini-cli (or brew install gemini-cli)')
		self.assertIn('if errorlevel 1 goto :missing', body)
		self.assertNotIn('(\r\n', body)

	def test_G05_windows_script_keeps_single_quote_and_spaces_in_quoted_cwd(self):
		"""Single quotes are harmless inside the required double-quoted cwd."""
		cwd = Path("C:/Users/O'Neil/My Project")
		body = self.embody_ext._buildTerminalScriptWin(cwd, 'codex', None)
		self.assertIn('cd /d "C:/Users/O\'Neil/My Project"', body)

	def test_G06_windows_install_dict_escapes_ampersands_outside_quotes(self):
		"""The cmd-native install command's && chain is ^-escaped in echo so
		it displays literally instead of executing."""
		install = {'name': 'Claude Code',
				   'win': 'curl -fsSL https://claude.ai/install.cmd -o install.cmd '
						  '&& install.cmd && del install.cmd',
				   'docs': 'https://code.claude.com/docs/en/setup'}
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'claude', None, install)
		self.assertIn('^&^&', body)
		for line in body.split('\r\n'):
			if line.startswith('echo'):
				self.assertNotIn(' && ', line,
					f'raw && must never reach an echo line: {line}')

	def test_G07_windows_install_dict_keeps_quoted_pipe_verbatim(self):
		"""Metacharacters inside double quotes are already literal to cmd --
		escaping them there would display stray carets."""
		install = {'name': 'Codex CLI',
				   'win': 'powershell -ExecutionPolicy ByPass -c '
						  '"irm https://chatgpt.com/codex/install.ps1 | iex"',
				   'docs': 'https://developers.openai.com/codex/cli'}
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'codex', None, install)
		self.assertIn('"irm https://chatgpt.com/codex/install.ps1 | iex"', body)
		self.assertNotIn('^|', body)

	def test_G08_windows_install_dict_echo_paren_form_and_crlf(self):
		"""Every guard display line uses the `echo(` form (immune to lines
		reading on/off//?), blanks are a bare `echo(`, flow stays
		label-based, and the .bat stays CRLF-only."""
		install = {'name': 'Gemini CLI',
				   'win': 'npm install -g @google/gemini-cli',
				   'note': 'needs Node.js 20 or newer',
				   'docs': 'https://example.com (docs)'}
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'gemini', None, install)
		self.assertIn('echo(\r\n', body)
		self.assertIn('Gemini CLI (gemini) is not installed', body)
		self.assertIn('When the install finishes', body)
		self.assertIn('if errorlevel 1 goto :missing', body)
		rows = body.split('\r\n')
		block = rows[rows.index(':missing') + 1:rows.index(':done')]
		self.assertTrue(block)
		for row in block:
			self.assertTrue(row.startswith('echo('),
				f'guard display line must use echo( form: {row!r}')
		self.assertFalse('\n' in body.replace('\r\n', ''),
			'Windows .bat must not contain lone LF characters')

	def test_G10_windows_script_disables_delayed_expansion(self):
		"""setlocal DisableDelayedExpansion follows @echo off, so a host with
		delayed expansion enabled cannot eat `!` in displayed text."""
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'gemini', None)
		self.assertEqual(body.split('\r\n')[1], 'setlocal DisableDelayedExpansion')

	def test_G09_windows_install_dict_doubles_percent(self):
		"""% expands even inside quotes in a batch file, so it is doubled."""
		install = {'name': 'X', 'win': 'set PATH=%PATH%;C:/tools',
				   'docs': 'https://example.com'}
		body = self.embody_ext._buildTerminalScriptWin(
			self._temp_dir, 'foo', None, install)
		self.assertIn('%%PATH%%', body)
		self.assertNotIn('echo set PATH=%PATH%', body)

	# ------------------------------------------------------------------
	# Group H: LaunchAIClient dialog failure paths
	# ------------------------------------------------------------------

	def test_H01_launch_editor_failure_consumes_dialog_seed(self):
		"""Editor launch failure shows the launch dialog and does not raise."""
		prev_client = self.embody.par.Aiclient.eval()
		saved = self._save_responses_and_seed_launch_dialog()
		try:
			self._set_aiclient_for_test('cursor')
			with mock.patch.object(type(self.embody_ext), '_findProjectRoot',
								   return_value=self._temp_dir), \
				 mock.patch.object(type(self.embody_ext), '_launchEditor',
								   return_value=False):
				self.embody_ext.LaunchAIClient()
			self.assertIsNone(op.Embody.fetch('_smoke_test_responses', None,
											  search=False))
		finally:
			self._set_aiclient_for_test(prev_client)
			self._restore_responses(saved)

	def test_H02_launch_terminal_failure_consumes_dialog_seed(self):
		"""Terminal launch failure shows the launch dialog and does not raise."""
		prev_client = self.embody.par.Aiclient.eval()
		saved = self._save_responses_and_seed_launch_dialog()
		try:
			self._set_aiclient_for_test('gemini')
			with mock.patch.object(type(self.embody_ext), '_findProjectRoot',
								   return_value=self._temp_dir), \
				 mock.patch.object(type(self.embody_ext), '_launchTerminal',
								   return_value=False):
				self.embody_ext.LaunchAIClient()
			self.assertIsNone(op.Embody.fetch('_smoke_test_responses', None,
											  search=False))
		finally:
			self._set_aiclient_for_test(prev_client)
			self._restore_responses(saved)

	def test_H03_no_launcher_dialog_uses_selected_menu_label(self):
		"""The no-launcher path names the selected entry, not the Aiclient par."""
		prev_client = self.embody.par.Aiclient.eval()
		saved = self._save_responses_and_seed_launch_dialog()
		captured = []
		p = self.embody.par.Aiclient
		names = list(getattr(p, 'menuNames', ()))
		token = 'none' if 'none' in names else p.eval()
		try:
			self._set_aiclient_for_test(token)
			p = self.embody.par.Aiclient
			try:
				expected_label = p.menuLabels[p.menuIndex]
			except Exception:
				expected_label = p.eval()

			orig_message_box = type(self.embody_ext)._messageBox

			def capture_message(instance, title, message, buttons):
				captured.append((title, message, buttons))
				return orig_message_box(instance, title, message, buttons)

			table = dict(self.embody_ext._AICLIENT_LAUNCH)
			table.pop(token, None)
			with mock.patch.object(type(self.embody_ext), '_AICLIENT_LAUNCH',
								   table), \
				 mock.patch.object(type(self.embody_ext), '_findProjectRoot',
								   return_value=self._temp_dir), \
				 mock.patch.object(type(self.embody_ext), '_messageBox',
								   capture_message):
				self.embody_ext.LaunchAIClient()

			self.assertTrue(captured, 'No-launcher path must show the dialog')
			self.assertIn(expected_label, captured[0][1])
			self.assertNotIn(f'"{self.embody.par.Aiclient.label}"', captured[0][1])
			self.assertIsNone(op.Embody.fetch('_smoke_test_responses', None,
											  search=False))
		finally:
			self._set_aiclient_for_test(prev_client)
			self._restore_responses(saved)

	# ------------------------------------------------------------------
	# Group I: install_summary (dialog text for install specs)
	# ------------------------------------------------------------------

	def _launch_module(self):
		return op.Embody.op('embody_launch').module

	def test_I01_summary_string_passthrough_and_empty(self):
		"""Legacy strings keep the one-line 'Install: ...' form; empty specs
		produce no text (the dialog then omits the hint entirely)."""
		m = self._launch_module()
		self.assertEqual(m.install_summary('npm install -g x'),
						 'Install: npm install -g x')
		self.assertEqual(m.install_summary(None), '')
		self.assertEqual(m.install_summary(''), '')

	def test_I02_summary_dict_picks_current_os_command(self):
		"""Per-OS dicts render this OS's copy/paste command on its own line
		and never the other OS's."""
		m = self._launch_module()
		install = {'name': 'Claude Code',
				   'mac': 'MAC_CMD_TOKEN', 'win': 'WIN_CMD_TOKEN',
				   'mac_alt': 'MAC_ALT_TOKEN', 'win_alt': 'WIN_ALT_TOKEN',
				   'docs': 'https://code.claude.com/docs/en/setup'}
		text = m.install_summary(install)
		is_win = sys.platform.startswith('win')
		expected = 'WIN_CMD_TOKEN' if is_win else 'MAC_CMD_TOKEN'
		other = 'MAC_CMD_TOKEN' if is_win else 'WIN_CMD_TOKEN'
		self.assertIn(f'\n\n{expected}', text)
		self.assertNotIn(other, text)
		self.assertIn('Docs:', text)

	def test_I03_summary_alt_note_renders_separately(self):
		"""The alt caveat renders on its own line in dialog text too."""
		m = self._launch_module()
		key = 'win_alt_note' if sys.platform.startswith('win') else 'mac_alt_note'
		alt_key = 'win_alt' if sys.platform.startswith('win') else 'mac_alt'
		install = {'name': 'X', 'mac': 'm', 'win': 'w',
				   alt_key: 'npm install -g @openai/codex',
				   key: 'needs Node.js', 'docs': 'https://example.com'}
		text = m.install_summary(install)
		self.assertIn('Alternative:  npm install -g @openai/codex\n(needs Node.js)',
					  text)
