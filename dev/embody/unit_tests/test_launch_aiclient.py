"""
Test suite: Launchaiclient launcher in EmbodyExt.

Covers the _AICLIENT_LAUNCH table shape, _resolveCliAbs probing, and
_buildTerminalScript content (the macOS .command). Does NOT spawn real
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

	def setUp(self):
		super().setUp()
		self._temp_dir = Path(tempfile.mkdtemp(prefix='launch_test_'))

	def tearDown(self):
		try:
			shutil.rmtree(self._temp_dir)
		except Exception:
			pass
		super().tearDown()

	# ------------------------------------------------------------------
	# Group A: _AICLIENT_LAUNCH table shape
	# ------------------------------------------------------------------

	def test_A01_table_covers_all_launchable_tokens(self):
		"""Every launchable Aiclient token is in the table ('none' excluded)."""
		table = self.embody_ext._AICLIENT_LAUNCH
		for token in ('claudecode', 'codex', 'gemini', 'vscode',
					  'cursor', 'copilot', 'windsurf'):
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
		"""vscode/cursor/copilot/windsurf are all editor-kind."""
		table = self.embody_ext._AICLIENT_LAUNCH
		for editor in ('vscode', 'cursor', 'copilot', 'windsurf'):
			self.assertEqual(table[editor]['kind'], 'editor', editor)

	def test_A06_copilot_opens_vscode(self):
		"""Copilot lives inside VS Code -> its launch spec IS VS Code's."""
		table = self.embody_ext._AICLIENT_LAUNCH
		self.assertEqual(table['copilot']['app'], 'Visual Studio Code')
		self.assertIs(table['copilot'], table['vscode'])

	def test_A07_cli_names_are_barewords(self):
		"""CLI tokens must be single shell-safe barewords (no spaces/flags)."""
		for token, spec in self.embody_ext._AICLIENT_LAUNCH.items():
			if spec['kind'] == 'terminal':
				cli = spec['cli']
				self.assertTrue(cli and ' ' not in cli and '/' not in cli,
					f'{token}: cli {cli!r} must be a bareword')

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
		self.assertIn(f"cd '{self._temp_dir}'", body)
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
		"""When the CLI is absent, the script echoes the install instructions."""
		body = self.embody_ext._buildTerminalScript(
			self._temp_dir, 'gemini', None,
			install='npm install -g @google/gemini-cli')
		self.assertIn('Install:', body)
		self.assertIn('npm install -g @google/gemini-cli', body)

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
		"""cmd /K with the doubled-quote form keeps the target literally quoted."""
		_, _, args = self._capture_win_terminal_popen(cli='claude')
		self.assertTrue(args, 'Popen was not called')
		self.assertEqual(args[0], 'cmd /K ""claude""')
