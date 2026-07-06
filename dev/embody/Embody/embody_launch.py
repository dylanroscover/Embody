"""Launch AI client / editor / terminal (module DAT).

Module DAT (mod.embody_launch) called by EmbodyExt on the MAIN THREAD only.
Holds the private implementations behind Embody's "Launch AI Client" button:
resolving and opening a GUI editor (Cursor, Windsurf, VS Code / Copilot) or a
new terminal running a CLI (Claude, Codex, Gemini) at the project root, with a
sanitized environment. EmbodyExt keeps a thin delegating stub for each -- these
functions carry the real bodies.

No module-level TD access; every function takes the ext instance (`ext`) as its
first argument and reaches TD through it (ext.Log, ext._messageBox, ...) or
through the TD globals available inside function bodies at call time. The
launch tables (_AICLIENT_LAUNCH / _VSCODE_LAUNCH / _LAUNCH_ENV_STRIP) stay as
class attributes on EmbodyExt and are read through `ext.` so unit-test
monkeypatches on the class still take effect (test_launch_aiclient patches
_AICLIENT_LAUNCH, _launchEditor, _launchTerminal, _findProjectRoot, _messageBox
on the ext type -- launch_ai_client routes those through `ext.`). The internal
helpers (resolve_cli_abs / launch_env / build_terminal_script[_win]) are not
monkeypatched, so intra-module calls to them stay module-local.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def launch_ai_client(ext) -> None:
    """Open the AI client selected in the Aiclient menu at the project root.

    Editors (Cursor, Windsurf; Copilot -> VS Code) open the root as
    a workspace. CLI tools (Claude, Codex, Gemini) open in a new terminal at
    the root. Driven by the _AICLIENT_LAUNCH table. Launch CWD is
    _findProjectRoot() (honors Aiprojectroot). Fire-and-forget: opens a
    window, does not block or confirm the tool actually ran.
    """
    # Whole body inside try: par eval and _findProjectRoot() can raise, and
    # this is a button callback -- a launch problem must log, never crash TD.
    try:
        client = ext.my.par.Aiclient.eval()
        label = ext._aiClientLabel()
        spec = ext._AICLIENT_LAUNCH.get(client)
        cwd = ext._findProjectRoot()
        title = 'Embody -- Launch AI Client'
        if spec is None:
            msg = f'No launcher is wired for "{label}". Open your AI tool manually at {cwd}.'
            ext.Log(f'No launcher for "{label}". Open it manually at {cwd}.', 'INFO')
            ext._messageBox(title, msg, ['OK'])
            return
        if spec['kind'] == 'editor':
            if ext._launchEditor(
                    cwd, spec['app'], bundle_id=spec.get('bundle'),
                    win_exe_candidates=spec.get('win_exe', ()),
                    win_shim=spec.get('win_shim'), mac_cli=spec.get('mac_cli'),
                    mac_alt_names=spec.get('alt_names', ()),
                    install=spec.get('install')):
                ext.Log(f'Launched {label} at {cwd}', 'SUCCESS')
            else:
                msg = f'Could not open {label}. Is it installed?'
                if spec.get('install'):
                    msg += f'\n\nInstall: {spec.get("install")}'
                msg += f'\n\nProject root: {cwd}'
                ext._messageBox(title, msg, ['OK'])
        elif ext._launchTerminal(cwd, spec['cli'], install=spec.get('install')):
            ext.Log(f'Opened a terminal for {label} at {cwd}', 'INFO')
        else:
            msg = f'Could not open a terminal for {label}. Is it installed?'
            if spec.get('install'):
                msg += f'\n\nInstall: {spec.get("install")}'
            msg += f'\n\nProject root: {cwd}'
            ext._messageBox(title, msg, ['OK'])
    except Exception as e:
        ext.Log(f'Failed to launch AI client: {e}', 'ERROR')
        ext._messageBox('Embody -- Launch AI Client', str(e), ['OK'])


def resolve_cli_abs(ext, cli: str) -> Optional[str]:
    """Absolute path to a CLI via fast filesystem probes, or None.

    No subprocess -- safe on the main thread. When None, the caller lets the
    new terminal's own login shell resolve the CLI (which is what defeats the
    Dock-truncated PATH on macOS, where ~/.local/bin is not on TD's PATH).
    """
    if sys.platform.startswith('win'):
        cands = [
            # Native installers (claude's recommended install.ps1 lands here
            # -- the Windows twin of ~/.local/bin below).
            os.path.expandvars(rf'%USERPROFILE%\.local\bin\{cli}.exe'),
            os.path.expandvars(rf'%APPDATA%\npm\{cli}.cmd'),
            os.path.expandvars(rf'%USERPROFILE%\.bun\bin\{cli}.exe'),
            os.path.expandvars(rf'%LOCALAPPDATA%\Programs\{cli}\{cli}.exe'),
        ]
    else:
        home = Path.home()
        cands = [
            home / '.local' / 'bin' / cli,
            Path('/opt/homebrew/bin') / cli,
            Path('/usr/local/bin') / cli,
            home / '.bun' / 'bin' / cli,
        ]
    for c in cands:
        try:
            if Path(c).exists():
                return str(c)
        except OSError:
            continue
    return None


def launch_env(ext) -> dict:
    """A copy of the process environment with TouchDesigner's injected
    variables removed, so externally launched apps/terminals get a clean env.

    TD sets ELECTRON_RUN_AS_NODE=1 -- which makes a freshly launched Electron
    editor (Cursor, Windsurf, or Copilot's VS Code) run headless-as-Node and quit instantly
    (the "dock icon bounces, then closes" bug) -- plus LD_LIBRARY_PATH/DYLD_*
    and PYTHON* pointing into TD's own bundle. `open` forwards the caller's
    environment to the launched app, so these must be stripped here.
    """
    return {k: v for k, v in os.environ.items()
            if k not in ext._LAUNCH_ENV_STRIP and not k.startswith('DYLD_')}


def launch_editor(ext, cwd, app_name, bundle_id=None, win_exe_candidates=(),
                  win_shim=None, mac_cli=None, mac_alt_names=(), install=None) -> bool:
    """Open a GUI editor with cwd as its workspace. Returns True on a launched
    window. macOS uses LaunchServices (PATH-independent); Windows launches the
    real .exe from known install dirs. Never a hijackable PATH shim unless
    nothing else resolves (logged). Mirrors OpenSaveFolder's OS split.
    """
    d = str(cwd)
    # Clean env: TD's ELECTRON_RUN_AS_NODE would make a fresh Electron editor
    # quit instantly ("bounce then close"); DYLD/PYTHON vars can mis-link it.
    env = launch_env(ext)
    if sys.platform.startswith('darwin'):
        # /usr/bin/open: absolute path so it resolves even if TD's PATH lacks
        # /usr/bin. -a/-b MANDATORY: a bare `open <dir>` opens Finder, not the
        # editor. Each attempt returns non-zero WITHOUT launching if that app
        # is absent, so exit-code gating (subprocess.call, ~ms since open hands
        # off to LaunchServices and exits) doubles as install detection.
        _open = '/usr/bin/open'
        attempts = [[_open, '-a', app_name, d]]
        if bundle_id:
            attempts.append([_open, '-b', bundle_id, d])
        attempts += [[_open, '-a', n, d] for n in mac_alt_names]
        if mac_cli and Path(mac_cli).exists():
            attempts.append([mac_cli, d])   # app-own CLI, not a hijackable shim
        for cmd in attempts:
            try:
                if subprocess.call(cmd, stdin=subprocess.DEVNULL, env=env) == 0:
                    return True
            except OSError:
                continue
        msg = f'Could not open {app_name} at {d}; is it installed?'
        if install:
            msg += f' Install: {install}'
        ext.Log(msg, 'WARNING')
        return False
    if sys.platform.startswith('win'):
        try:
            for cand in win_exe_candidates:
                exe = os.path.expandvars(cand)
                if Path(exe).exists():
                    # argv (no shell): spaces/&/trailing-sep in the dir are safe.
                    subprocess.Popen([exe, d], stdin=subprocess.DEVNULL, env=env)
                    return True
            if win_shim:
                # Resolve FIRST so a missing shim returns False (no false SUCCESS
                # -- shell=True with a list could "succeed" launching cmd.exe
                # with the editor absent).
                resolved = shutil.which(win_shim)
                if resolved:
                    ext.Log(f'{app_name}: launching via PATH "{win_shim}" ({resolved}) '
                            '-- may resolve to a different editor build.', 'WARNING')
                    # .cmd/.bat shims run through cmd; the doubled-quote form
                    # (""prog" "arg"") keeps program+dir literally quoted so a
                    # metachar (& | etc.) in the dir is not re-parsed by cmd.
                    subprocess.Popen(f'cmd /c ""{resolved}" "{d}""',
                                     stdin=subprocess.DEVNULL, env=env)
                    return True
        except OSError as e:
            ext.Log(f'{app_name}: launch failed ({e}).', 'WARNING')
            return False
        msg = f'Could not locate {app_name}.'
        if install:
            msg += f' Install: {install}'
        ext.Log(msg, 'WARNING')
        return False
    ext.Log(f'Editor launch unsupported on {sys.platform}.', 'INFO')
    return False


def build_terminal_script(ext, cwd, cli, abs_cli, install=None) -> str:
    """Return the macOS .command script text that cd's to cwd and runs <cli>.

    Pure (no I/O) so the correctness-critical content is unit-testable.
    abs_cli: the CLI's resolved absolute path, or None to defer to the new
    terminal's own login-shell PATH (with a visible install guard if truly
    absent). install: the how-to-install hint shown in that guard.
    """
    q = str(cwd).replace("'", "'\\''")        # single-quote-escape the dir
    lines = ['#!/bin/zsh -l',
             f"cd '{q}' || {{ echo \"launch dir missing\"; exec \"${{SHELL:-/bin/zsh}}\" -il; }}"]
    if abs_cli:
        lines.append(f'exec {shlex.quote(abs_cli)}')
    else:
        # Not found by fast probe -- let the login shell resolve it. If truly
        # absent, print install guidance and keep the window open.
        hint = (install or 'see the tool website').replace('"', "'").replace('$', '').replace('`', '')
        lines.append(f'if ! command -v {cli} >/dev/null 2>&1; then')
        lines.append(f'  echo "{cli} not found on PATH."')
        lines.append(f'  echo "Install:  {hint}"')
        lines.append('  echo "Then close this window and press Launch AI Client again."')
        lines.append('  exec "${SHELL:-/bin/zsh}" -i')
        lines.append('fi')
        lines.append(f'exec "${{SHELL:-/bin/zsh}}" -ilc {shlex.quote(cli)}')
    return '\n'.join(lines) + '\n'


def build_terminal_script_win(ext, cwd, cli, abs_cli, install=None) -> str:
    """Windows twin of _buildTerminalScript: the .bat run via cmd /K.
    Pure (no I/O) so the correctness-critical content is unit-testable."""
    d = str(cwd).replace('"', '')
    lines = ['@echo off',
             f'cd /d "{d}"',
             'if errorlevel 1 echo launch dir missing.']
    if abs_cli:
        lines.append(f'"{str(abs_cli).replace(chr(34), "")}"')
    else:
        hint = ''.join(c for c in (install or 'see the tool website')
                       if re.match(r"[A-Za-z0-9 ._:/@()+=,'-]", c))
        hint = ' '.join(hint.split()) or 'see the tool website'
        lines += [
            f'where {cli} >nul 2>nul',
            'if errorlevel 1 goto :missing',
            cli,
            'goto :done',
            ':missing',
            f'echo {cli} not found on PATH.',
            f'echo Install:  {hint}',
            'echo Then close this window and press Launch AI Client again.',
            ':done',
        ]
    return '\r\n'.join(lines) + '\r\n'


def launch_terminal(ext, cwd, cli, install=None) -> bool:
    """Open a new terminal at cwd running <cli>. Returns True if a terminal was
    launched, False on failure or an unsupported OS (so the caller only logs
    success when a window actually opened).

    macOS: write a .command and hand it to `open` -- the terminal app's login
    shell rebuilds the real PATH. NEVER execute the script directly from TD
    (that re-inherits TD's truncated PATH). Windows: `cmd /K` in a new console.
    The CLI's absolute path is resolved first so it works even when the CLI
    lives in ~/.local/bin, invisible to a Dock-launched TD.
    """
    d = str(cwd)
    env = launch_env(ext)   # strip TD's injected vars from the terminal too
    try:
        if sys.platform.startswith('darwin'):
            body = build_terminal_script(ext, cwd, cli, resolve_cli_abs(ext, cli), install)
            scripts_dir = Path(cwd) / '.embody'
            scripts_dir.mkdir(parents=True, exist_ok=True)
            script = scripts_dir / f'launch_{cli}.command'
            script.write_text(body, encoding='utf-8')
            script.chmod(0o755)
            # Do NOT delete: `open` returns before the terminal reads the file.
            # /usr/bin/open: absolute so it resolves even if PATH lacks /usr/bin.
            if subprocess.call(['/usr/bin/open', str(script)],
                               stdin=subprocess.DEVNULL, env=env) != 0:
                ext.Log(f'Failed to open a terminal for {cli}.', 'WARNING')
                return False
            return True
        if sys.platform.startswith('win'):
            body = build_terminal_script_win(ext, cwd, cli, resolve_cli_abs(ext, cli), install)
            scripts_dir = Path(cwd) / '.embody'
            scripts_dir.mkdir(parents=True, exist_ok=True)
            script = scripts_dir / f'launch_{cli}.bat'
            script.write_text(body, encoding='utf-8', newline='')
            # Do NOT delete: cmd /K returns after starting the console, and
            # the console reads this file after Popen returns.
            #
            # NO std-handle redirection here. The CLIs are interactive
            # Ink/Node TUIs (claude/codex/gemini) that need a real console
            # TTY on stdin -- claude's OAuth login especially. Passing
            # stdin=DEVNULL sets STARTF_USESTDHANDLES, which (a) pins the
            # child's stdin to NUL so Ink cannot enter raw mode, and (b) from
            # a GUI parent like TD -- which has no valid console handles --
            # also hands the child bogus stdout/stderr. That combination is
            # exactly the "blank terminal, login browser flashes then closes"
            # bug on Windows. With CREATE_NEW_CONSOLE and no redirection, the
            # fresh console owns fully-working stdin/stdout/stderr.
            subprocess.Popen(f'cmd /K ""{script}""', cwd=d,
                             creationflags=subprocess.CREATE_NEW_CONSOLE, env=env)
            return True
    except OSError as e:
        ext.Log(f'Failed to open a terminal for {cli}: {e}', 'WARNING')
        return False
    ext.Log(f'Terminal launch unsupported on {sys.platform}.', 'INFO')
    return False
